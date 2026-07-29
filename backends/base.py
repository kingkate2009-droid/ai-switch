import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Union

from core.data import get_backend_config


# Install forms a backend product may take
INSTALL_CLI = "cli"
INSTALL_APP = "app"          # desktop / client
INSTALL_EXTENSION = "extension"  # IDE plugin
INSTALL_CONFIG = "config"    # product config footprint only (weak)


def make_status(
    *,
    installed: bool,
    running: bool = False,
    version: str = "",
    message: str = "",
    install_kinds: Optional[Sequence[str]] = None,
    **extra,
) -> dict:
    """Normalize backend status for UI + sync.

    States:
    - not installed: installed=False, running=False → UI「未安装」, do not sync
    - stopped: installed=True, running=False → UI「已停止」, may sync
    - running: installed=True, running=True → UI「运行中」

    install_kinds: one or more of cli / app / extension / config
    """
    installed = bool(installed)
    running = bool(running) and installed
    kinds = [str(k) for k in (install_kinds or []) if k]
    if not installed and not message:
        message = "Not installed"
    out = {
        "installed": installed,
        "running": running,
        "version": version or "",
        "message": message or "",
        "install_kinds": kinds,
    }
    out.update(extra)
    return out


def format_install_kinds(kinds: Sequence[str]) -> str:
    """Human-readable install form label (EN tokens; UI may i18n later)."""
    order = [INSTALL_CLI, INSTALL_APP, INSTALL_EXTENSION, INSTALL_CONFIG]
    labels = {
        INSTALL_CLI: "CLI",
        INSTALL_APP: "app",
        INSTALL_EXTENSION: "extension",
        INSTALL_CONFIG: "config",
    }
    seen = set()
    parts = []
    for k in order:
        if k in kinds and k not in seen:
            seen.add(k)
            parts.append(labels.get(k, k))
    for k in kinds:
        if k not in seen:
            seen.add(k)
            parts.append(labels.get(k, k))
    return "+".join(parts) if parts else ""


def status_from_detect(
    det: dict,
    *,
    not_installed_message: str = "Not installed",
    message: str = "",
    running: Optional[bool] = None,
    **extra,
) -> dict:
    """Build make_status() from detect_install() result."""
    kinds = list(det.get("install_kinds") or [])
    installed = bool(det.get("installed"))
    run = det.get("running") if running is None else running
    form = format_install_kinds(kinds)
    if not installed:
        msg = not_installed_message
    else:
        base = message or (det.get("evidence") or "Installed")
        msg = f"[{form}] {base}" if form else base
    return make_status(
        installed=installed,
        running=bool(run) and installed,
        version=det.get("version") or "",
        message=msg,
        install_kinds=kinds,
        **extra,
    )


def path_exists_any(paths: Sequence[Union[str, Path]]) -> Optional[Path]:
    for p in paths or []:
        try:
            pp = Path(p)
            if pp.exists():
                return pp
        except OSError:
            continue
    return None


def detect_install(
    *,
    cli_commands: Optional[Union[str, Sequence[str]]] = None,
    cli_version_args: Optional[Sequence[str]] = None,
    extension_ids: Optional[Sequence[str]] = None,
    app_paths: Optional[Sequence[Union[str, Path]]] = None,
    process_markers: Optional[Sequence[str]] = None,
    data_dirs: Optional[Sequence[Union[str, Path]]] = None,
    config_files: Optional[Sequence[Union[str, Path]]] = None,
    treat_config_as_installed: bool = False,
) -> dict:
    """Detect product presence across CLI / desktop app / IDE extension forms.

    Returns dict:
      installed: bool
      running: bool
      version: str
      install_kinds: list[str]  # cli, app, extension, config
      evidence: short str
    """
    kinds: list[str] = []
    version = ""
    evidence_parts: list[str] = []

    # 1) CLI
    if cli_commands:
        ok, ver = cli_available(cli_commands, cli_version_args)
        if ok:
            kinds.append(INSTALL_CLI)
            version = ver or version
            if isinstance(cli_commands, str):
                evidence_parts.append(f"cli:{cli_commands}")
            else:
                evidence_parts.append("cli:" + ",".join(str(c) for c in cli_commands[:3]))

    # 2) Desktop / client app paths
    hit = path_exists_any(app_paths or [])
    if hit is not None:
        if INSTALL_APP not in kinds:
            kinds.append(INSTALL_APP)
        evidence_parts.append(f"app:{hit.name}")
        if not version:
            version = hit.stem

    # 3) IDE extension
    if extension_ids and vscode_extension_installed(*extension_ids):
        if INSTALL_EXTENSION not in kinds:
            kinds.append(INSTALL_EXTENSION)
        evidence_parts.append("extension")
        if not version:
            version = "extension"

    # 4) Product data dirs (weak footprint → config, not desktop app)
    data_hit = path_exists_any(data_dirs or [])
    if data_hit is not None:
        try:
            has_content = data_hit.is_file() or (data_hit.is_dir() and any(data_hit.iterdir()))
        except OSError:
            has_content = data_hit.exists()
        if has_content:
            evidence_parts.append(f"data:{data_hit.name}")
            # Only count as installed if we already have a strong form, or config is allowed
            if not kinds and treat_config_as_installed:
                kinds.append(INSTALL_CONFIG)
                if not version:
                    version = "config"

    # 5) Config files (weak — only if allowed and no stronger form yet)
    cfg_hit = path_exists_any(config_files or [])
    if cfg_hit is not None:
        evidence_parts.append(f"config:{cfg_hit.name}")
        if treat_config_as_installed and not kinds:
            kinds.append(INSTALL_CONFIG)
            if not version:
                version = "config"

    # 6) Running process implies installed (daemon/app/cli process)
    running = False
    if process_markers:
        running = process_running(*process_markers)
        if running:
            evidence_parts.append("process")
            if not kinds:
                # Prefer cli if commands were requested, else app
                kinds.append(INSTALL_CLI if cli_commands else INSTALL_APP)
            if not version:
                version = "running"

    installed = bool(kinds)
    return {
        "installed": installed,
        "running": running and installed,
        "version": version or "",
        "install_kinds": kinds,
        "evidence": "; ".join(evidence_parts),
    }


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
    """True if any process cmdline/name contains one of the substrings (cross-platform)."""
    needles = [s.lower() for s in name_substrings if s]
    if not needles:
        return False

    def _match(text: str) -> bool:
        low = (text or "").lower()
        return any(n in low for n in needles)

    # Unix
    try:
        r = subprocess.run(
            ["ps", "-ax", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            for line in (r.stdout or "").splitlines():
                if _match(line):
                    return True
    except Exception:
        pass

    # Windows: tasklist / WMIC / PowerShell
    if os.name == "nt" or platform.system() == "Windows":
        try:
            r = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode == 0:
                for line in (r.stdout or "").splitlines():
                    if _match(line):
                        return True
        except Exception:
            pass
        try:
            r = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | Select-Object -ExpandProperty CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode == 0:
                for line in (r.stdout or "").splitlines():
                    if _match(line):
                        return True
        except Exception:
            pass
    return False


def port_listening(port: int) -> bool:
    """True if something is listening on TCP port (localhost). Cross-platform."""
    port = int(port)
    # Unix lsof
    try:
        r = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and bool((r.stdout or "").strip()):
            return True
    except Exception:
        pass
    # Windows netstat
    if os.name == "nt" or platform.system() == "Windows":
        try:
            r = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            needle = f":{port}"
            for line in (r.stdout or "").splitlines():
                low = line.lower()
                if "listen" in low and needle in line:
                    return True
        except Exception:
            pass
    # portable socket probe (bind fails if in use on some OS; connect is safer for LISTEN check)
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def is_windows() -> bool:
    return platform.system() == "Windows" or os.name == "nt"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def env_path(*keys: str) -> Optional[Path]:
    for k in keys:
        v = (os.environ.get(k) or "").strip()
        if v:
            return Path(v)
    return None


def home_config_dir(*parts: str) -> Path:
    """User config dir: ~/.config/<parts> or %APPDATA%\\<parts> on Windows."""
    if is_windows():
        base = env_path("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return base.joinpath(*parts) if parts else base
    xdg = env_path("XDG_CONFIG_HOME")
    if xdg:
        return xdg.joinpath(*parts) if parts else xdg
    return Path.home().joinpath(".config", *parts) if parts else (Path.home() / ".config")


def home_data_dir(*parts: str) -> Path:
    """User data dir: ~/.local/share/<parts> or %LOCALAPPDATA%\\<parts> on Windows."""
    if is_windows():
        base = env_path("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return base.joinpath(*parts) if parts else base
    xdg = env_path("XDG_DATA_HOME")
    if xdg:
        return xdg.joinpath(*parts) if parts else xdg
    return Path.home().joinpath(".local", "share", *parts) if parts else (Path.home() / ".local" / "share")


def home_dot_dir(*parts: str) -> Path:
    """Portable home-relative path: ~/.name/... (works on Windows as %USERPROFILE%\\.name)."""
    return Path.home().joinpath(*parts)


def vscode_extension_roots() -> list[Path]:
    """Common VS Code / Cursor / VSCodium / Windsurf extension directories (all OS)."""
    home = Path.home()
    roots = [
        home / ".vscode" / "extensions",
        home / ".vscode-oss" / "extensions",
        home / ".cursor" / "extensions",
        home / ".cursor-server" / "extensions",
        home / ".windsurf" / "extensions",
    ]
    if is_macos():
        support = home / "Library" / "Application Support"
        for app in ("Code", "Cursor", "VSCodium", "Code - Insiders", "Windsurf"):
            roots.append(support / app / "User" / "extensions")
    elif is_windows():
        roaming = env_path("APPDATA") or (home / "AppData" / "Roaming")
        for app in ("Code", "Cursor", "VSCodium", "Code - Insiders", "Windsurf"):
            roots.append(roaming / app / "User" / "extensions")
    else:
        for app in ("Code", "Cursor", "VSCodium", "Code - Insiders", "Windsurf"):
            roots.append(home / ".config" / app / "User" / "extensions")
    return roots


def enriched_env() -> dict:
    """Environment with common user tool paths (nvm/homebrew/Windows) prepended to PATH."""
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
    if is_windows():
        for p in (
            home / "AppData" / "Local" / "Programs",
            env_path("LOCALAPPDATA") / "Programs" if env_path("LOCALAPPDATA") else None,
            Path(r"C:\Program Files\nodejs"),
            Path(r"C:\Program Files\Git\cmd"),
        ):
            if p and p.is_dir():
                extras.append(str(p))
    if extras:
        cur = env.get("PATH", "")
        sep = ";" if is_windows() else ":"
        env["PATH"] = sep.join(extras + ([cur] if cur else []))
    return env


def vscode_extension_installed(*extension_id_prefixes: str) -> bool:
    """True if a VS Code / Cursor / VSCodium extension is installed.

    Looks under common extension directories only. Config dirs written by this
    manager must NOT be treated as installation evidence.
    """
    if not extension_id_prefixes:
        return False
    prefixes = tuple(p.lower() for p in extension_id_prefixes if p)
    for root in vscode_extension_roots():
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
            return False
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

    @staticmethod
    def iter_syncable_keys():
        """Yield (vendor, key) pairs that may stay in any backend engine config.

        Prefer this over raw enabled+api_key loops so quota/auth failures are
        stripped on reconcile even when health_auto_disable left enabled=True.
        """
        from core.data import get_vendors
        from core.health_checker import is_key_backend_syncable

        for v in get_vendors():
            vid = str(v.get("id") or "")
            for k in v.get("keys") or []:
                if not k.get("api_key") or k.get("enabled") is False:
                    continue
                if not is_key_backend_syncable(vid, k):
                    continue
                yield v, k

    def pick_syncable_key(self, vendor: dict = None, *, providers: set = None):
        """First syncable key, optionally filtered by vendor or provider set."""
        want_providers = {p.lower() for p in (providers or set()) if p}
        for v, k in self.iter_syncable_keys():
            if vendor is not None and str(v.get("id")) != str(vendor.get("id")):
                continue
            if want_providers:
                prov = str(v.get("provider") or "").lower()
                ep = str(v.get("endpoint_type") or "").lower()
                if prov not in want_providers and ep not in want_providers:
                    continue
            if not self.should_sync(v, k):
                continue
            return v, k
        return None

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
    def supports_active_switch(self) -> bool:
        """Whether this backend has a single/default active provider slot that can be switched."""
        return False

    def list_providers(self) -> list[dict]:
        """List switchable providers/slots for UI.

        Each item ideally:
          {id, name, base_url, vendor_id, vendor_name, models, active, managed}
        """
        return []

    def get_active_provider(self) -> dict:
        """Return {active_provider, name, base_url, model?} for the current slot."""
        return {"active_provider": "", "name": "", "base_url": ""}

    def switch_provider(self, provider_id: str = "", vendor_id: str = "", key_id: str = "") -> dict:
        """Switch the active provider/slot. Return {success, active_provider, message, ...}."""
        return {"success": False, "message": "Active provider switch not supported for this backend"}

    @property
    def config_files(self) -> list[dict]:
        """Config files this adapter manages. Each: {path, label, type}"""
        return []
