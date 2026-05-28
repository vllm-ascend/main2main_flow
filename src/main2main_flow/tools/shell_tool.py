"""Shell command execution tool for main2main adapter crew.

Provides a safe, scoped shell execution capability for agents that need to run
git operations, grep searches, and pre-CI check scripts within the vllm-ascend
repository.
"""
from __future__ import annotations

import subprocess
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class ShellCommandInput(BaseModel):
    command: str = Field(
        ...,
        description=(
            "Shell command to execute. Suitable for: "
            "git diff, git status, git log, grep searches in source files, "
            "python3 scripts/pre_ci_check.py, python3 -m py_compile <file>. "
            "Do NOT use for destructive operations outside the vllm-ascend repository."
        ),
    )
    cwd: str = Field(
        default="",
        description="Working directory for the command. Leave empty to use the system default.",
    )


class ShellCommandTool(BaseTool):
    name: str = "shell_command"
    description: str = (
        "Execute a shell command and return its stdout output. "
        "Use for: reading git diffs, checking file status, running pre_ci_check.py, "
        "searching source code with grep, and validating Python syntax. "
        "Commands are run with a 120-second timeout."
    )
    args_schema: Type[BaseModel] = ShellCommandInput

    def _run(self, command: str, cwd: str = "") -> str:
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=cwd if cwd else None,
                timeout=120,
            )
            output = result.stdout
            if result.returncode != 0 and result.stderr:
                output += f"\n[stderr]: {result.stderr}"
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return "[error]: Command timed out after 120 seconds"
        except Exception as e:
            return f"[error]: {e}"
