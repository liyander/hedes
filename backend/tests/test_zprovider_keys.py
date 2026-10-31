"""Pasted provider keys: surrounding whitespace is trimmed; URLs and embedded tabs/newlines are rejected with a clear message."""
import httpx
import pytest

from app.api.ai import clean_secret
from app.services import ai_gateway
from conftest import login


def test_clean_secret():
    assert clean_secret("  AIzaSyExample123 \n") == "AIzaSyExample123"
    assert clean_secret("env:GEMINI_API_KEY") == "env:GEMINI_API_KEY"
    for bad, words in (("\thttps://generativelanguage.googleapis.com/v1beta/", "URL"), ("AIza abc", "spaces"), ("key\tmore", "tabs")):
        with pytest.raises(ValueError) as e:
            clean_secret(bad)
        assert words in str(e.value)


def test_dialog_endpoints_explain_a_bad_key(server):
    owner = login(server, "owner")
    r = owner.post("/api/v1/ai/providers/discover-models", json={"provider_type": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/",
                                                                   "api_key": "\thttps://generativelanguage.googleapis.com/v1beta/"})
    assert r.status_code == 422 and "looks like a URL" in r.text
    r = owner.post("/api/v1/ai/providers", json={"name": "G", "provider_type": "gemini", "api_key": "abc\ndef", "model_id": "gemini-2.5-flash"})
    assert r.status_code == 422 and "line breaks" in r.text


def test_a_bad_header_already_saved_gets_a_readable_error(monkeypatch):
    def boom(*a, **k):
        raise httpx.LocalProtocolError("Illegal header value b'\\thttps://x'")
    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(ai_gateway.ProviderError) as e:
        ai_gateway.list_models("gemini", "https://generativelanguage.googleapis.com/v1beta", {"api_key": "x"})
    assert "Paste only the key" in str(e.value) and e.value.retryable is False
