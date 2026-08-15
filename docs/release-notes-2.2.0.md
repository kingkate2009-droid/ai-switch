# AI Switch v2.2.0

## Highlights

- Added per-model endpoint detection for OpenAI Chat, OpenAI Responses, Anthropic Messages and Gemini, with automatic or manual endpoint selection.
- Added vendor-grouped quality checks. Run the four-prompt benchmark for a whole vendor or expand it to test one model.
- Backend synchronization now includes only recently verified models and consistently applies primary/backup key selection.
- Improved OpenClaw and OpenCode mixed-protocol generation, OpenCode atomic writes/live apply, and Codex atomic configuration updates.
- Migrated primary state to transactional SQLite at `~/.ai-switch/ai-switch.db`; existing JSON data is imported once and retained as `.legacy` rollback files.
- Expanded non-blocking task reporting for health checks, imports, endpoint detection, quality checks and backend synchronization.
- Added npm/npx launch support.

## Upgrade Notes

- The first launch migrates `~/.ai-switch/data.json` automatically. Keep the generated `.legacy` files until the upgraded installation has been verified.
- Models need a recent successful health and endpoint check before they are synchronized to backends.
- A quality check sends four short API requests per model and may consume provider quota.
- If the configured port is already occupied, AI Switch now exits with an explicit error instead of selecting another port.
