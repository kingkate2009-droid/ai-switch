import json
from pathlib import Path
from typing import Optional

def _locales_dir() -> Path:
    try:
        from core.paths import resource_root
        return resource_root() / "locales"
    except Exception:
        return Path(__file__).resolve().parent.parent / "locales"


LOCALES_DIR = _locales_dir()

_translations: dict[str, dict[str, str]] = {}
_locale_mtimes: dict[str, float] = {}

SUPPORTED_LANGS = {
    "en": "English",
    "zh-CN": "简体中文",
    "zh-TW": "繁體中文",
}

_LANG_FILE = {
    "zh-CN": "zh_CN",
    "zh-TW": "zh_TW",
    "en": "en",
    "zh": "zh_CN",
}


def _locale_path(lang: str) -> Path:
    filename = _LANG_FILE.get(lang, "en")
    return _locales_dir() / f"{filename}.json"


def _load(lang: str) -> dict[str, str]:
    path = _locale_path(lang)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        try:
            _locale_mtimes[lang] = path.stat().st_mtime
        except OSError:
            pass
        return data
    return {}


def _is_stale(lang: str) -> bool:
    path = _locale_path(lang)
    if not path.exists():
        return False
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return lang not in _locale_mtimes or _locale_mtimes.get(lang) != mtime


def get_translations(lang: str) -> dict[str, str]:
    """Load locale strings; auto-reload when locale file changes on disk."""
    if lang not in _translations or _is_stale(lang):
        _translations[lang] = _load(lang)
    return _translations[lang]


def clear_translation_cache() -> None:
    _translations.clear()
    _locale_mtimes.clear()


def t(key: str, lang: str = "en", default: Optional[str] = None) -> str:
    strings = get_translations(lang)
    if key in strings:
        return strings[key]
    en_strings = get_translations("en")
    if key in en_strings:
        return en_strings[key]
    return default if default is not None else key


def resolve_lang(accept_language: Optional[str], cookie_lang: Optional[str]) -> str:
    if cookie_lang and cookie_lang in SUPPORTED_LANGS:
        return cookie_lang
    if accept_language:
        for part in accept_language.split(","):
            code = part.split(";")[0].strip()
            if code.startswith("zh-CN") or code.startswith("zh-Hans"):
                return "zh-CN"
            if code.startswith("zh-TW") or code.startswith("zh-HK") or code.startswith("zh-Hant"):
                return "zh-TW"
            if code.startswith("zh"):
                return "zh-CN"
            if code.startswith("en"):
                return "en"
    return "en"
