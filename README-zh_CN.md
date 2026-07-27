<p align="center">
  <a href="README-zh_CN.md">简体中文</a> ·
  <a href="README-zh_TW.md">繁體中文</a> ·
  <a href="README.md">English</a>
</p>

<h1 align="center">AI Switch</h1>

<p align="center">
  <strong>本地 AI Key 中枢 — 一把 Key 改完，多工具立刻能用</strong>
</p>

<p align="center">
  <a href="https://github.com/kingkate2009-droid/ai-switch/releases"><img alt="release" src="https://img.shields.io/github/v/release/kingkate2009-droid/ai-switch?include_prereleases"></a>
  <a href="https://github.com/kingkate2009-droid/ai-switch/stargazers"><img alt="stars" src="https://img.shields.io/github/stars/kingkate2009-droid/ai-switch?style=social"></a>
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-Apache%202.0-blue.svg"></a>
  <img alt="python" src="https://img.shields.io/badge/python-3.9%2B-blue">
</p>

<p align="center">
  <a href="#为什么需要">为什么需要</a> ·
  <a href="#三分钟上手">三分钟上手</a> ·
  <a href="#它到底解决什么">解决什么</a> ·
  <a href="#文档">文档</a> ·
  <a href="#兼容的后端">后端</a> ·
  <a href="#开源协议">协议</a>
</p>

---

## 为什么需要

中转站 / 多供应商 Key 一多，OpenClaw、OpenCode、Codex、Claude Code 配置就容易乱：

| 痛点 | 自己改配置 | 用 AI Switch |
|------|------------|--------------|
| 换一把 Key | 改 5 个文件 | 改一处 → 推到已安装工具 |
| 坏 Key | 工具挂、报错一堆 | 健康检测 → 从后端踢掉 |
| 写错配置 | 静默覆盖 | **推送前 Preview** · 未安装不写 |
| 新人上手 | 「Key 填哪？」 | **四步**：加 Key → 检测 → 推送 → 发一句测试 |

**定位：** 本地 **Key 中枢**（管 Key、验健康、推配置），不是又一个聊天客户端。

> 一把 Key 改完，多工具立刻能用；坏 Key 别把工具搞挂。

---

## 三分钟上手

**推荐：** 从 [Releases](https://github.com/kingkate2009-droid/ai-switch/releases) 下对应平台包  
（Windows / macOS Intel / Apple Silicon / Linux）→ 解压运行 → 打开 **http://127.0.0.1:8787**

**源码：**

```bash
git clone https://github.com/kingkate2009-droid/ai-switch.git
cd ai-switch
pip install -r requirements.txt
python3 run.py
# → http://127.0.0.1:8787
```

**Docker：**

```bash
docker compose up -d
```

图文四步：[docs/quickstart.md](docs/quickstart.md)  
Codex 连不上：[docs/troubleshoot-codex.md](docs/troubleshoot-codex.md)

---

## 它到底解决什么

日常只关心三件事：

1. **Key 集中管** — 供应商、标签、智能导入、MetaAPI 合并、去重  
2. **知道谁还能用** — 分层探测（Chat + Codex Responses）、可读错误、连续成败自适应间隔  
3. **同步别写错** — Preview · 上次同步摘要 · 未安装零写入 · 按后端切换活跃供应商  

进阶能力（按需）：签到 URL、预算告警、加密 Profile、诊断包、下游路由、桌面安装包。

<details>
<summary><strong>完整功能列表（展开）</strong></summary>

- 多供应商 + Key 表，搜索 / 标签 / 批量启停删  
- 26+ 内置供应商 + 自定义 OpenAI 兼容地址  
- 定时健康监测，可选失败禁用 / 主备切换  
- 同步 OpenClaw、OpenCode、Claude Code、Codex、Cline、Aider、Continue 等  
- 批量 / 备份 / MetaAPI 导入可撤销  
- 浅色/暗色、中英繁  
- 适配器扩展：[贡献指南](docs/adapter-contribution.md)

</details>

---

## 文档

| 文档 | 用途 |
|------|------|
| [3 分钟入门](docs/quickstart.md) | 第一次跑通 |
| [Codex 连不上](docs/troubleshoot-codex.md) | Responses / 环境 / 模型 |
| [后端说明](docs/backends.md) | 各引擎支持什么 |
| [适配器贡献](docs/adapter-contribution.md) | 加一个后端 |

有问题请直接 [开 Issue](https://github.com/kingkate2009-droid/ai-switch/issues)。已整理 FAQ：

- [#1 安装与启动](https://github.com/kingkate2009-droid/ai-switch/issues/1)
- [#2 Codex 连不上](https://github.com/kingkate2009-droid/ai-switch/issues/2)
- [#3 导入与合并](https://github.com/kingkate2009-droid/ai-switch/issues/3)
- 最新版说明：[v2.0.5 Release](https://github.com/kingkate2009-droid/ai-switch/releases/tag/v2.0.5) · [变更要点](docs/release-notes-2.0.5.md)

---

## 兼容的后端

| 后端 | 说明 |
|------|------|
| OpenClaw / OpenCode / Claude Code / Codex CLI | 主路径，含活跃切换与健康联动 |
| Cline · Aider · Continue.dev | IDE / CLI 配置 |
| Hermes · QwenCode · Kimi Code · TRAE Work | 详见 [backends.md](docs/backends.md) |

后端页三态：**未安装** | **已停止（可同步）** | **运行中** — 未安装绝不会写配置。

### 供应商探测类型

| 类型 | 示例 |
|------|------|
| `openai_chat` | OpenAI、DeepSeek、OpenRouter、Groq、月之暗面、通义… |
| `anthropic` | Anthropic 兼容 |
| `gemini` | Google Gemini |
| `openai_responses` | Codex 用的 `/v1/responses` |

---

## 安装包（Windows / macOS / Linux）

| 平台 | Releases 文件名模式 |
|------|---------------------|
| Windows x64 | `ai-switch-<ver>-windows-amd64.zip` |
| macOS Intel | `ai-switch-<ver>-macos-amd64.tar.gz` |
| macOS Apple Silicon | `ai-switch-<ver>-macos-arm64.tar.gz` |
| Linux x64 | `ai-switch-<ver>-linux-amd64.tar.gz` |

```bash
bash scripts/build_package.sh   # 本地打包
./release.sh v2.0.5             # CI 多平台发版
```

---

## 配置与安全

| 项目 | 路径 |
|------|------|
| 主数据 | `~/.ai-switch/data.json` |
| 用量 | `~/.ai-switch/usage.json` |
| 端口 | `8787`（`AI_SWITCH_PORT`） |

> **Key 只存在 `~/.ai-switch/`，不要提交进 Git。** 局域网暴露可在设置里开 access token。

**技术栈：** Python + Flask · 原生前端 · JSON 存储 · 可插拔适配器 · Apache 2.0

---

## 友链 / 社区

- [Releases](https://github.com/kingkate2009-droid/ai-switch/releases)  
- [Issues](https://github.com/kingkate2009-droid/ai-switch/issues)  
- [Linux.do](https://linux.do/)  

## 开源协议

Apache 2.0
