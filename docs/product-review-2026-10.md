# AI Switch 产品复盘与下一步（2026-10）

> 视角：产品经理 + 可用性/可靠性审查
> 基线：v2.4.9，`docs/product-optimization.md`（2.0.5 时期）已完成大部分 P0
> 方法：读代码 + 真机跑 UI（Playwright）+ 对标 CC Switch / cc-switch-web / LiteLLM

## 一句话结论

能力面已经很宽，**但「信任层」还不足以支撑它现在的野心**。
用户敢把全部 Key 交给它，前提是三件事都成立：不会丢、不会错、出问题会告诉我。
目前第 1、2 件基本做到，**第 3 件（主动告知）几乎为零**。

## 一、现状盘点（有证据）

| 维度 | 现状 | 判断 |
|---|---|---|
| 功能宽度 | 162 个路由；供应商/Key/健康/同步/模型/路由/统计/预算/归档/诊断/加密导出 | 强，甚至偏多 |
| i18n | en 975 / zh_CN 974 / zh_TW 974，仅缺 1 键 | 优秀 |
| 数据安全 | `data.json` 0600、可选 ENC1 字段加密、损坏自愈 | 良好，但默认关闭 |
| 自动化测试 | **0 个测试文件**；CI 只做打包发版 | 最大工程风险 |
| 可观测 | stderr 打日志；无 `/healthz`；无轮转 | 容器场景不合格 |
| 可访问性 | `aria-` 7 处、`role=` 1 处、`alt=` 0 处、无焦点陷阱 | 明显短板 |
| 前端规模 | `index.html` ~1 万行；`app.py` 168KB / ~4.3k 行 | 单人可维护，团队难 |

## 二、P0：会直接造成「信任崩塌」的问题

### 1. 定向推送存在静默数据丢失（已实证）

`POST /api/sync/push` 支持 `key_ids` / `vendor_ids` 定向推送。但部分适配器实现「按范围重建」，**会把同后端里其它 key 的条目一并清掉**。
后果：用户点单 Key 同步 → 另一个工具里别的 Key 消失 → 无报错、不可逆。
当前前端用「改为全量推送」绕过了，但 API 仍对外暴露该参数。

**建议**
- 适配器层统一为「读取-合并-写回」，禁止按范围重建整个配置段
- 加回归测试：2 个 key 的配置，定向推 1 个后断言另 1 个仍在
- 短期在 API 层对 `key_ids` 直接降级为全量，并在响应里标注

### 2. 零自动化测试 + 无测试 CI

今年踩的坑（SQLite 缓存陈旧、定向推送回归、下拉菜单层叠上下文）**都属于测试能拦住的类型**。
`templates/index.html` 一个下拉菜单的问题就需要 Playwright 才能定位——说明纯人工验证已到极限。

**建议（最小可行）**
- `pytest`：解析器（智能导入/URL 合并）、数据层（保存/损坏回退/加密）、适配器写文件
- Playwright 冒烟 5 条：首屏加载、添加 Key、打开「更多」菜单并点击、预览推送、切语言
- GitHub Actions：push 跑 lint + pytest + 冒烟（不需要打包矩阵）

### 3. 默认无鉴权 + Docker 绑 0.0.0.0

`access_token` 默认空 = 任何能访问端口的人可读走全部 Key。
`docker-compose.yml` 映射 `8787:8787` 到宿主机所有网卡。局域网/公网 VPS 部署即裸奔。

**建议**
- 绑定非 loopback 时**强制**生成并打印一次性 token（Jupyter 模式）
- 首次启动在 UI 顶部常驻横幅「未设置访问密码」
- docker-compose 默认 `127.0.0.1:8787:8787`

### 4. 没有任何外部告警通道

全仓库 `webhook` / `notify` 关键词命中 **0**。
预算超限、Key 全部失效、连续健康检查失败——用户只有在打开页面时才知道。
竞品里 LiteLLM 有告警，CC Switch 没有 —— **这正是差异化的空白位**。

**建议**
- Webhook（飞书/Slack/通用 JSON）+ 桌面通知 + 可选邮件
- 触发点：预算阈值（80%/100%）、Key 连续 N 次失败、供应商全部 Key 失效、同步失败
- 做成「通知中心」：规则可开关、有静默时段、有历史

**已实现（v1）**：`core/notify.py` + `core/budget.py`，入口 Settings → Notifications。

- 渠道：飞书（文本消息）/ Slack（text + attachments 带颜色）/ 通用 JSON webhook（`{source, event, level, title, message, detail, timestamp}`）
- 事件：`budget_exceeded` / `key_failing` / `vendor_all_keys_failed` / `sync_failed`，逐个可勾选
- 静默时段（`23:00-08:00`，支持跨午夜，critical 事件不受限）、按事件限流（默认 60 分钟）、预算告警每天每档最多一次
- 投递在后台线程 + 8s 超时，绝不阻塞健康检查 / 同步；HTTP 200 但被渠道拒绝（Slack `{"ok":false}`、飞书 `{"code":≠0}`）也记为失败
- 每渠道「发送测试」按钮 + 「最近投递」列表（含错误原文）
- 刻意**不做**：桌面通知（需要常驻托盘进程，和「浏览器打开即用」的产品形态冲突）、邮件（需要 SMTP 配置面板与凭据存储，收益低于 webhook）。Webhook URL 不进 policy 模板，避免凭据随模板外泄

## 三、P1：主路径与规模

### 5. 自己的数据量已经压到前端

实测库里有 584 个供应商、902 个 Key，页面 DOM 约 16MB 量级。
现状：无分页、无虚拟滚动、搜索 `oninput` 无防抖、表格全量渲染。

**建议**：列表虚拟化（或 50/页分页）+ 搜索 150ms 防抖 + 供应商列表懒加载。
验收：1000 个 Key 时筛选响应 < 100ms，首屏 < 1.5s。

### 6. 推送预览不是「差异」，是「名单」

`preview_push_all()` 只返回「会写入 / 会跳过」的后端清单。
用户真正想问的是：**这次会改掉我 Codex 配置里的哪几行？**

**建议**：预览里给出每个文件的 unified diff（写入前 vs 写入后），红绿高亮，可折叠。

### 7. 数据生命周期失控

`usage.json`、`audit`、`health_history` 无保留策略，只增不减。
900 个 Key 每天检测一次，一年后是几十万条记录。

**建议**：保留策略（明细 30 天 / 按天聚合 1 年）+ 归档导出 + 设置项可见。

### 8. 价值主张需要收口：你其实是两个产品

代码里已经有完整的 OpenAI/Anthropic 兼容网关：`/v1/chat/completions`、`/v1/messages`、`/v1/images/generations`、`/v1/audio/*`，还有路由、预算、用量。但 README 只把它当「Key 中枢」卖。

同时，**cc-switch-web 已明确支持 OpenClaw / OpenCode / OMO**——「配置同步」这个楔子正在被填平。

**建议**：把定位从「配置同步工具」上移到 **「AI Key 的可靠性层」**：
> cc-switch 帮你切供应商；AI Switch 保证你切过去的那个**还能用**，且花超了会喊你。

差异化护城河 = 健康检测 + 自动降级/故障转移 + 预算与告警 + 用量归因。
配置同步只是入口，不是壁垒。

如果保留网关能力，需补：限流、并发上限、请求日志脱敏、（可选）OpenAPI 文档。

### 9. 首屏与新手路径

- Dashboard「Action needed」在无问题时仍占一块大卡片 → 无问题时应折叠成一行
- 「Recent activity」直接显示 `health.history_append`、`sync.push` 这类事件名 → 应转人话（「3 个 Key 健康检查通过」）
- 「3 分钟闭环」最后一步仍需用户自己去别的应用里发 `hi` → 可在应用内用网关跑一次真实探活，给绿勾

## 四、P2：体验与工程卫生

| 问题 | 证据 | 建议 |
|---|---|---|
| 金额格式错 | `toFixed(5)` → `$0.00000` | 统一 `formatMoney()`：<0.01 用 4 位有效数字，其余 2 位 |
| 无焦点管理 | 无 tabindex/焦点陷阱，Esc 只关 action menu | 模态框焦点陷阱 + Esc 关闭 + 返回焦点 |
| 无命令面板 | 900 个 Key 只能靠点 | `Cmd/Ctrl+K` 搜 Key/供应商/动作 |
| 无 `/healthz` | 容器无健康检查 | 加 `/healthz`（不鉴权、无敏感信息）+ compose healthcheck |
| 日志不落盘/不轮转 | 只有 stderr；仓库根有 4.6MB 日志 | `RotatingFileHandler` 写 `~/.ai-switch/logs`，仓库加 ignore |
| 单体过大 | app.py 4.3k 行 / 162 路由 | 拆 Blueprint：vendors / keys / sync / stats / routes / settings |
| 无更新提示 | 已有版本检查接口，但无常驻入口 | 设置页 + 有新版本时顶栏小圆点 |

## 五、建议的下一步（按投入产出排序）

1. **修定向推送的数据丢失**（0.5 天，风险最高）
2. **加 pytest + Playwright 冒烟 + CI**（2–3 天，止住回归）
3. **Webhook / 桌面通知 + 通知规则**（2–3 天，补齐信任闭环，也是差异化）
4. **鉴权默认安全**（1 天，开源后第一个被问的问题）
5. **列表虚拟化 + 搜索防抖**（1–2 天，自己的数据已经压线）
6. **推送 diff 预览**（1–2 天，降低「敢不敢点」的心理门槛）
7. **定位与 README 改写**（0.5 天，纯文案，收益最大）
8. 金额格式、事件人话、模态焦点、`/healthz`（半天打包）

## 六、不建议做的事

- 继续加边缘后端（安装检测与同步语义跟不上会反噬）
- 云账号体系（与本地定位冲突）
- 静默自动切供应商（用户失控感 > 便利）
- 做聊天客户端（已有大量强敌，且不服务「Key 中枢」定位）

## 七、实施进度（滚动更新）

| 项 | 状态 | 说明 |
|----|------|------|
| P0-1 定向推送数据丢失 | ✅ | `backends.normalize_push_scope` 把 `vendor_ids`/`key_ids` 推送统一升级为全量 reconcile；API 返回 `scope_promoted`；`tests/test_scoped_push_safety.py` 锁死不变式 |
| P0-2 测试 + CI | ✅ 基本 | `tests/`：推送安全性 5 + 鉴权策略 10 + app 冒烟 5 + 通知 29，共 49 条；`check_template_js.py` 校验内联脚本；`conftest.py` 隔离测试数据；`requirements-dev.txt` + `.github/workflows/ci.yml`。浏览器冒烟（Playwright）仍为手工 |
| P0-3 鉴权默认安全 | ✅ | 绑定非 loopback 时自动生成并打印 token（`core/access.py`）；loopback 默认完全无感；支持 `AI_SWITCH_REQUIRE_AUTH` / `AI_SWITCH_ACCESS_TOKEN`；compose 改为 `127.0.0.1:8787:8787`，Dockerfile 显式 `AI_SWITCH_HOST=0.0.0.0`（并修好 app.py 之前硬编码 127.0.0.1 导致容器端口不可达的问题） |
| P0-4 外部告警通道 | ✅ | `core/notify.py`（飞书 / Slack / 通用 webhook，后台线程投递，永不阻塞）+ `core/budget.py`（预算评估共用，抽离自 dashboard）。4 类事件：预算 80%/100%、Key 连续失败、供应商全部 Key 失效、同步失败。含静默时段（可跨午夜）、按事件限流、每渠道测试按钮、最近投递列表。入口：Settings → Notifications |
| P1-5 列表虚拟化 + 搜索防抖 | ⬜ | |
| P1-6 推送 diff 预览 | ⬜ | |
| P2 金额格式等小项 | ⬜ | |
