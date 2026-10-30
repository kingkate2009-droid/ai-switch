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
