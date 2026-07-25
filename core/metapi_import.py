"""Import MetaAPI / metapi-style backups into AI Switch vendors/keys."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from core.data import (
    add_key,
    add_vendor,
    delete_key,
    delete_vendor,
    find_key_anywhere,
    get_vendor,
    update_key,
    update_vendor,
    _norm_secret,
    suggest_key_name,
)

# metapi platform -> AI Switch provider id / endpoint_type
_PLATFORM_MAP = {
    "openai": ("openai", "openai"),
    "claude": ("anthropic", "anthropic"),
    "anthropic": ("anthropic", "anthropic"),
    "gemini": ("google", "gemini"),
    "gemini-cli": ("google", "gemini"),
    "antigravity": ("google", "gemini"),
    "codex": ("openai", "openai"),
    "new-api": ("newapi", "openai"),
    "sub2api": ("sub2api", "openai"),
    "cliproxyapi": ("cliproxyapi", "openai"),
    "anyrouter": ("openrouter", "openai"),
}


def is_metapi_backup(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    acc = payload.get("accounts")
    if not isinstance(acc, dict):
        return False
    return isinstance(acc.get("sites"), list) and isinstance(acc.get("accounts"), list)


def _normalize_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    # drop trailing slash for comparison (except scheme-only)
    u = u.rstrip("/")
    # lowercase host only
    try:
        p = urlparse(u)
        if p.scheme and p.netloc:
            host = p.netloc.lower()
            path = p.path or ""
            if path != "/" and path.endswith("/"):
                path = path.rstrip("/")
            u = f"{p.scheme}://{host}{path}"
            if p.query:
                u += f"?{p.query}"
    except Exception:
        pass
    return u


def _url_looks_usable(url: str) -> bool:
    """Reject placeholder / incomplete site URLs from MetaAPI."""
    u = (url or "").strip()
    if not u:
        return False
    try:
        p = urlparse(u)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.netloc:
        return False
    host = p.netloc.split("@")[-1].split(":")[0].lower()
    if not host or " " in host or "%20" in host:
        return False
    # bare labels without TLD (e.g. https://anyrouter) — except localhost / IP
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        return True
    if "." not in host:
        return False
    return True


def _origin_from_url(url: str) -> str:
    try:
        p = urlparse((url or "").strip())
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc.lower()}"
    except Exception:
        pass
    return ""


def _resolve_site_url(site: dict) -> str:
    """Pick best API base URL: usable site.url, else origin of externalCheckinUrl."""
    raw = (site.get("url") or "").strip()
    if _url_looks_usable(raw):
        return raw
    checkin = (site.get("externalCheckinUrl") or "").strip()
    origin = _origin_from_url(checkin)
    if origin and _url_looks_usable(origin):
        return origin
    return raw


def _ensure_api_url(url: str, endpoint_type: str) -> str:
    u = _normalize_url(url)
    if not u:
        return ""
    if endpoint_type == "openai":
        # common OpenAI-compatible root without /v1
        low = u.lower()
        if not any(x in low for x in ("/v1", "/openai", "generativelanguage", "anthropic", "codex")):
            # only append if path is empty or just domain
            try:
                p = urlparse(u)
                if p.path in ("", "/"):
                    u = u + "/v1"
            except Exception:
                pass
    return u


def _map_platform(platform: str) -> tuple[str, str]:
    p = (platform or "").strip().lower()
    if p in _PLATFORM_MAP:
        return _PLATFORM_MAP[p]
    if "claude" in p or "anthropic" in p:
        return "anthropic", "anthropic"
    if "gemini" in p or "google" in p:
        return "google", "gemini"
    return (p or "custom"), "openai"


def _url_bases(api_url: str) -> set[str]:
    want = _normalize_url(api_url)
    if not want:
        return set()
    want_l = want.lower().rstrip("/")
    bases = {want_l}
    if want_l.endswith("/v1"):
        bases.add(want_l[:-3])
    else:
        bases.add(want_l + "/v1")
    return bases


def _find_vendor_by_url(api_url: str):
    """Match vendor by normalized api_url only (not provider id)."""
    from core.data import get_vendors
    want_bases = _url_bases(api_url)
    if not want_bases:
        return None
    for v in get_vendors():
        vu_bases = _url_bases(v.get("api_url") or "")
        if vu_bases & want_bases:
            return v
    return None


def _find_vendor_by_name(name: str):
    from core.data import get_vendors
    name_l = (name or "").strip().lower()
    if not name_l:
        return None
    for v in get_vendors():
        if (v.get("name") or "").strip().lower() == name_l:
            return v
    return None


def _site_dedupe_key(site: dict) -> str:
    url = _normalize_url(site.get("url") or "")
    if url:
        return "url:" + url.lower()
    name = (site.get("name") or "").strip().lower()
    plat = (site.get("platform") or "").strip().lower()
    return f"name:{name}|plat:{plat}|id:{site.get('id')}"


def _account_secret(acc: dict) -> str:
    for field in ("apiToken", "api_token", "apiKey", "api_key", "accessToken", "token"):
        s = _norm_secret(acc.get(field) or "")
        if s:
            return s
    return ""


def _account_name(acc: dict, site: dict) -> str:
    for field in ("username", "name", "label"):
        n = (acc.get(field) or "").strip()
        if n:
            return n[:80]
    secret = _account_secret(acc)
    if secret:
        return suggest_key_name(secret)
    return f"metapi-{acc.get('id') or 'key'}"


def _place_key_on_vendor(vid: str, acc: dict, site: dict) -> str:
    """Ensure account secret lives on vendor. Returns: added|moved|exists|skipped."""
    secret = _account_secret(acc)
    if not secret:
        return "skipped"
    kn = _account_name(acc, site)
    status = (acc.get("status") or "").lower()
    enabled = status not in ("disabled", "expired", "inactive", "banned")
    notes_parts = ["from metapi"]
    if acc.get("username"):
        notes_parts.append(f"user={acc.get('username')}")
    if status and status != "active":
        notes_parts.append(f"status={status}")
    notes = "; ".join(notes_parts)

    hit = find_key_anywhere(secret)
    if hit:
        src_v, src_k = hit
        if str(src_v.get("id")) == str(vid):
            return "exists"
        # Move key to the MetaAPI site vendor (preserve fields)
        payload = {
            "name": src_k.get("name") or kn,
            "enabled": src_k.get("enabled") if "enabled" in src_k else enabled,
            "notes": src_k.get("notes") or notes,
        }
        for f in ("models", "default_model", "check_model", "disabled_models", "role", "model_health"):
            if f in src_k:
                payload[f] = src_k.get(f)
        delete_key(str(src_v["id"]), str(src_k["id"]))
        entry = add_key(vid, payload["name"], secret, notes=str(payload.get("notes") or ""))
        if not entry or entry.get("_existing"):
            return "exists"
        extra = {k: v for k, v in payload.items() if k not in ("name", "notes") and v is not None}
        if extra:
            try:
                update_key(vid, str(entry["id"]), **extra)
            except Exception:
                pass
        return "moved"

    entry = add_key(vid, kn, secret, notes=notes)
    if not entry:
        return "skipped"
    if entry.get("_existing"):
        return "exists"
    if not enabled:
        try:
            update_key(vid, str(entry["id"]), enabled=False)
        except Exception:
            pass
    return "added"


def import_metapi_backup(payload: dict, *, include_disabled: bool = True) -> dict:
    """Import metapi backup: sites -> vendors, accounts -> keys (deduped merge).

    - Deduplicate sites by normalized URL (first wins for vendor identity)
    - Skip sites that have no KEY (no account secret linked by siteId)
    - Keys linked by siteId; move existing secrets onto the site vendor when needed
    - externalCheckinUrl -> vendor.checkin_url (set if present)
    - site/account status disabled -> key.enabled False (when creating)
    """
    if not is_metapi_backup(payload):
        raise ValueError("not a metapi backup (need accounts.sites + accounts.accounts)")

    acc_root = payload["accounts"]
    sites_raw = list(acc_root.get("sites") or [])
    accounts_raw = list(acc_root.get("accounts") or [])

    # Deduplicate sites
    site_by_id: dict[Any, dict] = {}
    dedupe_map: dict[str, dict] = {}  # dedupe_key -> canonical site
    site_id_to_canonical: dict[Any, dict] = {}
    skipped_sites = 0

    for s in sites_raw:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        site_by_id[sid] = s
        # normalize url field for better dedupe when recoverable
        resolved = _resolve_site_url(s)
        s_for_key = dict(s)
        if resolved:
            s_for_key["url"] = resolved
        dk = _site_dedupe_key(s_for_key)
        if dk in dedupe_map:
            skipped_sites += 1
            # map this site id to the first site with same URL
            site_id_to_canonical[sid] = dedupe_map[dk]
            # prefer non-null checkin from later sites
            can = dedupe_map[dk]
            if not can.get("externalCheckinUrl") and s.get("externalCheckinUrl"):
                can["externalCheckinUrl"] = s.get("externalCheckinUrl")
            continue
        # store resolved url on working copy
        work = dict(s)
        if resolved:
            work["url"] = resolved
        dedupe_map[dk] = work
        site_id_to_canonical[sid] = work

    # Group accounts by canonical site id (only keep accounts with secrets)
    from collections import defaultdict
    accounts_by_site: dict[Any, list] = defaultdict(list)
    empty_secret_accounts = 0
    for a in accounts_raw:
        if not isinstance(a, dict):
            continue
        if not _account_secret(a):
            empty_secret_accounts += 1
            continue
        sid = a.get("siteId")
        can = site_id_to_canonical.get(sid)
        if not can:
            continue
        accounts_by_site[can.get("id")].append(a)

    added_v = added_k = skipped_k = updated_checkin = 0
    merged_v = moved_k = 0
    sites_skipped_no_keys = 0
    sites_imported = 0

    for site in dedupe_map.values():
        if not include_disabled and (site.get("status") or "").lower() == "disabled":
            continue
        site_accounts = accounts_by_site.get(site.get("id"), [])
        # filter: no KEY → skip site entirely
        if not site_accounts:
            sites_skipped_no_keys += 1
            continue

        sites_imported += 1
        name = (site.get("name") or "").strip() or f"site-{site.get('id')}"
        # clean weird puny/encoded display names slightly
        name = re.sub(r"\s+", " ", name).strip()[:80]
        raw_url = _resolve_site_url(site) or (site.get("url") or "").strip()
        provider_id, endpoint_type = _map_platform(site.get("platform") or "")
        api_url = _ensure_api_url(raw_url, endpoint_type)
        checkin = (site.get("externalCheckinUrl") or "").strip()

        # Prefer URL match — many metapi sites share platform "new-api"
        existing = None
        if api_url:
            existing = _find_vendor_by_url(api_url)
        if not existing and name:
            existing = _find_vendor_by_name(name)

        if not existing:
            existing = add_vendor(
                name=name,
                provider=provider_id,
                api_url=api_url or raw_url,
                endpoint_type=endpoint_type,
                checkin_url=checkin,
            )
            added_v += 1
        else:
            merged_v += 1
            patch: dict[str, Any] = {}
            if checkin:
                cur = (existing.get("checkin_url") or "").strip()
                if cur != checkin:
                    patch["checkin_url"] = checkin
                    updated_checkin += 1
            cur_url = (existing.get("api_url") or "").strip()
            if api_url and _url_looks_usable(api_url) and (not cur_url or not _url_looks_usable(cur_url)):
                patch["api_url"] = api_url
            # Prefer MetaAPI site name when current name is generic placeholder
            cur_name = (existing.get("name") or "").strip()
            if name and (not cur_name or cur_name.lower() in ("custom", "newapi", "sub2api", "openai")):
                patch["name"] = name
            if patch:
                update_vendor(str(existing["id"]), **patch)
                existing = get_vendor(str(existing["id"])) or existing

        vid = str(existing["id"])
        for a in site_accounts:
            result = _place_key_on_vendor(vid, a, site)
            if result == "added":
                added_k += 1
            elif result == "moved":
                moved_k += 1
            else:
                skipped_k += 1

    # Drop vendors left empty after key moves
    from core.data import get_vendors
    removed_empty = 0
    for v in list(get_vendors()):
        if not (v.get("keys") or []):
            if delete_vendor(str(v["id"])):
                removed_empty += 1

    return {
        "format": "metapi",
        "mode": "merge",
        "sites_in": len(sites_raw),
        "sites_deduped": len(dedupe_map),
        "sites_skipped_dup": skipped_sites,
        "sites_skipped_no_keys": sites_skipped_no_keys,
        "sites_imported": sites_imported,
        "accounts_in": len(accounts_raw),
        "accounts_empty_secret": empty_secret_accounts,
        "vendors_added": added_v,
        "vendors_merged": merged_v,
        "vendors_removed_empty": removed_empty,
        "keys_added": added_k,
        "keys_moved": moved_k,
        "keys_skipped": skipped_k,
        "checkin_updated": updated_checkin,
    }
