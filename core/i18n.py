import json
from pathlib import Path
from typing import Optional

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

_translations: dict[str, dict[str, str]] = {}

SUPPORTED_LANGS = {
    "en": "English",
    "zh-CN": "简体中文",
    "zh-TW": "繁體中文",
}


def _load(lang: str) -> dict[str, str]:
    lang_map = {
        "zh-CN": "zh_CN",
        "zh-TW": "zh_TW",
        "en": "en",
        "zh": "zh_CN",
    }
    filename = lang_map.get(lang, "en")
    path = LOCALES_DIR / f"{filename}.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_translations(lang: str) -> dict[str, str]:
    if lang not in _translations:
        _translations[lang] = _load(lang)
    return _translations[lang]


def t(key: str, lang: str = "en", default: Optional[str] = None) -> str:
    strings = get_translations(lang)
    if key in strings:
        return strings[key]
    en_strings = get_translations("en")
    if key in en_strings:
        return en_strings[key]
    return default or key


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
