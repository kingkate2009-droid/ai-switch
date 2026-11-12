# Work Memory

## Vendor Availability Checks

### Goal

Check the actual vendor site users visit, distinguish real pages from proxy/WAF error pages, and retain the last result across page refreshes.

### Current Behavior

- Availability probes prefer a vendor's `checkin_url`, then fall back to `proxy_target` or `api_url`.
- A configured check-in URL is authoritative. API-only vendors additionally try `/v1/models` and `/models`.
- HTTP 4xx/5xx responses are unavailable. A 2xx response containing a recognizable HTML 4xx/5xx gateway/error page is also unavailable. Normal 2xx/3xx responses are available; redirects are followed.
- Transient timeout, TLS, and connection failures are retried once with a short jittered delay and a fresh thread-local session.
- Availability is stored on the vendor (`availability`, `availability_checked_at`) and returned by the vendors API. Health checks probe each relevant vendor once and persist the result.
- Bulk availability checks use at most six workers to avoid creating excessive simultaneous TLS connections.

### Implementation Locations

- `core/providers.py`: `check_vendor_availability()` and `_html_error_page()`.
- `core/data.py`: `update_vendor()` persists availability fields.
- `core/health_checker.py`: health-check runs perform and persist vendor-level availability checks.
- `app.py`: single-vendor and bulk availability routes persist results; bulk route caps concurrency.

### Verified Examples (2026-10-10)

- `https://checkin.new-api.abrdns.com/checkin`: HTTP 502, unavailable.
- `https://newapi.zmoon.top/profile`: HTTP 200 with an nginx 403 error page body, unavailable.
- `https://api-yi-hydrogel.seeseed1ck.icu/profile`: normal HTTP 200 page, available.
- `http://781391.cc.cd/dashboard`: HTTP 404, unavailable.
- `https://ai.huan666.de/profile`: normal HTTP 200 login page, available.
- `https://api.67.si/profile`: HTTP 200, available.
- `https://api-welfalre.fengwind.com/`, `https://byeapi.top/dashboard`, and `https://apivs.si/benefits`: normal HTTP 200 pages, available. `apivs.si` was not present in the local vendor list during verification.

### Caveats

- Network outcomes can vary by time, DNS, proxy/VPN, and client location. A single external probe is not a global uptime guarantee.
- Full scans across hundreds of vendors can be slow due to unreachable hosts and request timeouts; targeted checks are faster.
- Keep unrelated pending changes out of the availability commit unless explicitly requested.

## README Screenshots & UI Polish (2026-11-11/12)

### Goal

Re-shoot every README screenshot after the Calm Console rebuild, using only fictional demo data, with a caption under each image; keep secrets and runtime data out of the public repo.

### How screenshots are produced

- Seed a throwaway dataset with `HOME=/tmp/ocdemo` (never the real `~/.ai-switch`): fake vendors `Aurora Relay` / `Bluepeak AI` / `Cobalt Cloud` / `Driftwood Labs` (+ archived `Legacy Sandbox`, `Old Relay`), keys `sk-demo-*`, 7 days of usage, health cache, quality scores, downstream keys, audit JSONL.
- Run the app from a **copy** of the source at `/tmp/ocdemo-app` so Diagnostics `resource_root` cannot leak the developer's real path.
- Playwright (system `/usr/bin/python3` + `PYTHONPATH=.../Python/3.9/lib/python/site-packages`) with `locale="en-US"`, viewport 1440x1000, light theme, `aiswitch_onboarding_skip=1`.
- Correct `endpoint_capabilities` shape matters: `{mode, detected, selected, checks:{ep:{healthy,checked_at,status_code}}, checked_at, classified}`; `model_health[m]` needs a fresh `checked_at`, otherwise the Models "Enabled" column reads 0/N.

### Bugs the screenshots exposed (fixed)

- `#miniLabel` had `data-i18n` so `updateI18n()` kept resetting it to "Checking..."; removed the attribute and call `updateMiniStatus()` from `loadDashboard()`.
- `pagerHtml` compared `info.total <= info.size` where `size` is undefined for empty tables, printing `undefined–undefined / 0` and `NaN`; guard numerically.
- Check-in summary counted only access tokens, so URL-only vendors showed "Not supported" next to "Open link"; count `token || checkin_url` and add a `Manual link` badge.
- Accounts nav label and the models-modal filter toolbar were hardcoded Chinese; routed through i18n.
- Missing `models.modality.other` printed the raw key; added to all three locales.

### Caveats

- Backends screenshot reflects the real host CLI detection (running gateways/ports) — environment info only, no keys or accounts.
- `pytest tests/` as one run fails (~25) due to cross-module env/state leakage; this is pre-existing (same on a clean `f3eb7d7` worktree). Run each file standalone instead.
- Release `v2.5.0` tags `ea3c8af` on `main`.

