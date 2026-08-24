#!/usr/bin/env python3
"""Render docs/images/workflow.png from docs/images/workflow.dot.

Requires graphviz:  brew install graphviz
"""
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
dot_path = HERE / "workflow.dot"
png_path = HERE / "workflow.png"

subprocess.run(
    ["dot", "-Tpng", "-Gdpi=110", str(dot_path), "-o", str(png_path)],
    check=True,
)
print(f"rendered {png_path.name}")
