<p align="center">
  <a href="README-zh_CN.md">简体中文</a> •
  <a href="README-zh_TW.md">繁體中文</a> •
  <a href="README.md">English</a>
</p>

<h1 align="center">AI Switch</h1>

<p align="center">
  <strong>Local AI API Key hub — change once, every tool works</strong>
</p>

<p align="center">
  <a href="https://github.com/kingkate2009-droid/ai-switch/releases"><img alt="release" src="https://img.shields.io/github/v/release/kingkate2009-droid/ai-switch?include_prereleases"></a>
  <a href="https://github.com/kingkate2009-droid/ai-switch/stargazers"><img alt="stars" src="https://img.shields.io/github/stars/kingkate2009-droid/ai-switch?style=social"></a>
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-Apache%202.0-blue.svg"></a>
  <img alt="python" src="https://img.shields.io/badge/python-3.9%2B-blue">
</p>

<p align="center">
  <a href="#why">Why</a> ·
  <a href="#quick-start">Quick Start</a> ·
  <a href="#what-it-does">What it does</a> ·
  <a href="#docs">Docs</a> ·
  <a href="#supported-backends">Backends</a> ·
  <a href="#license">License</a>
</p>

---

## Why

If you juggle **relay / multi-vendor API keys** across **OpenClaw, OpenCode, Codex CLI, Claude Code**… you already know the pain:

| Pain | Without AI Switch | With AI Switch |
|------|-------------------|----------------|
| Rotate one key | Edit 5 config files by hand | Change once → push to installed tools |
| Bad key | Tool hangs / weird 401s | Health check → strip from backends |
| Wrong write | Silent overwrite | **Sync preview** · uninstalled = no write |
| Onboarding | “Where do I put the key?” | **3-minute path**: add → check → push → test |

**Positioning:** a local **Key hub** (manage · probe · push) — not another chat client.

> One key change. Every installed tool updates. Broken keys don’t take your stack down.

---

## Quick Start

**Easiest:** download a build from [Releases](https://github.com/kingkate2009-droid/ai-switch/releases)  
(Windows / macOS Intel / Apple Silicon / Linux) → extract → run `ai-switch` / `start.bat` / `start.sh`  
→ open **http://127.0.0.1:8787**

**From source:**

```bash
git clone https://github.com/kingkate2009-droid/ai-switch.git
cd ai-switch
pip install -r requirements.txt
python3 run.py
# → http://127.0.0.1:8787
```

**Docker:**

```bash
docker compose up -d
# → http://127.0.0.1:8787
```

Requires **Python 3.9+** for source installs.

**3-minute loop:** [docs/quickstart.md](docs/quickstart.md)

1. Add vendor + key (or smart import)  
2. Health check passes  
3. Push to backends (**preview first**)  
4. Send `hi` in OpenCode / Codex / Claude Code  

---

## What it does

Only three outcomes matter day-to-day:

1. **One place for keys** — vendors, tags, batch import, MetaAPI merge, dedupe  
2. **Know what’s alive** — layered probes (Chat + Codex Responses), readable errors, adaptive interval  
3. **Safe sync** — preview · last-sync summary · never write to uninstalled tools · active provider switch  

Also included when you need them: check-in URLs, budgets, encrypted profile export, diagnostics pack, downstream routes, desktop packages.

<details>
<summary><strong>Feature list (expand)</strong></summary>

- Multi-vendor + key table, search / tags / batch enable-disable-delete  
- 26+ built-in providers + custom OpenAI-compatible endpoints  
- Health monitor (scheduled), auto-disable / failover (optional)  
- Sync to OpenClaw, OpenCode, Claude Code, Codex, Cline, Aider, Continue, …  
- Batch / backup / MetaAPI import with undo  
- Light/dark theme, EN / 简体 / 繁中  
- Drop-in backend adapters ([contribution guide](docs/adapter-contribution.md))

</details>

---

## Docs

| Doc | For |
|-----|-----|
| [3-minute quickstart](docs/quickstart.md) | First run |
| [Codex won’t connect](docs/troubleshoot-codex.md) | Responses / env / models |
| [Backends](docs/backends.md) | What each engine supports |
| [Adapter contribution](docs/adapter-contribution.md) | Add a backend |

**Issues / help:** [open an issue](https://github.com/kingkate2009-droid/ai-switch/issues)

- [#1 Install & start](https://github.com/kingkate2009-droid/ai-switch/issues/1)
- [#2 Codex connectivity](https://github.com/kingkate2009-droid/ai-switch/issues/2)
- [#3 Import & merge](https://github.com/kingkate2009-droid/ai-switch/issues/3)

---

## Supported backends

| Backend | Notes |
|---------|--------|
| OpenClaw | `openclaw.json`, auth profiles, models |
| OpenCode | `auth.json`, `opencode.jsonc` |
| Claude Code | `settings.json`, `~/.claude.json` |
| Codex CLI | `auth.json`, `config.toml` + Responses health |
| Cline / Aider / Continue.dev | IDE / CLI configs |
| Hermes Agent, QwenCode, Kimi Code, TRAE Work | See [backends.md](docs/backends.md) |

Status on the Backends page: **Not installed** · **Stopped (syncable)** · **Running** — uninstalled tools are never written.

### Providers (probe types)

| Type | Examples |
|------|----------|
| `openai_chat` | OpenAI, DeepSeek, OpenRouter, Groq, Moonshot, Qwen, … |
| `anthropic` | Anthropic-compatible |
| `gemini` | Google Gemini |
| `openai_responses` | Codex-oriented `/v1/responses` path |

---

## Desktop packages

| Platform | Asset pattern on Releases |
|----------|---------------------------|
| Windows x64 | `ai-switch-<ver>-windows-amd64.zip` |
| macOS Intel | `ai-switch-<ver>-macos-amd64.tar.gz` |
| macOS Apple Silicon | `ai-switch-<ver>-macos-arm64.tar.gz` |
| Linux x64 | `ai-switch-<ver>-linux-amd64.tar.gz` |

```bash
# Local package
bash scripts/build_package.sh
# Multi-platform release via CI
./release.sh v2.0.4
```

---

## Configuration & security

| Item | Path |
|------|------|
| Data | `~/.ai-switch/data.json` |
| Usage | `~/.ai-switch/usage.json` |
| Port | `8787` (`AI_SWITCH_PORT`) |

> **Keys live only under `~/.ai-switch/`.** Never commit that directory. Optional access token in Settings for LAN exposure.

**Stack:** Python + Flask · vanilla JS · JSON storage · pluggable adapters · Apache 2.0

---

## Links

- [Releases](https://github.com/kingkate2009-droid/ai-switch/releases)  
- [Issues](https://github.com/kingkate2009-droid/ai-switch/issues)  
- [Linux.do](https://linux.do/) — community discussion  

## License

Apache 2.0
