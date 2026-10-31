"""AI provider configuration: presets, Amazon Bedrock (Converse + Signature V4), model discovery, assistant over Bedrock."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote

import pytest

from app.core.db import session_scope
from app.models import AIModel, AIProvider
from app.services import ai_gateway, engine, vault
from conftest import login

AK, SK, TOKEN, REGION = "AKIATESTBEDROCK", "secret/key+test", "session-token-xyz", "eu-west-1"


class Bedrock:
    """Behaves like bedrock-runtime: verifies Signature V4 the way AWS does and answers the Converse API."""
    def __init__(self):
        self.seen: list[dict] = []
        self.fail: int | None = None
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # OpenAI-style model listing for the discovery test
                out = json.dumps({"data": [{"id": "model-b"}, {"id": "model-a"}]}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                rec = {"path": self.path, "body": json.loads(body), "auth": self.headers.get("Authorization", ""), "token": self.headers.get("x-amz-security-token")}
                outer.seen.append(rec)
                if outer.fail:
                    out = json.dumps({"message": "User is not authorized: AccessDeniedException"}).encode()
                    self.send_response(outer.fail); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
                    return
                if rec["auth"].startswith("Bearer "):
                    ok = rec["auth"] == "Bearer br-api-key"
                else:
                    region = rec["auth"].split("Credential=")[1].split("/")[2]           # the scope the client signed for
                    expected = engine.sigv4_authorization("POST", self.headers["Host"], quote(self.path, safe="/-_.~"), {}, body, AK, SK, region, "bedrock",
                                                          self.headers["x-amz-date"], TOKEN)
                    ok = rec["auth"] == expected
                if not ok:
                    out = json.dumps({"message": "The request signature we calculated does not match"}).encode()
                    self.send_response(403); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
                    return
                scripted = outer.reply or {"reply": "ok"}
                out = json.dumps({"output": {"message": {"role": "assistant", "content": [{"text": json.dumps(scripted)}]}},
                                  "usage": {"inputTokens": 11, "outputTokens": 7}}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.reply = None
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture(scope="module")
def br():
    b = Bedrock()
    yield b
    b.srv.shutdown()


KEYS = json.dumps({"access_key_id": AK, "secret_access_key": SK, "session_token": TOKEN, "region": REGION})
MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"


def test_bedrock_converse_request_is_signed_and_shaped(br):
    br.seen.clear()
    text, tin, tout = ai_gateway._call_bedrock(br.url, KEYS, MODEL, "SYS", [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}, {"role": "assistant", "content": "c"},
                                                                          {"role": "user", "content": "d"}], False, 10)
    assert json.loads(text) == {"reply": "ok"} and (tin, tout) == (11, 7)
    req = br.seen[-1]
    assert req["path"] == "/model/anthropic.claude-3-5-sonnet-20241022-v2%3A0/converse"       # ':' is percent-encoded in the URL
    assert req["body"]["system"] == [{"text": "SYS"}] and req["body"]["inferenceConfig"]["maxTokens"] == 4096
    assert [m["role"] for m in req["body"]["messages"]] == ["user", "assistant", "user"]       # consecutive user turns merged: roles alternate
    assert req["body"]["messages"][0]["content"][0]["text"] == "a\n\nb"
    assert req["token"] == TOKEN and "SignedHeaders=host;x-amz-date;x-amz-security-token" in req["auth"]


def test_bedrock_inference_profile_ids_and_bearer_keys(br):
    ai_gateway._call_bedrock(br.url, KEYS, "us.anthropic.claude-3-7-sonnet-20250219-v1:0", "s", [{"role": "user", "content": "x"}], False, 10)
    assert br.seen[-1]["path"].endswith("us.anthropic.claude-3-7-sonnet-20250219-v1%3A0/converse")
    text, *_ = ai_gateway._call_bedrock(br.url, json.dumps({"bearer_token": "br-api-key", "region": REGION}), MODEL, "s", [{"role": "user", "content": "x"}], False, 10)
    assert br.seen[-1]["auth"] == "Bearer br-api-key" and text


def test_bedrock_errors_are_actionable(br):
    with pytest.raises(ai_gateway.ProviderError) as e:
        ai_gateway._call_bedrock(br.url, json.dumps({"access_key_id": AK, "secret_access_key": "wrong", "region": REGION}), MODEL, "s", [{"role": "user", "content": "x"}], False, 10)
    assert "403" in str(e.value) and "Model access" in str(e.value)
    with pytest.raises(ai_gateway.ProviderError) as e:
        ai_gateway._call_bedrock(br.url, "{}", MODEL, "s", [{"role": "user", "content": "x"}], False, 10)
    assert "No AWS credentials" in str(e.value) and e.value.retryable is False


def test_region_comes_from_the_endpoint_host():
    assert ai_gateway.bedrock_region("https://bedrock-runtime.ap-southeast-2.amazonaws.com", {}) == "ap-southeast-2"
    assert ai_gateway.bedrock_region("", {"region": "eu-west-1"}) == "eu-west-1"


def test_presets_cover_the_common_endpoints(server, builder):
    presets = {p["id"]: p for p in builder.get("/api/v1/ai/provider-presets").json()}
    assert {"bedrock", "nvidia", "groq", "xai", "gemini", "openai", "anthropic", "openrouter", "ollama", "custom"} <= set(presets)
    assert presets["nvidia"]["base_url"] == "https://integrate.api.nvidia.com/v1" and presets["groq"]["provider_type"] == "openai_compatible"
    assert presets["bedrock"]["provider_type"] == "bedrock" and presets["xai"]["base_url"] == "https://api.x.ai/v1"


def test_register_rotate_and_remove_a_bedrock_provider(server):
    owner = login(server, "owner")
    assert owner.post("/api/v1/ai/providers", json={"name": "B", "provider_type": "bedrock", "region": "eu-west-1", "model_id": MODEL}).status_code == 422   # no credentials
    assert owner.post("/api/v1/ai/providers", json={"name": "B", "provider_type": "bedrock", "region": "mars", "api_key": "k", "model_id": MODEL}).status_code == 422
    r = owner.post("/api/v1/ai/providers", json={"name": "Bedrock EU", "provider_type": "bedrock", "region": "eu-west-1", "aws_access_key_id": AK,
                                                  "aws_secret_access_key": SK, "model_id": MODEL, "purposes": ["general", "assistant", "config_generation"]})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["region"] == "eu-west-1" and p["base_url"] == "https://bedrock-runtime.eu-west-1.amazonaws.com" and p["has_key"]
    assert SK not in json.dumps(p) and p["models"][0]["purposes"] == ["general", "assistant", "config_generation"]
    assert owner.patch(f"/api/v1/ai/providers/{p['id']}", json={"region": "us-west-2", "api_key": "new-bedrock-key"}).json()["region"] == "us-west-2"
    with session_scope() as db:
        assert vault.read_secret(db, db.get(AIProvider, p["id"]).secret_ref, None) == {"bearer_token": "new-bedrock-key"}
    assert owner.delete(f"/api/v1/ai/providers/{p['id']}").status_code == 200
    assert login(server, "builder").post("/api/v1/ai/providers", json={"name": "x", "provider_type": "bedrock", "api_key": "k", "model_id": "m"}).status_code == 403


def test_model_discovery_lists_what_an_endpoint_offers(server, br):
    owner = login(server, "owner")
    r = owner.post("/api/v1/ai/providers/discover-models", json={"provider_type": "openai_compatible", "base_url": br.url, "api_key": "k"})
    assert r.json()["models"] == ["model-a", "model-b"]
    assert owner.post("/api/v1/ai/providers/discover-models", json={"provider_type": "openai_compatible", "base_url": "http://127.0.0.1:1/v1"}).status_code == 502


def test_assistant_and_build_pipeline_tasks_route_to_bedrock(server, br):
    owner, admin = login(server, "owner"), login(server, "admin")
    p = owner.post("/api/v1/ai/providers", json={"name": "Bedrock", "provider_type": "bedrock", "region": REGION, "aws_access_key_id": AK,
                                                  "aws_secret_access_key": SK, "aws_session_token": TOKEN, "model_id": MODEL}).json()
    with session_scope() as db:                                    # point the provider at the local stand-in for bedrock-runtime
        db.get(AIProvider, p["id"]).base_url = br.url
    try:
        assert admin.get("/api/v1/assistant/status").json()["configured"] is True
        routing = {t["task"]: t["candidates"] for t in admin.get("/api/v1/ai/routing").json()["tasks"]}
        assert routing["assistant"][0].startswith("Bedrock") and routing["config_generation"][0].startswith("Bedrock")
        assert admin.post(f"/api/v1/ai/providers/{p['id']}/test").json()["ok"] is True
        br.reply = {"reply": "Bedrock answered **this**."}
        tid = admin.post("/api/v1/assistant/threads", json={}).json()["id"]
        r = admin.post(f"/api/v1/assistant/threads/{tid}/messages", json={"content": "hello"})
        assert r.status_code == 200 and r.json()["message"]["content"] == "Bedrock answered **this**." and "Bedrock" in r.json()["message"]["meta"]["model"]
    finally:
        br.reply = None
        owner.delete(f"/api/v1/ai/providers/{p['id']}")
