"""Credential vault. Secrets are Fernet-encrypted at rest and addressed only by reference.

Nothing outside the execution engine (and connection testing) ever calls ``read_secret``; secrets are never
returned by the API, logged, or included in AI prompts.
"""
from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models import Secret

PREFIX = "vault:"
log = logging.getLogger("integra.vault")


@lru_cache
def _fernet() -> Fernet:
    return Fernet(get_settings().secret_encryption_key.encode())


class EnvRefNotAllowed(ValueError):
    pass


def _env_allowed(org_id: str | None) -> set[str] | None:
    """None = any variable (platform secrets). Organisation secrets may only name operator-approved variables, otherwise
    anyone who can create a connection could have the server send its own secrets (JWT key, vault key, AI keys) out."""
    return None if org_id is None else get_settings().connection_env_names


def check_env_refs(payload: dict, org_id: str | None) -> None:
    allowed = _env_allowed(org_id)
    if allowed is None:
        return
    for v in payload.values():
        m = ENV_REF.match(v) if isinstance(v, str) else None
        if m and m.group(1) not in allowed:
            raise EnvRefNotAllowed(f"Environment variable {m.group(1)} is not allowed for credentials. "
                                   "The server operator must list it in CONNECTION_ENV_VARS.")


def store_secret(db: Session, org_id: str | None, payload: dict) -> str:
    check_env_refs(payload, org_id)
    row = Secret(organization_id=org_id, ciphertext=_fernet().encrypt(json.dumps(payload).encode()).decode())
    db.add(row)
    db.flush()
    return PREFIX + row.id


def read_secret(db: Session, ref: str | None, org_id: str | None) -> dict | None:
    if not ref or not ref.startswith(PREFIX):
        return None
    row = db.get(Secret, ref[len(PREFIX):])
    if not row or row.organization_id != org_id:
        return None
    try:
        payload = json.loads(_fernet().decrypt(row.ciphertext.encode()))
    except InvalidToken:
        # Almost always a different SECRET_ENCRYPTION_KEY than the one the secret was stored with (e.g. a generated key lost
        # in a redeploy): say so, instead of every credential silently looking "not set up".
        log.warning("Secret %s cannot be decrypted with the current SECRET_ENCRYPTION_KEY", row.id)
        return None
    allowed = _env_allowed(org_id)
    return {k: resolve_env(v, allowed) for k, v in payload.items()}


ENV_REF = re.compile(r"^env:([A-Z][A-Z0-9_]{1,63})$")


def resolve_env(value, allowed: set[str] | None = None):
    """A stored value of the form ``env:NAME`` is read from the server's environment at use time (nothing secret is stored).
    ``allowed`` restricts which names resolve (None = any); a disallowed name resolves to "" (an unusable credential)."""
    if isinstance(value, str):
        m = ENV_REF.match(value)
        if m:
            return os.environ.get(m.group(1), "") if allowed is None or m.group(1) in allowed else ""
    return value


def delete_secret(db: Session, ref: str | None) -> None:
    if ref and ref.startswith(PREFIX):
        row = db.get(Secret, ref[len(PREFIX):])
        if row:
            db.delete(row)


def mask(value: str) -> str:
    if ENV_REF.match(value or ""):
        return value
    return "••••" + value[-4:] if value and len(value) > 8 else "••••"
