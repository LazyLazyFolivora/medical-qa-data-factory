"""Small checks for the experiment's source presentation and targeted lookup."""
from medical_qa_data_factory.graph_question_pilot import Search, execute, present_materials
from medical_qa_data_factory.evidence_enrichment.pubmed import PubMedFetchResult


def test_graph_sampling_preserves_raw_material_and_reports_omissions():
    edges = [{"relation": "protein_protein", "head": "A", "tail": f"B{i}"}
             for i in range(30)]
    edges.append({"relation": "bioprocess_protein", "head": "A", "tail": "process"})
    source = {"graph": {"neighbors": edges, "truncated": True}}

    shown = present_materials(source)["graph"]

    assert len(source["graph"]["neighbors"]) == 31
    assert shown["truncated"] is True
    assert shown["neighbor_samples"]["protein_protein"]["omitted_from_prompt"] == 6
    assert shown["neighbor_samples"]["bioprocess_protein"]["examples"] == ["A -> process"]


def test_cited_pmid_is_fetched_without_substituting_a_search():
    class Client:
        def fetch_result(self, pmids, *, limit):
            assert pmids == ["12345"]
            assert limit == 3
            return PubMedFetchResult(status="no_abstract", snapshot_id="fixture",
                                     requested_pmids=pmids, no_abstract_pmids=pmids)

    result = execute(Search(tool="pubmed_fetch", query="12345", purpose="Read cited abstract"),
                     graph=None, client=Client())

    assert result["material"]["fetch"]["status"] == "no_abstract"
    assert result["material"]["fetch"]["records"] == []
