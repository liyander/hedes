"""Deterministic policy checks. These are authoritative; the AI security agent only adds explanation."""
from __future__ import annotations

import math
import re
from collections import Counter

from . import egress
from .validation import FORBIDDEN_HEADERS, validate_config

SEVERITY_WEIGHT = {"critical": 60, "high": 25, "medium": 10, "low": 3, "info": 0}
SECRET_PATTERNS = [
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"xox[abpors]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "API secret key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "Google API key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"), "bearer token"),
]
SENSITIVE_FIELDS = re.compile(r"(?i)(password|passwd|secret|ssn|social_security|credit_card|card_number|cvv|iban|dob|date_of_birth)")


def _entropy(s: str) -> float:
    c = Counter(s)
    return -sum(n / len(s) * math.log2(n / len(s)) for n in c.values()) if s else 0.0


def _strings(value, path=""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, f"{path}[{i}]")


def review(config: dict, policy: dict | None = None, connections: dict[str, dict] | None = None) -> dict:
    """connections: {name: {"auth_type": ..., "status": ...}} for the organisation."""
    policy = policy or {}
    findings: list[dict] = []

    def add(sev, code, title, detail, blocking=False):
        findings.append({"severity": sev, "code": code, "title": title, "detail": detail, "blocking": blocking})

    validation = validate_config(config)
    checks = {"schema_valid": validation["valid"], "destination_checked": False, "secrets_referenced_safely": True,
              "credential_connected": None, "headers_safe": True}
    if not validation["valid"]:
        add("critical", "schema_invalid", "Configuration does not validate",
            f"{len(validation['errors'])} schema/semantic error(s) must be fixed first.", True)
        return _finish(findings, checks, config, validation)

    integ = config["integration"]
    base = integ["base_url"]
    host = egress.host_of(base)
    try:
        egress.static_check(base + "/")
        checks["destination_checked"] = True
    except egress.EgressDenied as e:
        add("critical", "destination_blocked", "Destination is not allowed", str(e), True)
    allowed = policy.get("allowed_domains") or []
    if allowed and not egress.domain_allowed(host, allowed) and checks["destination_checked"]:
        if not egress.allowlisted(base + "/"):
            add("high", "destination_unlisted", "Destination outside the organisation allow-list",
                f"{host} is not in allowed_domains ({', '.join(allowed)}). Runtime calls will be refused until it is added.", True)

    for path, s in _strings(config):
        for rx, label in SECRET_PATTERNS:
            if rx.search(s):
                checks["secrets_referenced_safely"] = False
                add("critical", "inline_secret", "Credential embedded in configuration",
                    f"Something that looks like a {label} appears at {path}. Credentials must live in a connection.", True)
                break
        else:
            if len(s) >= 32 and " " not in s and "${" not in s and not s.startswith(("http", "$", "/")) and _entropy(s) > 4.2:
                add("medium", "high_entropy", "High-entropy literal value", f"{path} contains a value that may be a secret.")

    for op in integ["operations"]:
        hdrs = {**(integ.get("default_headers") or {}), **((op.get("request") or {}).get("headers") or {})}
        for h in hdrs:
            if h.lower() in FORBIDDEN_HEADERS:
                checks["headers_safe"] = False
                add("critical", "forbidden_header", "Forbidden header", f"{op['id']} sets {h}.", True)

    auth = integ["authentication"]
    if auth["type"] == "none":
        if any(op["method"] != "GET" for op in integ["operations"]):
            add("medium", "unauthenticated_writes", "Unauthenticated write operations",
                "The API is called without credentials but performs writes. Confirm this is intended.")
    else:
        ref = auth.get("credential_ref")
        conn = (connections or {}).get(ref)
        if connections is not None:
            if not conn:
                checks["credential_connected"] = False
                add("medium", "credential_not_connected", "Credential not connected yet",
                    f"No connection named {ref!r} exists. It must be created before approval.")
            elif conn.get("auth_type") != auth["type"]:
                checks["credential_connected"] = False
                add("high", "credential_mismatch", "Connection type mismatch",
                    f"Connection {ref!r} holds a {conn.get('auth_type')} credential but the integration needs {auth['type']}.", True)
            elif conn.get("status") != "active":
                checks["credential_connected"] = False
                add("medium", "credential_inactive", "Connection is not active", f"Connection {ref!r} is {conn.get('status')}.")
            else:
                checks["credential_connected"] = True
        if auth["type"] == "api_key_query":
            add("low", "key_in_query", "API key sent in the query string", "Query strings are often logged by proxies.")
        if any(s in ("admin", "repo", "write:org", "delete_repo", "*") for s in auth.get("scopes") or []):
            add("high", "broad_scopes", "Broad credential scopes", f"Requested scopes: {', '.join(auth['scopes'])}.")

    methods = Counter(op["method"] for op in integ["operations"])
    destructive = [op["id"] for op in integ["operations"] if op.get("safety") == "destructive" or op["method"] == "DELETE"]
    if destructive:
        add("high", "destructive_ops", "Destructive operations", "Can delete external data: " + ", ".join(destructive) + ".")
    writes = [op["id"] for op in integ["operations"] if op["method"] in ("POST", "PUT", "PATCH")]
    if writes:
        add("low", "write_ops", "Creates or modifies external data", ", ".join(writes) + ".")
    for op in integ["operations"]:
        sensitive = [p for p in (op["input_schema"].get("properties") or {}) if SENSITIVE_FIELDS.search(p)]
        if sensitive:
            add("medium", "sensitive_inputs", "Sensitive data in inputs", f"{op['id']} accepts {', '.join(sensitive)}.")
        if op.get("pagination") and int(op["pagination"].get("max_pages", 5)) > 10:
            add("low", "large_pagination", "Large pagination window", f"{op['id']} may fetch up to {op['pagination']['max_pages']} pages.")
    for w in validation["warnings"]:
        add("info", "validation_warning", "Validation warning", f"{w['path']}: {w['message']}")
    return _finish(findings, checks, config, validation, methods)


def _finish(findings, checks, config, validation, methods=None) -> dict:
    score = min(100, sum(SEVERITY_WEIGHT[f["severity"]] for f in findings))
    level = "critical" if any(f["severity"] == "critical" for f in findings) else \
        "high" if score >= 50 else "medium" if score >= 20 else "low"
    integ = (config or {}).get("integration") or {}
    ops = integ.get("operations") or []
    capabilities = sorted({{"GET": "Read", "POST": "Create", "PUT": "Update", "PATCH": "Update", "DELETE": "Delete"}.get(o.get("method"), "Call") for o in ops if isinstance(o, dict)})
    return {
        "score": score,
        "level": level,
        "blocking": any(f["blocking"] for f in findings),
        "findings": sorted(findings, key=lambda f: -SEVERITY_WEIGHT[f["severity"]]),
        "checks": checks,
        "destinations": [egress.host_of(integ["base_url"])] if integ.get("base_url") else [],
        "capabilities": capabilities,
        "methods": dict(methods or {}),
        "credential_refs": [integ.get("authentication", {}).get("credential_ref")] if integ.get("authentication", {}).get("credential_ref") else [],
        "operations": [{"id": o.get("id"), "method": o.get("method"), "path": o.get("path"), "safety": o.get("safety")} for o in ops if isinstance(o, dict)],
        "validation_errors": validation["errors"],
    }


def summarize(report: dict) -> str:
    """Plain-language summary used when no AI model is available to write one."""
    blocking = [f for f in report["findings"] if f["blocking"]]
    caps = " + ".join(report["capabilities"]) or "no"
    dest = ", ".join(report["destinations"]) or "no destination"
    s = f"This integration can {caps.lower()} data on {dest}. Risk score {report['score']}/100 ({report['level']})."
    if blocking:
        s += " Blocking: " + "; ".join(f["title"] for f in blocking) + "."
    else:
        s += " No blocking issues were found by the policy checks."
    return s
