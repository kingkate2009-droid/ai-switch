# AI Switch v2.3.0

## Highlights

- **Vendor types**: tag each vendor as 公益站 (Public) / 中转站 (Relay) / 纯KEY (Raw key) and filter by type in the **Models catalog**, **Check-in** page and the **Vendors** list — great for grouping free community relays, reseller gateways and official keys.
- **Archive system**: one-click archive a vendor (and all its keys) or a single key. Archived items appear **only** in the new **Archived** view — hidden from every other page and **excluded from health checks and backend sync** — and can be restored anytime.
- **Auto-archive on continuous failures**: a key that keeps failing health checks for a configurable number of days (default 10, `Settings → health_archive_streak_days`) is archived automatically.
- **Single-key health now re-scans model lists every run** (was reusing the cached list after the first check), and **bulk check-all** behaves the same way.
- **Single-model check probes only that model** (the modal's per-model button no longer re-checks the whole key's inventory).
- **Single-key check syncs backends on completion**, scoped to that vendor only (no full rewrite of every engine).
- **Stability fix**: replaced a non-reentrant lock in the health-checker with an `RLock`, fixing a self-deadlock that could leave scheduled/manual health checks stuck indefinitely.

## Upgrade Notes

- No migration needed. Existing vendors ship with an empty type (shown as “—”); set it under Edit vendor → Vendor type.
- Auto-archive uses the streak already tracked in the health cache; keys that have been continuously failing for ≥ `health_archive_streak_days` will be archived on the next health run once enabled.