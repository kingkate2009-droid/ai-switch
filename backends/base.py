from typing import Optional

from core.data import get_backend_config


class BackendAdapter:
    """Base class for backend adapters (AI gateways, agent platforms, etc.)."""

    name = "base"
    display_name = "Base"

    @property
    def supports_byok(self) -> bool:
        """Whether this backend supports custom API key injection."""
        return True

    def should_sync(self, vendor: dict, key: dict) -> bool:
        """Check if a vendor/key should be synced to this backend.

        Consults per-backend config stored in data.backends.<name>.sync_vendors.
        - "all" or missing => sync all vendors
        - list => only sync vendors whose provider or id is in the list
        """
        config = get_backend_config(self.name)
        if config.get("disabled"):
            return False
        sync = config.get("sync_vendors", "all")
        if not isinstance(sync, list):
            return True
        return vendor.get("provider") in sync or vendor.get("id") in sync

    def on_key_added(self, vendor: dict, key: dict) -> None:
        pass

    def on_key_updated(self, vendor: dict, key: dict) -> None:
        pass

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
        return {"enabled": False, "running": False}

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
