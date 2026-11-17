"""Experimental open-access paper loading and stable, paged section indexes."""
import re
from xml.etree import ElementTree as ET

import requests


BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def parse_article(xml):
    root = ET.fromstring(xml)
    title_node = root.find(".//article-title")
    title = " ".join(" ".join(title_node.itertext()).split()) if title_node is not None else ""
    blocks = []

    def visit(node, section):
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "sec":
            heading = node.find("title")
            if heading is not None:
                section = " / ".join([section, " ".join(heading.itertext())]).strip(" / ")
        if tag in {"p", "fig", "table-wrap"}:
            text = " ".join(" ".join(node.itertext()).split())
            if text:
                blocks.append((section, text))
            return
        for child in node:
            if child.tag.rsplit("}", 1)[-1] != "title":
                visit(child, section)

    for label, path in (("Abstract", ".//article-meta/abstract"), ("Body", "./body")):
        for node in root.findall(path):
            visit(node, label)
    if not any(section.startswith("Body") for section, _ in blocks):
        raise ValueError("No substantive article body returned")
    text, index = title + "\n", []
    for number, (section, paragraph) in enumerate(blocks):
        block = f"[{section}]\n{paragraph}\n\n"
        index.append({"paragraph_id": f"p{number}", "section": section, "offset": len(text),
                      "length": len(block), "preview": paragraph[:110]})
        text += block
    return {"text": text, "paragraph_index": index, "title": title}


def fetch_open_article(pmid):
    if not re.fullmatch(r"[0-9]+", pmid):
        raise ValueError("Expected a numeric PMID")
    response = requests.get(BASE + "/search", params={"query": f"EXT_ID:{pmid} AND SRC:MED",
                            "format": "json", "resultType": "core"}, timeout=30)
    response.raise_for_status()
    records = response.json().get("resultList", {}).get("result", [])
    record = next((r for r in records if str(r.get("id")) == pmid), None)
    pmcid = (record or {}).get("pmcid") or ""
    if not re.fullmatch(r"PMC[0-9]+", pmcid):
        raise ValueError("No PMC full-text identifier found; this does not mean the paper has no full text")
    url = f"{BASE}/{pmcid}/fullTextXML"
    response = requests.get(url, timeout=45)
    response.raise_for_status()
    result = parse_article(response.content)
    result.update(pmid=pmid, pmcid=pmcid, source_url=url)
    return result


def index_plain_text(text):
    index, position, section = [], 0, "Body (provider text; check section headings)"
    for block in text.split("\n\n"):
        if block.strip():
            heading = re.match(r"\[([^\]]+)\]", block)
            if heading:
                section = heading.group(1)
                # The provider adapter wraps original paragraphs in an outer
                # '[section / pN]' label. Preserve the actual parent section
                # from the following original heading, not the paragraph ID.
                nested = re.match(r"\[([^\]]+)\]\s*\n\[([^\]]+)\]", block)
                if nested and re.fullmatch(re.escape(nested.group(2)) + r" / p\d+", section):
                    section = nested.group(2)
            for local in range(0, len(block), 6000):
                piece = block[local:local + 6000]
                index.append({"paragraph_id": f"p{len(index)}", "section": section,
                              "offset": position + local, "length": len(piece), "preview": piece[:110],
                              "paragraph_fragment": len(block) > 6000,
                              "continues_at": position + local + len(piece) if local + len(piece) < len(block) else None})
        position += len(block) + 2
    return {"paragraph_index": index, "text": text}


def section_context_spans(index, selected, max_chars):
    """Expand a selected finding to its bounded, contiguous named section."""
    chosen = {p["paragraph_id"]: p for p in selected}
    positions = {p["paragraph_id"]: i for i, p in enumerate(index)}
    related = set()
    for paragraph in selected:
        section = paragraph["section"]
        if " / " not in section:
            continue  # Unstructured provider text is not one coherent section.
        left = right = positions[paragraph["paragraph_id"]]
        while left and index[left - 1]["section"] == section:
            left -= 1
        while right + 1 < len(index) and index[right + 1]["section"] == section:
            right += 1
        group = index[left:right + 1]
        related.update(p["paragraph_id"] for p in group)
        candidate = {**chosen, **{p["paragraph_id"]: p for p in group}}
        if (sum(p["length"] + 2 for p in group) <= min(12000, max_chars)
                and sum(p["length"] + 2 for p in candidate.values()) <= max_chars):
            chosen = candidate
    ordered = [p for p in index if p["paragraph_id"] in chosen]
    missing = [p["paragraph_id"] for p in index if p["paragraph_id"] in related - chosen.keys()]
    return ordered, missing
