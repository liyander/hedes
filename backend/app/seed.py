"""First-start bootstrap: demo organisation, accounts for each role, AI providers and reference integrations."""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from .core.config import DATA_DIR, get_settings
from .core.db import utcnow
from .core.security import hash_password
from .mock.services import NOTIFY_KEY
from .models import AIModel, AIProvider, ApprovalEvent, ApprovalRequest, Connection, Membership, Organization, User
from .services import audit, catalog, integrations, vault

log = logging.getLogger("integra.seed")

ACCOUNTS = [("owner", "Olivia Owner", "owner"), ("admin", "Adam Admin", "admin"), ("builder", "Bea Builder", "user")]


def seed_ai_providers(db: Session) -> None:
    if db.scalars(select(AIProvider)).first():
        return
    s = get_settings()
    local = AIProvider(name="Built-in (offline)", provider_type="local", base_url="")
    db.add(local)
    db.flush()
    db.add(AIModel(provider_id=local.id, model_id="heuristic-v1", label="Heuristic agents", purposes=["general"],
                   capabilities={"json_mode": True, "context_tokens": 1_000_000}, priority=1000))
    configured = [
        ("OpenAI-compatible", "openai_compatible", s.openai_base_url, s.openai_api_key, s.openai_model, ["general"], 30, (0.15, 0.6)),
        ("Anthropic", "anthropic", "https://api.anthropic.com", s.anthropic_api_key, s.anthropic_model,
         ["requirements", "api_discovery", "config_generation", "security_review", "repair", "workflow_planning"], 10, (3.0, 15.0)),
        ("Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta", s.gemini_api_key, s.gemini_model,
         ["conversation", "error_explanation"], 20, (0.3, 2.5)),
    ]
    for name, ptype, base, key, model, purposes, prio, (cin, cout) in configured:
        if not key:
            continue
        p = AIProvider(name=name, provider_type=ptype, base_url=base, secret_ref=vault.store_secret(db, None, {"api_key": key}))
        db.add(p)
        db.flush()
        db.add(AIModel(provider_id=p.id, model_id=model, label=model, purposes=purposes, priority=prio,
                       capabilities={"json_mode": True, "context_tokens": 128000}, cost_input_per_mtok=cin, cost_output_per_mtok=cout))
        log.info("Registered AI provider %s (%s)", name, model)


def _publish_seed(db: Session, org: Organization, builder: User, admin: User, slug: str) -> None:
    integ, v = integrations.create_integration(db, org.id, builder, catalog.known_services()[slug], "seed",
                                               notes="Reference integration installed with the platform")
    integrations.run_tests(db, v, builder)
    req = ApprovalRequest(organization_id=org.id, integration_id=integ.id, version_id=v.id, config_hash=v.config_hash,
                          requester_id=builder.id, reviewer_id=admin.id, decision="approved", kind="new",
                          comment="Installed and approved at platform setup", decided_at=utcnow(),
                          risk={k: v.security_report.get(k) for k in ("score", "level", "capabilities", "destinations")})
    db.add(req)
    db.flush()
    db.add(ApprovalEvent(request_id=req.id, actor_id=admin.id, action="approved", comment="Platform setup"))
    integrations._publish(db, integ, v)
    audit.record(db, org.id, "setup", "integration.publish", f"{slug}@{v.version}", seeded=True)


def run(db: Session) -> None:
    seed_ai_providers(db)
    if db.scalars(select(User)).first():
        db.commit()
        return
    s = get_settings()
    password = s.seed_password or secrets.token_urlsafe(10)
    org = Organization(name="Acme Corp", policy={"allowed_domains": [], "ai_monthly_budget_usd": 25,
                                                 "require_tests": True, "repair_after_failures": 2})
    db.add(org)
    db.flush()
    users = {}
    for username, name, role in ACCOUNTS:
        u = User(email=f"{username}@integra.local", username=username, name=name, password_hash=hash_password(password))
        db.add(u)
        db.flush()
        db.add(Membership(user_id=u.id, organization_id=org.id, role=role))
        users[role] = u
    db.add(Connection(organization_id=org.id, name="notify", auth_type="api_key_header",
                      secret_ref=vault.store_secret(db, org.id, {"api_key": NOTIFY_KEY}), hint=vault.mask(NOTIFY_KEY),
                      created_by=users["admin"].id))
    db.flush()
    for slug in ("github", "slack", "notify"):
        _publish_seed(db, org, users["user"], users["admin"], slug)
    audit.record(db, org.id, "setup", "platform.seed", org.name)
    db.commit()
    if not s.seed_password:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "seed-credentials.txt").write_text(
            "Demo accounts (password shared): owner / admin / builder\n" f"password: {password}\n")
        log.warning("Seeded demo accounts owner/admin/builder with generated password %s (also in backend/data/seed-credentials.txt)", password)
