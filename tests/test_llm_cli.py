"""The model diagnostics command must never reveal a secret."""
from __future__ import annotations

import json
from pathlib import Path

from medical_qa_data_factory.cli import main


def _config(tmp_path: Path, **overrides) -> Path:
    payload = {
        "default_provider": "local",
        "providers": {
            "local": {
                "driver": "openai_compatible",
                "base_url": "http://127.0.0.1:1/v1/chat/completions",
                "model_id": "test-model",
                "api_key_env": "TEST_MODEL_KEY",
            }
        },
    }
    payload.update(overrides)
    path = tmp_path / "models.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_providers_command_lists_the_configuration(tmp_path, capsys) -> None:
    path = _config(tmp_path)
    code = main(["model", "providers", "--config", str(path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["default_provider"] == "local"
    assert payload["providers"][0]["model_id"] == "test-model"


def test_the_providers_command_never_prints_the_key(
    tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "super-secret-value")
    path = _config(tmp_path)
    main(["model", "providers", "--config", str(path)])
    out = capsys.readouterr().out
    assert "super-secret-value" not in out
    # It reports the variable name and whether it is present, nothing more.
    assert "TEST_MODEL_KEY" in out
    assert "api_key_present" in out


def test_a_missing_configuration_is_a_config_error(tmp_path, capsys) -> None:
    code = main(["model", "providers", "--config", str(tmp_path / "absent.json")])
    err = json.loads(capsys.readouterr().err)
    assert code == 3
    assert err["error"]["code"] == "model_config_error"