# Work Memory

## Vendor Availability Checks

- Probe `checkin_url` when configured; otherwise probe `proxy_target` or `api_url`. API-only vendors also try `/v1/models` and `/models`.
- Normal 2xx/3xx responses are available. HTTP 4xx/5xx and HTML error pages disguised as HTTP 200 are unavailable.
- Retry transient timeout, TLS, and connection failures once, with modest bulk concurrency.
- Persist `availability` and `availability_checked_at` on vendor records. Health checks perform a vendor-level check and persist it; the API exposes single-vendor and bulk checks.
- Validation examples previously observed: abrdns 502 unavailable; zmoon 200 with an nginx 403 body unavailable; seeseed 200 normal page available; 781391 dashboard 404 unavailable; huan666 login page 200 available; 67.si profile 200 available. External statuses can change over time/location.
- User-reported available sites validated with HTTP 200: `api-welfalre.fengwind.com`, `byeapi.top/dashboard`, and `apivs.si/benefits` (apivs was not in the local vendor inventory at the time).
- Full sweeps over hundreds of endpoints can take many minutes. Use targeted checks when confirming a particular vendor.
