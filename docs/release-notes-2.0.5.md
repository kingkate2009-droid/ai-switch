# AI Switch v2.0.5

> 你如果遇到「重复供应商」「检测总扫全量」「主模型挂了 Key 就红」——这版专门治。

## 你能直接感知的

### 1. 导入 / 新增按 URL 自动合并
- `https://x.com` 与 `https://x.com/v1` 视为同一供应商  
- 智能导入、手动新增、MetaAPI、备份合并同一规则  
- 工具栏新增 **合并重复 URL**（可预览再执行）

### 2. 健康检测更聪明
- **每次检测刷新模型列表**  
- 设置了 **主检测模型**：先测它 → 失败则从列表 **随机** 换其它模型  
- 候选都失败才判 Key 异常；鉴权/额度硬错误立即失败  
- **自适应频率**（可配置）：连续失败/成功 N 次后，该 Key 改用更长间隔；定时任务只检到期项  
- 探测提示词轻微随机，减少每次同一句 `hi`

### 3. 错误更好懂
- new-api 常见：`No available channel under group ...` 会尽量原文透出  
- 不再只显示笼统的 `No compatible model found`

## 升级

- [Releases](https://github.com/kingkate2009-droid/ai-switch/releases) 下对应平台包  
- 或源码 `git pull` 后重启  

数据仍在 `~/.ai-switch/`，一般无需迁移。

## 文档 / 社区

- [3 分钟入门](./quickstart.md)  
- [Codex 排障](./troubleshoot-codex.md)  
- FAQ Issues：安装 #1 · Codex #2 · 导入 #3  

## 已知限制

- 不同路径（如 `/v1` vs `/anthropic`）**不会**自动合并  
- 部分中转 Key「分组无通道」属于站侧配置，工具只能如实报错  
