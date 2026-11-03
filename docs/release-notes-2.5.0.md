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

## Volcengine Ark (火山方舟)

The old single preset pointed at `…/api/coding/v3`, which is the **Coding Plan**
endpoint. Anyone using a normal pay-as-you-go Ark key got model-not-found errors
from it, so Volcengine "did not work".

Ark is now four presets, because the two products differ in base URL, API key
and model naming:

| Preset | Base URL | Protocol |
|---|---|---|
| `volcengine` | `/api/coding/v3` | OpenAI |
| `volcengine-coding-anthropic` | `/api/coding` | Anthropic |
| `volcengine-ark` | `/api/v3` | OpenAI |
| `volcengine-ark-anthropic` | `/api/compatible` | Anthropic |

Probing now picks model candidates from the product the URL belongs to
(`ark-code-latest` for Coding Plan, dated IDs such as `doubao-seed-2-1-pro-…`
for pay-as-you-go) instead of sharing one wrong list. `recognize_provider()`
also matches the **full base URL** rather than only the hostname, so
`…/api/v3` is no longer mistaken for `…/api/coding/v3`.

Note the asymmetric failure: a Coding Plan key on `/api/v3` does not error, it
silently bills pay-as-you-go. See [volcengine.md](volcengine.md).

## Balance token fixes

- **A saved balance token can finally be removed.** The client only ever sent
  `access_token` when the field was non-empty, so once set it could be replaced
  but never cleared — which also kept that vendor inside the check-in flow
  forever. There is now an explicit "remove saved token" action.
- **Setting the token while adding a key works.** `POST /keys` ignored it and
  only a later `PUT` stored it, so the value silently vanished.
- **`access_token_user_id` is persisted.** The key endpoint accepted it but
  `_KEY_FIELDS` did not, so the `New-Api-User` header value was dropped.
- Check-in remains strictly manual: `GET /checkin-status` never claims, and the
  only thing that does is `POST /checkin`.

## free.ai

free.ai returns OpenAI-shaped payloads but does not serve OpenAI's paths, so it
was previously unusable: chat lives at `/v1/chat/` rather than
`/v1/chat/completions`, and `/v1/models` answers `{"models": […]}` instead of
`{"data": […]}`.

- **New `Free.ai` preset** (`https://api.free.ai/v1`). The probe, model scan and
  downstream proxy all consult a small host-matched wire profile, so the
  canonical paths are rewritten on the way out while clients keep speaking
  OpenAI.
- **Model discovery reads free.ai's own inventory shape.** Only routable types
  are kept (`chat`, `image`), self-hosted models sort first, and because
  `/v1/responses` and `/v1/messages` both answer 404 the endpoint candidate list
  is narrowed to chat completions.
- **Images are translated, not just forwarded.** free.ai answers
  `{job_id, url, image_url, share_url}`; the proxy reshapes that into OpenAI's
  `{created, data: [{url}]}` so existing clients work unchanged.
- `/v1/image/generate/` and `/v1/chat/` both require their trailing slash, which
  the probe and proxy preserve.
- Video, TTS and translate are deliberately **not** routed through the
  OpenAI-compatible endpoints — their contracts (job URLs, `text`/`target`
  fields) do not map. See [docs/freeai.md](freeai.md).

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

A regression suite (99 checks) covering access policy, push safety, the
notification engine, account adapters, end-to-end account switching, provider
presets, the balance-token lifecycle, free.ai's wire profile, and app smoke
tests — plus a template inline-script syntax check. `tests/conftest.py`
redirects `HOME` before any app module loads, so the suite cannot touch real
data. CI runs `compileall`, the suite, and the template check.

## Not in this release

Ten platform variants store their login encrypted (VS Code `safeStorage`,
Trae's icube cipher, ZCode's AES-256-GCM). Reading or writing those needs a
crypto layer, which is a separate decision — see
[docs/account-switching.md](account-switching.md). Automatic rotation on quota
exhaustion is designed but not implemented; it will be opt-in and off by
default.