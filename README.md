<p align="center">
  <a href="README-zh_CN.md">简体中文</a> •
  <a href="README-zh_TW.md">繁體中文</a> •
  <a href="README.md">English</a>
</p>

<h1 align="center">AI Switch</h1>

<p align="center">
  <strong>Unified AI API Key &amp; Backend Management</strong>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#features">Features</a> •
  <a href="#supported-providers">Providers</a> •
  <a href="#supported-backends">Backends</a> •
  <a href="#configuration">Config</a> •
  <a href="#license">License</a>
</p>

---

A unified management platform for AI provider API keys and development tool backends. Add, detect, and sync your API keys across OpenClaw, OpenCode, Claude Code, Codex CLI, Cline, Aider, Continue.dev, Hermes Agent, QwenCode — all from one place.

## Features

- **Multi-vendor Management** — Vendor list + key table layout
- **26+ Built-in Providers** — OpenAI, Anthropic, DeepSeek, Groq, Google Gemini, xAI, Together AI, and more
- **Provider-specific Health Checks** — Each provider uses correct API format for probing
- **Multi-backend Auto-Sync** — Healthy keys auto-sync to all supported AI tools
- **Enable/Disable Keys** — Toggle keys to control backend sync
- **Batch Import** — Paste text to auto-parse URL + API Key + provider (Base64 auto-decode)
- **Config File Editor** — View and edit backend configuration files with backup
- **Dashboard View** — Stats cards, backend status, quick actions
- **Light/Dark Theme** — One-click toggle, saves preference
- **Multi-language** — English / 简体中文 / 繁體中文
- **Adapter Architecture** — Add new backends by dropping in one Python file

## Quick Start

```bash
# Clone
git clone https://github.com/kingkate2009-droid/ai-switch.git
cd ai-switch

# Install dependencies
pip install -r requirements.txt

# Start
python3 run.py
# → Open http://127.0.0.1:8787
```

**Requirements**: Python 3.9+

**Compatible with**: OpenClaw 2026.6.11+, OpenCode, Claude Code, Codex CLI, Cline 3.x+, Aider, Continue.dev, Hermes Agent, QwenCode

### Docker

```bash
docker compose up -d
# → Open http://127.0.0.1:8787
```

## Usage

### Add a Provider

Click **Vendors** → **+ Add Vendor** → Select provider (auto-fills URL) → Enter name → Save

### Add an API Key

Select vendor → Click **+ Add Key** → Enter name and key → Save

### Health Check

- Single check: Click **Check** on the key row
- Check all: Click **Check All Health** on Dashboard
- Healthy key → auto-sync to all backend tools

### Batch Import

Drag or paste key text. Supports multi-line, JSON, and Base64 formats:

```
openai https://api.openai.com/v1 sk-proj-xxxx...
deepseek https://api.deepseek.com/v1 sk-xxxx...
```

Auto-detects URL → matches provider → preview → one-click import

## Supported Providers

| Check Type | Providers |
|---|---|
| `openai_chat` | OpenAI, DeepSeek, OpenRouter, Groq, Together AI, xAI, Perplexity, Mistral, Cohere, Moonshot, Z.AI, MiniMax, Alibaba (Qwen), Volcengine, Fireworks, StepFun, DeepInfra, Cerebras, Novita, Venice, 01.AI, Ollama, Qianfan, Xiaomi |
| `anthropic` | Anthropic |
| `gemini` | Google Gemini |

Unknown providers default to `openai_chat` probe.

## Supported Backends

| Backend | Config Files |
|---|---|
| OpenClaw | `openclaw.json`, `auth-profiles.json`, `models.json` |
| OpenCode | `auth.json`, `opencode.jsonc`, `tui.jsonc` |
| Claude Code | `settings.json`, `~/.claude.json`, `keybindings.json` |
| Codex CLI | `auth.json`, `config.toml` |
| Cline | `secrets.json`, `config.json`, `globalState.json`, `cline_mcp_settings.json` |
| Aider | `.aider.conf.yml`, `.aider.model.settings.yml`, `.aider.model.metadata.json` |
| Continue.dev | `config.json`, `config.yaml`, `.continuerc.json` |
| Hermes Agent | `config.yaml`, `.env` |
| QwenCode | `settings.json`, `.env` |



## Desktop Packages (Windows / macOS / Linux)

Prebuilt binaries are published on [GitHub Releases](https://github.com/kingkate2009-droid/ai-switch/releases).

### Download

| Platform | Asset name pattern |
|---|---|
| Windows x64 | `ai-switch-<ver>-windows-amd64.zip` |
| macOS Intel | `ai-switch-<ver>-macos-amd64.tar.gz` |
| macOS Apple Silicon | `ai-switch-<ver>-macos-arm64.tar.gz` |
| Linux x64 | `ai-switch-<ver>-linux-amd64.tar.gz` |

Extract, then run `ai-switch` (or `start.sh` / `start.bat`). UI opens at http://127.0.0.1:8787

### Build locally

```bash
# Current platform
bash scripts/build_package.sh
# Output: dist/packages/ai-switch-*-*.tar.gz (or .zip on Windows)
```

### Release (CI builds all platforms)

```bash
./release.sh v1.3.0
# GitHub Actions builds Win/macOS/Linux and attaches packages to the release
```

## Configuration

| Item | Path |
|---|---|
| Manager data | `~/.ai-switch/data.json` (auto-migrated from `~/.openclaw-auto-manager`) |
| Health cache | `~/.ai-switch/health_cache.json` |
| Port | `8787` (env: `AI_SWITCH_PORT`) |

## Security

> **API Keys are stored in `~/.ai-switch/data.json`.**
> This file is NOT inside the project directory.
> Never commit it to version control.

## Tech Stack

- **Backend**: Python + Flask
- **Frontend**: Vanilla JS + CSS Variables theming
- **Storage**: JSON file (no database needed)
- **i18n**: Client-side locale switching
- **Architecture**: Pluggable adapter pattern

## Roadmap

- [ ] Key search/filter
- [ ] Batch operations (multi-select enable/disable/delete)
- [ ] Webhook alerts on key failure
- [ ] Key usage statistics
- [ ] Export/import config
- [ ] PWA support
- [ ] Additional backend adapters

## License

Apache 2.0
