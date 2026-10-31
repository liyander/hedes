"""Database schema. UUID string keys, JSON columns, every tenant-owned row carries organization_id."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .core.db import Base, new_id, utcnow


def pk() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=new_id)


def fk(target: str, nullable: bool = False, ondelete: str = "CASCADE") -> Mapped:
    return mapped_column(String(36), ForeignKey(target, ondelete=ondelete), nullable=nullable, index=True)


def created() -> Mapped[datetime]:
    return mapped_column(DateTime, default=utcnow, index=True)


ROLES = ("user", "admin", "owner")


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = pk()
    email: Mapped[str] = mapped_column(String(255), unique=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = created()


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = pk()
    name: Mapped[str] = mapped_column(String(120))
    # allowed_domains, ai_monthly_budget_usd, require_tests, max_auto_risk ...
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = created()


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id"),)
    id: Mapped[str] = pk()
    user_id: Mapped[str] = fk("users.id")
    organization_id: Mapped[str] = fk("organizations.id")
    role: Mapped[str] = mapped_column(String(16), default="user")


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    email: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[str] = fk("users.id")
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = created()


class ApiToken(Base):
    """Personal access tokens for API / MCP clients. They can never approve or publish."""
    __tablename__ = "api_tokens"
    id: Mapped[str] = pk()
    user_id: Mapped[str] = fk("users.id")
    organization_id: Mapped[str] = fk("organizations.id")
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = created()


class Secret(Base):
    """The credential vault: Fernet-encrypted payloads addressed by secret_ref ("vault:<id>")."""
    __tablename__ = "secrets"
    id: Mapped[str] = pk()
    organization_id: Mapped[str | None] = fk("organizations.id", nullable=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created()


class AIProvider(Base):
    __tablename__ = "ai_providers"
    id: Mapped[str] = pk()
    name: Mapped[str] = mapped_column(String(120))
    provider_type: Mapped[str] = mapped_column(String(32))  # openai_compatible | anthropic | gemini | local
    base_url: Mapped[str] = mapped_column(String(500), default="")
    secret_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = created()


class AIModel(Base):
    __tablename__ = "ai_models"
    id: Mapped[str] = pk()
    provider_id: Mapped[str] = fk("ai_providers.id")
    model_id: Mapped[str] = mapped_column(String(200))
    label: Mapped[str] = mapped_column(String(120), default="")
    # {"json_mode": bool, "context_tokens": int}
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    # tasks this model is preferred for; "general" matches any task
    purposes: Mapped[list] = mapped_column(JSON, default=list)
    # may confidential material (API docs, configurations, error details) be sent to this model?
    allow_confidential: Mapped[bool] = mapped_column(Boolean, default=True)
    cost_input_per_mtok: Mapped[float] = mapped_column(Float, default=0.0)
    cost_output_per_mtok: Mapped[float] = mapped_column(Float, default=0.0)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = created()


class UsageRecord(Base):
    __tablename__ = "usage_records"
    id: Mapped[str] = pk()
    organization_id: Mapped[str | None] = fk("organizations.id", nullable=True)
    provider_id: Mapped[str | None] = fk("ai_providers.id", nullable=True, ondelete="SET NULL")
    model_id: Mapped[str | None] = fk("ai_models.id", nullable=True, ondelete="SET NULL")
    model_name: Mapped[str] = mapped_column(String(200), default="")
    task: Mapped[str] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = created()


class AISession(Base):
    __tablename__ = "ai_sessions"
    id: Mapped[str] = pk()
    user_id: Mapped[str] = fk("users.id")
    organization_id: Mapped[str] = fk("organizations.id")
    title: Mapped[str] = mapped_column(String(200), default="New integration")
    status: Mapped[str] = mapped_column(String(32), default="collecting")  # collecting | running | ready | failed
    stage: Mapped[str] = mapped_column(String(48), default="COLLECT_REQUIREMENTS")
    requirements: Mapped[dict] = mapped_column(JSON, default=dict)
    # per-stage {status, output, error, model, at}; persisted so a failed stage can be retried alone
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    documents: Mapped[list] = mapped_column(JSON, default=list)  # [{id, name, kind, summary, spec}]
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AIMessage(Base):
    __tablename__ = "ai_messages"
    id: Mapped[str] = pk()
    session_id: Mapped[str] = fk("ai_sessions.id")
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = created()


class Integration(Base):
    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("organization_id", "slug"),)
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    # draft | validating | testing | pending_approval | revision | published | disabled | archived
    status: Mapped[str] = mapped_column(String(24), default="draft")
    created_by: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    session_id: Mapped[str | None] = fk("ai_sessions.id", nullable=True, ondelete="SET NULL")
    latest_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    published_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class IntegrationVersion(Base):
    """A configuration snapshot. Editable only while status == draft; frozen once submitted."""
    __tablename__ = "integration_versions"
    __table_args__ = (UniqueConstraint("integration_id", "number"),)
    id: Mapped[str] = pk()
    integration_id: Mapped[str] = fk("integrations.id")
    number: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict] = mapped_column(JSON)
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    # draft | pending_approval | changes_requested | rejected | published | superseded
    status: Mapped[str] = mapped_column(String(24), default="draft")
    source: Mapped[str] = mapped_column(String(16), default="manual")  # ai | manual | openapi | repair | rollback | seed
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    security_report: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    name: Mapped[str] = mapped_column(String(64))  # what configs reference as credential_ref
    auth_type: Mapped[str] = mapped_column(String(32))
    secret_ref: Mapped[str] = mapped_column(String(64))
    hint: Mapped[str] = mapped_column(String(32), default="")  # masked tail, e.g. "••••abcd"
    test_url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | invalid | revoked
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_result: Mapped[str] = mapped_column(String(200), default="")
    # Credential hygiene: optional expiry, last rotation and last use drive the rotation/expiry alerts.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class Tool(Base):
    """One executable operation of a published integration version."""
    __tablename__ = "tools"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    integration_id: Mapped[str] = fk("integrations.id")
    integration_version_id: Mapped[str] = fk("integration_versions.id")
    name: Mapped[str] = mapped_column(String(160), index=True)  # "<slug>.<operation>"
    operation_id: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    method: Mapped[str] = mapped_column(String(8))
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    output_fields: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    allowed_roles: Mapped[list] = mapped_column(JSON, default=lambda: list(ROLES))
    mcp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created()


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    integration_id: Mapped[str] = fk("integrations.id")
    version_id: Mapped[str] = fk("integration_versions.id")
    config_hash: Mapped[str] = mapped_column(String(64))
    requester_id: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    reviewer_id: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    decision: Mapped[str] = mapped_column(String(24), default="pending")  # pending | approved | rejected | changes_requested | superseded
    kind: Mapped[str] = mapped_column(String(16), default="new")  # new | update | repair
    risk: Mapped[dict] = mapped_column(JSON, default=dict)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = created()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApprovalEvent(Base):
    __tablename__ = "approval_events"
    id: Mapped[str] = pk()
    request_id: Mapped[str] = fk("approval_requests.id")
    actor_id: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    action: Mapped[str] = mapped_column(String(32))
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = created()


class TestRun(Base):
    __tablename__ = "test_runs"
    __test__ = False  # not a pytest class
    id: Mapped[str] = pk()
    version_id: Mapped[str] = fk("integration_versions.id")
    config_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="mock")
    result: Mapped[str] = mapped_column(String(16))  # passed | failed
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    executed_by: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    executed_at: Mapped[datetime] = created()


class Workflow(Base):
    __tablename__ = "workflows"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | active | paused
    created_by: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    webhook_secret_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"
    id: Mapped[str] = pk()
    workflow_id: Mapped[str] = fk("workflows.id")
    version: Mapped[int] = mapped_column(Integer)
    graph: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created()


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    workflow_id: Mapped[str] = fk("workflows.id")
    workflow_version_id: Mapped[str] = fk("workflow_versions.id")
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | succeeded | failed | cancelled
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    delivery_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"
    id: Mapped[str] = pk()
    run_id: Mapped[str] = fk("workflow_runs.id")
    node_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # succeeded | failed | skipped | planned
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (Index("ix_exec_org_created", "organization_id", "created_at"),)
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    tool_id: Mapped[str | None] = fk("tools.id", nullable=True, ondelete="SET NULL")
    tool_name: Mapped[str] = mapped_column(String(160), default="")
    integration_id: Mapped[str | None] = fk("integrations.id", nullable=True, ondelete="SET NULL")
    version_id: Mapped[str | None] = fk("integration_versions.id", nullable=True, ondelete="SET NULL")
    actor_id: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    actor_type: Mapped[str] = mapped_column(String(16), default="user")  # user | workflow | mcp
    status: Mapped[str] = mapped_column(String(16), default="queued")
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_class: Mapped[str] = mapped_column(String(32), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    request_preview: Mapped[dict] = mapped_column(JSON, default=dict)  # credentials redacted
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    diagnostics: Mapped[dict] = mapped_column(JSON, default=dict)  # response shape, mapping misses
    created_at: Mapped[datetime] = created()


class RepairProposal(Base):
    __tablename__ = "repair_proposals"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    integration_id: Mapped[str] = fk("integrations.id")
    base_version_id: Mapped[str] = fk("integration_versions.id")
    proposed_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approval_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_class: Mapped[str] = mapped_column(String(32))
    diagnosis: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="proposed")  # proposed | pending_approval | approved | rejected | failed
    created_at: Mapped[datetime] = created()


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16), default="medium")
    message: Mapped[str] = mapped_column(Text)
    integration_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    link: Mapped[str] = mapped_column(String(200), default="")
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = pk()
    organization_id: Mapped[str | None] = fk("organizations.id", nullable=True)
    actor_id: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    actor_label: Mapped[str] = mapped_column(String(120), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(200), default="")
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = created()


class WebhookDelivery(Base):
    """Replay protection for inbound webhooks."""
    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("workflow_id", "delivery_id"),)
    id: Mapped[str] = pk()
    workflow_id: Mapped[str] = fk("workflows.id")
    delivery_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = created()


class IngestSource(Base):
    """One scheduled data source: an ``ingest`` operation of a published integration. Carries run state and health."""
    __tablename__ = "ingest_sources"
    __table_args__ = (UniqueConstraint("organization_id", "integration_id", "operation_id"),)
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    integration_id: Mapped[str] = fk("integrations.id")
    operation_id: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    cron: Mapped[str | None] = mapped_column(String(64), nullable=True)  # when set, replaces the interval
    watermark: Mapped[str] = mapped_column(String(64), default="")  # newest canonical timestamp ingested so far
    state: Mapped[str] = mapped_column(String(16), default="never_run")  # never_run | running | ok | degraded | failing
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error_class: Mapped[str] = mapped_column(String(32), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    total_stored: Mapped[int] = mapped_column(Integer, default=0)
    total_duplicates: Mapped[int] = mapped_column(Integer, default=0)
    total_rejected: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = created()


class IngestRun(Base):
    __tablename__ = "ingest_runs"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    source_id: Mapped[str] = fk("ingest_sources.id")
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")  # schedule | manual
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | succeeded | partial | failed
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    stored: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_class: Mapped[str] = mapped_column(String(32), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    watermark_before: Mapped[str] = mapped_column(String(64), default="")
    watermark_after: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class NormalizedEvent(Base):
    """The common format every source is mapped into (see services/ingest.py CANONICAL_FIELDS)."""
    __tablename__ = "normalized_events"
    __table_args__ = (UniqueConstraint("organization_id", "integration_id", "event_id"),
                      Index("ix_events_org_ts", "organization_id", "timestamp"))
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    integration_id: Mapped[str] = fk("integrations.id")
    source_id: Mapped[str] = fk("ingest_sources.id")
    source: Mapped[str] = mapped_column(String(64), index=True)  # integration slug
    event_id: Mapped[str] = mapped_column(String(200))
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    category: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(200), default="")
    actor: Mapped[str] = mapped_column(String(255), default="")
    target: Mapped[str] = mapped_column(String(255), default="")
    outcome: Mapped[str] = mapped_column(String(16), default="unknown")  # success | failure | unknown
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info | low | medium | high | critical
    message: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    ingested_at: Mapped[datetime] = created()


class DeadLetter(Base):
    """A record the platform could not normalise. Kept (with the reason) instead of failing the whole run."""
    __tablename__ = "dead_letters"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    source_id: Mapped[str] = fk("ingest_sources.id")
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64), default="", index=True)  # same bad record is parked once, not every run
    created_at: Mapped[datetime] = created()


class NormalizedUser(Base):
    """Common 'user' shape. One row per external account; later runs update it in place (upsert)."""
    __tablename__ = "normalized_users"
    __table_args__ = (UniqueConstraint("organization_id", "integration_id", "external_id"),)
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    integration_id: Mapped[str] = fk("integrations.id")
    source_id: Mapped[str] = fk("ingest_sources.id")
    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="")
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = created()
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NormalizedDevice(Base):
    __tablename__ = "normalized_devices"
    __table_args__ = (UniqueConstraint("organization_id", "integration_id", "external_id"),)
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    integration_id: Mapped[str] = fk("integrations.id")
    source_id: Mapped[str] = fk("ingest_sources.id")
    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    hostname: Mapped[str] = mapped_column(String(255), default="", index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    os: Mapped[str] = mapped_column(String(120), default="")
    owner: Mapped[str] = mapped_column(String(255), default="")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = created()
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AssistantThread(Base):
    """A conversation with the platform assistant (separate from AI Studio build sessions)."""
    __tablename__ = "assistant_threads"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    user_id: Mapped[str] = fk("users.id")
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"
    id: Mapped[str] = pk()
    thread_id: Mapped[str] = fk("assistant_threads.id")
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)   # [{type, id, label}] the user attached with @
    meta: Mapped[dict] = mapped_column(JSON, default=dict)   # model, looked_up, page
    created_at: Mapped[datetime] = created()


class AssistantAction(Base):
    """An action the assistant wants to perform. Nothing runs until the user approves it."""
    __tablename__ = "assistant_actions"
    id: Mapped[str] = pk()
    thread_id: Mapped[str] = fk("assistant_threads.id")
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    organization_id: Mapped[str] = fk("organizations.id")
    user_id: Mapped[str] = fk("users.id")
    tool: Mapped[str] = mapped_column(String(64))
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    why: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed | executed | failed | rejected
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = created()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class HealingCase(Base):
    """One self-healing investigation: what failed, the traced root cause, every fix attempted and what is still needed."""
    __tablename__ = "healing_cases"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = fk("organizations.id")
    integration_id: Mapped[str | None] = fk("integrations.id", nullable=True, ondelete="SET NULL")
    workflow_id: Mapped[str | None] = fk("workflows.id", nullable=True, ondelete="SET NULL")
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    target: Mapped[str] = mapped_column(String(200), default="")
    error_class: Mapped[str] = mapped_column(String(32), default="")
    # investigating | resolved | awaiting_approval | needs_admin
    status: Mapped[str] = mapped_column(String(24), default="investigating", index=True)
    root_cause: Mapped[str] = mapped_column(Text, default="")
    analysis: Mapped[str] = mapped_column(Text, default="")  # the AI's narrative over the evidence
    attempts: Mapped[list] = mapped_column(JSON, default=list)  # [{"action", "result", "ok"}]
    required: Mapped[list] = mapped_column(JSON, default=list)  # what an administrator must do
    endpoint: Mapped[dict] = mapped_column(JSON, default=dict)  # {"method", "url", "operation"}
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    still_failing: Mapped[str] = mapped_column(Text, default="")
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    fix_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approval_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model: Mapped[str] = mapped_column(String(200), default="")
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Timestamped history that survives re-investigations: [{"at", "run", "stage", "text", "ok"}]
    log: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    runs: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
