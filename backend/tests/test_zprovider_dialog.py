"""Add-provider dialog backend: model pick-lists for every provider type and testing settings before saving."""
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from app.services import ai_gateway, engine
from conftest import login


class FakeGet:
    """Stands in for httpx.get inside the gateway: answers by URL and records every request."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "params": params})
        for prefix, (status, body) in self.routes.items():
            if url.startswith(prefix):
                return httpx.Response(status, json=body, request=httpx.Request("GET", url))
        return httpx.Response(404, json={"message": "not found"}, request=httpx.Request("GET", url))


def use(monkeypatch, routes):
    fake = FakeGet(routes)
    monkeypatch.setattr(ai_gateway.httpx, "get", fake)
    return fake


def test_openai_gemini_and_anthropic_model_lists(monkeypatch):
    fake = use(monkeypatch, {
        "https://api.example.com/v1/models": (200, {"data": [{"id": "b-model"}, {"id": "a-model"}]}),
        "https://gem.example.com/v1beta/models": (200, {"models": [{"name": "models/gemini-x", "supportedGenerationMethods": ["generateContent"]},
                                                                 {"name": "models/embed-y", "supportedGenerationMethods": ["embedContent"]}]}),
        "https://api.anthropic.com/v1/models": (200, {"data": [{"id": "claude-x"}]}),
    })
    assert ai_gateway.list_models("openai_compatible", "https://api.example.com/v1", {"api_key": "k1"}) == ["a-model", "b-model"]
    assert fake.calls[-1]["headers"]["Authorization"] == "Bearer k1"
    assert ai_gateway.list_models("gemini", "https://gem.example.com/v1beta", {"api_key": "g"}) == ["gemini-x"]   # embeddings filtered out
    assert ai_gateway.list_models("anthropic", "", {"api_key": "a"}) == ["claude-x"]
    assert fake.calls[-1]["headers"]["x-api-key"] == "a"


def test_bedrock_lists_profiles_first_and_signs_with_sigv4(monkeypatch):
    fake = use(monkeypatch, {
        "https://bedrock.ap-south-1.amazonaws.com/foundation-models": (200, {"modelSummaries": [
            {"modelId": "amazon.nova-pro-v1:0", "inferenceTypesSupported": ["ON_DEMAND"]},
            {"modelId": "anthropic.claude-sonnet-4", "inferenceTypesSupported": ["INFERENCE_PROFILE"]}]}),
        "https://bedrock.ap-south-1.amazonaws.com/inference-profiles": (200, {"inferenceProfileSummaries": [{"inferenceProfileId": "apac.anthropic.claude-sonnet-4"}]}),
    })
    secret = {"access_key_id": "AKIATEST", "secret_access_key": "s3cret"}
    models = ai_gateway.list_models("bedrock", "https://bedrock-runtime.ap-south-1.amazonaws.com", secret)
    assert models == ["apac.anthropic.claude-sonnet-4", "amazon.nova-pro-v1:0"]      # profile-only base model is not offered directly
    call = fake.calls[0]
    u = urlsplit(call["url"])
    expected = engine.sigv4_authorization("GET", u.hostname, u.path, dict(parse_qsl(u.query)), b"", "AKIATEST", "s3cret",
                                          "ap-south-1", "bedrock", call["headers"]["x-amz-date"])
    assert call["headers"]["Authorization"] == expected


def test_bedrock_openai_endpoint_falls_back_to_the_bedrock_catalogue(monkeypatch):
    fake = use(monkeypatch, {"https://bedrock.ap-south-1.amazonaws.com/foundation-models": (200, {"modelSummaries": [
        {"modelId": "openai.gpt-oss-120b-1:0", "inferenceTypesSupported": ["ON_DEMAND"]}]})})
    models = ai_gateway.list_models("openai_compatible", "https://bedrock-runtime.ap-south-1.amazonaws.com/openai/v1", {"api_key": "bedrock-api-key"})
    assert models == ["openai.gpt-oss-120b-1:0"]
    assert fake.calls[-1]["headers"]["Authorization"] == "Bearer bedrock-api-key"


def test_provider_errors_are_reported(monkeypatch):
    use(monkeypatch, {"https://api.example.com/v1/models": (401, {"error": "invalid api key"})})
    with pytest.raises(ai_gateway.ProviderError, match="401"):
        ai_gateway.list_models("openai_compatible", "https://api.example.com/v1", {"api_key": "bad"})


def test_settings_can_be_tested_before_saving(server, monkeypatch):
    owner, admin = login(server, "owner"), login(server, "admin")
    seen = {}

    def fake_adapter(base, key, model, system, messages, json_mode, timeout):
        seen.update(base=base, key=key, model=model)
        if key != "good-key":
            raise ai_gateway.ProviderError("HTTP 401: invalid api key", retryable=False)
        return "ready", 5, 1

    monkeypatch.setitem(ai_gateway.ADAPTERS, "openai_compatible", fake_adapter)
    monkeypatch.setenv("HEDES_TEST_AI_KEY", "good-key")
    draft = {"provider_type": "openai_compatible", "base_url": "https://api.example.com/v1", "api_key": "good-key", "model_id": "gpt-x"}
    before = len(owner.get("/api/v1/ai/providers").json())

    r = owner.post("/api/v1/ai/providers/test-config", json=draft).json()
    assert r["ok"] is True and r["reply"] == "ready" and seen == {"base": "https://api.example.com/v1", "key": "good-key", "model": "gpt-x"}
    assert owner.post("/api/v1/ai/providers/test-config", json={**draft, "api_key": "env:HEDES_TEST_AI_KEY"}).json()["ok"] is True
    bad = owner.post("/api/v1/ai/providers/test-config", json={**draft, "api_key": "wrong"}).json()
    assert bad["ok"] is False and "401" in bad["error"]
    assert owner.post("/api/v1/ai/providers/test-config", json={**draft, "model_id": ""}).status_code == 422
    assert admin.post("/api/v1/ai/providers/test-config", json=draft).status_code == 403                 # owner only, like adding
    assert len(owner.get("/api/v1/ai/providers").json()) == before                                        # nothing was saved
