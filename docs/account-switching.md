# Account Switching (换号器)

> Status: **framework + first adapters landed**. See the table at the bottom for
> per-platform coverage.

## Why

AI Switch already had an *accounts* store (`core/accounts.py`): it could
**import** OAuth logins (Claude Code, Codex, Gemini, Grok), run browser OAuth,
and query official quotas. What it could **not** do was the thing people
actually want — *switch* which account a local tool is logged in as.

Everything under `backends/` writes **API keys** into tool configs. Accounts are
a different axis: they are *login identities*, and putting one into a tool means
writing the tool's own credential store (a Keychain item, `auth.json`, or a VS
Code `state.vscdb`).

This feature adds that missing half.

## Architecture

```
core/account_adapters/
  __init__.py     registry: providers() / get() / norm() / switch() / read_active()
  base.py         OS paths, atomic JSON writes + backups, StateVscdb, Keychain
  claude.py       ~/.claude/.credentials.json  |  macOS Keychain
  codex.py        ~/.codex/auth.json
  gemini.py       ~/.gemini/oauth_creds.json
  grok.py         ~/.grok/auth.json
  …               one module per supported tool
```

The registry **auto-discovers** every module in the package, so adding a tool is
"drop in a file" — there is no central list to keep in sync. `core/accounts.py`
sources `PROVIDERS` / `PROVIDER_LABELS` from the registry, and the UI builds its
type tabs and provider dropdowns from the same source.

### The adapter contract

```python
class AccountAdapter:
    provider: str                 # canonical id, e.g. "kiro"
    label: str                    # display name
    aliases: tuple[str, ...]      # "claude-code", "anthropic", ...
    supports_import / _switch / _oauth / _quota: bool

    def credential_path(self) -> Path | None   # primary on-disk location
    def detect(self) -> bool                   # tool present on this machine?
    def read(self) -> dict | None              # parse the *current* login
    def switch(self, account) -> dict          # write an account in (换号)
```

Rules the adapters follow:

- **Probes never raise.** `detect()` / `read()` return `False` / `None` on any
  problem, so one broken tool can never take down the accounts page.
- **`switch()` raises `SwitchError`** with a human-readable reason so the API can
  return a useful 400.
- **Never destroy data.** Every write goes through `atomic_write_json` or
  `StateVscdb.write`, both of which drop a `.ai-switch.bak` copy first.

### API

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/accounts` | List accounts; `providers[]` now carries `supports_switch` / `installed` / `credential_path` |
| `POST` | `/api/accounts/<id>/activate` | **Switch** this account into its tool |
| `POST` | `/api/accounts/detect` | Probe tools for their *current* login (explicit action) |

`activate` is a mutation, so read-only mode blocks it (403) like every other
write. That is intentional: it edits files outside AI Switch.

### UI

The Accounts page gains a **切换 (Switch to)** button per row, a **检测本机登录**
button, and type tabs rendered from the registry. Switching asks for
confirmation, reports the target path, and never happens on page load.

## Safety notes

- **Backups.** First write to a file/DB leaves `<name>.ai-switch.bak`.
- **Running apps.** Writing a `state.vscdb` while the IDE is running can be
  overwritten by the IDE or lose the write; prefer switching while the tool is
  closed. (The SQLite writes use a 5 s timeout and an upsert, so they do not
  hang or duplicate rows.)
- **macOS Keychain.** Claude Code keeps its credential in the login Keychain
  (`Claude Code-credentials`). Reading it can show an access prompt, which is
  why the bulk `detect` only probes tools that are actually installed.
- **Keychain fallback.** If a Keychain write fails, `switch` raises instead of
  silently leaving the on-disk copy out of sync.

## Rotation: manual now, optional auto later

Two distinct things, deliberately kept separate:

1. **Manual switch (shipped).** The user picks an account and clicks *Switch to*.
   Predictable, one explicit action, no surprises.
2. **Auto-rotation (planned, default OFF).** When an account hits its quota,
   move to the next one. This is what tools like
   [`jlcodes99/cockpit-tools`](https://github.com/jlcodes99/cockpit-tools) and
   [`decolua/9router`](https://github.com/decolua/9router) do.

Design constraints for (2) when it lands:

- Opt-in per provider, **default off**, with a clear warning.
- Driven by the quota snapshots we already collect (`core/account_quota.py`) and
  the notification engine (`core/notify.py`) so a switch is observable.
- A cool-down and a "max switches per hour" cap, so a broken quota reading
  cannot cause a switch storm.
- Never rotate silently in a way that hides *why* a tool changed identity.

> Note on terms of service: managing several accounts you own is normal;
> **rotating accounts specifically to work around a provider's per-account
> quota** is prohibited by most providers' ToS. That is why auto-rotation is
> opt-in and off by default.

### Relationship to 9router

[9router](https://github.com/decolua/9router) is a *local router*: it points
every CLI at `localhost:20128/v1`, then does quota tracking, OAuth token
refresh, per-provider multi-account round-robin and subscription → cheap → free
fallback. That is the same niche as our `core/downstream.py` relay, plus
rotation. When auto-rotation lands it should reuse our existing downstream
routing rather than re-implement a second gateway.

## Platform coverage

Credential locations were derived from the tools' own on-disk formats and
cross-checked against `cockpit-tools` — see
[docs/account-platform-credentials.md](account-platform-credentials.md) for the
full per-platform spec (exact paths, SQL keys, OAuth endpoints, quota URLs).

### Shipped — no extra dependency

| Platform | Credential store | Import | Switch |
|---|---|---|---|
| Claude Code | macOS Keychain `Claude Code-credentials` / `~/.claude/.credentials.json` | ✅ | ✅ |
| Codex / ChatGPT | `~/.codex/auth.json` (`tokens`) | ✅ | ✅ |
| Gemini CLI | `~/.gemini/oauth_creds.json` | ✅ | ✅ |
| Grok CLI | `~/.grok/auth.json` (OIDC registry) | ✅ | ✅ |
| Kiro | `~/.aws/sso/cache/kiro-auth-token.json` + `…/Kiro/…/kiro.kiroagent/profile.json` | ✅ | ✅ |
| Cursor | `…/Cursor/User/globalStorage/state.vscdb` — **plain-text** `cursorAuth/*` keys | ✅ | ✅ |
| Antigravity IDE | Keychain `service=gemini, account=antigravity` + `~/.gemini/jetski-standalone-oauth-token` | ✅ | ✅ |
| Zed | macOS Keychain internet-password `https://zed.dev` | ✅ | ✅ |

### Remaining — needs a crypto layer

These store their login **encrypted**, so switching them requires reproducing
the host app's encryption. That is a real dependency decision, not a small
patch:

| Platform | Credential store | Encryption |
|---|---|---|
| Windsurf | `…/Windsurf/User/globalStorage/state.vscdb` | VS Code `secret://` → Electron `safeStorage` |
| GitHub Copilot | VS Code `state.vscdb` | VS Code `secret://` → `safeStorage` |
| Qoder | `…/Qoder/User/globalStorage/state.vscdb` | `secret://aicoding.auth.*` → `safeStorage` |
| CodeBuddy / CN | `…/CodeBuddy[ CN]/User/globalStorage/state.vscdb` | `secret://…planning-genie.new.accessToken` → `safeStorage` |
| Trae / SOLO / CN / SOLO CN | `…/<name>/User/globalStorage/storage.json` | `byte_crypto_encrypt_v1` ("icube cipher") |
| ZCode | `~/.zcode/v2/credentials.json` | AES-256-GCM (`enc:v1:` values) |

The Electron scheme is Chromium's `os_crypt`:

- **macOS** — Keychain password for `<App> Safe Storage`, then
  PBKDF2-HMAC-SHA1(1 iteration, salt `saltysalt`) → AES-128-CBC, `v10` prefix.
- **Windows** — `os_crypt.encrypted_key` in `Local State` → DPAPI → AES-256-GCM.
- **Linux** — Secret Service (`v11` + AES-128-CBC) or the fixed `v10` key.

Planned approach: one `os_crypt.py` module that uses the `cryptography` package
**when it is installed**, and makes those adapters report
"crypto layer unavailable" instead of failing obscurely when it is not. That
keeps AI Switch installable without the dependency while still reaching parity
for users who want it.
