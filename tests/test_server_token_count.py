import json

import pytest

from medical_qa_data_factory.llm.context import ContextManager, estimate_tokens
from medical_qa_data_factory.llm.errors import ModelError
from medical_qa_data_factory.llm.providers.openai_compatible import OpenAICompatibleModel, _compact_context_payload
from test_llm_openai_adapter import _config


def test_server_counts_chat_template_not_decoder_grammar_and_caches():
    calls = []
    def transport(url, headers, payload, timeout):
        calls.append(payload)
        return 200, {}, json.dumps({"count": 120, "max_model_len": 32768})
    config = _config("http://localhost/v1/chat/completions", auth_mode="none", tokenization_url="http://localhost/tokenize")
    model = OpenAICompatibleModel(config=config, transport=transport)
    body = {"model": "test", "messages": [{"role": "user", "content": "Hello"}],
            "response_format": {"schema": "x" * 100000}, "chat_template_kwargs": {"enable_thinking": False},
            "max_tokens": 4096}
    manager = ContextManager(config, model._count_input_tokens)
    _, _, report = manager.prepare(body, lambda x: x, lambda x, **_: x)
    assert report["estimated_before"] == 120 and report["estimator"] == "server_tokenizer"
    assert report["input_budget"] == 27648
    assert model._count_input_tokens(body)[0] == 120 and len(calls) == 1
    assert calls[0]["chat_template_kwargs"] == body["chat_template_kwargs"]
    assert calls[0]["add_generation_prompt"] is True
    assert "response_format" not in calls[0] and "max_tokens" not in calls[0]
    body["messages"][0]["content"] = "Changed"
    model._count_input_tokens(body)
    assert len(calls) == 2


@pytest.mark.parametrize("status,result", [(404, {}), (200, []), (200, {"count": True}), (200, {"count": -1})])
def test_unavailable_tokenizer_falls_back_without_repeated_network_requests(status, result):
    calls = []
    def transport(*args):
        calls.append(args)
        return status, {}, json.dumps(result)
    config = _config("http://localhost/v1/chat/completions", auth_mode="none", tokenization_url="http://localhost/tokenize")
    model = OpenAICompatibleModel(config=config, transport=transport)
    body = {"messages": [{"role": "user", "content": "Hello"}]}
    assert model._count_input_tokens(body) == (estimate_tokens(body), "heuristic_ascii3_unicode2")
    model._count_input_tokens(body)
    assert len(calls) == 1


def test_exact_count_still_enforces_total_window():
    config = _config("http://localhost", auth_mode="none")
    manager = ContextManager(config, lambda body: (28000, "server_tokenizer"))
    with pytest.raises(ModelError, match="context budget exceeded"):
        manager.prepare({"max_tokens": 4096}, lambda x: x, lambda x, **_: x)


def test_compaction_does_not_erase_react_execution_history():
    payload = {"control_mode": "objective-react-v1", "prior_searches": [
        {"action": "retrieve_materials", "sequence": 1, "parameters": {"query": {"mention": "EGFR"}}},
        {"action": "read_artifacts", "sequence": 2, "result_ids": ["read"]}]}
    assert _compact_context_payload(payload, round_number=2)["prior_searches"] == payload["prior_searches"]
