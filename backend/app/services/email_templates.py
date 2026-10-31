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
        "button": "Open Credentials", "color": "#d97706",
        "theme": {"dark": "#92400e", "light": "#d97706", "eyebrow": "Credential alert", "tagline": "Found by H.E.D.E.S credential monitoring"},
    },
    "approval": {
        "name": "Review requested",
        "kinds": ["approval"],
        "subject": "[H.E.D.E.S] Review requested: {message}",
        "headline": "An integration is waiting for your review",
        "intro": "A new or changed integration version has passed validation and safe tests and needs an administrator's decision.",
        "action": "Open the review, check what it can do and where it connects, then approve or request changes.",
        "button": "Open the review", "color": "#4f46e5",
        "theme": {"dark": "#312e81", "light": "#4f46e5", "eyebrow": "Review requested", "tagline": "A change is waiting for an administrator"},
    },
    "api_change": {
        "name": "API changed / repair ready",
        "kinds": ["drift", "repair"],
        "subject": "[H.E.D.E.S] {severity}: an external API changed",
        "headline": "An external API changed its responses",
        "intro": "The service answered, but the fields an integration relies on have moved or disappeared. A repair is prepared for review where possible.",
        "action": "Review the proposed repair. The live version is not changed until an administrator approves it.",
        "button": "Review the change", "color": "#7c3aed",
        "theme": {"dark": "#4c1d95", "light": "#7c3aed", "eyebrow": "API change", "tagline": "Detected by response-drift monitoring"},
    },
    "data_source": {
        "name": "Data source problem",
        "kinds": ["source_failing", "bad_data"],
        "subject": "[H.E.D.E.S] {severity}: a data source needs attention",
        "headline": "A data source needs attention",
        "intro": "A scheduled data source keeps failing, or many of its records are being rejected.",
        "action": "Open Data Sources to see the last error, recent runs and rejected records.",
        "button": "Open Data Sources", "color": "#0891b2",
        "theme": {"dark": "#164e63", "light": "#0891b2", "eyebrow": "Data source alert", "tagline": "Reported by the ingestion scheduler"},
    },
    "integration_failure": {
        "name": "Integration failure",
        "kinds": ["upstream_error", "not_found", "request_rejected"],
        "subject": "[H.E.D.E.S] {severity}: an integration is failing",
        "headline": "An integration is failing",
        "intro": "Calls to an external service are failing: the service returned errors, an endpoint is missing, or requests were rejected.",
        "action": "Open the execution to see the request (credentials redacted), the response and the suggested fix.",
        "button": "Open the details", "color": "#dc2626",
        "theme": {"dark": "#7f1d1d", "light": "#dc2626", "eyebrow": "Integration failure", "tagline": "Detected by execution monitoring"},
    },
    "self_healing": {
        "name": "Self-healing report",
        "kinds": ["self_healing"],
        "subject": "[H.E.D.E.S] Action needed - {message}",
        "headline": "Self-healing needs an administrator",
        "intro": "Self-healing traced a failure and tried to fix it. It either could not fix it on its own or prepared a fix that needs your approval. "
                 "The root cause, what it tried and what is required from you are below.",
        "action": "Do what is listed under 'Required from an administrator', then the next run will confirm it is healed.",
        "button": "Open the case", "color": "#e11d48",
        "theme": {"dark": "#881337", "light": "#e11d48", "eyebrow": "Self-healing report", "tagline": "Traced by the self-healing analyst"},
    },
    "general": {
        "name": "Other alerts",
        "kinds": [],
        "subject": "[H.E.D.E.S] {severity}: {kind}",
        "headline": "New alert",
        "intro": "H.E.D.E.S raised an alert.",
        "action": "Open H.E.D.E.S for details.",
        "button": "Open H.E.D.E.S", "color": "#475569",
        "theme": {"dark": "#1e293b", "light": "#475569", "eyebrow": "Alert", "tagline": "Raised by H.E.D.E.S"},
    },
    "test": {
        "name": "Test message",
        "kinds": [],
        "subject": "[H.E.D.E.S] Test notification",
        "headline": "Alert delivery works",
        "intro": "This is a test message from H.E.D.E.S. If you can read it, email alerts will reach this mailbox.",
        "action": "Nothing to do.",
        "button": "Open H.E.D.E.S", "color": "#2f4f4a",
        "theme": {"dark": "#1f3532", "light": "#3f6b63", "eyebrow": "Test message", "tagline": "Checking that alert delivery works"},
    },
    "healing_fixed": {
        "name": "Fixed automatically",
        "kinds": ["self_healed"],
        "subject": "[H.E.D.E.S] Fixed automatically: {message}",
        "headline": "Self-healing fixed a failure on its own",
        "intro": "Automatic self-healing found the root cause, applied a non-critical fix, verified it and published it. Nothing is required from you; "
                 "the change is recorded in the audit log and can be rolled back from Version History.",
        "action": "Review the session log if you want to see exactly what changed.",
        "button": "Open the session log", "color": "#059669",
        "theme": {"dark": "#064e3b", "light": "#059669", "eyebrow": "Fixed automatically", "tagline": "Resolved by self-healing - no action needed"},
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
    "healing_fixed": ("self_healed", "info", "notify.send_message\nRoot cause: The endpoint moved from /messages to /messages/v2 (HTTP 404).\n"
                      "What was done:\n- Compared the failing path with the API documentation and prepared version 1.0.1\n- Ran the safe tests (4/4 passed)\n"
                      "- Published it automatically (non-critical: same host, same method, no new permissions) and re-checked the service", "#/healing"),
}
SEVERITY_COLORS = {"critical": "#c62828", "high": "#e8590c", "medium": "#a15c00", "low": "#6b6b6b", "info": "#1f5fbf"}


_NOISE_WORDS = ("h3d3s", "1ngest", "c0nf1g", "sch3ma", "v4ult", "p4g1n8", "r3try", "0x7f", "appr0ve", "m4pp1ng", "3v3nt", "s0urce", "dr1ft",
                "h34l", "t0k3n", "c0rr3l8", "sync", "4ud1t", "w4tch", "1ntgr8")


def _noise(key: str) -> list[str]:
    """Faint 'data stream' lines for the banner (deterministic per template, text only - no images to block)."""
    seed = sum(map(ord, key))
    rows = []
    for r in range(3):
        words = [_NOISE_WORDS[(seed * (r + 3) + i * 7) % len(_NOISE_WORDS)] for i in range(12)]
        rows.append(" ".join(words)[:78])
    return rows


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
    th = t.get("theme") or TEMPLATES["general"]["theme"]
    sev_color = SEVERITY_COLORS.get(severity, "#6b6b6b")
    font = "Segoe UI,Helvetica,Arial,sans-serif"
    noise = _noise(key)
    lines = [ln.rstrip() for ln in message.strip().splitlines()]
    detail, bullets = [], []

    def flush():
        if bullets:
            detail.append('<ul style="margin:6px 0 10px;padding-left:20px">' + "".join(f'<li style="margin:3px 0">{e(x)}</li>' for x in bullets) + "</ul>")
            bullets.clear()
    for ln in lines:
        if ln.lstrip().startswith("- "):
            bullets.append(ln.lstrip()[2:])
            continue
        flush()
        if ln.strip():
            label, sep, rest = ln.partition(": ")
            detail.append(f'<p style="margin:0 0 8px"><b>{e(label)}:</b> {e(rest)}</p>' if sep and len(label) <= 32 and rest
                          else f'<p style="margin:0 0 8px">{e(ln)}</p>')
    flush()
    cta = (f'<tr><td align="center" style="padding:6px 40px 6px"><a href="{e(link, quote=True)}" style="display:inline-block;background:{th["dark"]};color:#ffffff;'
           f'text-decoration:none;font-family:{font};font-weight:700;font-size:16px;padding:14px 32px;border-radius:999px">{e(t["button"])}</a></td></tr>'
           f'<tr><td align="center" style="padding:10px 40px 0;font-family:{font};font-size:12px;color:#7a7a7a">Or paste this link into your browser:<br>'
           f'<a href="{e(link, quote=True)}" style="color:#1d4ed8">{e(link)}</a></td></tr>') if link else ""
    console = link.split("#")[0] if link else ""
    body = f"""<!doctype html><html><head><meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light"></head>
<body style="margin:0;padding:0;background:#f3f2ef">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f2ef;padding:24px 10px"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fcfcfb;font-family:{font};color:#1c1c1a">
<tr><td bgcolor="{th['dark']}" style="background:{th['dark']};background-image:linear-gradient(135deg,{th['dark']} 0%,{th['light']} 100%);padding:22px 28px 26px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
  <tr><td style="font-family:{font};font-size:15px;font-weight:700;letter-spacing:.18em;color:#ffffff">&#8644;&nbsp; H.E.D.E.S</td></tr>
  <tr><td style="padding-top:12px;font-family:Consolas,Menlo,monospace;font-size:12px;line-height:18px;color:rgba(255,255,255,.22);white-space:nowrap;overflow:hidden">{e(noise[0])}<br>{e(noise[1])}</td></tr>
  <tr><td align="center" style="padding:6px 0 2px;font-family:{font};font-size:40px;line-height:1.1;font-weight:700;color:#ffffff">{e(th['eyebrow'])}</td></tr>
  <tr><td align="center" style="padding:6px 0 0;font-family:{font};font-size:15px;color:rgba(255,255,255,.88)">{e(th['tagline'])}</td></tr>
  <tr><td style="padding-top:12px;font-family:Consolas,Menlo,monospace;font-size:12px;line-height:18px;color:rgba(255,255,255,.22);white-space:nowrap;overflow:hidden">{e(noise[2])}</td></tr>
  </table></td></tr>
<tr><td align="center" style="padding:30px 40px 0"><span style="display:inline-block;font-size:12px;font-weight:700;letter-spacing:.06em;color:{sev_color};border:1px solid {sev_color};border-radius:999px;padding:3px 12px">{e(severity.upper())}</span></td></tr>
<tr><td align="center" style="padding:14px 40px 0;font-family:{font};font-size:28px;line-height:1.25;font-weight:700;color:#141414">{e(t['headline'])}</td></tr>
<tr><td align="center" style="padding:14px 48px 22px;font-family:{font};font-size:16px;line-height:1.6;color:#2f2f2f">{e(intro).replace(chr(10), '<br>')}</td></tr>
{cta}
<tr><td style="padding:28px 40px 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-left:4px solid {th['light']};background:#ffffff;border-radius:4px">
  <tr><td style="padding:16px 18px;font-family:{font};font-size:14px;line-height:1.55;color:#262626">
    <div style="font-size:12px;font-weight:700;letter-spacing:.08em;color:{th['dark']};margin-bottom:8px">WHAT HAPPENED</div>{''.join(detail) or e(message)}</td></tr></table></td></tr>
<tr><td style="padding:18px 40px 0;font-family:{font};font-size:14px;line-height:1.55;color:#262626">
  <div style="font-size:12px;font-weight:700;letter-spacing:.08em;color:{th['dark']};margin-bottom:6px">WHAT TO DO</div>{e(t['action'])}</td></tr>
<tr><td style="padding:18px 40px 0;font-family:{font};font-size:12px;color:#7a7a7a">{e(organization)} &middot; {e(kind.replace('_', ' '))} &middot; {e(when)}</td></tr>
<tr><td align="center" style="padding:22px 40px 0;font-family:{font};font-size:13px;line-height:1.5;color:#6b6b6b">You receive this as an administrator of {e(organization)}. Which alerts are emailed is set under Settings, Alert delivery.</td></tr>
<tr><td style="padding:28px 40px 0"><div style="border-top:1px solid #e3e1dc;line-height:1px;font-size:1px">&nbsp;</div></td></tr>
<tr><td align="center" style="padding:24px 40px 0"><table role="presentation" cellpadding="0" cellspacing="0"><tr><td width="42" height="42" align="center" bgcolor="#1f2a37"
  style="background:#1f2a37;border-radius:9px;width:42px;height:42px;font-size:20px;line-height:42px;color:#ffffff;font-family:{font}">&#8644;</td></tr></table></td></tr>
<tr><td align="center" style="padding:12px 40px 0;font-family:{font};font-size:14px;font-weight:700;letter-spacing:.22em;color:#141414">H.E.D.E.S</td></tr>
<tr><td align="center" style="padding:6px 40px 0;font-family:{font};font-size:13px;color:#6b6b6b">Hybrid Automated Data Exchange System{f' &middot; <a href="{e(console, quote=True)}" style="color:#0f766e">Open the console</a>' if console else ''}</td></tr>
<tr><td align="center" style="padding:4px 40px 30px;font-family:{font};font-size:12px;color:#8a8a8a">This message was sent automatically. Replies are not read.</td></tr>
</table></td></tr></table></body></html>"""
    return {"subject": subject, "text": text, "html": body}


def render_sample(key: str, organization: str, base_url: str, overrides: dict | None = None) -> dict:
    kind, severity, message, link = SAMPLES[key]
    return render(key, kind=kind, severity=severity, message=message, organization=organization,
                  link=f"{base_url.rstrip('/')}/{link}" if link else base_url, overrides=overrides)
