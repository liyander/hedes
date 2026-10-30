import httpx
import pytest

from app.services import egress, engine, jsonpath, openapi, templates
from app.services.ai_gateway import redact_ai_context
from app.services.security_review import review
from app.services.validation import validate_config, validate_workflow_graph

CFG = {"schema_version": "1.0", "integration": {
    "id": "demo", "name": "Demo", "version": "1.0.0", "transport": "http", "base_url": "https://api.example.com",
    "authentication": {"type": "bearer", "credential_ref": "demo"},
    "operations": [{"id": "create", "method": "POST", "path": "/items/{group}",
                    "input_schema": {"type": "object", "required": ["group", "title"],
                                     "properties": {"group": {"type": "string"}, "title": {"type": "string"}, "note": {"type": "string"}}},
                    "request": {"path_params": {"group": "${inputs.group}"}, "json_body": {"title": "${inputs.title}", "note": "${inputs.note}"}},
                    "response": {"status_codes": [201], "mapping": {"id": "$.id", "owner": "$.owner.name"}}}]}}


def test_templates_keep_types_and_drop_missing():
    ctx = {"inputs": {"n": 3, "t": "x", "tags": ["a"]}}
    assert templates.render({"a": "${inputs.n}", "b": "id-${inputs.t}", "c": "${inputs.nope}", "d": "${inputs.tags[0]}"}, ctx) == \
        {"a": 3, "b": "id-x", "d": "a"}
    with pytest.raises(templates.TemplateError):
        templates.references("${__import__('os')}")


def test_jsonpath_subset():
    doc = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert jsonpath.query(doc, "$.a.b[1].c") == (True, 2)
    assert jsonpath.query(doc, "$.a.b[*].c") == (True, [1, 2])
    assert jsonpath.query(doc, "$.x")[0] is False
    with pytest.raises(jsonpath.JSONPathError):
        jsonpath.parse("$..c")


def test_engine_builds_request_maps_output_and_detects_drift():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers.get("authorization"), req.content
        return httpx.Response(201, json={"id": 7, "owner": {"name": "Alex"}})

    r = engine.execute(CFG, "create", {"group": "a/b", "title": "T"}, credential={"token": "secret"},
                       transport=httpx.MockTransport(handler), live=False)
    assert r.status == "succeeded" and r.output == {"id": 7, "owner": "Alex"}
    assert seen["url"] == "https://api.example.com/items/a%2Fb" and seen["auth"] == "Bearer secret"
    assert b"note" not in seen["body"]  # optional input omitted
    assert r.request_preview["headers"]["Authorization"] == "••••••"

    drift = engine.execute(CFG, "create", {"group": "a", "title": "T"}, credential={"token": "s"},
                           transport=httpx.MockTransport(lambda q: httpx.Response(201, json={"id": 7, "owner": {"display_name": "Alex"}})), live=False)
    assert drift.error_class == "response_drift" and "$.owner.display_name" in drift.diagnostics["response_shape"]


@pytest.mark.parametrize("status,cls", [(401, "auth_invalid"), (403, "forbidden"), (404, "not_found"), (429, "rate_limited"), (503, "upstream_error"), (422, "request_rejected")])
def test_engine_classifies_http_errors(status, cls):
    r = engine.execute(CFG, "create", {"group": "a", "title": "T"}, credential={"token": "s"},
                       transport=httpx.MockTransport(lambda q: httpx.Response(status, json={})), live=False)
    assert r.error_class == cls
    assert r.attempts == 1  # POST is not idempotent -> never retried


def test_engine_rejects_bad_inputs_and_traversal():
    assert engine.execute(CFG, "create", {"title": "T"}, credential={"token": "s"}, live=False).error_class == "input_invalid"
    assert engine.execute(CFG, "create", {"group": "..", "title": "T"}, credential={"token": "s"}, live=False).error_class == "input_invalid"


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data", "https://169.254.169.254/", "https://10.0.0.5/x",
                                 "https://localhost/x", "https://[::1]/", "ftp://example.com", "https://user:pw@example.com/"])
def test_egress_blocks_internal_destinations(url):
    with pytest.raises(egress.EgressDenied):
        egress.static_check(url)


def test_security_review_blocks_metadata_and_inline_secrets():
    bad = {"schema_version": "1.0", "integration": {**CFG["integration"], "base_url": "http://169.254.169.254",
                                                     "default_headers": {"X-Token": "ghp_" + "a" * 36}}}
    report = review(bad, {}, {})
    codes = {f["code"] for f in report["findings"]}
    assert report["blocking"] and {"destination_blocked", "inline_secret"} <= codes


def test_validation_catches_unknown_input_reference():
    cfg = {"schema_version": "1.0", "integration": {**CFG["integration"], "operations": [
        {**CFG["integration"]["operations"][0], "request": {"path_params": {"group": "${inputs.missing}"}}}]}}
    errors = validate_config(cfg)["errors"]
    assert any("not declared" in e["message"] for e in errors)


def test_workflow_graph_requires_dependencies_for_step_references():
    graph = {"trigger": {"type": "manual"}, "nodes": [
        {"id": "a", "tool": "x.op", "inputs": {}},
        {"id": "b", "tool": "x.op", "inputs": {"v": "${steps.a.output.id}"}}]}
    assert not validate_workflow_graph(graph, {})["valid"]
    graph["nodes"][1]["depends_on"] = ["a"]
    assert validate_workflow_graph(graph, {})["valid"]


def test_openapi_import_and_errors():
    with pytest.raises(openapi.OpenAPIError):
        openapi.parse_openapi("{not json")
    with pytest.raises(openapi.OpenAPIError):
        openapi.parse_openapi('{"swagger": "2.0", "paths": {}}')


def test_redaction_removes_secrets_before_ai():
    text = redact_ai_context("my token is ghp_" + "b" * 36 + " and api_key=abc123456789 password: hunter2xx")
    assert "ghp_" not in text and "abc123456789" not in text and "hunter2xx" not in text


def test_offline_planner_chains_create_before_notify():
    from app.services.agents import local_workflow, requirements_from_tools
    tools = {
        "notify.send_message": {"method": "POST", "description": "Send an alert message (notify a channel)",
                                "input_schema": {"required": ["channel", "text"], "properties": {"channel": {}, "text": {}}}, "output_fields": ["message_id"]},
        "taskflow.create_task": {"method": "POST", "description": "Create a task",
                                 "input_schema": {"required": ["title"], "properties": {"title": {}}}, "output_fields": ["id", "url"]},
    }
    req = requirements_from_tools("When a webhook arrives with priority high, create a TaskFlow task and notify the team in Notify", tools)
    graph = local_workflow(req, tools)
    first, second = graph["nodes"]
    assert first["tool"] == "taskflow.create_task" and second["depends_on"] == [first["id"]]
    assert "${steps.n1.output.url}" in second["inputs"]["text"]
    assert validate_workflow_graph(graph, tools)["valid"]
