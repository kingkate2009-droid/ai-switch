from __future__ import annotations

from core.paths import version_file

__version__ = "2.0.3"


def get_version() -> str:
    try:
        p = version_file()
        if p.exists():
            return p.read_text(encoding="utf-8").strip() or __version__
    except Exception:
        pass
    return __version__
