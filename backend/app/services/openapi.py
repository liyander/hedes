"""OpenAPI 3.x import: parse a spec into a capability catalog and convert operations into configuration."""
from __future__ import annotations

import json
import re
from typing import Any

import yaml

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
LIST_KEYS = ("items", "data", "results", "records", "values", "entries")
MAX_NODES = 200_000  # objects a document may expand to once its $refs are inlined


class OpenAPIError(ValueError):
    pass


def load_document(text: str) -> dict:
    try:
        doc = json.loads(text)
    except ValueError:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise OpenAPIError(f"Not valid JSON or YAML: {str(e)[:200]}")
    if not isinstance(doc, dict):
        raise OpenAPIError("The document is not a JSON/YAML object")
    return doc


def _resolve(doc: dict, node: Any, seen: tuple = (), budget: list | None = None) -> Any:
    """Inline local $refs. Recursive references (and chains deeper than 12) resolve to {}. ``budget`` counts the objects
    produced: a few KB whose references (or YAML aliases) each repeat the previous level would otherwise expand
    exponentially and hang the request."""
    budget = [MAX_NODES] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0:
        raise OpenAPIError("The document expands to too many objects once its $refs are inlined; split or simplify it")
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            ref = node["$ref"]
            if not ref.startswith("#/") or ref in seen or len(seen) > 12:
                return {}
            target: Any = doc
            for part in ref[2:].split("/"):
                target = target.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(target, dict) else {}
            return _resolve(doc, target, seen + (ref,), budget)
        return {k: _resolve(doc, v, seen, budget) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(doc, v, seen, budget) for v in node]
    return node


def snake(s: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    return (s if s and s[0].isalpha() else "op_" + s)[:62] or "op"


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return (s if s and s[0].isalpha() else "api-" + s)[:62] or "api"


def _json_content(content: dict | None) -> dict:
    content = content or {}
    for ctype, media in content.items():
        if "json" in ctype:
            return media or {}
    return next(iter(content.values()), {}) if content else {}


def parse_openapi(text: str) -> dict:
    try:
        return _parse_openapi(text)
    except (AttributeError, TypeError, KeyError, IndexError, RecursionError) as e:  # a field of an unexpected type or shape
        raise OpenAPIError(f"The document does not have the structure of an OpenAPI 3 document ({type(e).__name__}); check it in an OpenAPI validator")


def _parse_openapi(text: str) -> dict:
    raw = load_document(text)
    if "swagger" in raw:
        raise OpenAPIError("Swagger 2.0 is not supported; convert the document to OpenAPI 3.x")
    if not str(raw.get("openapi", "")).startswith("3."):
        raise OpenAPIError("Missing 'openapi: 3.x' version field - is this an OpenAPI document?")
    if not isinstance(raw.get("paths"), dict) or not raw["paths"]:
        raise OpenAPIError("The document defines no paths")
    doc = _resolve(raw, raw)
    info = doc.get("info") or {}
    servers = [s.get("url", "") for s in doc.get("servers") or [] if isinstance(s, dict)]
    base_url = next((s for s in servers if s.startswith("http")), "")

    auth = []
    for name, sch in ((doc.get("components") or {}).get("securitySchemes") or {}).items():
        t, scheme = sch.get("type"), str(sch.get("scheme", "")).lower()
        if t == "http" and scheme == "bearer" or t in ("oauth2", "openIdConnect"):
            auth.append({"name": name, "type": "bearer"})
        elif t == "http" and scheme == "basic":
            auth.append({"name": name, "type": "basic"})
        elif t == "apiKey" and sch.get("in") == "header":
            auth.append({"name": name, "type": "api_key_header", "header_name": sch.get("name", "X-API-Key")})
        elif t == "apiKey" and sch.get("in") == "query":
            auth.append({"name": name, "type": "api_key_query", "param_name": sch.get("name", "api_key")})

    ops = []
    used: set[str] = set()
    for path, item in doc["paths"].items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in HTTP_METHODS:
            spec = item.get(method)
            if not isinstance(spec, dict):
                continue
            op_id = snake(spec.get("operationId") or f"{method}_{path}")
            while op_id in used:
                op_id += "_2"
            used.add(op_id)
            params = []
            for p in shared + (spec.get("parameters") or []):
                if isinstance(p, dict) and p.get("in") in ("path", "query", "header") and p.get("name"):
                    params.append({"name": p["name"], "in": p["in"], "required": bool(p.get("required") or p["in"] == "path"),
                                   "schema": p.get("schema") or {"type": "string"}, "description": p.get("description", "")})
            body = _json_content((spec.get("requestBody") or {}).get("content"))
            responses = spec.get("responses") or {}
            ok_codes = sorted(int(c) for c in responses if str(c).isdigit() and 200 <= int(c) < 300)
            ok_media = _json_content((responses.get(str(ok_codes[0])) or {}).get("content")) if ok_codes else {}
            ops.append({
                "id": op_id, "method": method.upper(), "path": path,
                "summary": spec.get("summary") or spec.get("description", "")[:200],
                "tags": spec.get("tags") or [],
                "parameters": params,
                "body_schema": body.get("schema"),
                "body_required": bool((spec.get("requestBody") or {}).get("required")),
                "status_codes": ok_codes or [200],
                "response_schema": ok_media.get("schema"),
                "response_example": ok_media.get("example"),
                "deprecated": bool(spec.get("deprecated")),
                # What the vendor documents for setup: the OAuth scopes this operation requires and its reference page.
                "scopes": sorted({s for req in (spec.get("security") if spec.get("security") is not None else doc.get("security") or [])
                                  if isinstance(req, dict) for v in req.values() if isinstance(v, list) for s in v if isinstance(s, str)}),
                "docs_url": ((spec.get("externalDocs") or {}).get("url") or "") if isinstance(spec.get("externalDocs"), dict) else "",
            })
    if not ops:
        raise OpenAPIError("No HTTP operations were found under paths")
    return {"title": info.get("title") or "Imported API", "version": info.get("version", ""),
            "description": (info.get("description") or "")[:1000], "base_url": base_url,
            "auth": auth, "operations": ops,
            "docs_url": ((doc.get("externalDocs") or {}).get("url") or "") if isinstance(doc.get("externalDocs"), dict) else ""}


def catalog_summary(catalog: dict) -> str:
    lines = [f"{catalog['title']} ({catalog.get('base_url') or 'no server URL'})",
             "auth: " + (", ".join(a["type"] for a in catalog["auth"]) or "none declared")]
    for op in catalog["operations"]:
        lines.append(f"- {op['id']}: {op['method']} {op['path']} - {op['summary']}")
    return "\n".join(lines)


def _scalar(schema: dict) -> bool:
    return (schema or {}).get("type") in ("string", "integer", "number", "boolean") or "enum" in (schema or {})


def output_mapping(schema: dict | None) -> tuple[dict, str | None, list[str]]:
    """Derive (mapping, items_path, optional_fields) from a response schema."""
    schema = schema or {}
    items_path = None
    if schema.get("type") == "array":
        items_path, schema = "$", schema.get("items") or {}
    elif schema.get("type") == "object":
        props = schema.get("properties") or {}
        for k in LIST_KEYS:
            if (props.get(k) or {}).get("type") == "array":
                items_path, schema = f"$.{k}", props[k].get("items") or {}
                break
    mapping, optional = {}, []
    required = set(schema.get("required") or [])
    for name, prop in list((schema.get("properties") or {}).items())[:16]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        key = snake(name)
        if _scalar(prop):
            mapping[key] = f"$.{name}"
            if name not in required:
                optional.append(key)
        elif prop.get("type") == "object":
            sub_req = set(prop.get("required") or [])
            for sub, sp in list((prop.get("properties") or {}).items())[:6]:
                if _scalar(sp) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", sub):
                    k2 = f"{key}_{snake(sub)}"
                    mapping[k2] = f"$.{name}.{sub}"
                    if name not in required or sub not in sub_req:
                        optional.append(k2)
    return mapping, items_path, optional


def operation_to_config(op: dict) -> dict:
    props: dict = {}
    required: list[str] = []
    request: dict = {}
    for p in op["parameters"]:
        if p["in"] == "header":
            continue
        name = snake(p["name"])
        props[name] = {**{k: v for k, v in p["schema"].items() if k in ("type", "enum", "format", "default", "minimum", "maximum", "items")},
                       **({"description": p["description"][:200]} if p.get("description") else {})}
        props[name].setdefault("type", "string")
        if p["required"]:
            required.append(name)
        bucket = "path_params" if p["in"] == "path" else "query"
        request.setdefault(bucket, {})[p["name"]] = f"${{inputs.{name}}}"
    body = op.get("body_schema") or {}
    if body.get("type") == "object" or body.get("properties"):
        json_body = {}
        for bname, bprop in (body.get("properties") or {}).items():
            if (bprop or {}).get("readOnly") or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", bname):
                continue
            key = snake(bname) if snake(bname) not in props else "body_" + snake(bname)
            props[key] = {k: v for k, v in (bprop or {}).items() if k in ("type", "enum", "format", "default", "description", "items", "minimum", "maximum", "maxLength")} or {"type": "string"}
            if bname in (body.get("required") or []):
                required.append(key)
            json_body[bname] = f"${{inputs.{key}}}"
        request["json_body"] = json_body
    mapping, items_path, optional = output_mapping(op.get("response_schema"))
    response: dict = {"status_codes": op["status_codes"], "mapping": mapping}
    if items_path:
        response["items_path"] = items_path
    if optional:
        response["optional_fields"] = optional
    if not mapping:
        response.pop("mapping")
    input_schema: dict = {"type": "object", "properties": props}
    if required:
        input_schema["required"] = sorted(set(required))
    out = {"id": op["id"], "description": (op.get("summary") or f"{op['method']} {op['path']}")[:1000],
           "method": op["method"], "path": op["path"], "input_schema": input_schema,
           "request": request, "response": response}
    if not request:
        out.pop("request")
    return out


def catalog_to_config(catalog: dict, op_ids: list[str] | None, slug: str, name: str | None = None,
                      base_url: str | None = None, credential_ref: str | None = None) -> dict:
    chosen = [op for op in catalog["operations"] if not op_ids or op["id"] in op_ids]
    if not chosen:
        raise OpenAPIError("None of the selected operations exist in the document")
    auth_src = catalog["auth"][0] if catalog["auth"] else {"type": "none"}
    auth: dict = {"type": auth_src["type"]}
    if auth["type"] != "none":
        auth["credential_ref"] = credential_ref or slug
    for k in ("header_name", "param_name"):
        if auth_src.get(k):
            auth[k] = auth_src[k]
    return {"schema_version": "1.0", "integration": {
        "id": slug, "name": name or catalog["title"], "version": "1.0.0", "transport": "http",
        "description": catalog.get("description", "")[:500],
        "base_url": (base_url or catalog.get("base_url") or "https://api.example.com").rstrip("/"),
        "authentication": auth,
        "operations": [operation_to_config(op) for op in chosen]}}


def sample_value(schema: dict | None, name: str = "", depth: int = 0) -> Any:
    """A plausible example value for a JSON Schema (used for mock tests and the Test Lab)."""
    s = schema or {}
    if "example" in s:
        return s["example"]
    if "default" in s:
        return s["default"]
    if s.get("enum"):
        return s["enum"][0]
    t = s.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "object" or (t is None and "properties" in s):
        if depth > 4:
            return {}
        return {k: sample_value(v, k, depth + 1) for k, v in (s.get("properties") or {}).items()}
    if t == "array":
        return [sample_value(s.get("items") or {"type": "string"}, name, depth + 1)]
    if t == "integer":
        return 42 if "id" in name.lower() or "number" in name.lower() else 1
    if t == "number":
        return 1.5
    if t == "boolean":
        return True
    fmt = s.get("format", "")
    if fmt == "date-time":
        return "2026-01-01T00:00:00Z"
    if fmt in ("uri", "url") or name.endswith("url"):
        return "https://example.com/item/42"
    if fmt == "email" or "email" in name:
        return "alex@example.com"
    lname = name.lower()
    if lname in ("owner", "org", "organization"):
        return "acme"
    if lname in ("repo", "repository"):
        return "platform"
    if "name" in lname:
        return "Alex"
    if lname in ("channel",):
        return "#general"
    return f"sample-{lname or 'value'}"
