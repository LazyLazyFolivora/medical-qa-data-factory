import runpy
import sys

import pytest


def test_package_module_invokes_the_cli(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["medical_qa_data_factory", "generate", "--help"],
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("medical_qa_data_factory", run_name="__main__")

    assert excinfo.value.code == 0
    assert "validated" in capsys.readouterr().out
