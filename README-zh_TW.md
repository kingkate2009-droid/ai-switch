<h1 align="center">AI Switch</h1>

<p align="center">
  <strong>統一 AI API Key 與後端管理平台</strong>
</p>

<p align="center">
  <a href="#快速開始">快速開始</a> •
  <a href="#功能特性">功能</a> •
  <a href="#支援的供應商">供應商</a> •
  <a href="#相容的後端">後端</a> •
  <a href="#設定說明">設定</a> •
  <a href="#開源協議">協議</a>
</p>

---

統一管理 AI 供應商 API Key，自動同步到所有主流 AI 開發工具（OpenClaw、OpenCode、Claude Code、Codex CLI、Cline、Aider、Continue.dev、Hermes Agent、QwenCode 等）。

## 功能特性

- **多供應商管理** — 供應商列表 + Key 表格佈局，一目瞭然
- **26+ 內建供應商** — OpenAI、Anthropic、DeepSeek、Groq、Google Gemini、xAI、Together AI 等
- **供應商專屬健康偵測** — 每種供應商用正確的 API 格式探測
- **多後端自動同步** — 健康 Key 自動同步到所有支援的 AI 工具
- **啟用/停用** — 切換 Key 控制後端同步
- **批次匯入** — 貼上文字自動解析 URL + API Key + 供應商（Base64 自動解碼）
- **設定檔編輯器** — 檢視和編輯後端設定檔，自動備份
- **儀表板檢視** — 統計卡片、後端狀態、快捷操作
- **淺色/暗色主題** — 一鍵切換，儲存偏好
- **多語言** — English / 簡體中文 / 繁體中文
- **配接器架構** — 新增後端只需建立一個 Python 檔案

## 快速開始

```bash
# 複製
git clone https://github.com/kingkate2009-droid/ai-switch.git
cd ai-switch

# 安裝相依套件
pip install -r requirements.txt

# 啟動
python3 run.py
# → 瀏覽器開啟 http://127.0.0.1:8787
```

**要求**: Python 3.9+

**相容**: OpenClaw 2026.6.11+、OpenCode、Claude Code、Codex CLI、Cline 3.x+、Aider、Continue.dev、Hermes Agent、QwenCode

### Docker

```bash
docker compose up -d
# → 瀏覽器開啟 http://127.0.0.1:8787
```

## 使用方法

### 新增供應商

點選 **Vendors** → **+ Add Vendor** → 選擇供應商（自動填入 URL）→ 輸入名稱 → 儲存

### 新增 API Key

選中供應商 → 點選 **+ Add Key** → 輸入名稱和 Key → 儲存

### 健康偵測

- 單一偵測：點選 Key 行中的 **Check**
- 全部偵測：點選 Dashboard 上的 **Check All Health**
- 健康 Key → 自動同步到所有後端工具

### 批次匯入

拖入或貼上文字。支援多行、JSON 和 Base64 格式：

```
openai https://api.openai.com/v1 sk-proj-xxxx...
deepseek https://api.deepseek.com/v1 sk-xxxx...
```

自動辨識 URL → 匹配供應商 → 預覽 → 一鍵匯入

## 支援的供應商

| 偵測類型 | 供應商 |
|---|---|
| `openai_chat` | OpenAI、DeepSeek、OpenRouter、Groq、Together AI、xAI、Perplexity、Mistral、Cohere、Moonshot、Z.AI、MiniMax、阿里雲 (Qwen)、火山引擎、Fireworks、StepFun、DeepInfra、Cerebras、Novita、Venice、01.AI、Ollama、千帆、Xiaomi |
| `anthropic` | Anthropic |
| `gemini` | Google Gemini |

未知供應商預設使用 `openai_chat` 探測。

## 相容的後端

| 後端 | 設定檔 |
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

## 設定說明

| 項目 | 路徑 |
|---|---|
| 管理器資料 | `~/.ai-switch/data.json`（自動從 `~/.openclaw-auto-manager` 遷移） |
| 健康快取 | `~/.ai-switch/health_cache.json` |
| 連接埠 | `8787`（環境變數：`AI_SWITCH_PORT`） |

## 安全說明

> **API Key 儲存在 `~/.ai-switch/data.json` 中。**
> 此檔案不在專案目錄內。
> 請勿將其提交到版本控制。

## 技術棧

- **後端**: Python + Flask
- **前端**: 原生 JS + CSS Variables 主題
- **儲存**: JSON 檔案（無需資料庫）
- **國際化**: 用戶端語言切換
- **架構**: 可插拔配接器模式

## 開發路線

- [ ] Key 模糊搜尋
- [ ] 批次操作（多選啟用/停用/刪除）
- [ ] Webhook 告警
- [ ] Key 使用統計
- [ ] 匯出/匯入設定
- [ ] PWA 支援
- [ ] 更多後端配接器

## 開源協議

Apache 2.0
