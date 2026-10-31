"""Automatic workflow triggers: a H.E.D.E.S event that meets the trigger's conditions starts the workflow."""
from app.core.db import session_scope
from app.services import audit, automation
from test_e2e import wait


def _workflow(builder, conditions):
    graph = {"trigger": {"type": "automatic", "event": "alert", "conditions": conditions, "sample": automation.SAMPLES["alert"]},
             "nodes": [{"id": "ping", "tool": "notify.send_message",
                        "inputs": {"channel": "ops", "text": "Alert ${trigger.kind}: ${trigger.message}"}}]}
    wf = builder.post("/api/v1/workflows", json={"name": "Alert relay", "graph": graph}).json()
    assert wf["validation"]["valid"], wf["validation"]
    assert set(wf["events"]) == {"failure", "alert", "record"}
    builder.post(f"/api/v1/workflows/{wf['id']}/activate")
    return wf


def _alert(org_id, kind, message):
    with session_scope() as db:
        audit.alert(db, org_id, kind, message, "high")


def test_an_alert_matching_the_conditions_starts_the_workflow(builder):
    org = builder.get("/api/v1/auth/me").json()["organization"]["id"]
    wf = _workflow(builder, [{"left": "${trigger.kind}", "op": "eq", "right": "automation_test"}])
    runs = lambda: [r for r in builder.get(f"/api/v1/workflows/{wf['id']}/runs").json() if r["trigger"] == "automatic"]

    _alert(org, "something_else", "not for this workflow")
    _alert(org, "automation_test", "disk almost full")
    run = wait(lambda: next((r for r in runs() if r["status"] != "running"), None))
    assert run["status"] == "succeeded", run
    msgs = builder.get("/api/v1/demo/state").json()["messages"]
    assert any(m["text"] == "Alert automation_test: disk almost full" for m in msgs)
    assert not any("not for this workflow" in m["text"] for m in msgs)

    _alert(org, "automation_test", "disk almost full")  # the same event again within 5 minutes: not repeated
    _alert(org, "automation_test", "disk full")
    wait(lambda: len(runs()) >= 2)
    assert len(runs()) == 2
    builder.post(f"/api/v1/workflows/{wf['id']}/stop")


def test_failures_of_automatic_runs_do_not_start_more_runs():
    class Db:
        info: dict = {}
    db = Db()
    automation.emit(db, "org", "failure", {"tool": "x", "actor_type": automation.AUTOMATIC_ACTOR})
    automation.emit(db, "org", "failure", {"tool": "x", "actor_type": "user"})
    assert [e[2]["actor_type"] for e in db.info["workflow_events"]] == ["user"]


def test_trigger_conditions_must_use_event_fields(builder):
    graph = {"trigger": {"type": "automatic", "event": "failure", "conditions": [{"left": "${steps.x.output.y}", "op": "exists"}]},
             "nodes": [{"id": "ping", "tool": "notify.send_message", "inputs": {"channel": "ops", "text": "${trigger.error}"}}]}
    errors = builder.post("/api/v1/workflows", json={"name": "Bad trigger", "graph": graph}).json()["validation"]["errors"]
    assert any("trigger conditions can only use the event's fields" in e["message"] for e in errors)
    missing_event = {**graph, "trigger": {"type": "automatic"}}
    r = builder.post("/api/v1/workflows", json={"name": "No event", "graph": missing_event})
    assert r.status_code == 422 or not r.json()["validation"]["valid"]
