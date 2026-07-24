import os
import shutil
import subprocess
from pathlib import Path

from backends.base import BackendAdapter


class GrokCliAdapter(BackendAdapter):
    """Adapter for @stevederico/grok-cli (Universal LLM CLI).

    Config is stored as env vars in ~/.grok-cli/.env
    See: https://github.com/stevederico/grok-cli
    """

    name = "grok-cli"
    display_name = "Grok CLI"

    # provider id -> env var for API key
    _KEY_ENV = {
        "xai": "XAI_API_KEY",
        "grok": "XAI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GEMINI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "groq": "GROQ_API_KEY",
        "azure": "AZURE_OPENAI_API_KEY",
        "github": "GITHUB_TOKEN",
    }

    # provider id for GROKCLI_PROVIDER when known
    _PROVIDER_ALIAS = {
        "xai": "xai",
        "grok": "xai",
        "openai": "openai",
        "anthropic": "anthropic",
        "google": "google",
        "gemini": "google",
        "openrouter": "openrouter",
        "groq": "groq",
        "azure": "azure",
        "github": "github",
    }

    @property
    def _config_dir(self) -> Path:
        return Path.home() / ".grok-cli"

    @property
    def _env_path(self) -> Path:
        return self._config_dir / ".env"

    def _load_env(self) -> dict:
        if not self._env_path.exists():
            return {}
        env = {}
        with open(self._env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("\"'")
        return env

    def _save_env(self, env: dict) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        # Preserve comments / unknown keys order when possible
        lines = []
        seen = set()
        if self._env_path.exists():
            with open(self._env_path) as f:
                for line in f:
                    raw = line.rstrip("\n")
                    stripped = raw.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        lines.append(raw)
                        continue
                    k = stripped.split("=", 1)[0].strip()
                    if k in env:
                        val = env[k]
                        if val is None:
                            continue  # drop
                        lines.append(f'{k}="{val}"')
                        seen.add(k)
                    else:
                        lines.append(raw)
                        seen.add(k)
        for k, v in env.items():
            if k in seen or v is None:
                continue
            lines.append(f'{k}="{v}"')
        with open(self._env_path, "w") as f:
            f.write("\n".join(lines).rstrip() + "\n")

    def _resolve_key_env(self, vendor: dict) -> tuple[str, str]:
        """Return (env_var, provider_alias) for this vendor."""
        prov = (vendor.get("provider") or "").lower()
        if prov in self._KEY_ENV:
            return self._KEY_ENV[prov], self._PROVIDER_ALIAS.get(prov, prov)
        # Custom / unknown → CUSTOM_API_KEY + CUSTOM_BASE_URL
        return "CUSTOM_API_KEY", "custom"

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        env_var, alias = self._resolve_key_env(vendor)
        env = self._load_env()
        env[env_var] = key["api_key"]
        env["GROKCLI_PROVIDER"] = alias

        if alias == "custom" or env_var == "CUSTOM_API_KEY":
            api_url = vendor.get("proxy_target", "") or vendor.get("api_url", "")
            if api_url:
                env["CUSTOM_BASE_URL"] = api_url.rstrip("/")
            models = key.get("models") or []
            if models:
                mid = models[0]["id"] if isinstance(models[0], dict) else models[0]
                if mid:
                    env["CUSTOM_MODEL"] = mid
            dm = key.get("default_model", "")
            if dm:
                env["CUSTOM_MODEL"] = dm
        elif alias == "xai":
            dm = key.get("default_model", "")
            if dm:
                env["XAI_MODEL"] = dm
            elif key.get("models"):
                m0 = key["models"][0]
                env["XAI_MODEL"] = m0["id"] if isinstance(m0, dict) else m0
        elif alias == "azure":
            api_url = vendor.get("api_url", "")
            if api_url:
                env["AZURE_OPENAI_ENDPOINT"] = api_url.rstrip("/")

        self._save_env(env)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        env_var, alias = self._resolve_key_env(vendor)
        # Keep env if another active key of same provider remains
        from core.data import get_vendors
        for v in get_vendors():
            if (v.get("provider") or "").lower() != (vendor.get("provider") or "").lower():
                continue
            for k in v.get("keys", []):
                if k.get("id") == key.get("id"):
                    continue
                if k.get("enabled", True) and k.get("api_key"):
                    return

        env = self._load_env()
        env.pop(env_var, None)
        if env_var == "CUSTOM_API_KEY":
            env.pop("CUSTOM_BASE_URL", None)
            env.pop("CUSTOM_MODEL", None)
        if env.get("GROKCLI_PROVIDER") == alias:
            env.pop("GROKCLI_PROVIDER", None)
        self._save_env(env)

    def reconcile(self) -> None:
        from core.data import get_vendors
        # Pick the first enabled key to keep as active (single-slot CLI)
        chosen = None
        for v in get_vendors():
            for k in v.get("keys", []):
                if k.get("enabled", True) and k.get("api_key"):
                    chosen = (v, k)
                    break
            if chosen:
                break
        if chosen:
            self.on_key_added(chosen[0], chosen[1])
        else:
            # Clear known keys we manage
            env = self._load_env()
            changed = False
            for var in list(self._KEY_ENV.values()) + [
                "CUSTOM_API_KEY", "CUSTOM_BASE_URL", "CUSTOM_MODEL", "GROKCLI_PROVIDER", "XAI_MODEL",
            ]:
                if var in env:
                    del env[var]
                    changed = True
            if changed:
                self._save_env(env)

    def sync_from_backend(self) -> list[dict]:
        env = self._load_env()
        vendors = []
        rev = {}
        for prov, var in self._KEY_ENV.items():
            if var not in rev:
                rev[var] = prov
        if env.get("CUSTOM_API_KEY"):
            vendors.append({
                "name": "Custom (Grok CLI)",
                "provider": "custom",
                "api_url": env.get("CUSTOM_BASE_URL", ""),
                "endpoint_type": "openai",
                "keys": [{"name": f"from {self.name}", "api_key": env["CUSTOM_API_KEY"]}],
            })
        for var, prov in rev.items():
            api_key = env.get(var, "")
            if not api_key:
                continue
            if any(x["provider"] == prov for x in vendors):
                continue
            vendors.append({
                "name": prov.replace("-", " ").title(),
                "provider": prov,
                "api_url": "",
                "endpoint_type": "anthropic" if prov == "anthropic" else "openai",
                "keys": [{"name": f"from {self.name}", "api_key": api_key}],
            })
        return vendors

    def get_status(self) -> dict:
        from backends.base import make_status, cli_available

        env = self._load_env()
        key_vars = [v for v in set(self._KEY_ENV.values()) | {"CUSTOM_API_KEY"} if env.get(v)]
        installed, version = cli_available(("grok", "grok-cli"))
        if not installed and (self._env_path.exists() or self._config_dir.exists() or key_vars):
            installed = True
        if not installed:
            return make_status(installed=False, message="grok CLI not found")
        msg = f"{len(key_vars)} key(s) in .env"
        if env.get("GROKCLI_PROVIDER"):
            msg += f"; provider={env['GROKCLI_PROVIDER']}"
        return make_status(installed=True, running=False, version=version, message=msg)

    def get_version(self) -> str:
        for cmd in ("grok", "grok-cli"):
            try:
                r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
                return (r.stdout + r.stderr).strip().split("\n")[0][:80]
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return ""

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._env_path), "label": "Env Config", "type": "env"},
        ]

    def get_config_template(self) -> list[dict]:
        return [
            {"key": "env_path", "label": "Env File", "type": "text",
             "default": str(self._env_path),
             "help": "Path to ~/.grok-cli/.env (XAI_API_KEY / CUSTOM_API_KEY etc.)"},
        ]
