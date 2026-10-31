"""Self-healing history: every session keeps a timestamped log, outcomes are filterable, and a fix decision closes the loop."""
import pytest

from app.core.db import session_scope
from app.models import Execution, HealingCase, Integration
from app.services import healing
from conftest import login


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(healing, "_pause", lambda s: None)


_paths = iter(range(2, 100))


def _case_with_fix(org_id, integ_id, monkeypatch):
    path = f"/messages/v{next(_paths)}"           # always different from the live path, whatever earlier tests published
    monkeypatch.setattr(healing, "_documented_paths", lambda db, integ, method: [path])
    monkeypatch.setattr(healing, "endpoint_exists", lambda db, integ, cfg, op: (True, "exists (stubbed)"))
    monkeypatch.setattr(healing, "_ai_endpoint_guess", lambda *a, **k: None)
    with session_scope() as db:
        ex = Execution(organization_id=org_id, integration_id=integ_id, tool_name="notify.send_message", status="failed", error_class="not_found",
                       http_status=404, error="HTTP 404", actor_type="workflow", request_preview={"method": "POST", "url": "http://x/mock/notify/messages"})
        db.add(ex)
        db.flush()
        integ = db.get(Integration, integ_id)
        case = healing.on_failure(db, ex, integ, force=True)
        db.flush()
        db.info.pop("healing_cases", None)       # investigate here, synchronously, instead of in the background thread
        healing.investigate(db, case)
        return case.id, case.approval_request_id


def test_session_log_outcome_and_fix_decision(server, monkeypatch):
    admin = login(server, "admin")
    org_id = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    with session_scope() as db:
        integ_id = db.query(Integration).filter_by(organization_id=org_id, slug="notify").one().id
    cid, rid = _case_with_fix(org_id, integ_id, monkeypatch)
    c = admin.get(f"/api/v1/healing/cases/{cid}").json()
    stages = [x["stage"] for x in c["log"]]
    assert stages[:3] == ["opened", "started", "evidence"] and "attempt" in stages and "diagnosis" in stages and "outcome" in stages
    assert all(x["at"].endswith("Z") for x in c["log"]) and c["runs"] == 1
    assert c["status"] == "awaiting_approval" and c["fix"]["status"] == "pending_approval" and c["fix"]["decision"] == "pending"
    assert c["integration"]["slug"] == "notify" and c["executions"] and c["fixes_tried"] >= 1

    listing = admin.get("/api/v1/healing/cases", params={"status": "awaiting_approval"}).json()
    assert any(x["id"] == cid for x in listing["cases"]) and all(x["status"] == "awaiting_approval" for x in listing["cases"])
    assert listing["summary"]["total"] >= 1 and "fixed_rate" in listing["summary"]
    assert any(x["id"] == cid for x in admin.get("/api/v1/healing/cases", params={"q": "send_message"}).json()["cases"])

    # The admin rejects the prepared fix: the session records the decision and goes back to needing an admin.
    assert admin.post(f"/api/v1/admin/approvals/{rid}/reject", json={"comment": "not this way"}).status_code == 200
    c = admin.get(f"/api/v1/healing/cases/{cid}").json()
    assert c["status"] == "needs_admin" and c["log"][-1]["stage"] == "fix_decision" and "rejected" in c["log"][-1]["text"]
    assert c["fix"]["decision"] == "rejected"

    # A re-investigation is a new run in the same log; earlier entries are kept.
    with session_scope() as db:
        healing.investigate(db, db.get(HealingCase, cid))
    c = admin.get(f"/api/v1/healing/cases/{cid}").json()
    assert c["runs"] == 2 and {x["run"] for x in c["log"]} >= {1, 2} and c["log"][0]["stage"] == "opened"


def test_approving_the_fix_marks_the_session_fixed(server, monkeypatch):
    admin = login(server, "admin")
    org_id = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    with session_scope() as db:
        integ_id = db.query(Integration).filter_by(organization_id=org_id, slug="notify").one().id
    cid, rid = _case_with_fix(org_id, integ_id, monkeypatch)
    assert admin.post(f"/api/v1/admin/approvals/{rid}/approve", json={}).status_code == 200
    c = admin.get(f"/api/v1/healing/cases/{cid}").json()
    assert c["status"] == "resolved" and c["resolved_at"] and c["duration_s"] is not None
    assert c["log"][-1]["stage"] == "fix_decision" and c["log"][-1]["ok"] is True
    # put the original endpoint back for the rest of the suite
    with session_scope() as db:
        from app.models import IntegrationVersion
        integ = db.get(Integration, integ_id)
        prev = [v for v in db.query(IntegrationVersion).filter_by(integration_id=integ_id).all() if v.id != integ.published_version_id and v.status == "superseded"]
    if prev:
        admin.post(f"/api/v1/integrations/{integ_id}/rollback", json={"version_id": prev[-1].id})


# ------------------------------------------------------------------------------------------- automatic mode

def test_critical_rules():
    from types import SimpleNamespace
    base = {"integration": {"base_url": "https://api.x.com", "authentication": {"type": "bearer", "credential_ref": "x"},
                            "operations": [{"id": "send", "method": "POST", "path": "/m", "input_schema": {"type": "object"}, "safety": "write"},
                                           {"id": "drop", "method": "DELETE", "path": "/m/{id}", "input_schema": {"type": "object"}, "safety": "destructive"}]}}
    ok_version = SimpleNamespace(security_report={"level": "low"})
    import copy
    path_only = copy.deepcopy(base)
    path_only["integration"]["operations"][0]["path"] = "/m/v2"
    assert healing.critical_reasons(base, path_only, ok_version) == []                      # same host/method/inputs: not critical
    cases = {
        "destination host": lambda c: c["integration"].update(base_url="https://evil.example"),
        "authenticates": lambda c: c["integration"]["authentication"].update(type="basic"),
        "HTTP method": lambda c: c["integration"]["operations"][0].update(method="PUT"),
        "delete data": lambda c: c["integration"]["operations"][1].update(path="/x/{id}"),
        "inputs": lambda c: c["integration"]["operations"][0].update(input_schema={"type": "object", "required": ["a"]}),
        "adds or removes": lambda c: c["integration"]["operations"].pop(),
    }
    for words, change in cases.items():
        cfg = copy.deepcopy(base)
        change(cfg)
        assert any(words in r for r in healing.critical_reasons(base, cfg, ok_version)), words
    assert healing.critical_reasons(base, path_only, SimpleNamespace(security_report={"level": "high"}))


def test_automatic_mode_publishes_non_critical_fixes_and_reports_them(server, monkeypatch):
    admin = login(server, "admin")
    org_id = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    with session_scope() as db:
        integ = db.query(Integration).filter_by(organization_id=org_id, slug="notify").one()
        integ_id, before = integ.id, integ.published_version_id
    assert admin.patch("/api/v1/organizations/current/policy", json={"self_healing": True, "self_healing_auto": True}).status_code == 200
    try:
        assert admin.get("/api/v1/healing/cases").json()["auto"] is True
        cid, rid = _case_with_fix(org_id, integ_id, monkeypatch)
        c = admin.get(f"/api/v1/healing/cases/{cid}").json()
        stages = [x["stage"] for x in c["log"]]
        assert c["status"] == "resolved" and "auto_applied" in stages and "verified" in stages and c["resolved_at"]
        assert c["fix"]["decision"] == "approved" and c["fix"]["status"] == "published"
        with session_scope() as db:
            assert db.get(Integration, integ_id).published_version_id != before
        alert = next(a for a in admin.get("/api/v1/alerts").json() if a["kind"] == "self_healed")
        assert alert["link"] == f"#/healing/{cid}"
        audit = admin.get("/api/v1/audit", params={"action": "approval.approve"}).json()
        rows = audit if isinstance(audit, list) else audit.get("items", [])
        assert any((r.get("actor") or r.get("actor_label")) == "self-healing (automatic)" for r in rows)

        # A fix judged critical waits for an administrator even in automatic mode.
        monkeypatch.setattr(healing, "critical_reasons", lambda old, new, v: ["it changes the destination host"])
        cid2, _ = _case_with_fix(org_id, integ_id, monkeypatch)
        c2 = admin.get(f"/api/v1/healing/cases/{cid2}").json()
        assert c2["status"] == "awaiting_approval" and c2["log"][[x["stage"] for x in c2["log"]].index("auto_skip")]["text"].endswith("destination host")
    finally:
        admin.patch("/api/v1/organizations/current/policy", json={"self_healing": False, "self_healing_auto": False})
        for req in admin.get("/api/v1/admin/approvals").json():
            if req.get("integration_id") == integ_id and req.get("decision") == "pending":
                admin.post(f"/api/v1/admin/approvals/{req['id']}/reject", json={"comment": "test cleanup"})
        admin.post(f"/api/v1/integrations/{integ_id}/rollback", json={"version_id": before})


def test_failed_verification_rolls_back(server, monkeypatch):
    admin = login(server, "admin")
    org_id = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    with session_scope() as db:
        integ_id = db.query(Integration).filter_by(organization_id=org_id, slug="notify").one().id
        before = db.get(Integration, integ_id).published_version_id
    admin.patch("/api/v1/organizations/current/policy", json={"self_healing": True, "self_healing_auto": True})
    calls = []

    def probe(db, integ, version):
        calls.append(version.id)
        return (False, "not_found: HTTP 404") if len(calls) == 1 else (False, "still broken")
    monkeypatch.setattr(healing, "_probe", probe)
    try:
        cid, _ = _case_with_fix(org_id, integ_id, monkeypatch)
        c = admin.get(f"/api/v1/healing/cases/{cid}").json()
        assert "rolled_back" in [x["stage"] for x in c["log"]] and c["status"] == "needs_admin", c["log"]
        with session_scope() as db:
            assert db.get(Integration, integ_id).published_version_id == before          # the previous version is live again
    finally:
        admin.patch("/api/v1/organizations/current/policy", json={"self_healing": False, "self_healing_auto": False})


def test_every_email_template_has_its_own_theme():
    from app.services import email_templates as et
    themes = {k: t["theme"]["light"] for k, t in et.TEMPLATES.items()}
    assert len(set(themes.values())) == len(themes)                                        # a different colour per kind of mail
    assert et.template_for("self_healed") == "healing_fixed"
    m = et.render_sample("healing_fixed", "Acme", "https://h.example")
    assert "Fixed automatically" in m["html"] and "Open the session log" in m["html"] and "https://h.example/#/healing" in m["html"]
    assert "<li" in m["html"] and "Replies are not read" in m["html"]


def test_admin_can_mark_a_session_fixed_and_reopen_it(server):
    admin, builder = login(server, "admin"), login(server, "builder")
    org_id = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    with session_scope() as db:
        case = HealingCase(organization_id=org_id, target="slack.post_message", error_class="not_found", status="needs_admin",
                           required=["Fix the base URL"], log=[], runs=1)
        db.add(case)
        db.flush()
        cid = case.id
    assert builder.post(f"/api/v1/healing/cases/{cid}/resolve", json={}).status_code == 403
    c = admin.post(f"/api/v1/healing/cases/{cid}/resolve", json={"note": "Corrected the base URL by hand"}).json()
    assert c["status"] == "resolved" and c["resolved_at"] and c["required"] == []
    assert c["log"][-1]["stage"] == "resolved_by_admin" and "Corrected the base URL" in c["log"][-1]["text"]
    assert admin.post(f"/api/v1/healing/cases/{cid}/resolve", json={}).status_code == 409
    c = admin.post(f"/api/v1/healing/cases/{cid}/reopen", json={"note": "still 404"}).json()
    assert c["status"] == "needs_admin" and c["resolved_at"] is None and c["log"][-1]["stage"] == "reopened"
    assert any(r["action"] == "healing.resolve" for r in (lambda x: x if isinstance(x, list) else x.get("items", []))(admin.get("/api/v1/audit").json()))
