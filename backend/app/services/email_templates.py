"""Alert email templates: one per family of alerts, rendered as HTML (laid out for Gmail and other clients) plus plain text.

Administrators may override a template's subject and intro text in Settings. Overrides are plain text with the
placeholders {severity}, {kind}, {organization} and {message}; everything dynamic is HTML-escaped when rendered.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

PLACEHOLDERS = ("severity", "kind", "organization", "message")

# key -> definition. "kinds" are the alert kinds this template is used for.
TEMPLATES: dict[str, dict] = {
    "credential": {
        "name": "Credential problem",
        "kinds": ["auth_invalid", "forbidden", "credential_missing", "credential"],
        "subject": "[H.E.D.E.S] Action needed: a credential needs attention",
        "headline": "A credential needs attention",
        "intro": "An integration could not authenticate, or one of its credentials is expired, missing or due for rotation.",
        "action": "Open Credentials, rotate or reconnect the secret, then press Test.",
        "button": "Open Credentials", "color": "#c62828",
    },
    "approval": {
        "name": "Review requested",
        "kinds": ["approval"],
        "subject": "[H.E.D.E.S] Review requested: {message}",
        "headline": "An integration is waiting for your review",
        "intro": "A new or changed integration version has passed validation and safe tests and needs an administrator's decision.",
        "action": "Open the review, check what it can do and where it connects, then approve or request changes.",
        "button": "Open the review", "color": "#1f5fbf",
    },
    "api_change": {
        "name": "API changed / repair ready",
        "kinds": ["drift", "repair"],
        "subject": "[H.E.D.E.S] {severity}: an external API changed",
        "headline": "An external API changed its responses",
        "intro": "The service answered, but the fields an integration relies on have moved or disappeared. A repair is prepared for review where possible.",
        "action": "Review the proposed repair. The live version is not changed until an administrator approves it.",
        "button": "Review the change", "color": "#e8590c",
    },
    "data_source": {
        "name": "Data source problem",
        "kinds": ["source_failing", "bad_data"],
        "subject": "[H.E.D.E.S] {severity}: a data source needs attention",
        "headline": "A data source needs attention",
        "intro": "A scheduled data source keeps failing, or many of its records are being rejected.",
        "action": "Open Data Sources to see the last error, recent runs and rejected records.",
        "button": "Open Data Sources", "color": "#a15c00",
    },
    "integration_failure": {
        "name": "Integration failure",
        "kinds": ["upstream_error", "not_found", "request_rejected"],
        "subject": "[H.E.D.E.S] {severity}: an integration is failing",
        "headline": "An integration is failing",
        "intro": "Calls to an external service are failing: the service returned errors, an endpoint is missing, or requests were rejected.",
        "action": "Open the execution to see the request (credentials redacted), the response and the suggested fix.",
        "button": "Open the details", "color": "#c62828",
    },
    "self_healing": {
        "name": "Self-healing report",
        "kinds": ["self_healing"],
        "subject": "[H.E.D.E.S] Action needed - {message}",
        "headline": "Self-healing needs an administrator",
        "intro": "Self-healing traced a failure and tried to fix it. It either could not fix it on its own or prepared a fix that needs your approval. "
                 "The root cause, what it tried and what is required from you are below.",
        "action": "Do what is listed under 'Required from an administrator', then the next run will confirm it is healed.",
        "button": "Open the case", "color": "#7c3aed",
    },
    "general": {
        "name": "Other alerts",
        "kinds": [],
        "subject": "[H.E.D.E.S] {severity}: {kind}",
        "headline": "New alert",
        "intro": "H.E.D.E.S raised an alert.",
        "action": "Open H.E.D.E.S for details.",
        "button": "Open H.E.D.E.S", "color": "#0f766e",
    },
    "test": {
        "name": "Test message",
        "kinds": [],
        "subject": "[H.E.D.E.S] Test notification",
        "headline": "Alert delivery works",
        "intro": "This is a test message from H.E.D.E.S. If you can read it, email alerts will reach this mailbox.",
        "action": "Nothing to do.",
        "button": "Open H.E.D.E.S", "color": "#0f766e",
    },
}
EDITABLE = ("subject", "intro")
SAMPLES = {
    "credential": ("auth_invalid", "high", "GitHub Issues: HTTP 401: the API rejected the credentials (invalid or expired). Update the token in Credentials and re-test the connection.", "#/credentials"),
    "approval": ("approval", "info", "Okta 1.1.0 is waiting for review", "#/approvals"),
    "api_change": ("drift", "high", "TaskFlow: response structure changed for taskflow.create_task - assignee_user_name ($.assignee.user_name)", "#/monitoring"),
    "data_source": ("source_failing", "high", "Okta System Log: 3 consecutive runs failed (HTTP 429).", "#/sources"),
    "integration_failure": ("upstream_error", "medium", "Jira Cloud: the external service is failing (HTTP 503).", "#/monitoring"),
    "self_healing": ("self_healing", "high", "Self-healing could not fix this on its own: okta.list_system_log\n"
                     "Endpoint: GET https://acme.okta.com/api/v1/logs (HTTP 401)\n"
                     "Root cause: HTTP 401: the API rejected the credentials (invalid or expired). Secrets can only be changed by a person.\n"
                     "What I tried:\n- Re-tested the connection with the stored credential: auth_invalid: HTTP 401\n"
                     "Still failing: HTTP 401: Invalid token provided\n"
                     "Required from an administrator:\n- Rotate or reconnect the credential 'okta' (ssws) in Credentials, then press Test.", "#/monitoring"),
    "general": ("custom", "medium", "Something happened that needs a look.", ""),
    "test": ("test", "info", "If you can read this, alert delivery works.", ""),
}
SEVERITY_COLORS = {"critical": "#c62828", "high": "#e8590c", "medium": "#a15c00", "low": "#6b6b6b", "info": "#1f5fbf"}


def template_for(kind: str) -> str:
    return next((k for k, t in TEMPLATES.items() if kind in t["kinds"]), "general")


class TemplateError(ValueError):
    pass


def validate_override(key: str, field: str, value: str) -> str:
    if key not in TEMPLATES or field not in EDITABLE:
        raise TemplateError(f"Unknown template field {key}.{field}")
    value = value.strip()
    limit = 200 if field == "subject" else 1000
    if len(value) > limit:
        raise TemplateError(f"{TEMPLATES[key]['name']}: the {field} is longer than {limit} characters")
    if field == "subject" and re.search(r"[\r\n]", value):
        raise TemplateError("The subject must be a single line")
    for name in re.findall(r"\{([^{}]*)\}", value):
        if name not in PLACEHOLDERS:
            raise TemplateError(f"Unknown placeholder {{{name}}}. Use {', '.join('{' + p + '}' for p in PLACEHOLDERS)}")
    return value


def _fill(text: str, values: dict) -> str:
    return re.sub(r"\{(" + "|".join(PLACEHOLDERS) + r")\}", lambda m: str(values[m.group(1)]), text)


def render(key: str, *, kind: str, severity: str, message: str, organization: str, link: str, overrides: dict | None = None) -> dict:
    """Return {"subject", "text", "html"} for one alert."""
    t = {**TEMPLATES[key], **{f: v for f, v in ((overrides or {}).get(key) or {}).items() if f in EDITABLE and v}}
    values = {"severity": severity.upper(), "kind": kind.replace("_", " "), "organization": organization, "message": message}
    first_line = (message.strip().splitlines() or [""])[0]
    subject = re.sub(r"\s+", " ", _fill(t["subject"], {**values, "message": first_line})).strip()[:200]
    intro = _fill(t["intro"], values)
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = "\n".join([t["headline"], "", intro, "", f"What happened: {message}", f"Severity: {severity.upper()}", f"Organisation: {organization}",
                      f"Time: {when}", "", f"What to do: {t['action']}"] + ([f"Open: {link}"] if link else []))
    e = html.escape
    sev_color = SEVERITY_COLORS.get(severity, "#6b6b6b")
    button = (f'<tr><td style="padding:8px 28px 28px"><a href="{e(link, quote=True)}" style="display:inline-block;background:{t["color"]};color:#ffffff;'
              f'text-decoration:none;font-weight:600;padding:11px 20px;border-radius:8px">{e(t["button"])}</a></td></tr>') if link else ""
    body = f"""<!doctype html><html><body style="margin:0;padding:0;background:#f3f4f4">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f4;padding:24px 12px"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border:1px solid #e6e6e6;border-radius:12px;font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#1d1d1d">
<tr><td style="border-top:4px solid {t['color']};border-radius:12px 12px 0 0;padding:20px 28px 0;font-size:13px;letter-spacing:.08em;font-weight:700;color:#0f766e">H.E.D.E.S</td></tr>
<tr><td style="padding:10px 28px 0"><span style="display:inline-block;font-size:12px;font-weight:700;color:{sev_color};border:1px solid {sev_color};border-radius:999px;padding:2px 10px">{e(severity.upper())}</span></td></tr>
<tr><td style="padding:12px 28px 0;font-size:22px;font-weight:600;line-height:1.3">{e(t['headline'])}</td></tr>
<tr><td style="padding:10px 28px 0;font-size:15px;line-height:1.55;color:#3d3d3d">{e(intro).replace(chr(10), '<br>')}</td></tr>
<tr><td style="padding:16px 28px 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fafafa;border:1px solid #e6e6e6;border-radius:8px">
<tr><td style="padding:14px 16px;font-size:14px;line-height:1.5"><b>What happened</b><br>{e(message).replace(chr(10), '<br>')}</td></tr></table></td></tr>
<tr><td style="padding:16px 28px 0;font-size:14px;line-height:1.5"><b>What to do</b><br>{e(t['action'])}</td></tr>
<tr><td style="padding:16px 28px 4px;font-size:13px;color:#6b6b6b">{e(organization)} &middot; {e(kind.replace('_', ' '))} &middot; {e(when)}</td></tr>
{button}
</table>
<p style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:12px;color:#8a8a8a;margin:14px 0 0">You receive this because alert delivery is enabled in H.E.D.E.S Settings.</p>
</td></tr></table></body></html>"""
    return {"subject": subject, "text": text, "html": body}


def render_sample(key: str, organization: str, base_url: str, overrides: dict | None = None) -> dict:
    kind, severity, message, link = SAMPLES[key]
    return render(key, kind=kind, severity=severity, message=message, organization=organization,
                  link=f"{base_url.rstrip('/')}/{link}" if link else base_url, overrides=overrides)
