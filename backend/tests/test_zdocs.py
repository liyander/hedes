"""AI Studio: documentation search online, how-to-find guidance, sources referred, confirmation before creating records."""
import json
import time
from pathlib import Path

import pytest

from app.services import docs_search
from conftest import login

SPEC = json.loads((Path(__file__).resolve().parents[2] / "examples" / "mock-openapi.json").read_text())
SPEC["info"]["title"] = "Acmepay API"
SPEC["servers"] = [{"url": "https://api.acmepay.example/v1"}]
SPEC_URL = "https://api.apis.guru/v2/specs/acmepay.com/1.0/openapi.json"
DIRECTORY = {
    "acmepay.com": {"preferred": "1.0", "versions": {"1.0": {"openapiVer": "3.0.0", "swaggerUrl": SPEC_URL,
                    "info": {"title": "Acmepay API", "x-origin": [{"url": "https://acmepay.example/openapi.yaml"}]}}}},
    "acmepay.com:legacy": {"preferred": "0.9", "versions": {"0.9": {"openapiVer": "3.0.0", "swaggerUrl": "https://x/legacy.json", "info": {"title": "Acmepay legacy"}}}},
    "oldco.com": {"preferred": "1", "versions": {"1": {"openapiVer": "2.0", "swaggerUrl": "https://x/oldco.json", "info": {"title": "OldCo"}}}},
}


@pytest.fixture
def online(monkeypatch):
    fetched = []

    def fake_get(url, limit):
        fetched.append(url)
        if url == docs_search.DIRECTORY_URL:
            return json.dumps(DIRECTORY).encode()
        if url in (SPEC_URL, "https://docs.acmepay.example/openapi.json"):
            return json.dumps(SPEC).encode()
        raise docs_search.DocsError(f"HTTP 404 from {url}")

    monkeypatch.setattr(docs_search, "_get", fake_get)
    monkeypatch.setitem(docs_search._CACHE, "data", None)
    return fetched


def wait_done(c, sid):
    for _ in range(80):
        s = c.get(f"/api/v1/ai/sessions/{sid}").json()
        if s["status"] in ("ready", "failed"):
            return s
        time.sleep(0.25)
    raise AssertionError("pipeline did not finish")


def test_directory_search_ranks_the_service_itself_first(online):
    hits = docs_search.search("Acmepay")
    assert [h["key"] for h in hits] == ["acmepay.com", "acmepay.com:legacy"] and hits[0]["spec_url"] == SPEC_URL
    assert docs_search.search("xy") == []


def test_spec_links_and_egress_policy():
    assert docs_search.spec_urls_in("docs are at https://docs.x.io/openapi.json, thanks") == ["https://docs.x.io/openapi.json"]
    assert docs_search.spec_urls_in("see https://x.io/about") == []
    with pytest.raises(docs_search.DocsError, match="network policy"):
        docs_search.real_get("http://169.254.169.254/latest/openapi.json", 1000)


def test_unknown_service_is_found_online_and_built_with_sources(server, online):
    b = login(server, "builder")
    s = b.post("/api/v1/ai/sessions", json={"message": "Connect Acmepay and create tasks automatically"}).json()
    req = s["requirements"]
    assert req["ready"], req
    reply = s["messages"][-1]["content"]
    assert "I found the **Acmepay API** API documentation online" in reply and SPEC_URL in reply and "acmepay.com:legacy" in reply
    assert any(x["url"] == SPEC_URL for x in s["sources"]) and s["documents"][0]["title"] == "Acmepay API"
    assert online.count(docs_search.DIRECTORY_URL) == 1

    plan = b.get(f"/api/v1/ai/sessions/{s['id']}/plan").json()
    item = plan["items"][0]
    assert item["record"] == "new integration" and item["auth_type"] == "bearer" and "API token" in item["credential_help"]
    assert b.post(f"/api/v1/ai/sessions/{s['id']}/generate", json={"confirm": "stale-or-forged"}).status_code == 409
    b.post(f"/api/v1/ai/sessions/{s['id']}/generate", json={"confirm": plan["fingerprint"]})
    done = wait_done(b, s["id"])
    assert done["status"] == "ready", done["stages"]
    final = done["messages"][-1]["content"]
    assert "**Sources referred:**" in final and SPEC_URL in final and "Where to get it:" in final
    assert any(a["action"] == "ai.generate.confirmed" for a in login(server, "admin").get("/api/v1/audit").json())


def test_missing_documentation_explains_what_was_tried_and_how_to_find_it(server, online):
    b = login(server, "builder")
    s = b.post("/api/v1/ai/sessions", json={"message": "Connect Oldco and create tasks"}).json()
    req = s["requirements"]
    assert not req["ready"]
    q = next(x for x in req["missing_help"] if "Oldco" in x["question"])
    assert "only a Swagger 2.0 version" in q["question"] and "OpenAPI" in q["how"]
    assert "How to find it:" in s["messages"][-1]["content"]
    assert b.get(f"/api/v1/ai/sessions/{s['id']}/plan").status_code == 409

    s = b.post(f"/api/v1/ai/sessions/{s['id']}/messages", json={"content": "The docs are here: https://docs.acmepay.example/openapi.json"}).json()
    assert "I read the API documentation you linked" in s["messages"][-1]["content"]
    assert any(x["url"] == "https://docs.acmepay.example/openapi.json" for x in s["sources"])


def test_large_apis_keep_the_operations_the_user_named():
    from app.services import agents
    ops = [{"id": f"add_{n}", "method": "POST", "path": f"/{n}", "summary": ""} for n in
           ("boards", "labels", "lists", "checklists", "webhooks", "tokens", "members", "cards", "organizations")]
    ops.append({"id": "add_cards_comments", "method": "POST", "path": "/cards/{id}/comments", "summary": ""})
    docs = [{"id": "d1", "name": "x", "catalog": {"title": "Kanban", "base_url": "https://api.kanban.example", "auth": [], "operations": ops}}]
    req = agents.local_requirements([{"role": "user", "content": "Connect Kanban and create cards automatically"}], docs, {})
    assert [o["operation_id"] for o in req["operations"]] == ["add_cards", "add_cards_comments"]
