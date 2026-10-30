#!/usr/bin/env python3
"""Syntax-check the inline JavaScript in templates/index.html.

``templates/index.html`` carries a large inline ``<script>`` block.  A stray
brace there is invisible to Python tooling and only shows up as a dead page in
the browser -- exactly the kind of breakage a syntax check should catch.

Jinja expressions are replaced with ``null`` before checking so template tags
don't trip the parser.  Skips (exit 0) when ``node`` is unavailable.

Run:  python3 tests/check_template_js.py
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "templates" / "index.html"

_JINJA = (
    (re.compile(r"\{#.*?#\}", re.S), ""),
    (re.compile(r"\{\{.*?\}\}", re.S), "null"),
    (re.compile(r"\{%.*?%\}", re.S), ""),
)


def _strip_jinja(src: str) -> str:
    for pattern, repl in _JINJA:
        src = pattern.sub(repl, src)
    return src


def inline_scripts(html: str):
    """Yield (index, source) for every inline <script> block."""
    for i, m in enumerate(re.finditer(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", html, re.S)):
        if "src=" in (m.group("attrs") or ""):
            continue
        yield i, m.group("body")


def main() -> int:
    if not TEMPLATE.exists():
        print(f"missing template: {TEMPLATE}")
        return 1

    html = TEMPLATE.read_text(encoding="utf-8")
    blocks = list(inline_scripts(html))
    if not blocks:
        print("no inline <script> blocks found (nothing to check)")
        return 0

    node = shutil.which("node")
    if not node:
        print("node not found -- skipping JS syntax check")
        return 0

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for index, body in blocks:
            path = Path(tmp) / f"block{index}.js"
            path.write_text(_strip_jinja(body), encoding="utf-8")
            proc = subprocess.run(
                [node, "--check", str(path)],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                failures += 1
                print(f"FAIL  inline <script> block #{index}")
                print((proc.stderr or proc.stdout).strip()[:2000])
            else:
                print(f"PASS  inline <script> block #{index} ({len(body)} chars)")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
