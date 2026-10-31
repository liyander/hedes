"""Small adapters so other routers can reuse the integration summaries without importing a router module."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Integration, IntegrationVersion
from .integrations import integration_summary, version_summary


def integration_summary_for(db: Session, i: Integration) -> dict:
    return integration_summary(db, i)


def version_summary_for(db: Session, v: IntegrationVersion) -> dict:
    return version_summary(db, v)
