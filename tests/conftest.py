"""Shared fixtures. Everything runs offline against the scripted model."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import Settings
from atlas.memory.hub import MemoryHub
from atlas.security import ApiKeyAuthenticator, Role, hash_key
from atlas.tools.repo import RepoToolkit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "repos"

ANALYST_KEY = "test-analyst-key"
VIEWER_KEY = "test-viewer-key"
ADMIN_KEY = "test-admin-key"


@pytest.fixture
def settings() -> Settings:
    return Settings(provider="scripted", memory_enabled=True)


@pytest.fixture
def legacy_root() -> Path:
    return FIXTURES / "legacy-billing"


@pytest.fixture
def modern_root() -> Path:
    return FIXTURES / "modern-payments"


@pytest.fixture
def toolkit(legacy_root: Path) -> RepoToolkit:
    return RepoToolkit(legacy_root)


@pytest.fixture
def memory() -> MemoryHub:
    return MemoryHub(enabled=True)


@pytest.fixture
def authenticator() -> ApiKeyAuthenticator:
    return ApiKeyAuthenticator(
        {
            "analyst": (hash_key(ANALYST_KEY), (Role.ANALYST,)),
            "viewer": (hash_key(VIEWER_KEY), (Role.VIEWER,)),
            "admin": (hash_key(ADMIN_KEY), (Role.ADMIN,)),
        }
    )


@pytest.fixture
def auth_headers() -> dict[str, dict[str, str]]:
    return {
        "analyst": {"authorization": f"Bearer {ANALYST_KEY}"},
        "viewer": {"authorization": f"Bearer {VIEWER_KEY}"},
        "admin": {"authorization": f"Bearer {ADMIN_KEY}"},
    }
