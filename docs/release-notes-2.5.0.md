# AI Switch v2.5.0

This release is mostly about the two things that were missing from the previous
version: **a safe default when you expose the app**, and **the ability to
actually switch accounts** rather than just collect them.

## Network access is safe by default

Binding to a loopback address needs no token — local use stays frictionless.
Binding to a routable address, including `0.0.0.0`, now requires one. An
explicit `AI_SWITCH_ACCESS_TOKEN` still wins everywhere.

`docker-compose.yml` publishes on `127.0.0.1` only, and the image defaults to
`0.0.0.0` inside the container. `app.py`'s `__main__` now reads
`AI_SWITCH_HOST` instead of a hard-coded address — containers were previously
unreachable.

## Account switching (换号)

The accounts store could import OAuth logins and check quota, but nothing could
write an account *back into* a tool, so switching was impossible.
`core/account_adapters/` adds that half.

Each adapter knows where a tool stores its login, how to parse it, and how to
write an account back. The registry auto-discovers one module per tool, so
supporting a new platform means adding a file.

Shipped:

| Platform | Credential store |
|---|---|
| Claude Code | macOS Keychain `Claude Code-credentials`, or `~/.claude/.credentials.json` |
| Codex / ChatGPT | `~/.codex/auth.json` |
| Gemini CLI | `~/.gemini/oauth_creds.json` |
| Grok CLI | `~/.grok/auth.json` |
| Kiro | `~/.aws/sso/cache/kiro-auth-token.json` + `profile.json` |
| Cursor | `…/Cursor/User/globalStorage/state.vscdb` (plain-text keys) |
| Antigravity IDE | Keychain `service=gemini, account=antigravity` |
| Zed | macOS Keychain internet-password `https://zed.dev` |

Every write copies the previous credential to `<file>.ai-switch.bak` first.
Switching is an explicit action and is blocked in read-only mode. Switching
edits files outside AI Switch, which is also why it needs the same trust as
running the tool itself.

## Outbound notifications

Four events — budget exceeded, a key failing, all of a vendor's keys failing,
and sync failure — delivered to Feishu, Slack, or a generic webhook.

`notify()` enqueues to a daemon worker and posts with a short timeout, so it
never raises and never blocks. Gate order is `disabled → not subscribed →
quiet hours (critical exempt) → no channel → rate limited`. Budget evaluation
runs on its own 300 s tick so it works even with health checks off.

## Fixes

- **Targeted pushes no longer delete.** A push scoped to `vendor_ids` or
  `key_ids` used to make the adapter treat everything else as "remove me". Both
  now reconcile the full set; `model_ids` stays targeted.
- **Smart import** no longer explodes into a Cartesian product.
- **Dropdown menus** were painted under the table because row-level `opacity`
  plus `.actions-cell { position: sticky }` created a stacking context. Fixed
  by muting per cell, portalling the menu to `<body>`, and dimming the page with
  the menu's own shadow.
- **Zero means off.** `min_interval_seconds()` used `value or DEFAULT`, so a
  configured `0` silently became 60 minutes.
- **Archived keys stop nagging.** Health alerts now only consider keys still in
  rotation.

## Tests and CI

A regression suite (76 checks) covering access policy, push safety, the
notification engine, account adapters, end-to-end account switching, and app
smoke tests — plus a template inline-script syntax check. `tests/conftest.py`
redirects `HOME` before any app module loads, so the suite cannot touch real
data. CI runs `compileall`, the suite, and the template check.

## Not in this release

Ten platform variants store their login encrypted (VS Code `safeStorage`,
Trae's icube cipher, ZCode's AES-256-GCM). Reading or writing those needs a
crypto layer, which is a separate decision — see
[docs/account-switching.md](account-switching.md). Automatic rotation on quota
exhaustion is designed but not implemented; it will be opt-in and off by
default.