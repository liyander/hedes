"""A deliberately small JSONPath subset: ``$``, ``.key``, ``['key']``, ``[n]``, ``[*]`` and ``.*``."""
from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_\-]*)|\['([^']+)'\]|\[(\d+)\]|(\[\*\]|\.\*)")
WILD = object()


class JSONPathError(ValueError):
    pass


def parse(path: str) -> list:
    if not isinstance(path, str) or not path.startswith("$"):
        raise JSONPathError(f"JSONPath must start with '$': {path!r}")
    rest, pos, tokens = path[1:], 0, []
    while pos < len(rest):
        m = _TOKEN.match(rest, pos)
        if not m:
            raise JSONPathError(f"Unsupported JSONPath syntax at {rest[pos:]!r} in {path!r}")
        name, quoted, idx, wild = m.groups()
        tokens.append(WILD if wild else int(idx) if idx is not None else (name or quoted))
        pos = m.end()
    return tokens


def query(doc: Any, path: str) -> tuple[bool, Any]:
    """Return (found, value). A path with a wildcard returns a list of every match."""
    tokens = parse(path)
    nodes, wildcard = [doc], False
    for t in tokens:
        nxt = []
        for n in nodes:
            if t is WILD:
                wildcard = True
                if isinstance(n, list):
                    nxt.extend(n)
                elif isinstance(n, dict):
                    nxt.extend(n.values())
            elif isinstance(t, int):
                if isinstance(n, list) and t < len(n):
                    nxt.append(n[t])
            elif isinstance(n, dict) and t in n:
                nxt.append(n[t])
        nodes = nxt
    if wildcard:
        return True, nodes
    return (bool(nodes), nodes[0] if nodes else None)


def shape(doc: Any, prefix: str = "$", depth: int = 0, out: dict | None = None) -> dict[str, str]:
    """Flatten a document into {jsonpath: type} without any values (safe to show an AI model)."""
    out = {} if out is None else out
    if depth > 6 or len(out) > 300:
        return out
    if isinstance(doc, dict):
        out[prefix] = "object"
        for k, v in doc.items():
            key = f"{prefix}.{k}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\-]*", str(k)) else f"{prefix}['{k}']"
            shape(v, key, depth + 1, out)
    elif isinstance(doc, list):
        out[prefix] = "array"
        if doc:
            shape(doc[0], f"{prefix}[0]", depth + 1, out)
    else:
        out[prefix] = {bool: "boolean", int: "number", float: "number", str: "string"}.get(type(doc), "null")
    return out
