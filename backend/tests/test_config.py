"""Editing .env takes effect without a restart."""

from __future__ import annotations

import os
import time

from app import config


def test_env_changes_are_picked_up(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MODEL=groq:first\nPHOSPHO_TEST_KEY=1\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setattr(config, "_settings", None)
    # A stale copy in the environment (what VS Code's useEnvFile leaves in a terminal) must not win.
    monkeypatch.setenv("MODEL", "groq:stale-from-terminal")
    try:
        assert config.get_settings().model == "groq:first"
        assert os.environ["PHOSPHO_TEST_KEY"] == "1"

        time.sleep(0.01)
        env.write_text("MODEL=groq:second\n", encoding="utf-8")
        os.utime(env, (time.time() + 5, time.time() + 5))  # make sure the mtime differs
        assert config.get_settings().model == "groq:second"
        assert "PHOSPHO_TEST_KEY" not in os.environ          # removed from .env -> unset
    finally:
        os.environ.pop("PHOSPHO_TEST_KEY", None)
        config._env_keys.discard("PHOSPHO_TEST_KEY")
        monkeypatch.setattr(config, "_settings", None)  # next caller reloads the real .env
