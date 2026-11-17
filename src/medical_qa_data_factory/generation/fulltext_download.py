"""Optional, process-isolated fulltext-article-downloader fallback."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
from xml.etree.ElementTree import ParseError

import requests

from .research_documents import BASE, index_plain_text, parse_article

ROOT = Path(__file__).resolve().parents[3]
KEYS = ("UNPAYWALL_EMAIL", "ELSEVIER_API_KEY", "WILEY_API_KEY",
        "SPRINGER_API_KEY", "SEMANTIC_SCHOLAR_API_KEY")


def load_config():
    path = Path(os.environ.get("MEDICAL_QA_FULLTEXT_CONFIG", ROOT / "config/fulltext.json"))
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def download_article(doi, *, config=None):
    """Return stored text/index plus provenance, or an actionable failure."""
    config = load_config() if config is None else config
    if not config.get("enabled"):
        raise ValueError("Fulltext downloader disabled; configure config/fulltext.json")
    executable = Path(config["executable"])
    if not executable.is_absolute():
        executable = ROOT / executable
    directory = Path(config.get("output_dir", "data/cache/fulltext-downloads"))
    if not directory.is_absolute():
        directory = ROOT / directory
    # Separate attempts prevent concurrent runs from writing the same partial PDF.
    directory = directory / hashlib.sha256(doi.encode()).hexdigest()[:16] / uuid.uuid4().hex
    directory.mkdir(parents=True)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    for key in KEYS:
        env[key] = str(config.get("credentials", {}).get(key) or env.get(key, ""))
    command = [str(executable), doi, "-o", str(directory),
               "--log-file", str(directory / "attempts.log")]
    timeout = float(config.get("timeout_seconds", 180))
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", env=env, timeout=timeout,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        log = directory / "attempts.log"
        if log.exists():
            value = log.read_text(encoding="utf-8", errors="replace")
            for key in KEYS:
                if env[key]:
                    value = value.replace(env[key], "[REDACTED]")
            log.write_text(value, encoding="utf-8")
        (directory / "result.json").write_text(json.dumps({"status": "timeout", "doi": doi,
            "timeout_seconds": timeout}), encoding="utf-8")
        raise TimeoutError(f"Fulltext download exceeded {timeout}s; trace: {directory}") from exc
    # API keys may be echoed in remote errors: scrub before persistence or reporting.
    def scrub(value):
        for key in KEYS:
            if env[key]:
                value = value.replace(env[key], "[REDACTED]")
        return value
    for name, value in (("stdout.log", completed.stdout), ("stderr.log", completed.stderr)):
        (directory / name).write_text(scrub(value), encoding="utf-8")
    log = directory / "attempts.log"
    if log.exists():
        log.write_text(scrub(log.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
    try:
        result = json.loads(completed.stdout)[0]
    except (ValueError, IndexError, TypeError) as exc:
        raise ValueError(f"Invalid downloader response; trace: {directory}") from exc
    result = json.loads(scrub(json.dumps(result)))
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if completed.returncode or not result.get("success"):
        raise ValueError(f"Fulltext download failed: {result.get('error')}; trace: {directory}")
    path = Path(result["path"]).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Downloader returned a path outside its attempt directory")
    if path.suffix.lower() == ".xml":
        article = parse_article(path.read_bytes())
    elif path.suffix.lower() == ".pdf":
        extracted = subprocess.run([config.get("pdftotext", "pdftotext"), "-layout", str(path), "-"],
                                   capture_output=True, timeout=60, check=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = extracted.stdout.decode("utf-8", errors="replace").strip()
        if not text:
            raise ValueError("Downloaded PDF has no extractable text; OCR is not configured")
        article = {"text": text, **index_plain_text(text)}
    else:
        raise ValueError(f"Unsupported fulltext format: {path.suffix}")
    article["download_provenance"] = {"doi": doi, "path": str(path), "source": result.get("source"),
                                      "note": result.get("note"), "trace_dir": str(directory)}
    return article


def fetch_article_with_fallback(pmid):
    from .research_documents import fetch_open_article
    try:
        return fetch_open_article(pmid)
    except (requests.RequestException, ValueError, ParseError) as primary:
        config = load_config()
        if not config.get("enabled"):
            raise
        response = requests.get(BASE + "/search", params={"query": f"EXT_ID:{pmid} AND SRC:MED",
                                "format": "json", "resultType": "core"}, timeout=30)
        response.raise_for_status()
        record = next((r for r in response.json().get("resultList", {}).get("result", [])
                       if str(r.get("id")) == str(pmid)), {})
        if not record.get("doi"):
            raise ValueError(f"PMC failed ({primary}); no DOI found for PMID {pmid}") from primary
        article = download_article(record["doi"], config=config)
        article.update(pmid=pmid, title=record.get("title", ""))
        return article
