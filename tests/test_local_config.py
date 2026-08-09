"""Local Compose credential generation."""

from __future__ import annotations

import stat

import pytest
from scripts.init_local import generate_local_config

from atlas.security import hash_key


def test_local_config_generates_separate_untracked_roles(tmp_path):
    (tmp_path / "observability").mkdir()
    env_path, token_path = generate_local_config(tmp_path)

    values = dict(
        line.split("=", 1)
        for line in env_path.read_text().splitlines()
        if line and not line.startswith("#")
    )
    analyst_hash = hash_key(values["ATLAS_ANALYST_KEY"])
    viewer_hash = hash_key(values["ATLAS_VIEWER_KEY"])
    assert values["ATLAS_API_KEYS"].strip("'") == (
        f"local-analyst:{analyst_hash}:analyst;local-metrics:{viewer_hash}:viewer"
    )
    assert token_path.read_text().strip() == values["ATLAS_VIEWER_KEY"]
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_local_config_refuses_to_overwrite_existing_credentials(tmp_path):
    (tmp_path / "observability").mkdir()
    (tmp_path / ".env").write_text("existing=true\n")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        generate_local_config(tmp_path)
