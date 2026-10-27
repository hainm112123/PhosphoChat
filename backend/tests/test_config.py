"""Editing .env takes effect without a restart."""

from __future__ import annotations

import os
import time

from app import config


def test_secret_files(tmp_path, monkeypatch):
    secret = tmp_path / "groq_key"
    secret.write_text("gsk_from_file\n", encoding="utf-8")
    monkeypatch.setenv("PHOSPHO_SECRET_FILE", str(secret))
    monkeypatch.delenv("PHOSPHO_SECRET", raising=False)
    config._load_secret_files()
    assert os.environ["PHOSPHO_SECRET"] == "gsk_from_file"       # read and stripped

    monkeypatch.setenv("PHOSPHO_SECRET", "explicit")
    config._load_secret_files()
    assert os.environ["PHOSPHO_SECRET"] == "explicit"            # NAME set: the file is ignored

    monkeypatch.delenv("PHOSPHO_SECRET")
    monkeypatch.setenv("PHOSPHO_SECRET_FILE", str(tmp_path / "missing"))
    import pytest
    with pytest.raises(RuntimeError, match="PHOSPHO_SECRET_FILE"):
        config._load_secret_files()


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
