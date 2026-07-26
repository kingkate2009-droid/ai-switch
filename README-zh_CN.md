<h1 align="center">AI Switch</h1>

<p align="center">
  <strong>统一 AI API Key 与后端管理平台</strong>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> •
  <a href="#功能特性">功能</a> •
  <a href="#支持的供应商">供应商</a> •
  <a href="#兼容的后端">后端</a> •
  <a href="#配置说明">配置</a> •
  <a href="#开源协议">协议</a>
</p>

---

统一管理 AI 供应商 API Key，自动同步到所有主流 AI 开发工具（OpenClaw、OpenCode、Claude Code、Codex CLI、Cline、Aider、Continue.dev、Hermes Agent、QwenCode、Kimi Code 等）。

## 功能特性

- **多供应商管理** — 供应商列表 + Key 表格布局，一目了然
- **26+ 内置供应商** — OpenAI、Anthropic、DeepSeek、Groq、Google Gemini、xAI、Together AI 等
- **供应商专属健康检测** — 每种供应商用正确的 API 格式探测
- **多后端自动同步** — 健康 Key 自动同步到所有支持的 AI 工具
- **启用/禁用** — 开关 Key 控制后端同步
- **批量导入** — 粘贴文本自动解析 URL + API Key + 供应商（Base64 自动解码）
- **配置文件编辑器** — 查看和编辑后端配置文件，自动备份
- **仪表盘视图** — 统计卡片、后端状态、快捷操作
- **浅色/暗色主题** — 一键切换，保存偏好
- **多语言** — English / 简体中文 / 繁體中文
- **适配器架构** — 添加新后端只需创建一个 Python 文件

## 快速开始

```bash
# 克隆
git clone https://github.com/kingkate2009-droid/ai-switch.git
cd ai-switch

# 安装依赖
pip install -r requirements.txt

# 启动
python3 run.py
# → 浏览器打开 http://127.0.0.1:8787
```

图文四步：[docs/quickstart.md](docs/quickstart.md) · Codex 排障：[docs/troubleshoot-codex.md](docs/troubleshoot-codex.md)

**要求**: Python 3.9+

**兼容**: OpenClaw 2026.6.11+、OpenCode、Claude Code、Codex CLI、Cline 3.x+、Aider、Continue.dev、Hermes Agent、QwenCode、Kimi Code、TRAE Work

### Docker

```bash
docker compose up -d
# → 浏览器打开 http://127.0.0.1:8787
```

## 使用方法

### 添加供应商

点击 **Vendors** → **+ Add Vendor** → 选择供应商（自动填入 URL）→ 输入名称 → 保存

### 添加 API Key

选中供应商 → 点击 **+ Add Key** → 输入名称和 Key → 保存

### 健康检测

- 单个检测：点击 Key 行中的 **Check**
- 全部检测：点击 Dashboard 上的 **Check All Health**
- 健康 Key → 自动同步到所有后端工具

### 批量导入

拖入或粘贴文本。支持多行、JSON 和 Base64 格式：

```
openai https://api.openai.com/v1 sk-proj-xxxx...
deepseek https://api.deepseek.com/v1 sk-xxxx...
```

自动识别 URL → 匹配供应商 → 预览 → 一键导入

## 支持的供应商

| 检测类型 | 供应商 |
|---|---|
| `openai_chat` | OpenAI、DeepSeek、OpenRouter、Groq、Together AI、xAI、Perplexity、Mistral、Cohere、Moonshot、Z.AI、MiniMax、阿里云 (Qwen)、火山引擎、Fireworks、StepFun、DeepInfra、Cerebras、Novita、Venice、01.AI、Ollama、千帆、Xiaomi |
| `anthropic` | Anthropic |
| `gemini` | Google Gemini |

未知供应商默认使用 `openai_chat` 探测。

## 兼容的后端

| 后端 | 配置文件 |
|---|---|
| OpenClaw | `openclaw.json`、`auth-profiles.json`、`models.json` |
| OpenCode | `auth.json`、`opencode.jsonc`、`tui.jsonc` |
| Claude Code | `settings.json`、`~/.claude.json`、`keybindings.json` |
| Codex CLI | `auth.json`、`config.toml` |
| Cline | `secrets.json`、`config.json`、`globalState.json`、`cline_mcp_settings.json` |
| Aider | `.aider.conf.yml`、`.aider.model.settings.yml`、`.aider.model.metadata.json` |
| Continue.dev | `config.json`、`config.yaml`、`.continuerc.json` |
| Hermes Agent | `config.yaml`、`.env` |
| QwenCode | `settings.json`、`.env` |
| Kimi Code | `~/.kimi-code/config.toml`、`tui.toml` |
| TRAE Work | `~/.trae-work/ai-switch-models.json`；Windows `%APPDATA%\\ai-switch\\trae-work\\`；TRAE 应用数据目录 |



## 安装包（Windows / macOS / Linux）

预编译安装包发布在 [GitHub Releases](https://github.com/kingkate2009-droid/ai-switch/releases)。

| 平台 | 文件名模式 |
|---|---|
| Windows x64 | `ai-switch-<ver>-windows-amd64.zip` |
| macOS Intel | `ai-switch-<ver>-macos-amd64.tar.gz` |
| macOS Apple Silicon | `ai-switch-<ver>-macos-arm64.tar.gz` |
| Linux x64 | `ai-switch-<ver>-linux-amd64.tar.gz` |

解压后运行 `ai-switch`（或 `start.sh` / `start.bat`），浏览器打开 http://127.0.0.1:8787

### 本地打包

```bash
bash scripts/build_package.sh
# 产物：dist/packages/
```

### 发版（自动打多平台包）

```bash
./release.sh v1.3.0
```

## 配置说明

| 项目 | 路径 |
|---|---|
| 管理器数据 | `~/.ai-switch/data.json`（自动从 `~/.openclaw-auto-manager` 迁移） |
| 健康缓存 | `~/.ai-switch/health_cache.json` |
| 端口 | `8787`（环境变量：`AI_SWITCH_PORT`） |

## 安全说明

> **API Key 存储在 `~/.ai-switch/data.json` 中。**
> 此文件不在项目目录内。
> 请勿将其提交到版本控制。

## 技术栈

- **后端**: Python + Flask
- **前端**: 原生 JS + CSS Variables 主题
- **存储**: JSON 文件（无需数据库）
- **国际化**: 客户端语言切换
- **架构**: 可插拔适配器模式

## 开发路线

- [ ] Key 模糊搜索
- [ ] 批量操作（多选启用/禁用/删除）
- [ ] Webhook 告警
- [ ] Key 使用统计
- [ ] 导出/导入配置
- [ ] PWA 支持
- [ ] 更多后端适配器

## 友链

- [GitHub](https://github.com/kingkate2009-droid/ai-switch)
- [Linux.do](https://linux.do/) — 社区讨论

## 开源协议

Apache 2.0
