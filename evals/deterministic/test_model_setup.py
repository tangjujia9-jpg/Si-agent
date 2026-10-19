"""Local launch configuration must not expose backend keys to Vite."""

import sys
from pathlib import Path

import pytest

from scripts import run_copilot
from waku.providers.catalog import configured_catalog
from waku.providers.contracts import ModelRef


def test_catalog_requires_explicit_vision_configuration():
    values = {"SI_COMPATIBLE_MODEL": "fixture"}
    ref = ModelRef("openai-compatible", "fixture")
    assert not configured_catalog(values).get(ref).capabilities.vision
    values.update(SI_COMPATIBLE_VISION="true", SI_COMPATIBLE_STREAMING="false")
    cap = configured_catalog(values).get(ref).capabilities
    assert cap.vision and not cap.streaming
    assert "API_KEY" not in str(configured_catalog(values).public())


def test_web_process_never_receives_backend_or_vite_secrets(tmp_path, monkeypatch):
    config = tmp_path / "explicit.env"
    config.write_text("SI_COMPATIBLE_API_KEY=private-backend-key\nSI_DATABASE_URL=private-db\n", encoding="utf-8")
    monkeypatch.setenv("VITE_API_KEY", "inherited-vite-secret")
    monkeypatch.setenv("SI_EMBEDDING_API_KEY", "inherited-embedding-secret")
    monkeypatch.setattr(sys, "argv", ["launcher", "web", "--config", str(config)])
    monkeypatch.setattr(run_copilot.shutil, "which", lambda name: "node")
    captured = {}

    def launch(command, *, cwd, env):
        captured.update(command=command, cwd=cwd, env=env)
        return 0

    monkeypatch.setattr(run_copilot.subprocess, "call", launch)
    with pytest.raises(SystemExit) as result:
        run_copilot.main()
    assert result.value.code == 0
    assert captured["cwd"] == run_copilot.ROOT / "apps/web"
    assert captured["env"]["SI_API_URL"] == "http://127.0.0.1:8016"
    assert not any(k.startswith(("SI_", "VITE_")) for k in captured["env"] if k != "SI_API_URL")
    assert "private-backend-key" not in captured["env"].values()
    assert Path(captured["command"][1]).as_posix() == "node_modules/vite/bin/vite.js"
