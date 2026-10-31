"""Restricted reference templates: ``${inputs.title}``, ``${steps.n1.output.id}``, ``${trigger.issue.labels[0]}``.

Only dotted lookups into an explicit context are supported - no operators, calls or eval. A value that is
exactly one reference keeps its type; references embedded in text are stringified. A reference that does
not resolve makes the whole value MISSING, and MISSING entries are dropped from objects and lists (so
optional body fields simply disappear).
"""
from __future__ import annotations

import re
from typing import Any

_PATH = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_\-]+|\[\d+\])*"
REF = re.compile(r"\$\{\s*(" + _PATH + r")\s*\}")
_FULL = re.compile(r"^\$\{\s*(" + _PATH + r")\s*\}$")
_SEG = re.compile(r"\.([A-Za-z0-9_\-]+)|\[(\d+)\]")


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


class TemplateError(ValueError):
    pass


def split_ref(expr: str) -> tuple[str, list[str | int]]:
    root = re.match(r"[A-Za-z_][A-Za-z0-9_]*", expr).group(0)
    segs: list[str | int] = []
    for name, idx in _SEG.findall(expr[len(root):]):
        segs.append(int(idx) if idx else name)
    return root, segs


def lookup(ctx: dict, expr: str) -> Any:
    root, segs = split_ref(expr)
    if root not in ctx:
        return MISSING
    cur = ctx[root]
    for s in segs:
        if isinstance(s, int):
            if not isinstance(cur, list) or s >= len(cur):
                return MISSING
            cur = cur[s]
        else:
            if not isinstance(cur, dict) or s not in cur:
                return MISSING
            cur = cur[s]
    return MISSING if cur is None else cur


def render(value: Any, ctx: dict) -> Any:
    if isinstance(value, str):
        full = _FULL.match(value)
        if full:
            return lookup(ctx, full.group(1))
        if "${" not in value:
            return value
        missing = False

        def sub(m: re.Match) -> str:
            nonlocal missing
            v = lookup(ctx, m.group(1))
            if v is MISSING:
                missing = True
                return ""
            return v if isinstance(v, str) else _stringify(v)

        out = REF.sub(sub, value)
        if "${" in out:
            raise TemplateError(f"Unsupported template expression in {value!r}")
        return MISSING if missing else out
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            r = render(v, ctx)
            if r is not MISSING:
                out[k] = r
        return out
    if isinstance(value, list):
        return [r for r in (render(v, ctx) for v in value) if r is not MISSING]
    return value


def _stringify(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v, separators=(",", ":"))
    return str(v)


def references(value: Any) -> list[str]:
    """Every reference expression used anywhere inside value, raising on malformed ``${...}``."""
    found: list[str] = []
    if isinstance(value, str):
        refs = REF.findall(value)
        if "${" in REF.sub("", value):
            raise TemplateError(f"Unsupported template expression in {value!r}")
        found.extend(refs)
    elif isinstance(value, dict):
        for v in value.values():
            found.extend(references(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(references(v))
    return found
