# Linux.do 经历贴草稿（可直接发）

> 用途：P0 获客内容。语气偏真实、少广告。发之前自己改两处人名/数字更自然。  
> 建议分区：开发日志 / 资源分享（按站规）。标题可微调。

---

## 标题备选

1. 中转 Key 太多，Codex / OpenClaw 配置改到吐：我做了个本地 Key 中枢  
2. 别再手动改 5 份 auth 了：一把 Key 推到 OpenCode / Codex / Claude Code  
3. 开源小工具：AI Switch，管 Key + 健康检测 + 同步（求拍）

---

## 正文

最近 Key 有点多。中转站、公益站、公司的、自己的，混在一起。

OpenClaw 一份配置，OpenCode 一份，Codex 又一份，Claude Code 还得改 env。  
换一把 Key 就得改好几处，改漏了就 401，改错了更烦——工具还显示「运行中」，其实早就挂了。

以前也试过全靠记事本 + 脚本，能跑，但：

- 不知道哪把 Key 其实已经没额度  
- 未安装的工具有时也被写了配置（纯属自己作）  
- Codex 有时只出 GPT 相关模型，或者 Responses 不通，排了半天才发现是 Key 分组/通道问题  

就自己搓了个本地小东西，叫 **AI Switch**（GitHub：https://github.com/kingkate2009-droid/ai-switch ）。

定位很窄：

**不是聊天客户端。**  
就是管 Key、测一下健不健康、再推到本机已安装的后端。

现在大致能做：

1. 智能导入 / 批量粘贴（中转导出那种文本也行）  
2. 健康检测（Chat，Codex 相关会再探 Responses）  
3. 推送前有 Preview，**没装的后端不写**  
4. 坏 Key 可以从后端踢掉，避免把 CLI 搞挂  

数据在 `~/.ai-switch/`，不上传。有 Docker，也有 Win/macOS/Linux 包在 Release。

求拍的点其实就三个（也欢迎直接 Issue 骂）：

- 安装是否一步能起来  
- Codex 场景是否还是坑  
- 导入会不会把原有供应商搞乱（我们做了合并/去重，但仍怕边界 case）

文档里有个 3 分钟入门和 Codex 排障：

- https://github.com/kingkate2009-droid/ai-switch/blob/main/docs/quickstart.md  
- https://github.com/kingkate2009-droid/ai-switch/blob/main/docs/troubleshoot-codex.md  

如果有人是「Key 多、工具多、懒得改配置」同款，可以试试。  
有问题直接 GitHub Issue 就行，我挂着。

（以上纯自用刚需开源，不接云、不收 Key。）

---

## 发帖检查

- [ ] 标题不要「重磅开源」「颠覆」  
- [ ] 第一段是痛点，仓库链接放中间或后段  
- [ ] 不贴长功能列表  
- [ ] 主动说已知坑（Codex / 分组无通道）更真  
- [ ] 发完把帖子链接回填到 GitHub About 或 README Links（可选）
