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
        from backends.base import detect_install, status_from_detect, env_path, is_windows

        home = Path.home()
        app_paths = [
            Path("/Applications/Cursor.app"),
            home / "Applications" / "Cursor.app",
        ]
        if is_windows():
            local = env_path("LOCALAPPDATA") or (home / "AppData" / "Local")
            app_paths.extend([
                local / "Programs" / "cursor" / "Cursor.exe",
                local / "Programs" / "Cursor" / "Cursor.exe",
            ])
        det = detect_install(
            cli_commands=("cursor", "cursor.cmd", "cursor.exe"),
            app_paths=app_paths,
            process_markers=("Cursor.exe", "Cursor.app", "Cursor Helper"),
            data_dirs=[home / ".cursor"],
            config_files=[self._cli_config_path, self._mcp_path],
            treat_config_as_installed=False,  # need CLI or app, not only config we might write
        )
        # Cursor is primarily a desktop app; CLI is optional
        has_cli = self._cli_config_path.exists()
        msg = "Cursor account auth only; no BYOK support" if (has_cli or det["installed"]) else "not configured"
        return status_from_detect(
            det,
            not_installed_message="Cursor app/CLI not installed",
            message=msg,
        )

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._cli_config_path), "label": "CLI Config", "type": "json"},
            {"path": str(self._mcp_path), "label": "MCP Servers", "type": "json"},
        ]
