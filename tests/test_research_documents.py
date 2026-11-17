import pytest
from medical_qa_data_factory.generation.research_documents import parse_article


def test_indexes_results_figures_and_methods_without_duplicate_paragraphs():
    xml = """<article><front><article-meta><title-group><article-title>Fc <italic>study</italic></article-title></title-group>
    <abstract><p>Summary.</p></abstract></article-meta></front><body>
    <sec><title>Results</title><p>Fc silent control reduced inhibition.</p><fig><label>Fig 1</label><caption><p>Model X.</p></caption></fig>
    <sec><title>Depletion</title><p>Macrophage removal.</p></sec></sec>
    <sec><title>Methods</title><p>Mouse model.</p></sec></body></article>"""
    doc = parse_article(xml)
    assert doc["title"] == "Fc study"
    assert doc["text"].count("Model X.") == 1
    assert any(p["section"] == "Body / Results / Depletion" for p in doc["paragraph_index"])
    for paragraph in doc["paragraph_index"]:
        text = doc["text"][paragraph["offset"]:paragraph["offset"]+paragraph["length"]]
        assert paragraph["preview"] in text


def test_abstract_only_is_not_full_text():
    with pytest.raises(ValueError, match="No substantive"):
        parse_article("<article><front><article-meta><abstract><p>Summary</p></abstract></article-meta></front></article>")
