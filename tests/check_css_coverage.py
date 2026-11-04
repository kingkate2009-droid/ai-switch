#!/usr/bin/env python3
"""Check that every CSS class the UI uses is actually defined.

The single-page UI composes markup in two places: the Jinja template and the
inline JavaScript that builds rows/cards. A redesign that drops a selector for
one of those classes fails silently — the page still renders, it just looks
wrong in a corner nobody screenshotted.

This keeps that honest: extract every class token from templates/index.html,
compare it against static/style.css, and exit non-zero on anything unstyled.

Usage:
    python3 tests/check_css_coverage.py
    python3 tests/check_css_coverage.py --list   # print every class in use
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "templates" / "index.html"
CSS = ROOT / "static" / "style.css"

# Classes that are intentionally unstyled: state hooks toggled by JS, or
# presentational helpers whose absence is the point.
ALLOWLIST = {
    "hidden",  # display:none is applied inline by JS
}

CLASS_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
CSS_CLASS = re.compile(r"\.(-?[A-Za-z_][A-Za-z0-9_-]*)")


def html_classes(text: str) -> dict[str, int]:
    """Collect class tokens from class="…" attributes, including JS strings."""
    found: dict[str, int] = {}
    # class="a b c"  /  className = 'a b'  /  'backend-card' literals are
    # covered by the class= pattern plus className assignments.
    for match in re.finditer(r"""class\s*=\s*["']([^"']*)["']""", text):
        for token in match.group(1).split():
            token = token.strip()
            if not CLASS_TOKEN.fullmatch(token):
                continue
            # JS concatenation leaves prefixes like 'task-' or 'vendor-status-'
            # in the attribute; those are not real class names.
            if token.endswith("-") or len(token) < 3:
                continue
            found[token] = found.get(token, 0) + 1
    # JS: card.className = 'a' + more ;  el.classList.add('x')
    for match in re.finditer(r"""class(?:Name|List\.add)\s*=\s*['"]([^'"]+)['"]""", text):
        for token in match.group(1).split():
            if CLASS_TOKEN.fullmatch(token) and not token.endswith("-") and len(token) >= 3:
                found[token] = found.get(token, 0) + 1
    for match in re.finditer(r"""classList\.add\(\s*['"]([^'"]+)['"]""", text):
        for token in match.group(1).split():
            if CLASS_TOKEN.fullmatch(token) and not token.endswith("-") and len(token) >= 3:
                found[token] = found.get(token, 0) + 1
    return found


def css_classes(text: str) -> set[str]:
    # Strip comments so selectors quoted in prose do not count.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return set(CSS_CLASS.findall(text))


def main() -> int:
    if not HTML.exists() or not CSS.exists():
        print("missing template or stylesheet")
        return 2
    used = html_classes(HTML.read_text(encoding="utf-8"))
    defined = css_classes(CSS.read_text(encoding="utf-8"))

    if "--list" in sys.argv:
        for name in sorted(used):
            mark = "ok" if name in defined or name in ALLOWLIST else "MISSING"
            print(f"{mark:8} {name:42} {used[name]}")
        return 0

    missing = sorted(n for n in used if n not in defined and n not in ALLOWLIST)
    if missing:
        print(f"{len(missing)} class(es) used but not styled:")
        for name in missing:
            print(f"  .{name}  ({used[name]} use(s))")
        return 1
    print(f"OK — {len(used)} classes in use, all styled ({len(defined)} selectors defined)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
