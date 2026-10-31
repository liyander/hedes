"""Structural (JSON Schema) and semantic validation of integration configurations and workflow graphs."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ..core.config import SHARED_DIR
from . import cron, jsonpath, templates

ENTITY_FIELD_NAMES = {
    "event": ("event_id", "timestamp", "category", "action", "actor", "target", "outcome", "severity", "message"),
    "user": ("id", "name", "email", "status", "last_login"),
    "device": ("id", "hostname", "ip", "os", "owner", "last_seen"),
}
# The PRD's Event/Alert names (id, type, description) are accepted in an event mapping and stored under the platform's names.
EVENT_ALIASES = {"id": "event_id", "type": "category", "description": "message"}


def resolve_event_aliases(ing: dict) -> dict:
    """Rewrite PRD-style event field names in ``fields``/``value_maps``/``defaults`` to the stored names.
    A mapped ``type`` also fills ``action`` when that is not mapped, so a config written only in the PRD's terms is complete."""
    if (ing.get("entity") or "event") != "event" or not any(a in (ing.get(k) or {}) for k in ("fields", "value_maps", "defaults") for a in EVENT_ALIASES):
        return ing
    out = dict(ing)
    for key in ("fields", "value_maps", "defaults"):
        if ing.get(key):
            out[key] = {EVENT_ALIASES.get(k, k): v for k, v in ing[key].items() if not (k in EVENT_ALIASES and EVENT_ALIASES[k] in ing[key])}
    fields = ing.get("fields") or {}
    if "type" in fields and "action" not in fields:
        out["fields"]["action"] = fields["type"]
        maps = ing.get("value_maps") or {}
        if "type" in maps and "action" not in maps:
            out["value_maps"]["action"] = maps["type"]
    return out

SAFE_BY_METHOD = {"GET": "read", "POST": "write", "PUT": "write", "PATCH": "write", "DELETE": "destructive"}
FORBIDDEN_HEADERS = {"authorization", "cookie", "host", "content-length", "transfer-encoding", "connection",
                     "proxy-authorization", "x-forwarded-for", "x-forwarded-host", "x-real-ip"}


@lru_cache
def _schema(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((SHARED_DIR / name).read_text()))


def config_hash(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def normalize_config(config: dict) -> dict:
    """Fill in defaults that are implied by the format so stored versions are explicit."""
    cfg = copy.deepcopy(config)
    integ = cfg.get("integration") or {}
    if isinstance(integ.get("base_url"), str):
        integ["base_url"] = integ["base_url"].rstrip("/")
    for op in integ.get("operations") or []:
        if isinstance(op, dict) and isinstance(op.get("method"), str):
            op["method"] = op["method"].upper()
            op.setdefault("safety", SAFE_BY_METHOD.get(op["method"], "write"))
    return cfg


def _path(err) -> str:
    return "/".join(str(p) for p in err.absolute_path) or "(root)"


def validate_config(config: dict) -> dict:
    errors: list[dict] = []
    warnings: list[dict] = []
    for err in sorted(_schema("integration.schema.json").iter_errors(config), key=lambda e: list(e.absolute_path)):
        errors.append({"path": _path(err), "message": err.message[:300]})
    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings}

    integ = config["integration"]
    auth = integ["authentication"]
    if auth["type"] != "none" and not auth.get("credential_ref"):
        errors.append({"path": "integration/authentication", "message": "credential_ref is required when authentication is used"})
    if auth["type"] == "api_key_header" and not auth.get("header_name"):
        errors.append({"path": "integration/authentication", "message": "header_name is required for api_key_header"})
    if auth["type"] == "api_key_query" and not auth.get("param_name"):
        errors.append({"path": "integration/authentication", "message": "param_name is required for api_key_query"})
    if auth["type"] == "oauth2_client_credentials" and not auth.get("token_url"):
        errors.append({"path": "integration/authentication", "message": "token_url is required for oauth2_client_credentials"})
    if auth["type"] == "aws_sigv4" and not (auth.get("region") and auth.get("service")):
        errors.append({"path": "integration/authentication", "message": "region and service are required for aws_sigv4"})
    for h in (integ.get("default_headers") or {}):
        if h.lower() in FORBIDDEN_HEADERS:
            errors.append({"path": "integration/default_headers", "message": f"Header {h!r} may not be set by configuration"})

    seen: set[str] = set()
    for i, op in enumerate(integ["operations"]):
        where = f"integration/operations/{i} ({op['id']})"
        if op["id"] in seen:
            errors.append({"path": where, "message": "Duplicate operation id"})
        seen.add(op["id"])
        try:
            Draft202012Validator.check_schema(op["input_schema"])
        except SchemaError as e:
            errors.append({"path": where + "/input_schema", "message": f"Invalid JSON Schema: {e.message[:200]}"})
        props = (op["input_schema"].get("properties") or {})
        req = op.get("request") or {}
        placeholders = set(re.findall(r"\{([A-Za-z0-9_]+)\}", op["path"]))
        path_params = req.get("path_params") or {}
        for ph in placeholders - set(path_params):
            errors.append({"path": where + "/request/path_params", "message": f"Path placeholder {{{ph}}} has no path_params entry"})
        for extra in set(path_params) - placeholders:
            warnings.append({"path": where + "/request/path_params", "message": f"path_params.{extra} is not used in the path"})
        for h in (req.get("headers") or {}):
            if h.lower() in FORBIDDEN_HEADERS:
                errors.append({"path": where + "/request/headers", "message": f"Header {h!r} may not be set by configuration"})
        if req.get("json_body") is not None and req.get("form_body") is not None:
            errors.append({"path": where + "/request", "message": "Use either json_body or form_body, not both"})
        if op["method"] == "GET" and (req.get("json_body") is not None or req.get("form_body") is not None):
            warnings.append({"path": where + "/request", "message": "GET requests with a body are often ignored by servers"})
        try:
            for ref in templates.references(req):
                root, segs = templates.split_ref(ref)
                if root != "inputs":
                    errors.append({"path": where + "/request", "message": f"${{{ref}}}: only inputs.* may be referenced"})
                elif not segs or segs[0] not in props:
                    errors.append({"path": where + "/request", "message": f"${{{ref}}} refers to an input not declared in input_schema"})
        except templates.TemplateError as e:
            errors.append({"path": where + "/request", "message": str(e)})
        resp = op["response"]
        for name, path in (resp.get("mapping") or {}).items():
            try:
                jsonpath.parse(path)
            except jsonpath.JSONPathError as e:
                errors.append({"path": where + f"/response/mapping/{name}", "message": str(e)})
        rule = resp.get("success") or {}
        for key, path in [("path", rule.get("path")), ("error_path", rule.get("error_path"))] + [("detail_paths", p) for p in rule.get("detail_paths") or []]:
            if path:
                try:
                    jsonpath.parse(path)
                except jsonpath.JSONPathError as e:
                    errors.append({"path": where + f"/response/success/{key}", "message": str(e)})
        if resp.get("items_path"):
            try:
                jsonpath.parse(resp["items_path"])
            except jsonpath.JSONPathError as e:
                errors.append({"path": where + "/response/items_path", "message": str(e)})
        if op.get("retry", {}).get("max_attempts", 1) > 1 and op["method"] in ("POST", "PATCH") and not op.get("idempotent"):
            warnings.append({"path": where + "/retry", "message": "Retries are ignored for non-idempotent operations"})
        pag = op.get("pagination")
        if pag and not resp.get("items_path"):
            errors.append({"path": where + "/pagination", "message": "Paginated operations need response.items_path"})
        if pag and pag["type"] == "cursor" and not (pag.get("cursor_param") and pag.get("next_cursor_path")):
            errors.append({"path": where + "/pagination", "message": "Cursor pagination needs cursor_param and next_cursor_path"})
        if pag and pag["type"] == "offset" and not (pag.get("offset_param") and pag.get("size_param")):
            errors.append({"path": where + "/pagination", "message": "Offset pagination needs offset_param and size_param"})
        ing = op.get("ingest")
        if ing:
            if op["method"] != "GET":
                errors.append({"path": where + "/ingest", "message": "Ingestion operations must use GET (they are re-run on a schedule)"})
            if not resp.get("items_path"):
                errors.append({"path": where + "/ingest", "message": "Ingestion needs response.items_path to find the records"})
            required_inputs = set(op["input_schema"].get("required") or [])
            if required_inputs:
                errors.append({"path": where + "/input_schema", "message": "Ingestion operations run unattended: inputs cannot be required"})
            entity = ing.get("entity") or "event"
            allowed = ENTITY_FIELD_NAMES[entity]
            if entity == "event":
                for alias, name in EVENT_ALIASES.items():
                    if alias in (ing.get("fields") or {}) and name in ing["fields"]:
                        errors.append({"path": where + f"/ingest/fields/{alias}", "message": f"{alias!r} and {name!r} are the same field; map only one of them"})
                ing = resolve_event_aliases(ing)
            fields = ing.get("fields") or {}
            for name, fspec in fields.items():
                if name not in allowed:
                    errors.append({"path": where + f"/ingest/fields/{name}", "message": f"{name!r} is not a field of the {entity} format (allowed: {', '.join(allowed)})"})
                    continue
                paths = [fspec] if isinstance(fspec, str) else ([fspec["path"]] if fspec.get("path") else []) + list(fspec.get("paths") or [])
                if not isinstance(fspec, str) and not paths:
                    errors.append({"path": where + f"/ingest/fields/{name}", "message": "A field mapping needs path or paths"})
                if not isinstance(fspec, str) and fspec.get("transform") == "thresholds" and not fspec.get("thresholds"):
                    errors.append({"path": where + f"/ingest/fields/{name}", "message": "The thresholds transform needs a thresholds list, e.g. [[90, critical], [50, medium]]"})
                for path in paths:
                    try:
                        jsonpath.parse(path)
                    except jsonpath.JSONPathError as e:
                        errors.append({"path": where + f"/ingest/fields/{name}", "message": str(e)})
            required = {"event": ("timestamp", "action"), "user": ("id",), "device": ("id",)}[entity]
            for need in required:
                if need not in fields and need not in (ing.get("defaults") or {}):
                    errors.append({"path": where + "/ingest/fields", "message": f"The {entity} format needs a {need!r} mapping"})
            if entity == "event" and "event_id" not in fields:
                warnings.append({"path": where + "/ingest/fields", "message": "No event_id mapping: records are de-duplicated by a hash of the raw record"})
            if entity != "event" and ing.get("incremental"):
                warnings.append({"path": where + "/ingest/incremental", "message": "Incremental sync follows event timestamps; users and devices are re-read in full and updated in place"})
            cron_expr = (ing.get("schedule") or {}).get("cron")
            if cron_expr:
                try:
                    cron.parse(cron_expr)
                except cron.CronError as e:
                    errors.append({"path": where + "/ingest/schedule/cron", "message": str(e)})
            if (ing.get("incremental") or {}).get("param") in (op.get("request") or {}).get("query", {}):
                warnings.append({"path": where + "/ingest/incremental", "message": "The incremental parameter is also set in request.query and will be overwritten"})
        elif not resp.get("mapping"):
            warnings.append({"path": where + "/response", "message": "No output mapping: the raw response body will be returned"})
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def validate_workflow_graph(graph: dict, known_tools: dict[str, dict]) -> dict:
    """known_tools: {tool_name: {"input_schema": ..., "output_fields": [...]}} for published, enabled tools."""
    errors: list[dict] = []
    warnings: list[dict] = []
    for err in _schema("workflow.schema.json").iter_errors(graph):
        errors.append({"path": _path(err), "message": err.message[:300]})
    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings, "order": []}
    ids = [n["id"] for n in graph["nodes"]]
    if len(ids) != len(set(ids)):
        errors.append({"path": "nodes", "message": "Node ids must be unique"})
    node_ids = set(ids)
    for n in graph["nodes"]:
        where = f"nodes/{n['id']}"
        for d in n.get("depends_on") or []:
            if d not in node_ids:
                errors.append({"path": where, "message": f"depends_on references unknown node {d!r}"})
        tool = known_tools.get(n["tool"])
        if not tool:
            warnings.append({"path": where, "message": f"Tool {n['tool']!r} is not published or not available to you yet"})
        else:
            required = set((tool["input_schema"] or {}).get("required") or [])
            missing = required - set((n.get("inputs") or {}).keys())
            if missing:
                errors.append({"path": where, "message": f"Missing required inputs: {', '.join(sorted(missing))}"})
        try:
            refs = templates.references(n.get("inputs") or {}) + templates.references([c.get("left", "") for c in n.get("when") or []])
        except templates.TemplateError as e:
            errors.append({"path": where, "message": str(e)})
            continue
        for ref in refs:
            root, segs = templates.split_ref(ref)
            if root == "trigger":
                continue
            if root != "steps" or not segs:
                errors.append({"path": where, "message": f"${{{ref}}}: only trigger.* and steps.<id>.output.* may be referenced"})
            elif segs[0] not in node_ids:
                errors.append({"path": where, "message": f"${{{ref}}} refers to unknown step {segs[0]!r}"})
            elif segs[0] not in _ancestors(graph, n["id"]):
                errors.append({"path": where, "message": f"${{{ref}}}: step {segs[0]!r} must be listed (directly or transitively) in depends_on"})
    order = topo_layers(graph) if not errors else []
    if not errors and not order:
        errors.append({"path": "nodes", "message": "The graph contains a cycle"})
    return {"valid": not errors, "errors": errors, "warnings": warnings, "order": order}


def _ancestors(graph: dict, node_id: str) -> set[str]:
    deps = {n["id"]: set(n.get("depends_on") or []) for n in graph["nodes"]}
    seen: set[str] = set()
    stack = list(deps.get(node_id, ()))
    while stack:
        d = stack.pop()
        if d not in seen:
            seen.add(d)
            stack.extend(deps.get(d, ()))
    return seen


def topo_layers(graph: dict) -> list[list[str]]:
    """Kahn layering: each layer's nodes are independent and can run in parallel. [] on a cycle."""
    deps = {n["id"]: set(n.get("depends_on") or []) for n in graph["nodes"]}
    layers, done = [], set()
    while len(done) < len(deps):
        layer = sorted(n for n, d in deps.items() if n not in done and d <= done)
        if not layer:
            return []
        layers.append(layer)
        done.update(layer)
    return layers
