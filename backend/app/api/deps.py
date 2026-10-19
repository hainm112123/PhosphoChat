from __future__ import annotations

from fastapi import HTTPException

from app.config import get_settings
from app.vault.store import Vault, VaultRegistry

_registry: VaultRegistry | None = None


def registry() -> VaultRegistry:
    global _registry
    if _registry is None:
        _registry = VaultRegistry(get_settings().vaults_path)
    return _registry


def get_vault(vault: str) -> Vault:
    try:
        return registry().get(vault)
    except KeyError:
        raise HTTPException(404, f"No vault '{vault}'.") from None
