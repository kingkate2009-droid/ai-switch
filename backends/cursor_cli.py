import json
import os
import subprocess
from pathlib import Path

from backends.base import BackendAdapter


class CursorCliAdapter(BackendAdapter):
    name = "cursor-cli"
    display_name = "Cursor CLI"

    @property
    def supports_byok(self) -> bool:
        return False

    @property
    def _cli_config_path(self) -> Path:
        return Path.home() / ".cursor" / "cli-config.json"

    @property
    def _mcp_path(self) -> Path:
        return Path.home() / ".cursor" / "mcp.json"

    def on_key_added(self, vendor: dict, key: dict) -> None:
        pass

    def on_key_updated(self, vendor: dict, key: dict) -> None:
        pass

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        pass

    def reconcile(self) -> None:
        pass

    def sync_from_backend(self) -> list[dict]:
        return []

    def get_status(self) -> dict:
        try:
            r = subprocess.run(
                ["cursor", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            version = (r.stdout + r.stderr).strip()
            has_cli = self._cli_config_path.exists()
            return {
                "running": r.returncode == 0,
                "version": version,
                "message": "Cursor account auth only; no BYOK support"
                    if has_cli else "CLI not configured",
            }
        except FileNotFoundError:
            return {"running": False, "version": "", "message": "cursor CLI not found"}
        except Exception as e:
            return {"running": False, "version": "", "message": str(e)[:100]}

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._cli_config_path), "label": "CLI Config", "type": "json"},
            {"path": str(self._mcp_path), "label": "MCP Servers", "type": "json"},
        ]
