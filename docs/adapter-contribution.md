# 适配器贡献指南

> 加一个后端 = 在 `backends/` 丢一个 Python 文件，实现 `BackendAdapter`。自动发现注册，无需改 `__init__.py` 列表。

## 最小清单

| 改什么 | 说明 |
|--------|------|
| `backends/your_tool.py` | 新建适配器类 |
| （可选）`docs/backends.md` | 补一行能力表与路径说明 |
| （可选）`locales/*.json` | `backends.name.your-tool` 显示名 |
| （可选）README 兼容列表 | 用户可见的后端名单 |

**不要**在适配器里写密钥到仓库；只读写用户本机配置目录。

## 1. 骨架

```python
# backends/your_tool.py
from pathlib import Path
from backends.base import BackendAdapter, detect_install, status_from_detect


class YourToolAdapter(BackendAdapter):
    name = "your-tool"              # 唯一 id，API/UI 用
    display_name = "Your Tool"

    @property
    def supports_byok(self) -> bool:
        return True                 # False = 只读展示，不推 Key

    def is_installed(self) -> bool:
        return super().is_installed()

    def get_status(self) -> dict:
        det = detect_install(
            cli_commands=("your-tool", "your-tool.exe"),
            process_markers=("your-tool",),
            data_dirs=[Path.home() / ".your-tool"],
            config_files=[self._config_path],
            treat_config_as_installed=False,  # 重要：勿仅凭本管理器写过的目录算已安装
        )
        return status_from_detect(det, not_installed_message="your-tool not found")

    def on_key_added(self, vendor: dict, key: dict) -> None:
        ...

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        ...

    def reconcile(self) -> None:
        """全量按系统健康 Key 重建配置；未安装应直接 return。"""
        if not self.is_installed():
            return
        ...

    def sync_from_backend(self) -> list[dict]:
        """可选：从后端配置反读 Key，供「从后端导入」。"""
        return []

    @property
    def config_files(self) -> list[dict]:
        return [{"path": str(self._config_path), "label": "Config", "type": "json"}]
```

启动时 `backends.init_backends()` 会 `pkgutil` 扫描模块，实例化所有 `BackendAdapter` 子类并 `register`。

## 2. 必须遵守的语义

1. **未安装 = 零写入**  
   `should_sync` / `reconcile` / `on_key_*` 在 `not is_installed()` 时不要写文件。  
   安装检测失败时 **fail-closed**（当作未安装）。

2. **安装证据**  
   认 CLI / 官方 App / IDE 扩展目录；**不要**把 `~/.ai-switch/...` 或仅本工具生成的配置目录当成「已安装」。

3. **坏 Key 不同步**  
   默认 `should_sync` 已过滤 `enabled=False` 与健康不可用 Key（`is_key_backend_syncable`）。覆盖时请保留这层。

4. **配置可编辑**  
   尽量实现 `config_files`，方便 UI 查看路径。写文件建议原子写（tmp + replace）。

5. **活跃槽（可选）**  
   若后端是「当前只用一个供应商」：

   ```python
   @property
   def supports_active_switch(self) -> bool:
       return True

   def list_providers(self) -> list[dict]: ...
   def get_active_provider(self) -> dict: ...
   def switch_provider(self, provider_id="", vendor_id="", key_id="") -> dict:
       return {"success": True/False, "active_provider": "...", "message": "..."}
   ```

   UI 会自动出现「供应商」页；API：  
   `GET/POST /api/backends/<name>/providers`、`/switch-provider`。

## 3. 状态字段约定

`get_status()` 建议经 `make_status` / `status_from_detect` 返回：

| 字段 | 含义 |
|------|------|
| `installed` | 是否检测到产品 |
| `running` | 进程/网关是否在跑 |
| `version` | 版本字符串 |
| `message` | 人类可读摘要 |
| `install_kinds` | `cli` / `app` / `extension` / `config` |

UI 三态：**未安装 | 已停止（可同步）| 运行中**。

## 4. 本地验证

```bash
python3 -c "
from backends import init_backends, get_all
init_backends()
a = get_all()['your-tool']
print(a.get_status())
print('installed', a.is_installed())
"
# 启动 UI 后：后端页应出现卡片；未安装时推送 Preview 应显示 Skip
python3 run.py
```

## 5. 参考实现

| 类型 | 文件 |
|------|------|
| 基类 + detect_install | `backends/base.py` |
| 多 provider TOML + 切换 | `backends/codex_cli.py` |
| JSONC + auth | `backends/opencode.py` |
| 单槽 env | `backends/claude_code.py` |
| 自动发现 | `backends/__init__.py` |

## 6. PR 检查表

- [ ] `name` 唯一、kebab-case  
- [ ] 未安装不写盘  
- [ ] 不把管理器自建目录当安装证据  
- [ ] `reconcile` 只推健康/启用 Key  
- [ ] 跨平台路径（`Path.home()`、Windows `%APPDATA%`）  
- [ ] 无密钥、无绝对本机隐私路径写死在仓库  
- [ ] （建议）`docs/backends.md` 补一行  

## 相关文档

- [backends.md](./backends.md)  
- [quickstart.md](./quickstart.md)  
- [troubleshoot-codex.md](./troubleshoot-codex.md)  
