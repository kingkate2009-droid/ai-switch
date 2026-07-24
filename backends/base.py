import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Union

from core.data import get_backend_config


def make_status(
    *,
    installed: bool,
    running: bool = False,
    version: str = "",
    message: str = "",
    **extra,
) -> dict:
    """Normalize backend status for UI + sync.

    States:
    - not installed: installed=False, running=False → UI「未安装」, do not sync
    - stopped: installed=True, running=False → UI「已停止」, may sync
    - running: installed=True, running=True → UI「运行中」
    """
    installed = bool(installed)
    running = bool(running) and installed
    if not installed and not message:
        message = "Not installed"
    out = {
        "installed": installed,
        "running": running,
        "version": version or "",
        "message": message or "",
    }
    out.update(extra)
    return out


def cli_available(commands: Union[str, Sequence[str]], version_args: Optional[Sequence[str]] = None) -> tuple[bool, str]:
    """Return (installed, version) for CLI tools."""
    if isinstance(commands, str):
        commands = [commands]
    args = list(version_args or ["--version"])
    env = enriched_env()
    which = lambda c: shutil.which(c, path=env.get("PATH"))
    for cmd in commands:
        if not cmd:
            continue
        # PATH hit
        if not which(cmd) and not Path(cmd).exists():
            continue
        try:
            r = subprocess.run(
                [cmd, *args],
                capture_output=True,
                text=True,
                timeout=5,
                env=env,
            )
            ver = (r.stdout or r.stderr or "").strip().splitlines()
            version = (ver[0] if ver else "").strip()[:80]
            # Some CLIs print version but non-zero (e.g. missing runtime). Still installed.
            if r.returncode == 0 or version:
                return True, version
            return True, version
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return True, ""
        except Exception:
            if which(cmd):
                return True, ""
            continue
    return False, ""


def process_running(*name_substrings: str) -> bool:
    """True if any process cmdline contains one of the substrings (case-insensitive)."""
    needles = [s.lower() for s in name_substrings if s]
    if not needles:
        return False
    try:
        r = subprocess.run(
            ["ps", "-ax", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in (r.stdout or "").splitlines():
            low = line.lower()
            if any(n in low for n in needles):
                return True
    except Exception:
        pass
    return False


def port_listening(port: int) -> bool:
    """True if something is listening on TCP port (localhost)."""
    try:
        r = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0 and bool((r.stdout or "").strip())
    except Exception:
        return False


def enriched_env() -> dict:
    """Environment with common user tool paths (nvm/homebrew) prepended to PATH."""
    env = os.environ.copy()
    home = Path.home()
    extras = []
    # nvm current / latest node bins
    nvm = home / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        try:
            versions = sorted([p for p in nvm.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
            for p in versions[:3]:
                b = p / "bin"
                if b.is_dir():
                    extras.append(str(b))
        except OSError:
            pass
    for p in (
        home / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ):
        if p.is_dir():
            extras.append(str(p))
    if extras:
        cur = env.get("PATH", "")
        env["PATH"] = ":".join(extras + ([cur] if cur else []))
    return env


def vscode_extension_installed(*extension_id_prefixes: str) -> bool:
    """True if a VS Code / Cursor / VSCodium extension is installed.

    Looks under common extension directories only. Config dirs written by this
    manager must NOT be treated as installation evidence.
    """
    if not extension_id_prefixes:
        return False
    home = Path.home()
    roots = [
        home / ".vscode" / "extensions",
        home / ".vscode-oss" / "extensions",
        home / ".cursor" / "extensions",
        home / ".cursor-server" / "extensions",
        home / ".windsurf" / "extensions",
        home / "Library" / "Application Support" / "Code" / "User" / "extensions",
        home / "Library" / "Application Support" / "Cursor" / "User" / "extensions",
        home / "Library" / "Application Support" / "VSCodium" / "User" / "extensions",
        home / ".config" / "Code" / "User" / "extensions",
        home / ".config" / "Cursor" / "User" / "extensions",
    ]
    prefixes = tuple(p.lower() for p in extension_id_prefixes if p)
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                name = child.name.lower()
                if any(name == p or name.startswith(p + "-") for p in prefixes):
                    return True
        except OSError:
            continue
    return False


class BackendAdapter:
    """Base class for backend adapters (AI gateways, agent platforms, etc.)."""

    name = "base"
    display_name = "Base"

    @property
    def supports_byok(self) -> bool:
        """Whether this backend supports custom API key injection."""
        return True

    def is_installed(self) -> bool:
        """Whether the backend tool is installed on this machine."""
        try:
            st = self.get_status() or {}
            if "installed" in st:
                return bool(st.get("installed"))
            # legacy: message keywords
            msg = str(st.get("message") or "").lower()
            if any(k in msg for k in ("not found", "not installed", "no such file", "command not found")):
                return False
            return bool(st.get("running") or st.get("version"))
        except Exception:
            return False

    def should_sync(self, vendor: dict, key: dict) -> bool:
        """Check if a vendor/key should be synced to this backend.

        Consults per-backend config stored in data.backends.<name>.sync_vendors.
        - "all" or missing => sync all vendors
        - list => only sync vendors whose provider or id is in the list
        Also skips disabled / known-unhealthy keys so failed system keys
        never remain in engine configs.
        Not-installed backends never receive sync.
        """
        config = get_backend_config(self.name)
        if config.get("disabled"):
            return False
        if not key or not key.get("api_key") or key.get("enabled") is False:
            return False
        try:
            if not self.is_installed():
                return False
        except Exception:
            pass
        try:
            from core.health_checker import is_key_backend_syncable
            if not is_key_backend_syncable(str(vendor.get("id") or ""), key):
                return False
        except Exception:
            pass
        sync = config.get("sync_vendors", "all")
        if not isinstance(sync, list):
            return True
        return vendor.get("provider") in sync or vendor.get("id") in sync

    def on_key_added(self, vendor: dict, key: dict) -> None:
        pass

    def on_key_updated(self, vendor: dict, key: dict) -> None:
        """Default: re-add the key so backends that don't override still pick up value changes."""
        self.on_key_added(vendor, key)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        pass

    def on_vendor_removed(self, vendor: dict) -> None:
        pass

    def reconcile(self) -> None:
        """Full reconciliation – clean stale entries, rebuild aggregate config."""
        pass

    def sync_from_backend(self) -> list[dict]:
        """Import vendors/keys from backend. Returns list of vendor dicts."""
        return []

    def get_status(self) -> dict:
        return make_status(installed=False, running=False, message="Not installed")

    def restart(self) -> dict:
        return {"success": False, "message": "Not supported"}

    def get_version(self) -> str:
        return ""

    def get_config_template(self) -> list[dict]:
        """Return schema for UI config form. Each item: {key, label, type, default, help}"""
        return []

    @property
    def config_files(self) -> list[dict]:
        """Config files this adapter manages. Each: {path, label, type}"""
        return []
