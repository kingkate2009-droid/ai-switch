# 发帖终稿（去 AI 味 · 可直接粘贴）

> 发之前随手改一两个细节（你自己的 Key 数量、工具组合），别一字不差群发。

---

## 1）Linux.do

**标题：**
```
中转 Key 一堆，Codex/OpenClaw 配置改到吐，自己搓了个本地管 Key 的
```

**正文：**
```
最近 Key 有点多。中转的、公益的、自己的，OpenClaw 一份、OpenCode 一份、Codex 又一份，Claude Code 还得改 env。

换一把 Key 就得改好几处。改漏了 401，改错了更烦——界面还显示运行中，其实早挂了。

以前靠记事本+脚本也能凑合，但有几个烦的：
1. 不知道哪把其实已经没额度
2. 有的工具根本没装，配置文件也被写过（自己作的）
3. Codex 有时 Responses 不通，排半天才发现是 Key 分组没通道，不是配置写错

就写了个本地小工具，叫 AI Switch：
https://github.com/kingkate2009-droid/ai-switch

就干三件事：
- 管 Key（导入、去重、按 URL 合并）
- 测一下能不能用（坏的尽量从后端踢掉）
- 推到本机已安装的工具（推之前有 Preview，没装的不写）

不是聊天客户端，数据在 ~/.ai-switch/，不上传。
Win/mac/Linux 包在 Release：
https://github.com/kingkate2009-droid/ai-switch/releases/tag/v2.0.5

求拍的点：
- 安装能不能一步起来
- Codex 场景还坑不坑
- 导入会不会把原来的供应商搞乱

有问题直接 GitHub Issue 就行，我挂着。
FAQ 也先放了：安装 / Codex / 导入各一条。
```

---

## 2）V2EX（节点建议：分享创造 / 程序员）

**标题：**
```
开源了个本地 AI Key 管理小工具：改一处，同步到 OpenClaw / OpenCode / Codex
```

**正文：**
```
自己用中转 Key 比较多，CLI/插件配置分散，改起来很烦，顺手开源了。

仓库：https://github.com/kingkate2009-droid/ai-switch
最新包：https://github.com/kingkate2009-droid/ai-switch/releases/tag/v2.0.5

能做什么（就这些）：
1. 批量导入 Key，同 URL 自动合并
2. 健康检测（Codex 相关会探 Responses）
3. 一键推到本机已安装后端，未安装不写

Python，本地跑，数据在用户目录。有 Docker 和桌面包。

已知坑：有些中转 Key「分组无模型通道」，工具只能把上游错误透出来，治不好站侧配置。

欢迎提 Issue 喷，别光 star 不说话也行…… star 也行。
```

---

## 3）相关帖回复模板（别硬广）

```
这类一般是 Key 分组没通道，或者 Responses 没通，不一定是客户端配置写错。

可以先在中转控制台看这把 Key 的 group 有没有模型；
Codex 的话再确认 /v1/responses 是否可用。

我自己 Key 太多，后来做成一个本地检测+同步的小工具了（不上传）：
https://github.com/kingkate2009-droid/ai-switch
排障文档：https://github.com/kingkate2009-droid/ai-switch/blob/main/docs/troubleshoot-codex.md
```

---

## 发帖检查

- [ ] 第一段是痛点，链接靠后
- [ ] 不写「赋能」「闭环」「一站式」
- [ ] 主动说已知坑
- [ ] Linux.do / V2EX 各发一版，别复制到第三家还一字不改
