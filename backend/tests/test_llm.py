import json
import httpx
from llm import LLMCategorizer


def make(provider, reply_text, calls):
    def handler(request):
        calls.append(json.loads(request.content))
        if provider == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": reply_text}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": reply_text}}]})
    return httpx.MockTransport(handler)


ITEMS = [{"key": "a", "payee": "Zhang Liang Malatang", "description": "ZHANG LIANG", "is_credit": False},
         {"key": "b", "payee": "Mystery Co", "description": "MYSTERY", "is_credit": False}]
REPLY = '```json\n{"results":[{"id":"0","category":"dining & hawker","confidence":0.95},{"id":"1","category":"Made Up","confidence":0.9}]}\n```'


def test_openai_compatible_validates_and_caches(tmp_path):
    calls = []
    llm = LLMCategorizer("ollama", "qwen2.5:7b", "http://x/v1", "", make("openai", REPLY, calls),
                         cache_file=tmp_path / "c.json")
    out = llm.categorize(ITEMS, ["Dining & Hawker", "Groceries"])
    assert out == {"a": {"category": "Dining & Hawker", "confidence": 0.75, "cached": False}}  # capped, case-fixed
    assert "b" not in out                                                                     # not in allowed list
    assert calls[0]["model"] == "qwen2.5:7b"
    again = llm.categorize(ITEMS, ["Dining & Hawker", "Groceries"])
    assert again["a"]["cached"] and len(calls) == 1                                           # both answered from cache


def test_anthropic(tmp_path):
    calls = []
    llm = LLMCategorizer("anthropic", "claude-haiku-4-5", None, "k", make("anthropic", REPLY, calls),
                         cache_file=tmp_path / "c.json")
    assert llm.categorize(ITEMS[:1], ["Dining & Hawker"])["a"]["category"] == "Dining & Hawker"
    assert calls[0]["model"] == "claude-haiku-4-5"


def test_disabled_by_default():
    assert not LLMCategorizer("none").enabled
    assert not LLMCategorizer("openai", api_key="").enabled
