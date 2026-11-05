# UI 二次优化方案 · 2026-11-05

上一轮（`docs/ui-redesign-2026-11.md`）解决的是**视觉**：配色、层级、卡片密度、状态截断。
这一轮解决**可用性**：13 个页面逐页截图体检后，剩下的问题都不再是"好不好看"，而是"能不能用"。

## 1. 体检结论

用 Playwright 对 13 个视图逐个独立加载、等数据就绪、量页面高度和横向溢出：

| 页面 | 文档高度 | 行数 | 结论 |
| --- | --- | --- | --- |
| Models | **244,735 px** | 3,814 | 滚动条完全失去意义 |
| Health | **101,275 px** | 1,058 | 每行 130px，974 行失败建议重复铺陈 |
| Check-in | 40,360 px | 634 | 无分页 |
| Quality | 24,231 px | ~1,000 | 无分页 |
| 供应商 Key 表 | 单供应商最多 279 行 | 279 | 无分页 |
| 其余 | < 4,000 px | — | 正常 |

横向溢出 0、JS 报错 0、卡在 Loading 0 —— 布局本身是对的，**问题在规模**。

### 具体问题清单

**A. 长列表无分页（P0）**
六张表一次性把全部行塞进 DOM。244,735px 的滚动条等于没有滚动条；3814 行 DOM 让筛选和重新渲染都有肉眼可见的延迟。

**B. 指标数字中途换行（P0）**
统计页 "Total Tokens" 渲染成 `100,248,57` + `3` 两行 —— `.stat-value` 上的 `overflow-wrap: anywhere` 会把数字从中间劈开。

**C. 健康页行高 130px（P1）**
每个失败行把"检查 API key / 端点 / 是否被吊销"这句建议原样铺在单元格里，974 行重复 974 次。建议信息应该进 tooltip，行内只留错误分类。

**D. 设置页控件归属不清（P1）**
两个复选框并排在同一行（`Enable scheduled health checks ☑ Auto-disable…`），下面紧跟的提示文案看不出属于谁；保存按钮在 3,700px 页面的最底部，改完要滚到底才能存。

**E. 后端页"Last push"喧宾夺主（P1）**
19 行 bullet 列表占据首屏，真正的后端状态卡被挤到折叠线以下；summary bar 里还重复了一个 Refresh。

**F. 图表 x 轴标签过密（P2）**
30 天趋势图逐日标注，旋转 45° 依然拥挤。

**G. 空状态占比过大（P2）**
Config profiles 的"暂无"状态居中在整行卡片里，浪费约 180px 高度。

## 2. 方案

### 2.1 通用分页组件（P0）

新增一套与业务无关的 JS helper + CSS：

```js
pagerSlice('models', rows)        // -> {rows, page, pages, size, total, start, end}
setPager('models', tbodyEl, info) // 同时渲染到表格上方和下方
```

- 每页 25 / 50 / 100 / 200，默认 50，选择写入 `localStorage`
- 页码状态按表保存；**任何筛选条件变化都回到第 1 页**（靠 filter signature 判断，翻页本身不会重置）
- 整份结果只占一页时不渲染分页栏
- 上下各一条：进来就能看到，滚完 50 行也不用回头找
- 接入 6 处：Models、Health key 状态、Check-in、Quality、Archived、供应商 Key 表

这是一次性投入、六处复用的改动，也是本轮唯一能真正把 24 万像素压回 1 万像素的手段。

### 2.2 指标数字（P0）

- `.stat-value` 改 `white-space: nowrap` + `text-overflow: ellipsis`，禁止从数字中间断行
- 新增 `fmtCompact()`：`3,517` → `3.5K`，`100,248,573` → `100.2M`，完整数值放 `title`
- 只用于 KPI 卡；表格里仍用 `fmtNum()` 全量

### 2.3 健康页行压缩（P1）

错误建议从行内 `<div class="form-hint">` 移到 badge 的 `title`（代码里本来就已经算好 `tip` 了），行高从 ~130px 降到 ~52px，配合分页后一次只看 50 行。

### 2.4 设置页（P1）

- `.settings-card .checkbox-label` 改为块级 + 上下 8px 间距，提示文案紧跟其所属复选框
- 保存栏移出网格，改 `position: sticky; bottom: 12px`，滚动到任何位置都能保存；脏状态显示"有未保存的修改"
- 卡片区从 2 列 grid 改成 `columns: 2` 文档流：卡片高度差异极大，grid 会在短卡旁边留出几百像素的空洞；`break-inside: avoid` 保证卡片不被拆开，`span-2` 用 `column-span: all` 保持整行卡片

### 2.5 后端页（P1）

- "Last push" 列表折叠为一行摘要（`11 ok / 0 fail · 详情`），默认收起
- 移除 summary bar 里重复的 Refresh

### 2.6 图表与空状态（P2）

- 日趋势 x 轴 `maxTicksLimit: 8`
- 空状态在整行卡片里降到 120px 内

### 2.7 验证过程中额外发现（原计划外）

| # | 问题 | 处理 |
| --- | --- | --- |
| 1 | 延迟显示 `67329 ms` | 新增 `fmtLatency()`：<1s 用 ms，<60s 用 s，再往上用 `1m 07s` |
| 2 | 供应商 Key 表在 1440px 宽度下横向溢出，操作列被挤出屏幕 | <1600px 隐藏 Notes / Latency 两列（信息量最低），供应商列表 320→260px，表格恰好放下 |
| 3 | Key 表 STATUS 列里未断行的 JSON 把 min-content 宽度撑爆 | `.status-text` 加 `overflow-wrap: anywhere`，2 行截断（完整内容本来就在 `title` 里） |
| 4 | Quality 页平均分直接显示裸数字 `4`，看着像残留 | 改成 `avg 4` 胶囊，与状态 pill 区分 |
| 5 | Quality 汇总数字无千分位 | 统一走 `fmtNum()` |
| 6 | Check-in "1 keys" | 单复数修正 |
| 7 | 供应商详情头部在变宽后被按钮挤压，复制图标掉到第二行 | `.detail-head-left` flex-basis 320→440px，让动作组自己换行 |

## 3. 落地结果

| 页面 | 改造前 | 改造后 | 降幅 |
| --- | --- | --- | --- |
| Models | 244,735 px | 3,697 px | **98.5%** |
| Health | 101,275 px | 5,797 px | **94.3%** |
| Check-in | 40,360 px | 3,553 px | **91.2%** |
| Quality | 24,231 px | 3,095 px | **87.2%** |
| Backends | 1,530 px | 1,038 px | 32% |
| Settings | 3,655 px | 3,061 px | 16% |

验收：

1. 13 个视图 × 深浅两套主题截图逐张目检，横向溢出 0、JS 报错 0、卡 Loading 0
2. 分页三种操作逐一验证：翻页（1/77 → 2/77）、改每页条数（50 → 25，1/77 → 1/153）、搜索后回到第 1 页（2/77 → 1/12 → 1/77）
3. 每页条数是全局偏好，改一次所有表同步（`localStorage.aiswitch_page_size`）
4. `tests/check_css_coverage.py`：404 个 class 全部有样式
5. `tests/check_template_js.py` PASS
6. 9 个测试文件 102 条用例全绿

## 4. 不做的事

- 不做虚拟滚动：分页已经解决 DOM 规模问题，虚拟滚动的复杂度（行高变动、sticky 表头、选择框状态）在这个项目里不划算
- 不改后端 API 分页：数据本来就在前端缓存里，服务端分页会让筛选逻辑分叉
- 不做 CSS masonry：`columns` + `break-inside: avoid` 已消除设置页的空洞，masonry 兼容性不足

