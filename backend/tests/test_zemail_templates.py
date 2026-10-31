import pytest

from app.core.db import session_scope
from app.models import Organization
from app.services import email_templates as et
from app.services import notify
from conftest import login


def test_each_alert_kind_gets_its_family_template():
    assert et.template_for("auth_invalid") == et.template_for("credential") == "credential"
    assert et.template_for("approval") == "approval"
    assert et.template_for("drift") == et.template_for("repair") == "api_change"
    assert et.template_for("source_failing") == et.template_for("bad_data") == "data_source"
    assert et.template_for("upstream_error") == "integration_failure"
    assert et.template_for("something_new") == "general"


def test_rendering_escapes_and_fills_placeholders():
    m = et.render("approval", kind="approval", severity="info", message='<script>alert(1)</script> "Okta" 1.1.0', organization="Acme & Co",
                  link="https://hedes.example/#/approvals/1", overrides={"approval": {"subject": "{organization}: review {message}"}})
    assert m["subject"].startswith('Acme & Co: review <script>')          # subject is plain text (a mail header), not HTML
    assert "<script>" not in m["html"] and "&lt;script&gt;" in m["html"] and "Acme &amp; Co" in m["html"]
    assert 'href="https://hedes.example/#/approvals/1"' in m["html"] and "Open the review" in m["html"]
    assert "What to do:" in m["text"] and "https://hedes.example/#/approvals/1" in m["text"]


@pytest.mark.parametrize("field,value,err", [("subject", "a\nb", "single line"), ("intro", "{secret}", "Unknown placeholder"),
                                             ("subject", "x" * 201, "longer than"), ("html", "x", "Unknown template field")])
def test_overrides_are_validated(field, value, err):
    with pytest.raises(et.TemplateError, match=err):
        et.validate_override("credential", field, value)


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


def test_alerts_are_emailed_with_their_template(server, monkeypatch):
    admin, builder = login(server, "admin"), login(server, "builder")
    assert builder.get("/api/v1/settings/notifications/templates").status_code == 403
    r = admin.put("/api/v1/settings/notifications", json={"enabled": True, "min_severity": "info", "email": {
        "host": "smtp.example.com", "port": 587, "security": "starttls", "username": "u", "password": "pw-1234567890",
        "from": "hedes@example.com", "to": ["secops@example.com"]}})
    assert r.status_code == 200, r.text
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent.clear()
    org_id = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    try:
        # customise one template, leave the others built-in
        t = admin.put("/api/v1/settings/notifications/templates", json={"overrides": {
            "credential": {"subject": "[SOC] {severity} credential issue", "intro": ""}}}).json()
        cred = next(x for x in t["templates"] if x["key"] == "credential")
        assert cred["override"] == {"subject": "[SOC] {severity} credential issue"} and cred["preview"]["subject"] == "[SOC] HIGH credential issue"
        assert admin.put("/api/v1/settings/notifications/templates", json={"overrides": {"credential": {"subject": "{nope}"}}}).status_code == 422

        notify._dispatch([
            {"org_id": org_id, "kind": "auth_invalid", "severity": "high", "message": "GitHub: HTTP 401 (tmpl test)", "link": "#/credentials"},
            {"org_id": org_id, "kind": "approval", "severity": "info", "message": "Okta 2.0.0 is waiting for review (tmpl test)", "link": "#/approvals/x"},
        ])
        subjects = [m["Subject"] for m in FakeSMTP.sent]
        assert subjects == ["[SOC] HIGH credential issue", "[H.E.D.E.S] Review requested: Okta 2.0.0 is waiting for review (tmpl test)"]
        msg = FakeSMTP.sent[1]
        assert msg.get_content_type() == "multipart/alternative"
        html = msg.get_body(("html",)).get_content()
        assert "An integration is waiting for your review" in html and "#/approvals/x" in html
        assert "What to do:" in msg.get_body(("plain",)).get_content()

        p = admin.post("/api/v1/settings/notifications/templates/preview", json={"template": "data_source", "subject": "Source down: {message}", "intro": ""}).json()
        assert p["subject"].startswith("Source down: Okta System Log") and "A data source needs attention" in p["html"]
        FakeSMTP.sent.clear()
        r = admin.post("/api/v1/settings/notifications/test", json={"template": "api_change"}).json()
        assert r["results"]["email"] == "sent" and "external API changed" in FakeSMTP.sent[0]["Subject"]
        assert admin.post("/api/v1/settings/notifications/test", json={"template": "nope"}).status_code == 422
    finally:
        admin.put("/api/v1/settings/notifications/templates", json={"overrides": {}})
        admin.put("/api/v1/settings/notifications", json={"enabled": False, "min_severity": "high", "email": {"host": "", "to": []}})
        with session_scope() as db:
            assert "templates" in notify.config_of(db.get(Organization, org_id))
