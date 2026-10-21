# SPDX-License-Identifier: Apache-2.0
"""Bounded pinned-source excerpts; no imports or execution of scanned code."""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from urllib.parse import quote


class SourceCatalog:
    """Run-local source/AST cache shared by all evidence in one report."""

    def __init__(self):
        self.cache = {}

    def read(self, root: Path, sha: str, path: str):
        key = (root, sha, path)
        if key not in self.cache:
            result = subprocess.run(["git", "-C", str(root), "show", f"{sha}:{path}"],
                                    capture_output=True, text=True, encoding="utf-8")
            code = result.stdout if result.returncode == 0 else None
            try:
                tree = ast.parse(code) if code is not None else None
            except SyntaxError:
                tree = None
            self.cache[key] = (code, tree)
        return self.cache[key]

    def locate_class(self, root: Path, sha: str, qualified: str, path: str | None = None):
        if not qualified:
            return None
        parts = qualified.split(".")
        candidates = [(path, parts[-1])] if path else [
            ("/".join(parts[:i]) + suffix, ".".join(parts[i:]))
            for i in range(len(parts) - 1, 0, -1) for suffix in (".py", "/__init__.py")]
        for filename, name in candidates:
            if not filename:
                continue
            _, tree = self.read(root, sha, filename)
            matches = []

            def visit(node, owners=()):
                for child in getattr(node, "body", []):
                    if isinstance(child, ast.ClassDef):
                        chain = (*owners, child.name)
                        if ".".join(chain) == name:
                            matches.append(child)
                        visit(child, chain)

            visit(tree)
            if len(matches) == 1:
                return {"file": filename, "name": name, "line": matches[0].lineno, "symbol_kind": "class"}
        return None

    def callable_at(self, root: Path, sha: str, endpoint: dict):
        _, tree = self.read(root, sha, endpoint["file"])
        line = endpoint["line"]
        nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.lineno <= line <= n.end_lineno] if tree else []
        if not nodes:
            return endpoint
        node = min(nodes, key=lambda n: n.end_lineno - n.lineno)
        return {**endpoint, "line": node.lineno, "name": node.name}

    def declaration(self, root: Path, sha: str, endpoint: dict):
        """Find only the exact declaration; never infer an inherited binding."""
        path = endpoint.get("file")
        if not path or not endpoint.get("name"):
            return None
        code, tree = self.read(root, sha, path)
        matches = []

        def visit(node, owners=()):
            for child in getattr(node, "body", []):
                if isinstance(child, ast.ClassDef):
                    visit(child, (*owners, child.name))
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name == endpoint["name"] and ".".join(owners) == (endpoint.get("owner") or ""):
                        matches.append(child)

        visit(tree)
        if len(matches) != 1:
            matches = [node for node in matches if node.lineno == endpoint.get("line")]
        if len(matches) != 1:
            return None
        node = matches[0]
        decorators = [ast.unparse(d) for d in node.decorator_list]
        signature = ("async " if isinstance(node, ast.AsyncFunctionDef) else "") + node.name + "(" + ast.unparse(node.args) + ")"
        if node.returns is not None:
            signature += " -> " + ast.unparse(node.returns)
        if "property" in decorators:
            signature = "@property " + signature
        nodes = list(_scoped_nodes(node))
        returns = list(dict.fromkeys(ast.unparse(n) for n in nodes if isinstance(n, ast.Return)))
        if returns:
            behavior = "；".join(returns)
        elif any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in nodes):
            behavior = "包含 yield；生成器协议须结合源码核验"
        else:
            behavior = "无显式 return；正常执行到末尾返回 None"
        return {"endpoint": {**endpoint, "line": node.lineno}, "signature": signature,
                "behavior": behavior, "definition": ast.dump(node, include_attributes=False), "file_source": code}


def _scoped_nodes(node):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield child
        yield from _scoped_nodes(child)


def _excerpt(lines: list[str], tree, endpoint: dict):
    line = endpoint["line"]
    if tree is None:
        selected = set(range(line, min(len(lines), line + 2) + 1))
        return selected, "源码语法未能解析；仅展示定位行，函数边界及返回语义须另行核验。"
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    selected = set()

    def span(node):
        selected.update(range(node.lineno, node.end_lineno + 1))
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler)):
                body = getattr(parent, "body", [])
                if body:
                    selected.update(range(parent.lineno, body[0].lineno))
            parent = parents.get(parent)
        if node.lineno > 1 and lines[node.lineno - 2].strip().startswith(("else:", "finally:", "except")):
            selected.add(node.lineno - 1)

    def header(node):
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        first = node.body[0].lineno if node.body else node.lineno + 1
        selected.update(range(start, max(node.lineno + 1, first)))

    containers = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  and n.lineno <= line <= n.end_lineno]
    container = min(containers, key=lambda n: n.end_lineno - n.lineno) if containers else None
    note = ""
    if isinstance(container, ast.ClassDef):
        header(container)
        constructor = next((n for n in container.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and n.name == "__init__"), None)
        if constructor:
            header(constructor)
            for node in _scoped_nodes(constructor):
                super_init = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                                 and n.func.attr == "__init__" and isinstance(n.func.value, ast.Call)
                                 and isinstance(n.func.value.func, ast.Name) and n.func.value.func.id == "super"
                                 for n in ast.walk(node))
                if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)) and super_init:
                    span(node)
        else:
            note = "本类未直接定义 __init__；初始化关系需结合所列基类核验。"
    elif isinstance(container, (ast.FunctionDef, ast.AsyncFunctionDef)):
        header(container)
        nodes = list(_scoped_nodes(container))
        returns = [n for n in nodes if isinstance(n, ast.Return)]
        returned_names = {n.value.id for n in returns if isinstance(n.value, ast.Name)}
        focus = endpoint.get("name") if endpoint.get("name") != container.name else None
        for node in nodes:
            if isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom, ast.Assert)):
                span(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
                if names & (returned_names | {"output"}):
                    span(node)
            if focus and isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Return, ast.Assert, ast.Expr)) and any(
                isinstance(n, ast.Attribute) and n.attr == focus for n in ast.walk(node)):
                span(node)
        if not returns and not focus and container.body:
            span(container.body[-1])
        if not returns and not focus and container.name != "__init__" and not any(
            isinstance(n, (ast.Yield, ast.YieldFrom)) for n in nodes):
            note = "函数体无显式 return；若正常执行到末尾则返回 None。"
    else:
        statements = [n for n in ast.walk(tree) if isinstance(n, ast.stmt) and n.lineno <= line <= n.end_lineno]
        if statements:
            span(min(statements, key=lambda n: n.end_lineno - n.lineno))
    selected.add(line)
    return selected, note


def source_excerpt(root: Path, sha: str, endpoint: dict, repository: str, *, catalog=None) -> str:
    path, line = endpoint.get("file"), endpoint.get("line")
    if not path:
        return "无可定位源码；证据不足。\n"
    label = f"{path}:{line or '?'}"
    url = f"https://github.com/{repository}/blob/{sha}/{quote(path)}"
    if isinstance(line, int) and line > 0:
        url += f"#L{line}"
    location = f"[{label}]({url})" if repository else f"`{label}`（扫描基线提交 `{sha}`；未提供公开链接）"
    if not isinstance(line, int) or line < 1:
        return f"{location}：位置未知，证据不足；不能据此证明符号不存在。\n"
    code, tree = (catalog or SourceCatalog()).read(root, sha, path)
    if code is None:
        return f"{location}：源码不可读取，证据不足。\n"
    lines = code.splitlines()
    if line > len(lines):
        return f"{location}：行号超出文件范围，证据不足。\n"
    selected, note = _excerpt(lines, tree, endpoint)
    selected = sorted(i for i in selected if 1 <= i <= len(lines))
    if len(selected) > 96:
        selected = selected[:64] + selected[-32:]
        note += " 证据超过 96 行，片段已截断，须查看完整源码。"
    output = []
    previous = None
    for index in selected:
        if previous is not None and index > previous + 1:
            output.append(f"...（省略第 {previous + 1}–{index - 1} 行）")
        output.append(f"{index}: {lines[index - 1]}")
        previous = index
    snippet = "\n".join(output)
    fence = "`" * max(3, max((len(p) for p in snippet.split() if set(p) == {"`"}), default=0) + 1)
    return f"{location}\n\n{fence}text\n{snippet}\n{fence}\n{note}\n"
