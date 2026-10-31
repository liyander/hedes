"""Self-healing: trace failures, try fixes, and email the administrators what it could not fix."""
import time

import pytest

from app.core.db import session_scope, utcnow
from app.models import Execution, HealingCase, Integration, Workflow, WorkflowRun, WorkflowStep, WorkflowVersion
from app.services import healing, notify
from conftest import login


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def admin(server):
    return login(server, "admin")


@pytest.fixture
def org_id(admin):
    return admin.get("/api/v1/auth/me").json()["organization"]["id"]


@pytest.fixture
def notify_integration(org_id):
    with session_scope() as db:
        integ = db.query(Integration).filter_by(organization_id=org_id, slug="notify").one()
        return integ.id


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(healing, "_pause", lambda s: None)


def wait_case(admin, pred, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        for c in admin.get("/api/v1/healing/cases").json()["cases"]:
            if pred(c) and c["status"] != "investigating":
                return c
        time.sleep(0.25)
    raise AssertionError("no finished case")


def run_send(client):
    tool = next(t for t in client.get("/api/v1/tools").json() if t["name"] == "notify.send_message")
    return client.post(f"/api/v1/tools/{tool['id']}/execute", json={"inputs": {"channel": "ops", "text": "healing test"}}).json()


def test_off_by_default_then_traces_a_broken_credential_and_emails_the_admin(admin, org_id, monkeypatch):
    assert admin.get("/api/v1/healing/cases").json()["enabled"] is False
    conns = {c["name"]: c for c in admin.get("/api/v1/connections").json()}
    admin.put(f"/api/v1/connections/{conns['notify']['id']}/secret", json={"auth_type": "api_key_header", "api_key": "revoked-key-value"})
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent.clear()
    try:
        assert run_send(admin)["error_class"] == "auth_invalid"
        time.sleep(0.5)
        assert not admin.get("/api/v1/healing/cases").json()["cases"]          # disabled: nothing is investigated

        admin.patch("/api/v1/organizations/current/policy", json={"self_healing": True})
        admin.put("/api/v1/settings/notifications", json={"enabled": True, "min_severity": "high", "email": {
            "host": "smtp.example.com", "port": 587, "security": "starttls", "username": "u", "password": "pw-1234567890",
            "from": "hedes@example.com", "to": ["admins@example.com"]}})
        assert run_send(admin)["error_class"] == "auth_invalid"
        case = wait_case(admin, lambda c: c["error_class"] == "auth_invalid")
        assert case["status"] == "needs_admin" and case["endpoint"]["url"].endswith("/mock/notify/messages")
        assert any("Re-tested the connection" in a["action"] and not a["ok"] for a in case["attempts"])
        assert any("credential 'notify'" in r for r in case["required"]) and "401" in case["still_failing"]
        assert "What I tried:" in case["report"] and "Required from an administrator:" in case["report"]

        run_send(admin)                                                          # a repeat joins the open case
        time.sleep(0.5)
        cases = [c for c in admin.get("/api/v1/healing/cases").json()["cases"] if c["error_class"] == "auth_invalid"]
        assert len(cases) == 1 and cases[0]["occurrences"] == 2

        for _ in range(40):
            if FakeSMTP.sent:
                break
            time.sleep(0.25)
        msg = FakeSMTP.sent[0]
        assert msg["Subject"].startswith("[H.E.D.E.S] Action needed - Self-healing could not fix this on its own: notify.send_message")
        html = msg.get_body(("html",)).get_content()
        assert "Self-healing needs an administrator" in html and "What I tried:" in html and "Rotate or reconnect the credential" in html
    finally:
        admin.put(f"/api/v1/connections/{conns['notify']['id']}/secret", json={"auth_type": "api_key_header", "api_key": "notify-demo-key"})
        admin.put("/api/v1/settings/notifications", json={"enabled": False, "min_severity": "high", "email": {"host": "", "to": []}})

    # after the administrator fixed the credential, re-investigating confirms the problem is gone
    c = admin.post(f"/api/v1/healing/cases/{cases[0]['id']}/retry").json()
    assert c["status"] == "investigating"
    assert wait_case(admin, lambda x: x["id"] == c["id"])["status"] == "resolved"


def test_recovered_outage_is_resolved_without_bothering_anyone(org_id, notify_integration):
    with session_scope() as db:
        case = HealingCase(organization_id=org_id, integration_id=notify_integration, target="notify.send_message", error_class="upstream_error",
                           endpoint={"method": "POST", "url": "http://x/mock/notify/messages"})
        db.add(case)
        db.flush()
        healing.investigate(db, case)
        assert case.status == "resolved" and "temporary failure" in case.root_cause and case.notified_at is None
        assert case.attempts[0]["ok"] is True


def test_wrong_endpoint_is_compared_with_the_documentation_and_a_fix_is_submitted(admin, org_id, notify_integration, monkeypatch):
    monkeypatch.setattr(healing, "_documented_paths", lambda db, integ, method: ["/messages/v2", "/channels"])
    with session_scope() as db:
        ex = Execution(organization_id=org_id, integration_id=notify_integration, tool_name="notify.send_message", status="failed",
                       error_class="not_found", http_status=404, error="HTTP 404", actor_type="workflow",
                       request_preview={"method": "POST", "url": "http://x/mock/notify/messages"})
        db.add(ex)
        db.flush()
        case = HealingCase(organization_id=org_id, integration_id=notify_integration, execution_id=ex.id, target=ex.tool_name,
                           error_class="not_found", endpoint=healing._endpoint(ex))
        db.add(case)
        db.flush()
        healing.investigate(db, case)
        assert case.status == "awaiting_approval" and case.approval_request_id
        assert any("/messages/v2" in a["result"] for a in case.attempts)
        rid = case.approval_request_id
    review = admin.get(f"/api/v1/admin/approvals/{rid}").json()
    assert review["kind"] == "repair" and review["config"]["integration"]["operations"][0]["path"] == "/messages/v2"
    assert admin.post(f"/api/v1/admin/approvals/{rid}/reject", json={"comment": "test only"}).status_code == 200


def test_workflow_failing_on_an_unavailable_tool_is_reported(admin, org_id):
    with session_scope() as db:
        owner = admin.get("/api/v1/auth/me").json()["id"]
        wf = Workflow(organization_id=org_id, name="Healing check", created_by=owner)
        db.add(wf)
        db.flush()
        v = WorkflowVersion(workflow_id=wf.id, version=1, graph={"trigger": {"type": "manual"}, "nodes": [{"id": "n1", "tool": "gone.op"}]})
        db.add(v)
        db.flush()
        wf.current_version_id = v.id
        run = WorkflowRun(organization_id=org_id, workflow_id=wf.id, workflow_version_id=v.id, status="failed", error="Step n1 failed")
        db.add(run)
        db.flush()
        db.add(WorkflowStep(run_id=run.id, node_id="n1", status="failed", error="Tool not found", finished_at=utcnow()))
        db.flush()
        case = HealingCase(organization_id=org_id, workflow_id=wf.id, target="workflow: Healing check", error_class="workflow_step",
                           evidence={"run_id": run.id})
        db.add(case)
        db.flush()
        healing.investigate(db, case)
        assert case.status == "needs_admin" and any("gone.op" in r for r in case.required)


def test_any_failed_execution_can_be_investigated_on_demand(admin, server):
    builder = login(server, "builder")
    admin.patch("/api/v1/organizations/current/policy", json={"self_healing": False})
    ex = next(e for e in admin.get("/api/v1/executions", params={"status": "failed"}).json() if e["tool"] == "notify.send_message")
    c = builder.post(f"/api/v1/executions/{ex['id']}/heal").json()
    assert c["status"] == "investigating"
    assert wait_case(admin, lambda x: x["id"] == c["id"])["status"] in ("resolved", "needs_admin")
    assert builder.post(f"/api/v1/healing/cases/{c['id']}/retry").status_code == 403           # re-running is an admin action
