"""APIs that answer HTTP 200 for failures (Slack's {"ok": false}) must be reported as failures, with the API's reason."""
import httpx

from app.services import catalog, engine, integrations
from app.services.validation import validate_config

SLACK = catalog.known_services()["slack"]


def slack_reply(body):
    return httpx.MockTransport(lambda r: httpx.Response(200, json=body))


def test_slack_missing_scope_is_a_failure_with_the_reason():
    r = engine.execute(SLACK, "post_message", {"channel": "#all-hedes", "text": "hello world"}, credential={"token": "xoxb-x"},
                       transport=slack_reply({"ok": False, "error": "missing_scope", "needed": "chat:write", "provided": "identify,channels:read"}),
                       live=False)
    assert r.status == "failed" and r.error_class == "forbidden" and r.http_status == 200
    assert r.error == "HTTP 200 but the API reported a failure: missing_scope (needed: chat:write; provided: identify,channels:read)"


def test_other_slack_errors_are_classified():
    cases = {"invalid_auth": "auth_invalid", "channel_not_found": "not_found", "not_in_channel": "request_rejected", "ratelimited": "rate_limited"}
    for code, cls in cases.items():
        r = engine.execute(SLACK, "post_message", {"channel": "#x", "text": "t"}, credential={"token": "t"},
                           transport=slack_reply({"ok": False, "error": code}), live=False)
        assert (r.status, r.error_class) == ("failed", cls) and code in r.error


def test_a_real_success_still_succeeds():
    r = engine.execute(SLACK, "post_message", {"channel": "#x", "text": "t"}, credential={"token": "t"},
                       transport=slack_reply({"ok": True, "channel": "C123", "ts": "1730.01"}), live=False)
    assert r.status == "succeeded" and r.output == {"ok": True, "channel": "C123", "ts": "1730.01"}


def test_slack_config_validates_and_its_generated_tests_cover_the_failure():
    assert validate_config(SLACK)["valid"]
    from app.services import agents
    cases = agents.generate_tests(SLACK, [])
    assert any("API-level failure sent with HTTP 200" in c["name"] for c in cases)
    report = integrations.run_mock_tests(SLACK, cases)
    assert report["failed"] == 0, [c for c in report["cases"] if not c["passed"]]


def test_bad_success_paths_are_rejected():
    bad = {**SLACK, "integration": {**SLACK["integration"], "operations": [
        {**SLACK["integration"]["operations"][0], "response": {"status_codes": [200], "success": {"path": "$..ok", "equals": True}}}]}}
    assert any("success" in e["path"] for e in validate_config(bad)["errors"])
