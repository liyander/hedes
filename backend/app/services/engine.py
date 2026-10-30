"""Generic configuration-driven HTTP execution engine.

Executes any operation described by an integration configuration. It contains no service-specific code
and no database access: callers hand it a published configuration, validated inputs and (optionally) a
decrypted credential. ``transport`` lets tests and mock runs swap the network for canned responses.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from jsonschema import Draft202012Validator

from . import egress, jsonpath, templates

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RETRYABLE_STATUS = {429, 502, 503, 504}
IDEMPOTENT_METHODS = {"GET", "PUT", "DELETE"}

ERROR_GUIDE = {
    "input_invalid": ("The inputs do not match the operation's input schema.", "Fix the inputs and try again."),
    "credential_missing": ("No usable credential is connected for this integration.", "Create or re-activate the connection in Credentials."),
    "egress_denied": ("The destination was blocked by the network egress policy.", "Only approved public HTTPS destinations can be called."),
    "auth_invalid": ("HTTP 401: the API rejected the credentials (invalid or expired).", "Update the token in Credentials and re-test the connection."),
    "forbidden": ("HTTP 403: the credential lacks the required permission or scope.", "Grant the missing scope to the token, or use a different account."),
    "not_found": ("HTTP 404: the endpoint or resource does not exist.", "Check the identifiers in the inputs; if it persists the endpoint may have moved - request a repair."),
    "rate_limited": ("HTTP 429: the API is rate-limiting requests.", "Retried within policy; slow down or raise the API quota."),
    "upstream_error": ("HTTP 5xx: the external service failed.", "Usually transient. Retried within policy for idempotent operations."),
    "request_rejected": ("HTTP 4xx: the API rejected the request body or parameters.", "The API's request schema may have changed - request a repair."),
    "unexpected_status": ("The API returned a status code the configuration does not expect.", "Review the operation's expected status codes."),
    "timeout": ("The request timed out.", "The service may be slow or unreachable; try again later."),
    "network_error": ("The service could not be reached.", "Check that the base URL is correct and the service is up."),
    "response_invalid": ("The response was not valid JSON.", "Set response.format to text or fix the endpoint."),
    "response_too_large": ("The response exceeded the 2 MB limit.", "Use pagination or narrower queries."),
    "response_drift": ("The API answered, but fields the output mapping expects are missing.", "The response structure changed. A repair can be proposed for admin review."),
    "config_error": ("The configuration could not be rendered into a request.", "Validate the integration configuration."),
}


class EngineError(Exception):
    def __init__(self, error_class: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.error_class = error_class
        self.http_status = http_status


@dataclass
class PreparedRequest:
    method: str
    url: str
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    json_body: Any = None
    form_body: dict | None = None


@dataclass
class EngineResult:
    status: str
    output: Any = None
    error_class: str = ""
    error: str = ""
    http_status: int | None = None
    duration_ms: int = 0
    attempts: int = 0
    request_preview: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("status", "output", "error_class", "error", "http_status",
                                               "duration_ms", "attempts", "request_preview", "diagnostics")}


def find_operation(config: dict, op_id: str) -> dict:
    for op in config["integration"]["operations"]:
        if op["id"] == op_id:
            return op
    raise EngineError("config_error", f"Unknown operation {op_id!r}")


def validate_inputs(op: dict, inputs: dict) -> list[str]:
    if not isinstance(inputs, dict):
        return ["Inputs must be a JSON object"]
    v = Draft202012Validator(op.get("input_schema") or {"type": "object"})
    return [f"{'/'.join(map(str, e.absolute_path)) or 'inputs'}: {e.message}"[:300] for e in v.iter_errors(inputs)]


def _query_value(v: Any) -> Any:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict,)):
        return json.dumps(v, separators=(",", ":"))
    if isinstance(v, list):
        return [_query_value(x) for x in v]
    return str(v)


def build_request(config: dict, op: dict, inputs: dict) -> PreparedRequest:
    integ = config["integration"]
    ctx = {"inputs": inputs}
    req = op.get("request") or {}
    try:
        path_params = templates.render(req.get("path_params") or {}, ctx)
        path = op["path"]
        for name in re.findall(r"\{([A-Za-z0-9_]+)\}", path):
            if name not in path_params or path_params[name] in ("", None):
                raise EngineError("input_invalid", f"Missing value for path parameter {name!r}")
            value = str(path_params[name])
            if value in (".", ".."):
                raise EngineError("input_invalid", f"Invalid value for path parameter {name!r}")
            path = path.replace("{" + name + "}", quote(value, safe=""))
        query = {k: _query_value(v) for k, v in templates.render(req.get("query") or {}, ctx).items()}
        headers = {**(integ.get("default_headers") or {}), **templates.render(req.get("headers") or {}, ctx)}
        headers = {k: str(v) for k, v in headers.items()}
        json_body = templates.render(req["json_body"], ctx) if req.get("json_body") is not None else None
        if json_body is templates.MISSING:
            json_body = None
        form_body = templates.render(req["form_body"], ctx) if req.get("form_body") is not None else None
    except templates.TemplateError as e:
        raise EngineError("config_error", str(e))
    base = integ["base_url"].rstrip("/")
    url = base + path
    if urlsplit(url).netloc != urlsplit(base).netloc:
        raise EngineError("config_error", "Rendered URL left the configured base URL")
    return PreparedRequest(op["method"], url, query, headers, json_body, form_body)


def apply_auth(req: PreparedRequest, auth: dict, credential: dict | None) -> None:
    kind = auth.get("type", "none")
    if kind == "none":
        return
    secret = credential or {}
    if kind == "bearer" and secret.get("token"):
        req.headers["Authorization"] = f"Bearer {secret['token']}"
    elif kind == "api_key_header" and secret.get("api_key"):
        req.headers[auth.get("header_name") or "X-API-Key"] = secret["api_key"]
    elif kind == "api_key_query" and secret.get("api_key"):
        req.query[auth.get("param_name") or "api_key"] = secret["api_key"]
    elif kind == "basic" and secret.get("username") is not None:
        raw = f"{secret['username']}:{secret.get('password', '')}".encode()
        req.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    else:
        raise EngineError("credential_missing", f"The connection does not hold a {kind} credential")


def redact(req: PreparedRequest, auth: dict) -> dict:
    secret_header = (auth.get("header_name") or "").lower() if auth.get("type") == "api_key_header" else ""
    secret_param = auth.get("param_name") if auth.get("type") == "api_key_query" else None
    headers = {k: ("••••••" if k.lower() in ("authorization", secret_header) else v) for k, v in req.headers.items()}
    query = {k: ("••••••" if k == secret_param else v) for k, v in req.query.items()}
    preview = {"method": req.method, "url": req.url, "query": query, "headers": headers}
    if req.json_body is not None:
        preview["json"] = req.json_body
    if req.form_body is not None:
        preview["form"] = req.form_body
    return preview


def _classify_status(code: int) -> str:
    if code == 401:
        return "auth_invalid"
    if code == 403:
        return "forbidden"
    if code == 404:
        return "not_found"
    if code == 429:
        return "rate_limited"
    if code >= 500:
        return "upstream_error"
    if code in (400, 409, 415, 422):
        return "request_rejected"
    return "unexpected_status"


def map_output(resp_cfg: dict, body: Any) -> tuple[Any, list[str]]:
    mapping = resp_cfg.get("mapping") or {}
    optional = set(resp_cfg.get("optional_fields") or [])
    missing: list[str] = []

    def map_one(doc: Any, check: bool) -> dict:
        out = {}
        for name, path in mapping.items():
            found, value = jsonpath.query(doc, path)
            if found:
                out[name] = value
            elif check and name not in optional:
                missing.append(f"{name} ({path})")
        return out

    if resp_cfg.get("items_path"):
        found, items = jsonpath.query(body, resp_cfg["items_path"])
        if not found or not isinstance(items, list):
            return {"items": [], "count": 0}, [f"items ({resp_cfg['items_path']})"]
        if not mapping:
            return {"items": items, "count": len(items)}, []
        mapped = [map_one(it, i == 0) for i, it in enumerate(items)]
        return {"items": mapped, "count": len(mapped)}, missing
    if not mapping:
        return body, []
    return map_one(body, True), missing


def _send(client: httpx.Client, req: PreparedRequest, timeout: float) -> tuple[int, dict, bytes]:
    kwargs: dict = {"params": req.query, "headers": req.headers, "timeout": timeout}
    if req.json_body is not None:
        kwargs["json"] = req.json_body
    elif req.form_body is not None:
        kwargs["data"] = req.form_body
    with client.stream(req.method, req.url, **kwargs) as r:
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf.extend(chunk)
            if len(buf) > MAX_RESPONSE_BYTES:
                raise EngineError("response_too_large", "Response exceeded 2 MB", r.status_code)
        return r.status_code, dict(r.headers), bytes(buf)


def execute(config: dict, op_id: str, inputs: dict, *, credential: dict | None = None,
            transport: httpx.BaseTransport | None = None, allowed_domains: list[str] | None = None,
            live: bool = True) -> EngineResult:
    """Run one operation. ``live=False`` (mock/test runs) skips DNS-based egress checks."""
    started = time.monotonic()
    result = EngineResult(status="failed")
    try:
        integ = config["integration"]
        op = find_operation(config, op_id)
        problems = validate_inputs(op, inputs or {})
        if problems:
            raise EngineError("input_invalid", "; ".join(problems[:5]))
        req = build_request(config, op, inputs or {})
        auth = integ.get("authentication") or {"type": "none"}
        apply_auth(req, auth, credential)
        result.request_preview = redact(req, auth)
        try:
            if live:
                egress.check_url(req.url, allowed_domains)
            else:
                egress.static_check(req.url)
        except egress.EgressDenied as e:
            raise EngineError("egress_denied", str(e))

        resp_cfg = op.get("response") or {}
        expected = set(resp_cfg.get("status_codes") or [200, 201, 202, 204])
        timeout = float(op.get("timeout_seconds") or integ.get("timeout_seconds") or 15)
        retry = op.get("retry") or {}
        can_retry = op["method"] in IDEMPOTENT_METHODS or bool(op.get("idempotent"))
        max_attempts = int(retry.get("max_attempts", 2 if can_retry else 1)) if can_retry else 1
        backoff = float(retry.get("backoff_seconds", 0.5))
        pag = op.get("pagination")
        pages_left = int(pag.get("max_pages", 5)) if pag else 1
        page_no, cursor = 1, None
        collected: list = []
        first_body: Any = None

        with httpx.Client(transport=transport, follow_redirects=False) as client:
            while pages_left > 0:
                if pag and pag["type"] == "page":
                    req.query[pag.get("page_param", "page")] = str(page_no)
                    if pag.get("size_param"):
                        req.query[pag["size_param"]] = str(pag.get("size", 50))
                elif pag and cursor is not None:
                    req.query[pag["cursor_param"]] = str(cursor)

                status, headers, raw = 0, {}, b""
                for attempt in range(1, max_attempts + 1):
                    result.attempts += 1
                    try:
                        status, headers, raw = _send(client, req, timeout)
                    except httpx.TimeoutException:
                        if attempt == max_attempts:
                            raise EngineError("timeout", f"No response within {timeout:g}s")
                    except httpx.TransportError as e:
                        if attempt == max_attempts:
                            raise EngineError("network_error", f"{type(e).__name__}: {str(e)[:200]}")
                    else:
                        if status not in RETRYABLE_STATUS or attempt == max_attempts:
                            break
                    wait = backoff * (2 ** (attempt - 1))
                    ra = headers.get("retry-after", "")
                    if ra.isdigit():
                        wait = max(wait, float(ra))
                    time.sleep(min(wait, 10.0))

                result.http_status = status
                if status not in expected:
                    cls = _classify_status(status)
                    detail = raw[:300].decode("utf-8", "replace").strip()
                    raise EngineError(cls, f"HTTP {status}" + (f": {detail}" if detail else ""), status)
                if (resp_cfg.get("format") or "json") == "text":
                    body: Any = {"text": raw.decode("utf-8", "replace")}
                elif not raw.strip():
                    body = {}
                else:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        raise EngineError("response_invalid", "Response body is not valid JSON", status)
                if first_body is None:
                    first_body = body
                if not pag:
                    break
                found, items = jsonpath.query(body, resp_cfg["items_path"])
                items = items if found and isinstance(items, list) else []
                collected.extend(items)
                pages_left -= 1
                if pag["type"] == "page":
                    if len(items) < int(pag.get("size", 50)) or not items:
                        break
                    page_no += 1
                else:
                    ok, cursor = jsonpath.query(body, pag["next_cursor_path"])
                    if not ok or cursor in (None, ""):
                        break

        if pag:
            items_cfg = {**resp_cfg, "items_path": "$"}
            output, missing = map_output(items_cfg, collected)
        else:
            output, missing = map_output(resp_cfg, first_body)
        result.diagnostics = {"response_shape": jsonpath.shape(first_body)}
        result.output = output
        if missing:
            result.diagnostics["missing_fields"] = missing
            raise EngineError("response_drift", "Expected fields missing from response: " + ", ".join(missing), result.http_status)
        result.status = "succeeded"
    except EngineError as e:
        result.error_class = e.error_class
        result.error = str(e)
        if e.http_status:
            result.http_status = e.http_status
    except (KeyError, TypeError) as e:
        result.error_class = "config_error"
        result.error = f"Configuration error: {e}"
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


def preview(config: dict, op_id: str, inputs: dict) -> dict:
    """Dry run: render the request exactly as it would be sent, with placeholder credentials."""
    op = find_operation(config, op_id)
    problems = validate_inputs(op, inputs or {})
    req = build_request(config, op, inputs or {}) if not problems else None
    if req is None:
        return {"ok": False, "errors": problems}
    auth = config["integration"].get("authentication") or {"type": "none"}
    placeholder = {"token": "x", "api_key": "x", "username": "x", "password": "x"}
    apply_auth(req, auth, placeholder)
    out = {"ok": True, "request": redact(req, auth)}
    try:
        egress.static_check(req.url)
    except egress.EgressDenied as e:
        out["egress_warning"] = str(e)
    return out
