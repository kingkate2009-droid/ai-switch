# AI Switch v2.4.0

## Highlights

- **OpenCode config generation**: click a key's more-menu → "Copy OpenCode Config" to get a ready-to-paste `opencode.jsonc` or `auth.json` snippet for that specific key/provider pair. Works with built-in providers (OpenAI, Anthropic, DeepSeek, etc.) and any custom provider.
- **New backends — WorkBuddy & ZCode**: push key configs to WorkBuddy (Tencent desktop app) and ZCode (智谱 ZCode) — both auto-detected on install.
- **Async reconcile for key edits**: promote, enable, disable, delete, batch operations — all return instantly instead of blocking the UI during backend sync.
- **Health check auto-enable**: when a health check passes for a disabled key, it is automatically re-enabled (no more manual enable after a successful check).
- **Key actions UI overhaul**: replaced text buttons with compact icon buttons + a "More" dropdown — less clutter, same functionality.
- **Bug fix**: `add_vendor` now correctly stores the `vtype` field on creation (was always `None`).
- **Bug fix**: async reconcile worker now correctly scopes its initial batch to the caller's vendor IDs (was always empty, causing a full sync on every call).

## Upgrade Notes

- No migration needed. Existing vendors keep their type.
- OpenCode config generation requires the key's API key to be visible in the UI; hidden keys show the snippet but without the API key in the provider block (auth section still has it).