import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from medical_qa_data_factory.generation import fulltext_download as module


def config(tmp_path):
    return {"enabled": True, "executable": "downloader", "output_dir": str(tmp_path),
            "credentials": {"ELSEVIER_API_KEY": "test-secret"}}


def test_failure_preserves_channel_reason_and_redacts_key(tmp_path, monkeypatch):
    def run(command, **kwargs):
        assert kwargs["env"]["ELSEVIER_API_KEY"] == "test-secret"
        return SimpleNamespace(returncode=1, stdout=json.dumps([{"success": False,
            "error": "elsevier rejected test-secret"}]), stderr="test-secret")
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(ValueError, match="elsevier rejected") as exc:
        module.download_article("10.1/example", config=config(tmp_path))
    assert "test-secret" not in str(exc.value)
    assert all("test-secret" not in p.read_text() for p in tmp_path.rglob("*.log"))


def test_pdf_is_extracted_and_indexed(tmp_path, monkeypatch):
    def run(command, **kwargs):
        if "-layout" in command:
            return SimpleNamespace(stdout=b"Study title\n\nResults\nMeasured results.")
        folder = Path(command[command.index("-o") + 1])
        pdf = folder / "paper.pdf"
        pdf.write_bytes(b"%PDF")
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps([
            {"success": True, "path": str(pdf), "source": "plos"}]))
    monkeypatch.setattr(module.subprocess, "run", run)
    article = module.download_article("10.1/example", config=config(tmp_path))
    assert "Measured results" in article["text"]
    assert article["paragraph_index"]
    assert article["download_provenance"]["source"] == "plos"


def test_timeout_is_explicit(tmp_path, monkeypatch):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(TimeoutError, match="180"):
        module.download_article("10.1/example", config=config(tmp_path))
    assert next(tmp_path.rglob("result.json")).exists()


def test_pmc_success_does_not_call_downloader(monkeypatch):
    from medical_qa_data_factory.generation import research_documents
    monkeypatch.setattr(research_documents, "fetch_open_article", lambda pmid: {"text": "PMC body"})
    monkeypatch.setattr(module, "download_article", lambda *a, **kw: pytest.fail("unnecessary download"))
    assert module.fetch_article_with_fallback("123")["text"] == "PMC body"


@pytest.mark.parametrize("malformed_xml", [False, True])
def test_pmc_failure_uses_resolved_doi(monkeypatch, malformed_xml):
    if malformed_xml:
        from medical_qa_data_factory.generation import research_documents
        def malformed(pmid):
            raise module.ParseError("invalid XML")
        monkeypatch.setattr(research_documents, "fetch_open_article", malformed)
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
        "resultList": {"result": [{"id": "123", "pmcid": None,
                                   "doi": "10.1/example", "title": "Study"}]}})
    monkeypatch.setattr(module.requests, "get", lambda *a, **kw: response)
    monkeypatch.setattr(module, "load_config", lambda: {"enabled": True})
    def download(doi, **kwargs):
        assert doi == "10.1/example"
        return {"text": "Body", "paragraph_index": []}
    monkeypatch.setattr(module, "download_article", download)
    assert module.fetch_article_with_fallback("123")["text"] == "Body"
