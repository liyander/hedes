"""Self-healing history: every session keeps a timestamped log, outcomes are filterable, and a fix decision closes the loop."""
import pytest

from app.core.db import session_scope
from app.models import Execution, HealingCase, Integration
from app.services import healing
from conftest import login


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(healing, "_pause", lambda s: None)


def _case_with_fix(org_id, integ_id, monkeypatch):
    monkeypatch.setattr(healing, "_documented_paths", lambda db, integ, method: ["/messages/v2"])
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
