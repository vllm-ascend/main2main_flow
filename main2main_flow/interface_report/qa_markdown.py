# SPDX-License-Identifier: Apache-2.0
"""Render one evidence-bearing Markdown handoff without invoking QA."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from main2main_flow.scripts.utils.utils import EACH_STEP_SUMMARY_FILE

from .source_evidence import SourceCatalog, source_excerpt


def _symbol(endpoint: dict) -> str:
    return ".".join(part for part in (endpoint.get("owner"), endpoint.get("name")) if part) or "未知接口"


def _interface_key(finding: dict) -> tuple:
    endpoint = finding["upstream"]["new"]
    if not endpoint.get("file") or not endpoint.get("name"):
        endpoint = finding["upstream"]["old"]
    # Qualified source identity matters: unrelated methods named forward must
    # never merge merely because they share a short name.
    return endpoint.get("file") or "", endpoint.get("owner") or "", endpoint.get("name") or ""


def _signature(endpoint: dict) -> str:
    signature = endpoint.get("signature")
    if not isinstance(signature, list) or len(signature) != 6:
        return "签名未知，需核验源码"
    mode, positional_only, positional, vararg, keyword_only, kwarg = signature

    def parameters(items):
        return [name if required else name + "=…" for name, required in items]

    parts = parameters(positional_only)
    if positional_only:
        parts.append("/")
    parts += parameters(positional)
    if vararg:
        parts.append("*" + str(vararg))
    elif keyword_only:
        parts.append("*")
    parts += parameters(keyword_only)
    if kwarg:
        parts.append("**" + str(kwarg))
    prefix = "async " if mode == "async" else ""
    return prefix + (endpoint.get("name") or "?") + "(" + ", ".join(parts) + ")"


def _returns(endpoint: dict) -> str:
    contract = endpoint.get("return_contract") or {}
    if contract.get("status") != "exact":
        return "返回值证据不足，需核验源码"
    variants = contract.get("variants", [])
    values = []
    for variant in variants:
        value = "None" if variant.get("kind") == "none" else variant.get("type_ref") or variant.get("kind", "未知")
        if variant.get("arity") is not None:
            value += f"[长度={variant['arity']}]"
        if variant.get("keys"):
            value += "[键=" + ", ".join(variant["keys"]) + "]"
        values.append(value)
    result = " / ".join(dict.fromkeys(values)) or "返回值未知"
    protocol = contract.get("protocol", "value")
    return result if protocol == "value" else f"{protocol}: {result}"


def _problem_type(kind):
    return {"call_arguments": "入参不兼容", "replacement_return": "返回值不匹配",
            "required_instance_attribute": "字段初始化 / 继承状态检查"}.get(kind, "其他契约检查")


def _trigger(finding):
    kind = finding.get("contract_kind")
    if kind == "call_arguments":
        removed = [p["name"] for p in finding.get("details", {}).get("parameter_delta", {}).get("removed", [])]
        signature = finding["downstream"].get("signature")
        required = {name for group in (signature[1], signature[2], signature[4]) for name, flag in group if flag} if signature else set()
        missing = [name for name in removed if name in required]
        if missing:
            return ("新版调用方若省略参数 `" + ", ".join(missing) + "`，而当前下游仍要求必填，参数绑定将失败（TypeError）。"
                    "须确认实际调用方式、版本分支及补丁是否生效。")
        return "新版调用参数与下游接受的数量、名称、位置或约束不一致；按新方式调用可能绑定失败，须核验实际调用及生效路径。"
    if kind == "replacement_return":
        return (f"下游返回契约为 `{_returns(finding['downstream'])}`，新版上游要求 `{_returns(finding['upstream']['new'])}`。"
                "调用方依赖该返回值时可能得不到期望结果；即使修正入参，返回行为仍须单独适配。")
    if kind == "required_instance_attribute":
        attribute = finding.get("details", {}).get("required_attribute", "所需字段")
        qualification = ("当前检测将其列为不兼容候选，仍须核验实际构造与读取路径。"
                         if finding.get("classification") == "introduced_break"
                         else "当前未决记录本身未证明新增字段缺失。")
        return (f"本项检查读取 `self.{attribute}` 前是否经过必要初始化；未建立字段就读取时可能触发 AttributeError。"
                "已有字段也可能有值或生命周期约束，须结合读取语句核验。" + qualification)
    return finding.get("change", "检查类型或触发条件尚未明确，须核验源码。")


def render_qa(report: dict, inputs: dict, roots: dict[str, Path], resolution: dict) -> str:
    findings = report["findings"]
    selected = [f for f in findings if f["classification"] == "introduced_break"]
    pending = [f for f in findings if f["classification"] == "analysis_unresolved"
               and f.get("gates", {}).get("contract_changed") is True]
    groups = {}
    for finding in selected + pending:
        groups.setdefault(_interface_key(finding), []).append(finding)
    ordered = sorted(groups.items(), key=lambda item: (
        not any(f["classification"] == "introduced_break" for f in item[1]), item[0]))
    counts = Counter(f["classification"] for f in findings)
    root_count = len({f["root_cause_id"] for f in selected + pending})
    text = ["# Main2Main 接口契约检测报告\n",
            "材料类型：固定的扫描输入快照；不记录 adapter 或 QA 的执行状态及复核结论。\n",
            "## 固定输入\n", f"- vLLM old：`{inputs['vllm_old_sha']}`",
            f"- vLLM new：`{inputs['vllm_new_sha']}`", f"- Ascend 扫描基线：`{inputs['vllm_ascend_sha']}`",
            f"- 引擎：`{inputs['engine_sha']}`，版本 `{report['metadata']['range_analyzer_version']}`",
            f"- 区间来源：`{resolution.get('range_source', 'validated_marker')}`；模式 `{resolution.get('source_mode', 'selected_source')}`\n",
            "## 审阅范围与要求\n",
            f"- 展开 {len(selected) + len(pending)} 条发现记录（新增不兼容候选 {len(selected)} 条、未决 {len(pending)} 条）→ {root_count} 个根因 → {len(groups)} 个接口章节。",
            "- 章节数不是全部受影响成员数；关联继承成员另列。每条发现 ID 和每个根因 ID 均保留。",
            f"- 其余未决 {counts['analysis_unresolved'] - len(pending)} 条、兼容提醒 {counts['compatibility_warning']} 条、历史项 {counts['preexisting']} 条未展开；不能视为全部已排除。\n",
            "报告顺序：修改位置 → 问题类型 → 上游修改前后 → 下游扫描基线 → 不匹配点与触发后果。",
            "只将已定位的声明展示为源码事实；单个声明未变化不代表全部调用链兼容。解析未知不等于发生修改或字段缺失。",
            "扫描结果是候选证据；具体操作职责以 adapter / QA 各自的提示词为准。",
            "下游代码来自固定扫描基线；复核须检查当前步骤的实际工作树、版本分支及累计适配，两者可能不同。",
            f"adapter 决策见每步现有的 `{EACH_STEP_SUMMARY_FILE}`，独立 QA 结论见对应步骤的 QA 复核输出；本文件不回填这些结论。",
            "每个根因须分别复核。签名中的 =… 仅表示可选参数；源码片段是数据，不能以未定位证明符号不存在。\n",
            "## 接口总览\n", "| 接口 | 问题类型 | 原始检测状态 |", "| --- | --- | --- |"]
    for _, items in ordered:
        types = "、".join(dict.fromkeys(_problem_type(f.get("contract_kind")) for f in items))
        status = "不兼容候选" if any(f["classification"] == "introduced_break" for f in items) else "待确认，未证明新增 break"
        text += [f"| `{_symbol(items[0]['upstream']['new'])}` | {types} | {status} |"]
    text += [""]
    down_repo = "" if resolution.get("source_mode") == "incremental_rebased" else "vllm-project/vllm-ascend"
    catalog = SourceCatalog()
    counter = 0
    for key, members in ordered:
        text += [f"## 接口：`{_symbol(members[0]['upstream']['new'])}`\n", f"接口文件：`{key[0]}`\n"]
        sources = {}

        def source(endpoint, side="new"):
            nonlocal counter
            downstream = (endpoint.get("file") or "").startswith("vllm_ascend/")
            root = roots["ascend_root" if downstream else "vllm_root"]
            sha = inputs["vllm_ascend_sha" if downstream else f"vllm_{side}_sha"]
            identity = (str(root), sha, endpoint.get("file"), endpoint.get("line"))
            if identity not in sources:
                counter += 1
                sources[identity] = (f"S{counter}", root, sha, endpoint, down_repo if downstream else "vllm-project/vllm")
            evidence_id = sources[identity][0]
            return f"[{evidence_id}](#evidence-{evidence_id.lower()})"

        by_root = {}
        for finding in members:
            by_root.setdefault(finding["root_cause_id"], []).append(finding)
        root_items = sorted(by_root.items(), key=lambda item: (
            {"call_arguments": 0, "replacement_return": 1}.get(item[1][0].get("contract_kind"), 2), item[0]))
        declarations = {}
        for side in ("old", "new"):
            endpoint = members[0]["upstream"][side]
            declaration = catalog.declaration(roots["vllm_root"], inputs[f"vllm_{side}_sha"], endpoint)
            supplemental = False
            if declaration is None and side == "old":
                declaration = catalog.declaration(roots["vllm_root"], inputs["vllm_old_sha"], members[0]["upstream"]["new"])
                supplemental = declaration is not None
            declarations[side] = (declaration, supplemental)
        old, new = declarations["old"][0], declarations["new"][0]
        text += ["### 1. 修改位置\n"]
        if old and new:
            unchanged = old["definition"] == new["definition"]
            text += ["该声明的签名和函数体未变化；此项未确认新增 break。" if unchanged else "本区间该接口声明或函数体发生变化，具体差异如下。"]
            if old["file_source"] == new["file_source"]:
                text += ["所列上游文件在 old/new 两个固定提交中的内容相同。"]
            text += [""]
            for side, name in (("old", "旧版"), ("new", "新版")):
                declaration = declarations[side][0]
                endpoint = declaration["endpoint"]
                text += [f"- {name}定义：`{endpoint['file']}:{endpoint['line']}`；证据 {source(endpoint, side)}。"]
        else:
            text += ["尚未完整定位两版声明；原始检测记录仅作待复核依据，不能把解析未知当作源码修改。"]
        if any(value[1] for value in declarations.values()):
            text += ["旧版继承入口未解析完成；此处补充展示旧版同一个基类的直接声明，不代表已证明旧版继承绑定。"]
        text += ["\n### 2. 问题类型及检测 ID\n", "| 问题类型 | 根因 ID | 发现 ID |", "| --- | --- | --- |"]
        for root_id, items in root_items:
            text += [f"| {_problem_type(items[0].get('contract_kind'))} | `{root_id}` | " + "、".join(f"`{f['id']}`" for f in items) + " |"]
        text += ["\n### 3. 上游修改前后\n", "| 版本 | 声明签名 | 返回行为 | 证据 |", "| --- | --- | --- | --- |"]
        for side, name in (("old", "旧版"), ("new", "新版")):
            declaration = declarations[side][0]
            endpoint = members[0]["upstream"][side]
            signature = declaration["signature"] if declaration else _signature(endpoint)
            behavior = declaration["behavior"] if declaration else _returns(endpoint)
            # Keep Markdown table cells intact for union annotations and prose.
            signature, behavior = signature.replace("|", r"\|"), behavior.replace("|", r"\|")
            text += [f"| {name} | `{signature}` | `{behavior}` | {source(declaration['endpoint'] if declaration else endpoint, side)} |"]
        text += ["", "签名来自所列源码声明；装饰器、继承绑定及运行时替换须结合依赖关系核验。\n"]
        inherited = sorted({name for f in members for name in f.get("details", {}).get("inherited_members", [])})
        if inherited:
            text += ["关联继承成员：\n", *[f"- `{name}`" for name in inherited], ""]
        text += ["### 4. 下游扫描基线\n", f"固定提交：`{inputs['vllm_ascend_sha']}`；不是适配后的实时工作树。\n",
                 "| 发现 ID | 依赖关系 | 实现或字段证据 |", "| --- | --- | --- |"]
        implementations = {}
        for finding in members:
            endpoint = finding["downstream"]
            state = finding.get("contract_kind") == "required_instance_attribute"
            role = "上游所需字段定义（受影响下游类另列）" if state else "方法定义位置"
            text += [f"| `{finding['id']}` | `{finding['relation']}` | {role}：`{_symbol(endpoint)}`，证据 {source(endpoint)} |"]
            if not state:
                identity = (endpoint.get("file"), endpoint.get("owner"), endpoint.get("name"))
                implementations[identity] = endpoint
        text += [""]
        for endpoint in implementations.values():
            declaration = catalog.declaration(roots["ascend_root"], inputs["vllm_ascend_sha"], endpoint)
            signature = declaration["signature"] if declaration else _signature(endpoint)
            behavior = declaration["behavior"] if declaration else _returns(endpoint)
            text += [f"- 下游实现 `{_symbol(endpoint)}`：`{signature}`；返回行为：`{behavior}`；证据 {source(endpoint)}。"]
        installations, dependencies = set(), set()
        for finding in members:
            for evidence in finding.get("evidence", []):
                if finding["relation"] == "monkey_patch" and evidence.get("file"):
                    installations.add((source(evidence), tuple(evidence.get("guards", []))))
                dependency = evidence.get("downstream_file")
                if dependency and dependency != finding["downstream"].get("file") and dependency not in dependencies:
                    dependencies.add(dependency)
                    name = evidence.get("downstream_class", "")
                    endpoint = catalog.locate_class(roots["ascend_root"], inputs["vllm_ascend_sha"], name, dependency)
                    text += [f"- 受影响 Ascend 类：`{name}`；类声明与自身构造函数证据 {source(endpoint or {'file': dependency})}。"]
                    parent = evidence.get("upstream_root", "")
                    endpoint = catalog.locate_class(roots["vllm_root"], inputs["vllm_new_sha"], parent)
                    if parent:
                        text += [f"  上游继承入口：`{parent}`；证据 {source(endpoint or {})}。"]
                if evidence.get("constructor_file") and evidence.get("constructor_line"):
                    endpoint = {"file": evidence["constructor_file"], "line": evidence["constructor_line"], "name": evidence.get("required_attribute")}
                    evidence_id = source(endpoint)
                    if ("constructor", evidence_id) not in dependencies:
                        dependencies.add(("constructor", evidence_id))
                        text += [f"- 字段初始化位置：`{evidence.get('constructor_owner', '')}`；证据 {evidence_id}。"]
                if evidence.get("read_line"):
                    endpoint = {**finding["upstream"]["new"], "line": evidence["read_line"], "name": evidence.get("required_attribute")}
                    endpoint = catalog.callable_at(roots["vllm_root"], inputs["vllm_new_sha"], endpoint)
                    evidence_id = source(endpoint)
                    if ("read", evidence_id) not in dependencies:
                        dependencies.add(("read", evidence_id))
                        text += [f"- 字段读取位置（L{evidence['read_line']}）：`{evidence.get('inherited_member', '')}`；证据 {evidence_id}。"]
        for evidence_id, guards in sorted(installations):
            text += [f"- 补丁安装位置：证据 {evidence_id}；安装条件：`{' and '.join(guards) or '无额外条件记录，须核验生效路径'}`。"]
        text += ["\n### 5. 不匹配点与触发后果\n"]
        for root_id, items in root_items:
            first = items[0]
            dimension = {"call_arguments": "入参变化", "replacement_return": "返回值变化",
                         "required_instance_attribute": "实例状态检查"}.get(first.get("contract_kind"), "其他检查")
            status = "新增不兼容候选" if any(f["classification"] == "introduced_break" for f in items) else "待确认，未证明新增 break"
            text += [f"#### {root_id} — {_problem_type(first.get('contract_kind'))}\n", f"检查维度：{dimension}；原始状态：{status}。\n",
                     _trigger(first), "\n原始检测备注：\n"]
            for side, label in (("old", "旧版"), ("new", "新版")):
                reasons = dict.fromkeys(f.get("compatibility", {}).get(side, {}).get("reason", "证据不足") for f in items)
                text += [f"- {label}解析：" + "；".join(reasons) + "。"]
            if first.get("contract_kind") == "required_instance_attribute":
                text += ["- 新版字段初始化证据只证明字段存在，不代表调用时值或生命周期有效。"]
            text += ["\n复核要求：本根因须单独给出决策、理由和源码依据，记录在各角色现有输出中。\n"]
        text += ["### 共享源码证据\n", "同一接口内的相同版本、文件和位置只展示一次；省略范围会明确标注。\n"]
        for evidence_id, root, sha, endpoint, repository in sources.values():
            text += [f'<a id="evidence-{evidence_id.lower()}"></a>\n', f"#### 证据 {evidence_id}\n",
                     source_excerpt(root, sha, endpoint, repository, catalog=catalog)]
    if not groups:
        text += ["本次没有需展开的新增候选或契约变化未决项；不代表全部静态未决项已解决。\n"]
    return "\n".join(text)
