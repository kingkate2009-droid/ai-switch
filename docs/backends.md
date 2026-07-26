# 后端引擎差异说明 / Backend Engine Differences

本文说明 AI Switch 各后端引擎适配器的配置路径、同步行为与能力差异。  
English summary tables follow the Chinese sections.

相关：[快速入门](./quickstart.md) · [Codex 排障](./troubleshoot-codex.md) · [适配器贡献](./adapter-contribution.md)

---

## 总览

| 引擎 | 适配器 | BYOK | 多供应商并存 | 自定义 Base URL | 配置格式 | 活跃供应商切换 |
|------|--------|------|--------------|-----------------|----------|----------------|
| OpenClaw | `openclaw` | ✅ | ✅ | ✅ | JSON | 网关内模型选择 |
| OpenCode | `opencode` | ✅ | ✅ | ✅ | JSONC + auth.json | 工具内选择 |
| Claude Code | `claude-code` | ✅ | ❌ 单 Key | ✅ `ANTHROPIC_BASE_URL` | JSON env | 最后写入覆盖 |
| **Codex CLI** | `codex-cli` | ✅ | ✅ `model_providers` | ✅ | TOML + auth.json | **一键切换** |
| Cline | `cline` | ✅ | 有限 (openai/anthropic) | ✅ | JSON | 最后写入覆盖 |
| Aider | `aider` | ✅ | ✅ 多 `api-key` | 部分 | YAML | 工具内选择 |
| Continue.dev | `continue` | ✅ | ✅ models 列表 | ✅ | JSON/YAML | 工具内选择 |
| Hermes Agent | `hermes` | ✅ | ✅ 多环境变量 | 部分 | YAML + .env | 环境变量 |
| QwenCode | `qwencode` | ✅ | ✅ modelProviders | ✅ | JSON + .env | 工具内选择 |
| Kimi Code | `kimi-code` | ✅ | ✅ providers 表 | ✅ | TOML | default_model |
| Goose | `goose` | ✅ | ✅ 多 env | 部分 | YAML secrets | 环境变量 |
| Grok CLI | `grok-cli` | ✅ | 多 Key / 单 active provider | ✅ custom | .env | `GROKCLI_PROVIDER` |
| Copilot CLI | `copilot-cli` | ✅ 有限 | ❌ 单 BYOK | ✅ | JSON byok | 覆盖 |
| Devin CLI | `devin` | ✅ 专用 | 仅 Devin 系 | — | JSON + credentials.toml | 专用 token |
| Antigravity | `antigravity` | ✅ | 仅 Google | ✅ | JSON | 覆盖 |
| Cursor CLI | `cursor-cli` | ❌ 只读 | — | — | JSON | 账号体系 |
| **TRAE Work** | `trae-work` | ✅ 自定义模型清单 | ✅ 多模型条目 | ✅ baseUrl | JSON（托管文件） | TRAE Settings > Models |

### 安装形态：CLI / 客户端 / 插件

后端产品可能以多种形式安装，检测统一走 `detect_install()`（`backends/base.py`）：

| 形态 | `install_kinds` | 判定依据 | 典型引擎 |
|------|-----------------|----------|----------|
| **CLI** | `cli` | PATH 上可执行文件（`cli_available`） | OpenCode、Aider、Goose、Hermes、OpenClaw |
| **桌面客户端** | `app` | 安装目录 / `.app` / `Program Files` / 进程 | Cursor、TRAE Work、OpenClaw gateway |
| **IDE 插件** | `extension` | VS Code / Cursor 扩展目录 | Cline、Continue、Codex（ChatGPT 扩展） |
| **配置痕迹** | `config` | 仅配置文件存在（弱证据，按需开启） | 部分 CLI 仅有 `~/.tool` 配置 |

状态字段：

- `installed` / `running` / `version` / `message`
- `install_kinds`: 如 `["cli","extension"]`
- UI 徽章会附加形态标签，例如 `运行中 · CLI+Ext`

规则：

1. **任一形态命中即视为已安装**（可同步）
2. **未安装一律不同步**
3. 插件形态的「运行中」≈ 宿主 IDE 进程在跑
4. CLI 形态的「运行中」≈ CLI/守护进程在跑（如 OpenClaw gateway、Codex app-server）
5. 管理器自己写入的配置目录 **不能单独** 作为插件已安装证据（Cline / Continue）

### 跨平台路径约定（macOS / Windows / Linux）

通用助手在 `backends/base.py`：

| 助手 | macOS / Linux | Windows |
|------|---------------|---------|
| `home_dot_dir("x")` | `~/.x` | `%USERPROFILE%\.x` |
| `home_config_dir("x")` | `~/.config/x` 或 `$XDG_CONFIG_HOME/x` | `%APPDATA%\x` |
| `home_data_dir("x")` | `~/.local/share/x` 或 `$XDG_DATA_HOME/x` | `%LOCALAPPDATA%\x` |
| `process_running` | `ps` | `tasklist` / PowerShell |
| `vscode_extension_roots` | `~/.vscode/extensions` 等 | `%USERPROFILE%\.vscode\extensions`、`%APPDATA%\Code\User\extensions` 等 |

主要引擎配置根目录：

| 引擎 | macOS/Linux | Windows |
|------|-------------|---------|
| OpenClaw | `~/.openclaw` | `%USERPROFILE%\.openclaw` |
| OpenCode auth | `~/.local/share/opencode` | `%LOCALAPPDATA%\opencode` |
| OpenCode config | `~/.config/opencode` | `%APPDATA%\opencode` |
| Codex | `~/.codex`（或 `$CODEX_HOME`） | `%USERPROFILE%\.codex` |
| Claude Code | `~/.claude` | `%USERPROFILE%\.claude` |
| Goose | `~/.config/goose` | `%APPDATA%\goose` |
| Devin | `~/.config/devin` + data dir | `%APPDATA%\devin` / `%LOCALAPPDATA%\devin` |
| TRAE Work | 见上文 TRAE 节 | 见上文 TRAE 节 |

---

## 各引擎详解

### OpenClaw (`backends/openclaw.py`)

- **目录**: `~/.openclaw/`
- **文件**: `openclaw.json`、`agents/main/agent/auth-profiles.json`、`models.json`
- **差异**:
  - 最完整的多供应商/多模型同步，写入 OpenClaw 网关认可的 provider 结构
  - 会过滤网关不兼容模型（如部分 xAI multi-agent）
  - 支持从 OpenClaw 反向导入用量与配置
  - 版本有最低/推荐要求（≥ 2026.3.0 / 推荐 2026.6.11+）

### OpenCode (`backends/opencode.py`)

- **Auth**: `~/.local/share/opencode/auth.json` → `{ providerId: { type: "api", key } }`
- **Config**: `~/.config/opencode/opencode.jsonc`（及 `.json` 镜像）
- **差异**:
  - 内置供应商（models.dev）仅需 auth；自定义/代理需写 `provider.<id>`（npm、baseURL、models）
  - 自定义 endpoint 会按 `endpoint_type` 选择 `@ai-sdk/openai-compatible` / anthropic / google
  - 用 `_managed: ai-switch` 标记托管项，便于 reconcile 清理

### Claude Code (`backends/claude-code.py`)

- **文件**: `~/.claude/settings.json`（`env.ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`）
- **差异**:
  - **仅 Anthropic 兼容**供应商会同步
  - 单活跃 Key：后写入覆盖
  - 另暴露 `~/.claude.json`、keybindings 供编辑（不同步 Key）

### Codex CLI (`backends/codex-cli.py`)

- **目录**: `$CODEX_HOME` 或 `~/.codex/`
- **文件**: `auth.json`（`OPENAI_API_KEY`）、`config.toml`（`model_provider`、`model`、`[model_providers.*]`）
- **差异**（相对其他引擎最关键）:
  - 官方支持多 `model_providers` 与 `model_provider` 指针
  - 自定义供应商 **不能** 占用保留 ID：`openai` / `ollama` / `lmstudio`
  - 简单改官方 OpenAI 地址可用顶层 `openai_base_url`，不必新建 provider
  - AI Switch 会：
    1. 将健康 Key 同步为 `[model_providers.aiswitch-<id>]`（base_url、wire_api、name）
    2. 把当前活跃 Key 写入 `auth.json` 的 `OPENAI_API_KEY`
    3. 提供 **一键切换活跃供应商**（改 `model_provider` + auth + 可选 `model`）
  - `wire_api`：默认 `chat`；官方 OpenAI / 显式 responses 端点用 `responses`
  - 配置优先级：CLI 标志 > 项目 `.codex/` > profile 文件 > 用户 `config.toml`

### Cline (`backends/cline.py`)

- **路径**: `~/.cline/data/secrets.json`、`~/.cline/config.json`
- **差异**: 基本二分 OpenAI / Anthropic；baseUrl 写在 `apiProvider`

### Aider (`backends/aider.py`)

- **路径**: `~/.aider.conf.yml`（及 model settings/metadata）
- **差异**: `api-key` 列表 `provider=secret`；映射 openai/anthropic/deepseek/gemini/openrouter

### Continue.dev (`backends/continue.py`)

- **路径**: `~/.continue/config.json`（或 yaml）
- **差异**: `models[]` 每项 title/provider/apiKey/apiBase；可多模型并存

### Hermes Agent (`backends/hermes.py`)

- **路径**: `~/.hermes/config.yaml`、`~/.hermes/.env`
- **差异**: 按供应商映射 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 等独立 env

### QwenCode (`backends/qwencode.py`)

- **路径**: `~/.qwen/settings.json`、`.env`
- **差异**: DashScope/DeepSeek 专用 env；通用走 `modelProviders.openai[]`

### Kimi Code (`backends/kimi-code.py`)

- **路径**: `$KIMI_CODE_HOME` 或 `~/.kimi-code/config.toml`、`tui.toml`
- **差异**:
  - `providers` / `models` 表，托管 ID 前缀 `aiswitch@`
  - 类型：`kimi` / `openai` / `anthropic` / `google-genai`
  - 可写 `default_model` 与 coding 相关 services

### Goose (`backends/goose.py`)

- **路径**: `~/.config/goose/config.yaml`、`secrets.yaml`
- **差异**: 已知供应商 → 标准 env 名；支持 `custom_providers` 目录展示

### Grok CLI (`backends/grok-cli.py`)

- **路径**: `~/.grok-cli/.env`
- **差异**: 多 Key env + 当前 `GROKCLI_PROVIDER`；未知供应商走 `CUSTOM_API_KEY` / `CUSTOM_BASE_URL`

### Copilot CLI (`backends/copilot-cli.py`)

- **路径**: `~/.copilot/settings.json` 的 `byok` 段
- **差异**: 单 BYOK 槽位（provider_type + base_url + api_key + model）

### Devin CLI (`backends/devin.py`)

- **路径**: `~/.config/devin/config.json`、credentials.toml
- **差异**: 仅 Devin/Windsurf 等专用 token，非通用 OpenAI 同步

### Antigravity (`backends/antigravity.py`)

- **路径**: `~/.gemini/antigravity-cli/settings.json`
- **差异**: 仅 Google / Gemini Key 与可选 `GOOGLE_GEMINI_BASE_URL`

### Cursor CLI (`backends/cursor-cli.py`)

- **BYOK**: 否（账号体系）
- **差异**: 只读展示配置；不同步系统 Key

### TRAE Work (`backends/trae_work.py`)

- **产品**: [TRAE Work](https://www.trae.ai/) / TRAE IDE（Settings > Models 自定义模型）
- **文档**: https://docs.trae.ai/ide/models
- **配置 / 数据目录（跨平台）**:
  - **macOS**: `~/Library/Application Support/Trae*`、`Trae Work*`
  - **Windows**: `%APPDATA%\Trae*`、`%LOCALAPPDATA%\Trae*`（及 `ByteDance\Trae*`）；安装包常见于 `%LOCALAPPDATA%\Programs\Trae\`、`%ProgramFiles%\Trae\`
  - **Linux**: `~/.config/Trae*`、`~/.local/share/Trae*`
  - **托管清单（始终）**:
    - macOS/Linux: `~/.trae-work/ai-switch-models.json`
    - Windows: `%APPDATA%\ai-switch\trae-work\ai-switch-models.json` 与 `~/.trae-work/ai-switch-models.json`
    - 若检测到产品目录，同时写入 `ai-switch-models.json` 与 `User/globalStorage/ai-switch.trae-work/models.json`
- **差异**:
  - TRAE 官方以 **UI 添加自定义模型** 为主（API 格式：OpenAI chat/completions 或 Anthropic messages；Base URL + model ID + API Key）
  - 适配器将健康 Key 写成 **托管 JSON 清单**，便于对照导入；字段：`apiFormat` / `baseUrl` / `modelId` / `apiKey`
  - 安装检测：可执行文件 / 应用数据目录 / 进程（Windows 用 `tasklist`/`Get-CimInstance`）；**未安装不同步**
  - 运行中：跨平台进程名匹配（`Trae.exe` / `Trae.app` / `Trae Work` 等）
  - 若官方落盘格式有变更，可在本适配器中扩展写入路径

---

## 同步策略共性（`BackendAdapter`）

1. `should_sync(vendor, key)`：后端未禁用、Key 启用、健康可同步、且在 `sync_vendors` 白名单（或 `all`）
2. 生命周期：`on_key_added` / `on_key_updated` / `on_key_removed` / `reconcile` / `sync_from_backend`
3. 每后端可在 UI「同步」页关闭同步或限制供应商

---

## Codex 一键切换供应商

在 **后端引擎 → Codex CLI** 详情中：

1. 同步会把各供应商写入 `~/.codex/config.toml` 的 `[model_providers.aiswitch-…]`
2. 点击 **切换为活跃** 会：
   - 设置 `model_provider = "<id>"`
   - 更新 `auth.json` 的 `OPENAI_API_KEY`
   - 若有默认模型则写入 `model`
3. 之后在终端运行 `codex` 即使用该供应商

### API 端点

#### 获取供应商列表

```http
GET /api/backends/codex-cli/providers
```

响应示例：
```json
{
  "providers": [
    {
      "id": "aiswitch-1",
      "name": "OpenAI",
      "base_url": "https://api.openai.com/v1",
      "env_key": "OPENAI_API_KEY",
      "wire_api": "responses",
      "vendor_id": "1",
      "vendor_name": "OpenAI",
      "active": true,
      "managed": true
    },
    {
      "id": "aiswitch-2",
      "name": "DeepSeek",
      "base_url": "https://api.deepseek.com/v1",
      "env_key": "DEEPSEEK_API_KEY",
      "wire_api": "chat",
      "vendor_id": "2",
      "vendor_name": "DeepSeek",
      "active": false,
      "managed": true
    }
  ],
  "active_provider": "aiswitch-1"
}
```

#### 切换供应商

```http
POST /api/backends/codex-cli/switch-provider
Content-Type: application/json
```

请求体（方式一：直接指定 provider_id）：
```json
{ "provider_id": "aiswitch-2" }
```

请求体（方式二：通过 vendor_id）：
```json
{ "vendor_id": "2", "key_id": "optional" }
```

响应示例：
```json
{
  "success": true,
  "active_provider": "aiswitch-2",
  "message": "Switched to aiswitch-2"
}
```

### 配置文件结构

`~/.codex/config.toml` 示例：
```toml
model_provider = "aiswitch-1"

[model_providers.aiswitch-1]
name = "OpenAI"
base_url = "https://api.openai.com/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"

[model_providers.aiswitch-2]
name = "DeepSeek"
base_url = "https://api.deepseek.com/v1"
env_key = "DEEPSEEK_API_KEY"
wire_api = "chat"
```

---

## 添加新后端

1. 在 `backends/` 新增继承 `BackendAdapter` 的 Python 文件  
2. 实现 `name`、`display_name`、生命周期与 `config_files`  
3. 包自动发现注册，无需改 `__init__.py`  
4. 在本文件补充差异说明  

---

## English quick reference

| Concern | Multi-provider engines | Single-slot engines |
|---------|------------------------|---------------------|
| Store many keys | OpenClaw, OpenCode, Aider, Continue, Hermes, Kimi, Goose | Claude Code, Codex active auth, Cline pair, Copilot BYOK |
| Custom base URL | Most BYOK adapters | Cursor (N/A) |
| Active switch UI | **Codex CLI** (first-class) | Grok CLI via env `GROKCLI_PROVIDER` |
| Config style | JSON / JSONC / YAML / TOML / .env | — |

See also: [OpenAI Codex config basics](https://developers.openai.com/codex/config-file/config-basic), [Advanced config / model providers](https://developers.openai.com/codex/config-file/config-advanced).
