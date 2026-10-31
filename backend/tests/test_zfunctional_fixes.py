"""Functional regressions: workflow runs always finish, record text containing "${" is data, cron reads Sunday as 7 and ORs
the day fields, pathological OpenAPI documents are refused quickly, an approved assistant action runs once, and a cron source
is not reported stale between its runs."""
import json
import time
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.db import session_scope, utcnow
from app.core.security import Principal
from app.models import AssistantAction, AssistantThread, IngestSource, Organization, User
from app.services import assistant, cron, ingest, openapi, templates


def test_cron_reads_sunday_as_7_and_ors_the_day_fields():
    monday = datetime(2026, 11, 2, 8, 0)
    assert cron.next_after("0 9 * * 7", monday) == datetime(2026, 11, 8, 9, 0)            # Sunday, not every day
    assert cron.parse("0 9 * * 5-7")[4] == {5, 6, 0}
    assert cron.next_after("0 0 13 * 5", datetime(2026, 11, 1)) == datetime(2026, 11, 6)  # a Friday or the 13th, as in cron
    for never in ("0 0 31 2 *", "0, * * * *"):                                            # no such date / an empty list item
        with pytest.raises(cron.CronError):
            cron.parse(never)


def test_record_text_containing_a_template_marker_is_data():
    assert templates.render("New: ${trigger.title}", {"trigger": {"title": "${jndi:ldap://x}"}}) == "New: ${jndi:ldap://x}"
    with pytest.raises(templates.TemplateError):
        templates.render("bad ${trigger.", {"trigger": {}})


def test_pathological_openapi_documents_are_refused_quickly():
    schemas = {"l0": {"type": "string"}}
    for i in range(1, 13):  # each level refers to the previous one four times: 4^12 objects once inlined
        schemas[f"l{i}"] = {"type": "object", "properties": {k: {"$ref": f"#/components/schemas/l{i - 1}"} for k in "abcd"}}
    bomb = {"openapi": "3.0.0", "info": {"title": "Bomb", "version": "1"}, "components": {"schemas": schemas},
            "paths": {"/x": {"get": {"responses": {"200": {"description": "ok", "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/l12"}}}}}}}}}
    started = time.monotonic()
    with pytest.raises(openapi.OpenAPIError):
        openapi.parse_openapi(json.dumps(bomb))
    assert time.monotonic() - started < 5
    with pytest.raises(openapi.OpenAPIError):  # a field of the wrong type is a 422 for the user, not a 500
        openapi.parse_openapi(json.dumps({"openapi": "3.0.0", "info": "a string", "paths": {"/x": {"get": {"responses": {"200": "ok"}}}}}))


def test_a_workflow_run_that_hits_bad_data_finishes_as_failed(builder):
    graph = {"trigger": {"type": "manual", "sample": {"x": "1"}},
             "nodes": [{"id": "s1", "tool": "notify.send_message", "inputs": {"channel": "ops", "text": "hi"},
                        "when": [{"left": "${trigger.x}", "op": "eq", "right": "${oops"}]}]}
    wf = builder.post("/api/v1/workflows", json={"name": "Bad condition", "graph": graph}).json()
    r = builder.post(f"/api/v1/workflows/{wf['id']}/execute", json={"dry_run": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed" and r.json()["finished_at"] and "TemplateError" in r.json()["error"]


def test_an_approved_assistant_action_runs_once(server, monkeypatch):
    ran = []
    monkeypatch.setattr(assistant, "execute_action", lambda db, p, act: ran.append(act.id) or {"ok": True})
    with session_scope() as db:
        user = db.scalars(select(User).where(User.username == "builder")).first()
        org_id = db.scalars(select(Organization.id)).first()
        thread = AssistantThread(organization_id=org_id, user_id=user.id, title="double click")
        db.add(thread)
        db.flush()
        act = AssistantAction(thread_id=thread.id, organization_id=org_id, user_id=user.id, tool="run_tool", args={"tool": "x"})
        db.add(act)
        db.flush()
        act_id, user_id = act.id, user.id
    with session_scope() as a, session_scope() as b:
        first, second = a.get(AssistantAction, act_id), b.get(AssistantAction, act_id)   # both clicks see "proposed"
        assistant.decide(a, Principal(a.get(User, user_id), org_id, "user", "session"), first, True)
        with pytest.raises(HTTPException) as e:
            assistant.decide(b, Principal(b.get(User, user_id), org_id, "user", "session"), second, True)
    assert e.value.status_code == 409 and ran == [act_id]


def test_a_cron_source_is_not_stale_between_its_runs():
    five_hours_ago = utcnow() - timedelta(hours=5)
    src = IngestSource(id="health-check", enabled=True, interval_minutes=15, cron="0 3 * * *", state="ok",
                       last_run_at=five_hours_ago, last_success_at=five_hours_ago, consecutive_failures=0)
    assert ingest.health(src)["status"] == "healthy"                                       # a daily source, last run 5 h ago
    src.cron = None
    assert ingest.health(src)["status"] == "stale"                                         # every 15 min, nothing for 5 h
