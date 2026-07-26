# Codex 连不上 — 专项排障

> Codex CLI 0.144+ 仅支持 `wire_api = "responses"`。健康检测会对 GPT/Codex 相关 Key 额外探测 `/v1/responses` 流式。

## 快速对照

| 现象 | 常见原因 | 处理 |
|------|----------|------|
| `Missing OPENAI_API_KEY` | `~/.codex/auth.json` 无 key，或与 `env_key` 不一致 | 在 AI Switch 切换活跃供应商；确认 `auth.json` 有 `OPENAI_API_KEY` |
| 配置加载失败 / wire_api | 仍写了 `wire_api = "chat"` | 删除 chat；统一 `responses`；重新「推送到后端」 |
| 401 / Unauthorized | Key 无效、吊销，或 Bearer 未带上 | 健康检测看错误分类；换 Key 再切换活跃供应商 |
| 403 / 额度不足 | 余额或令牌额度用尽 | 充值 / 换 Key；可在设置预算里自动禁用 |
| 404 on `/responses` | 代理只支持 chat completions | 换支持 Responses 的端点，或不用 Codex 用 OpenCode |
| 429 限流 | 请求过快 | 稍后再试或换 Key |
| SSL / TLS 错误 | 代理 MITM、系统时间、证书 | 检查代理与 HTTPS URL |
| 超时 / 连不上 | 网络、DNS、防火墙 | 检查网络；诊断页看 CLI/后端状态 |
| 能 chat 不能 Codex | 只通了 `/chat/completions` | 看健康结果 `check_layers.responses`；必须 Responses 流式 OK |
| `web_search` 相关失败 | 第三方代理不支持该工具 | 关 web_search 或换官方/兼容代理 |

## 1. 确认文件落盘

| 文件 | 作用 |
|------|------|
| `~/.codex/config.toml`（或 `$CODEX_HOME`） | `model_provider`、`model`、`[model_providers.aiswitch-*]` |
| `~/.codex/auth.json` | 活跃 `OPENAI_API_KEY`（等 env_key） |

AI Switch 管理的供应商 ID 形如：`aiswitch-<vendor_id>`。

期望片段（示意）：

```toml
model_provider = "aiswitch-2"
model = "gpt-4o-mini"

[model_providers.aiswitch-2]
name = "My Proxy"
base_url = "https://example.com/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
```

```json
{
  "OPENAI_API_KEY": "sk-..."
}
```

## 2. env_key 与 auth 必须一致

- 配置里 `env_key = "OPENAI_API_KEY"` 时，auth 里必须有同名键。  
- 切换供应商后若仍报 Missing key：在 **后端 → Codex CLI → 供应商** 再点一次切换；或 Dashboard **推送到后端**（先看 Preview）。  
- 不要手改一半文件导致 provider 与 auth 指向不同 Key。

## 3. Responses vs Chat

| 层 | 含义 |
|----|------|
| connectivity / chat | `/v1/chat/completions` 通 |
| responses | `/v1/responses` 流式通（Codex 实际路径） |

健康检测对带 GPT 模型 / 标签 `codex` 的 Key 会：Chat OK 后再测 Responses；Responses 失败会显示类似：

`Chat OK; Responses failed: ...`

**Codex 切换活跃供应商前应保证 Responses 可用**，否则工具内仍会失败。

## 4. web_search

部分第三方 NewAPI/代理不支持 Responses 上的 `web_search`。  
表现：其它请求正常，一带搜索就 4xx/5xx。  
处理：关闭 Codex 侧 web_search / 相关 tool，或换支持该能力的上游。

## 5. 推荐操作顺序

1. AI Switch 对该 Key **健康检测**（看是否 `check_layer=responses` 失败）  
2. **后端 → Codex CLI → 供应商** 切换到目标供应商  
3. 终端：`codex` 发一句 `hi`  
4. 仍失败：打开 **诊断 → 下载诊断包**（无密钥）附到 Issue  

## 6. 相关文档

- [3 分钟入门](./quickstart.md)  
- [后端差异](./backends.md)（Codex 配置与一键切换）  
- [适配器贡献](./adapter-contribution.md)  
