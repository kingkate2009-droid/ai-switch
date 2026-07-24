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
        from backends.base import make_status, cli_available

        installed, version = cli_available("cursor")
        if not installed:
            return make_status(installed=False, message="cursor CLI not found")
        has_cli = self._cli_config_path.exists()
        return make_status(
            installed=True,
            running=False,
            version=version,
            message="Cursor account auth only; no BYOK support" if has_cli else "CLI not configured",
        )

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._cli_config_path), "label": "CLI Config", "type": "json"},
            {"path": str(self._mcp_path), "label": "MCP Servers", "type": "json"},
        ]
