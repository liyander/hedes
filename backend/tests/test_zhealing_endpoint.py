"""Self-healing finds a corrected endpoint without uploaded documentation (reference configuration / AI), proves it exists,
and - in automatic mode - publishes it. The assistant can apply the same kind of fix on request."""
import time

import pytest

from app.core.db import session_scope
from app.models import Integration, IntegrationVersion
from app.services import ai_gateway, healing
from conftest import login


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(healing, "_pause", lambda s: None)
    monkeypatch.setattr(healing, "_ai_endpoint_guess", lambda *a, **k: None)   # no AI provider in this test: the reference config must suffice


def _break_base_url(admin, integ_id, server):
    cfg = admin.get(f"/api/v1/integrations/{integ_id}").json()
    live = next(v for v in cfg["versions"] if v["status"] == "published")
    config = admin.get(f"/api/v1/versions/{live['id']}").json()["config"]
    config["integration"]["base_url"] = server + "/mock/notif"                      # typo, like slack.com/ap instead of /api
    v = admin.post(f"/api/v1/integrations/{integ_id}/revise", json={"version_id": live["id"], "config": config}).json()
    assert admin.post(f"/api/v1/integrations/{integ_id}/tests", json={"version_id": v["id"]}).json()["result"] == "passed"
    ap = admin.post(f"/api/v1/integrations/{integ_id}/submit", json={"version_id": v["id"]}).json()["approval_id"]
    assert admin.post(f"/api/v1/admin/approvals/{ap}/approve", json={}).status_code == 200
    return live["id"]


def test_wrong_base_url_is_fixed_automatically_from_the_reference_configuration(server):
    admin = login(server, "admin")
    org_id = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    with session_scope() as db:
        integ_id = db.query(Integration).filter_by(organization_id=org_id, slug="notify").one().id
    good = _break_base_url(admin, integ_id, server)
    admin.patch("/api/v1/organizations/current/policy", json={"self_healing": True, "self_healing_auto": True})
    try:
        tool = next(t for t in admin.get("/api/v1/tools").json() if t["name"] == "notify.send_message")
        ex = admin.post(f"/api/v1/tools/{tool['id']}/execute", json={"inputs": {"channel": "ops", "text": "hello"}}).json()
        assert ex["status"] == "failed" and ex["error_class"] == "not_found"
        # Self-healing is on, so the failure opened a session by itself.
        end, c = time.time() + 30, None
        while time.time() < end:
            c = next((x for x in admin.get("/api/v1/healing/cases").json()["cases"]
                      if ex["id"] in (x["execution_id"], x["evidence"].get("last_execution_id"))), None)
            if c and c["status"] != "investigating":
                break
            time.sleep(0.3)
        assert c, (ex, admin.get("/api/v1/healing/cases").json()["cases"][:3])
        c = admin.get(f"/api/v1/healing/cases/{c['id']}").json()
        assert c["status"] == "resolved", [(x["stage"], x["text"][:200]) for x in c["log"]]
        assert any("built-in reference" in a["action"] and a["ok"] for a in c["attempts"])
        assert "auto_applied" in [x["stage"] for x in c["log"]]
        with session_scope() as db:
            live = db.get(IntegrationVersion, db.get(Integration, integ_id).published_version_id)
            assert live.config["integration"]["base_url"].endswith("/mock/notify")
        tool = next(t for t in admin.get("/api/v1/tools").json() if t["name"] == "notify.send_message")    # publishing registers new tool rows
        assert admin.post(f"/api/v1/tools/{tool['id']}/execute", json={"inputs": {"channel": "ops", "text": "hello again"}}).json()["status"] == "succeeded"
    finally:
        admin.patch("/api/v1/organizations/current/policy", json={"self_healing": False, "self_healing_auto": False})
        admin.post(f"/api/v1/integrations/{integ_id}/rollback", json={"version_id": good})


def test_endpoint_check_distinguishes_missing_from_protected(server):
    admin = login(server, "admin")
    org_id = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    with session_scope() as db:
        integ = db.query(Integration).filter_by(organization_id=org_id, slug="notify").one()
        cfg = db.get(IntegrationVersion, integ.published_version_id).config
        op = {"id": "x", "method": "POST", "path": "/messages"}
        ok, detail = healing.endpoint_exists(db, integ, cfg, op)
        assert ok is True and "exists" in detail
        assert healing.endpoint_exists(db, integ, cfg, {**op, "path": "/nope"})[0] is False
        assert healing.endpoint_exists(db, integ, cfg, {**op, "path": "/messages/{id}"})[0] is None


def test_wrapper_tags_never_reach_people():
    assert ai_gateway.strip_wrappers("<untrusted_document> The root cause is X. </untrusted_document>") == "The root cause is X."
    assert ai_gateway.strip_wrappers('<untrusted_document name="a">ok</untrusted_document>') == "ok"


def test_assistant_fix_integration_action(server):
    from test_zassistant import Stub
    stub = Stub()
    owner, admin = login(server, "owner"), login(server, "admin")
    org_id = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    with session_scope() as db:
        integ_id = db.query(Integration).filter_by(organization_id=org_id, slug="notify").one().id
    good = _break_base_url(admin, integ_id, server)
    prov = owner.post("/api/v1/ai/providers", json={"name": "Stub", "provider_type": "openai_compatible", "base_url": stub.url, "api_key": "k-123456789",
                                                     "model_id": "m", "purposes": ["general"]}).json()
    try:
        stub.script = [{"reply": "The base URL has a typo. Approve and I will fix it.",
                        "tool_calls": [{"tool": "fix_integration", "args": {"integration_id": integ_id, "base_url": server + "/mock/notify"}, "why": "Correct the base URL"}]},
                       {"reply": "Fixed and published."}]
        tid = admin.post("/api/v1/assistant/threads", json={}).json()["id"]
        act = admin.post(f"/api/v1/assistant/threads/{tid}/messages", json={"content": "fix it yourself"}).json()["message"]["actions"][0]
        assert act["tool"] == "fix_integration" and act["status"] == "proposed" and act["needs_role"] == "admin"
        done = admin.post(f"/api/v1/assistant/actions/{act['id']}/approve", json={}).json()["action"]
        assert done["status"] == "executed" and done["result"]["published"] is True, done["error"] or done["result"]
        assert done["result"]["checks"] and all(c["exists"] for c in done["result"]["checks"] if c["exists"] is not None)
        with session_scope() as db:
            live = db.get(IntegrationVersion, db.get(Integration, integ_id).published_version_id)
            assert live.config["integration"]["base_url"].endswith("/mock/notify")

        # A correction that points nowhere is not published.
        stub.script = [{"reply": "Try this.", "tool_calls": [{"tool": "fix_integration", "args": {"integration_id": integ_id, "base_url": server + "/mock/nowhere"}}]}]
        act2 = admin.post(f"/api/v1/assistant/threads/{tid}/messages", json={"content": "change it"}).json()["message"]["actions"][0]
        out = admin.post(f"/api/v1/assistant/actions/{act2['id']}/approve", json={}).json()["action"]
        assert out["status"] == "executed" and out["result"]["published"] is False and "404" in out["result"]["reason"]
    finally:
        owner.delete(f"/api/v1/ai/providers/{prov['id']}")
        admin.post(f"/api/v1/integrations/{integ_id}/rollback", json={"version_id": good})
        stub.srv.shutdown()
