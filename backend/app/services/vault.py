"""Credential vault. Secrets are Fernet-encrypted at rest and addressed only by reference.

Nothing outside the execution engine (and connection testing) ever calls ``read_secret``; secrets are never
returned by the API, logged, or included in AI prompts.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models import Secret

PREFIX = "vault:"


@lru_cache
def _fernet() -> Fernet:
    return Fernet(get_settings().secret_encryption_key.encode())


def store_secret(db: Session, org_id: str | None, payload: dict) -> str:
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
        return None
    return {k: resolve_env(v) for k, v in payload.items()}


ENV_REF = re.compile(r"^env:([A-Z][A-Z0-9_]{1,63})$")


def resolve_env(value):
    """A stored value of the form ``env:NAME`` is read from the server's environment at use time (nothing secret is stored)."""
    if isinstance(value, str):
        m = ENV_REF.match(value)
        if m:
            return os.environ.get(m.group(1), "")
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
