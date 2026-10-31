from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.db import get_db, new_id, utcnow
from ..core.security import Principal, current_principal, require_human, require_role
from ..models import AIMessage, AIModel, AIProvider, AISession, UsageRecord
from ..services import audit, openapi, orchestrator, vault
from ..services import ai_gateway
from ..services.ai_gateway import TASKS, Gateway, ProviderError, bedrock_region
from .common import iso

router = APIRouter(prefix="/api/v1/ai", tags=["AI orchestration"])
MAX_DOC_BYTES = 1_000_000


def _session(db: Session, p: Principal, sid: str) -> AISession:
    s = db.get(AISession, sid)
    if not s or s.organization_id != p.org_id or (s.user_id != p.id and not p.at_least("admin")):
        raise HTTPException(404, "Session not found")
    return s


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class SessionIn(BaseModel):
    message: str | None = Field(default=None, max_length=8000)


@router.post("/sessions")
def create_chat(body: SessionIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    s = AISession(user_id=p.id, organization_id=p.org_id, state={"stages": {}})
    db.add(s)
    db.commit()
    if body.message:
        orchestrator.handle_message(db, s, body.message)
    return orchestrator.session_view(db, s)


@router.get("/sessions")
def list_sessions(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.execute(select(AISession, func.count(AIMessage.id)).outerjoin(AIMessage, AIMessage.session_id == AISession.id)
                      .where(AISession.user_id == p.id).group_by(AISession.id).order_by(AISession.updated_at.desc()).limit(50)).all()
    return [{"id": s.id, "title": s.title, "status": s.status, "stage": s.stage, "messages": n, "updated_at": iso(s.updated_at)} for s, n in rows]


@router.get("/sessions/{sid}")
def resume_session(sid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return orchestrator.session_view(db, _session(db, p, sid))


@router.delete("/sessions/{sid}")
def delete_session(sid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    s = _session(db, p, sid)
    for m in db.scalars(select(AIMessage).where(AIMessage.session_id == s.id)).all():
        db.delete(m)
    db.delete(s)
    db.commit()
    return {"ok": True}


@router.post("/sessions/{sid}/messages")
def send_message(sid: str, body: MessageIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    s = _session(db, p, sid)
    if orchestrator.session_view(db, s)["status"] == "running":
        raise HTTPException(409, "Wait for generation to finish")
    orchestrator.handle_message(db, s, body.content)
    return orchestrator.session_view(db, s)


class DocumentIn(BaseModel):
    name: str = Field(default="openapi.json", max_length=200)
    content: str = Field(min_length=2)


@router.post("/sessions/{sid}/documents")
def upload_document(sid: str, body: DocumentIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    s = _session(db, p, sid)
    if len(body.content.encode()) > MAX_DOC_BYTES:
        raise HTTPException(413, "Documents are limited to 1 MB")
    try:
        cat = openapi.parse_openapi(body.content)
    except openapi.OpenAPIError as e:
        raise HTTPException(422, f"Could not import {body.name}: {e}")
    docs = [d for d in (s.documents or []) if openapi.slugify(d["catalog"]["title"]) != openapi.slugify(cat["title"])]
    docs.append({"id": new_id(), "name": body.name, "kind": "openapi", "catalog": cat})
    s.documents = docs
    db.add(AIMessage(session_id=s.id, role="system", content=f"Uploaded {body.name}: {cat['title']} with {len(cat['operations'])} operations.",
                     meta={"document": cat["title"]}))
    db.commit()
    orchestrator.refresh_requirements(db, s)
    return orchestrator.session_view(db, s)


class GenerateIn(BaseModel):
    from_stage: str | None = None


@router.post("/sessions/{sid}/generate")
def generate(sid: str, body: GenerateIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    s = _session(db, p, sid)
    if not (s.requirements or {}).get("ready"):
        raise HTTPException(409, "Answer the open questions before generating")
    orchestrator.start(s.id, body.from_stage)
    s.status = "running"
    db.commit()
    return orchestrator.session_view(db, s)


@router.post("/sessions/{sid}/revise")
def revise(sid: str, body: MessageIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    s = _session(db, p, sid)
    orchestrator.handle_message(db, s, body.content)
    if (s.requirements or {}).get("ready"):
        orchestrator.start(s.id, None)
    return orchestrator.session_view(db, s)


# ------------------------------------------------------------------------------------ providers

def provider_view(db: Session, pr: AIProvider) -> dict:
    models = db.scalars(select(AIModel).where(AIModel.provider_id == pr.id).order_by(AIModel.priority)).all()
    return {"id": pr.id, "name": pr.name, "provider_type": pr.provider_type, "base_url": pr.base_url, "status": pr.status,
            "has_key": bool(pr.secret_ref), "last_error": pr.last_error, "region": bedrock_region(pr.base_url, {}) if pr.provider_type == "bedrock" else "",
            "models": [{"id": m.id, "model_id": m.model_id, "label": m.label, "purposes": m.purposes, "capabilities": m.capabilities,
                        "allow_confidential": m.allow_confidential, "cost_input_per_mtok": m.cost_input_per_mtok,
                        "cost_output_per_mtok": m.cost_output_per_mtok, "priority": m.priority, "enabled": m.enabled} for m in models]}


@router.get("/providers")
def list_providers(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return [provider_view(db, pr) for pr in db.scalars(select(AIProvider).order_by(AIProvider.created_at)).all()]


PROVIDER_TYPES = "^(openai_compatible|anthropic|gemini|bedrock)$"


class ProviderDraft(BaseModel):
    """Connection settings for a provider that is not saved yet (listing models, testing before saving)."""
    provider_type: str = Field(pattern=PROVIDER_TYPES)
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=2000)  # Bedrock: a Bedrock API key (bearer token)
    region: str = Field(default="", max_length=32)  # Bedrock only
    aws_access_key_id: str = Field(default="", max_length=128)
    aws_secret_access_key: str = Field(default="", max_length=2000)
    aws_session_token: str = Field(default="", max_length=8000)
    model_id: str = Field(default="", max_length=300)


class ProviderIn(ProviderDraft):
    name: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=300)
    purposes: list[str] = ["general"]


DEFAULT_BASE = {"openai_compatible": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com",
                "gemini": "https://generativelanguage.googleapis.com/v1beta", "bedrock": "https://bedrock-runtime.us-east-1.amazonaws.com"}

# Ready-made endpoints shown in the UI. Model ids are examples: providers rename and retire models, check their docs.
PRESETS = [
    {"id": "bedrock", "name": "Amazon Bedrock", "provider_type": "bedrock", "base_url": "", "key_label": "AWS access keys or a Bedrock API key",
     "models": ["us.anthropic.claude-3-7-sonnet-20250219-v1:0", "anthropic.claude-3-5-sonnet-20241022-v2:0", "amazon.nova-pro-v1:0", "meta.llama3-3-70b-instruct-v1:0"],
     "docs": "https://docs.aws.amazon.com/bedrock/latest/userguide/getting-started.html",
     "note": "Enable the model under Bedrock → Model access in your AWS account and region first. Newer models need an inference-profile id such as us.anthropic…"},
    {"id": "nvidia", "name": "NVIDIA NIM", "provider_type": "openai_compatible", "base_url": "https://integrate.api.nvidia.com/v1", "key_label": "NVIDIA API key (nvapi-…)",
     "models": ["meta/llama-3.1-70b-instruct", "nvidia/llama-3.1-nemotron-70b-instruct", "meta/llama-3.3-70b-instruct"], "docs": "https://build.nvidia.com/", "note": ""},
    {"id": "groq", "name": "Groq", "provider_type": "openai_compatible", "base_url": "https://api.groq.com/openai/v1", "key_label": "Groq API key",
     "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"], "docs": "https://console.groq.com/docs/models", "note": "Groq, the fast-inference service."},
    {"id": "xai", "name": "xAI Grok", "provider_type": "openai_compatible", "base_url": "https://api.x.ai/v1", "key_label": "xAI API key",
     "models": ["grok-3", "grok-3-mini", "grok-2-latest"], "docs": "https://docs.x.ai/docs/models", "note": "Grok from xAI."},
    {"id": "gemini", "name": "Google Gemini", "provider_type": "gemini", "base_url": "", "key_label": "Gemini API key",
     "models": ["gemini-2.5-flash", "gemini-2.5-pro"], "docs": "https://ai.google.dev/gemini-api/docs/models", "note": ""},
    {"id": "openai", "name": "OpenAI", "provider_type": "openai_compatible", "base_url": "https://api.openai.com/v1", "key_label": "OpenAI API key",
     "models": ["gpt-4o-mini", "gpt-4o"], "docs": "https://platform.openai.com/docs/models", "note": ""},
    {"id": "anthropic", "name": "Anthropic Claude", "provider_type": "anthropic", "base_url": "", "key_label": "Anthropic API key",
     "models": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"], "docs": "https://docs.anthropic.com/en/docs/about-claude/models", "note": ""},
    {"id": "openrouter", "name": "OpenRouter", "provider_type": "openai_compatible", "base_url": "https://openrouter.ai/api/v1", "key_label": "OpenRouter API key",
     "models": ["anthropic/claude-3.5-sonnet", "meta-llama/llama-3.3-70b-instruct"], "docs": "https://openrouter.ai/models", "note": "One key, many models."},
    {"id": "together", "name": "Together AI", "provider_type": "openai_compatible", "base_url": "https://api.together.xyz/v1", "key_label": "Together API key",
     "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"], "docs": "https://docs.together.ai/docs/serverless-models", "note": ""},
    {"id": "mistral", "name": "Mistral AI", "provider_type": "openai_compatible", "base_url": "https://api.mistral.ai/v1", "key_label": "Mistral API key",
     "models": ["mistral-large-latest", "mistral-small-latest"], "docs": "https://docs.mistral.ai/getting-started/models/", "note": ""},
    {"id": "deepseek", "name": "DeepSeek", "provider_type": "openai_compatible", "base_url": "https://api.deepseek.com/v1", "key_label": "DeepSeek API key",
     "models": ["deepseek-chat"], "docs": "https://api-docs.deepseek.com/", "note": ""},
    {"id": "fireworks", "name": "Fireworks AI", "provider_type": "openai_compatible", "base_url": "https://api.fireworks.ai/inference/v1", "key_label": "Fireworks API key",
     "models": ["accounts/fireworks/models/llama-v3p3-70b-instruct"], "docs": "https://docs.fireworks.ai/", "note": ""},
    {"id": "ollama", "name": "Ollama (local)", "provider_type": "openai_compatible", "base_url": "http://localhost:11434/v1", "key_label": "No key needed",
     "models": ["llama3.1"], "docs": "https://ollama.com/library", "note": "Runs on your own machine; nothing leaves it."},
    {"id": "lmstudio", "name": "LM Studio (local)", "provider_type": "openai_compatible", "base_url": "http://localhost:1234/v1", "key_label": "No key needed",
     "models": [], "docs": "https://lmstudio.ai/docs", "note": "Start the local server in LM Studio first."},
    {"id": "custom", "name": "Custom OpenAI-compatible endpoint", "provider_type": "openai_compatible", "base_url": "", "key_label": "API key (if the endpoint needs one)",
     "models": [], "docs": "", "note": "Anything that speaks /chat/completions: vLLM, LiteLLM, Azure OpenAI gateways, internal gateways…"},
]


@router.get("/provider-presets")
def provider_presets(p: Principal = Depends(current_principal)):
    return PRESETS


def _secret_for(body) -> dict | None:
    """The provider secret from whichever credential fields were supplied (None = nothing supplied)."""
    if body.provider_type == "bedrock":
        if body.aws_access_key_id and body.aws_secret_access_key:
            out = {"access_key_id": body.aws_access_key_id.strip(), "secret_access_key": body.aws_secret_access_key.strip()}
            if body.aws_session_token:
                out["session_token"] = body.aws_session_token.strip()
            return out
        if body.api_key:
            return {"bearer_token": body.api_key.strip()}
        return None
    return {"api_key": body.api_key} if body.api_key else None


def _bedrock_base(body) -> str:
    region = (body.region or "us-east-1").strip().lower()
    if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region):
        raise HTTPException(422, "Region should look like us-east-1")
    return f"https://bedrock-runtime.{region}.amazonaws.com"


def _target(body: ProviderDraft) -> tuple[str, dict | None]:
    """Base URL and secret for provider settings, validated the same way for saving, listing models and testing."""
    if body.provider_type == "bedrock":
        base = _bedrock_base(body)
        if _secret_for(body) is None:
            raise HTTPException(422, "Bedrock needs an AWS access key pair or a Bedrock API key (type env:NAME to read it from a server environment variable)")
    else:
        base = (body.base_url or DEFAULT_BASE[body.provider_type]).rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise HTTPException(422, "Base URL must be http(s)")
    return base, _secret_for(body)


def _live_secret(secret: dict | None) -> dict:
    # Platform (owner) secrets may name any environment variable, exactly as when the saved provider is used.
    return {k: vault.resolve_env(v) for k, v in (secret or {}).items()}


@router.post("/providers")
def register_provider(body: ProviderIn, p: Principal = Depends(require_human("owner")), db: Session = Depends(get_db)):
    base, secret = _target(body)
    pr = AIProvider(name=body.name, provider_type=body.provider_type, base_url=base,
                    secret_ref=vault.store_secret(db, None, secret) if secret else None)
    db.add(pr)
    db.flush()
    db.add(AIModel(provider_id=pr.id, model_id=body.model_id, label=body.model_id, purposes=[x for x in body.purposes if x in TASKS or x == "general"] or ["general"],
                   capabilities={"json_mode": body.provider_type != "bedrock", "context_tokens": 128000}, priority=50))
    audit.record(db, None, p, "ai.provider.create", body.name, type=body.provider_type)
    db.commit()
    return provider_view(db, pr)


class ProviderPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, max_length=500)
    region: str | None = Field(default=None, max_length=32)
    api_key: str | None = Field(default=None, max_length=2000)
    aws_access_key_id: str | None = Field(default=None, max_length=128)
    aws_secret_access_key: str | None = Field(default=None, max_length=2000)
    aws_session_token: str | None = Field(default=None, max_length=8000)


@router.patch("/providers/{pid}")
def update_provider(pid: str, body: ProviderPatch, p: Principal = Depends(require_human("owner")), db: Session = Depends(get_db)):
    pr = db.get(AIProvider, pid)
    if not pr:
        raise HTTPException(404, "Provider not found")
    if body.status:
        pr.status = body.status
    if body.name and pr.provider_type != "local":
        pr.name = body.name
    if pr.provider_type == "bedrock" and body.region:
        pr.base_url = _bedrock_base(body)
    elif body.base_url is not None and pr.provider_type not in ("local", "bedrock"):
        pr.base_url = body.base_url.rstrip("/")
    secret = _secret_for(type("S", (), {"provider_type": pr.provider_type, "api_key": body.api_key or "", "aws_access_key_id": body.aws_access_key_id or "",
                                         "aws_secret_access_key": body.aws_secret_access_key or "", "aws_session_token": body.aws_session_token or ""})())
    if secret:
        vault.delete_secret(db, pr.secret_ref)
        pr.secret_ref = vault.store_secret(db, None, secret)
    audit.record(db, None, p, "ai.provider.update", pr.name, status=pr.status, key_rotated=bool(secret))
    db.commit()
    return provider_view(db, pr)


@router.post("/providers/discover-models")
def discover_models(body: ProviderDraft, p: Principal = Depends(require_human("owner"))):
    """List the model ids the endpoint offers so the UI can show a pick-list. Nothing is stored."""
    base, secret = _target(body)
    try:
        ids = ai_gateway.list_models(body.provider_type, base, _live_secret(secret))
    except ProviderError as e:
        raise HTTPException(502, f"Could not list models: {str(e)[:300]}")
    return {"models": ids[:500]}


@router.post("/providers/test-config")
def test_config(body: ProviderDraft, p: Principal = Depends(require_human("owner"))):
    """Send one tiny request with unsaved settings, so a provider can be checked before it is added. Nothing is stored."""
    if not body.model_id.strip():
        raise HTTPException(422, "Pick a model to test")
    base, secret = _target(body)
    started = time.monotonic()
    try:
        reply = ai_gateway.probe(body.provider_type, base, _live_secret(secret), body.model_id.strip())
    except ProviderError as e:
        return {"ok": False, "error": str(e)[:400]}
    return {"ok": True, "reply": reply.strip()[:200], "latency_ms": int((time.monotonic() - started) * 1000)}


@router.delete("/providers/{pid}")
def delete_provider(pid: str, p: Principal = Depends(require_human("owner")), db: Session = Depends(get_db)):
    pr = db.get(AIProvider, pid)
    if not pr:
        raise HTTPException(404, "Provider not found")
    if pr.provider_type == "local":
        raise HTTPException(409, "The built-in model is the last-resort fallback; disable it instead")
    for m in db.scalars(select(AIModel).where(AIModel.provider_id == pr.id)).all():
        db.delete(m)
    vault.delete_secret(db, pr.secret_ref)
    db.delete(pr)
    audit.record(db, None, p, "ai.provider.delete", pr.name)
    db.commit()
    return {"ok": True}


@router.post("/providers/{pid}/test")
def test_provider(pid: str, p: Principal = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pr = db.get(AIProvider, pid)
    if not pr:
        raise HTTPException(404, "Provider not found")
    return Gateway(db, p.org_id).test_provider(pr)


class ModelIn(BaseModel):
    model_id: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=120)
    purposes: list[str] = ["general"]
    allow_confidential: bool = True
    cost_input_per_mtok: float = Field(default=0, ge=0)
    cost_output_per_mtok: float = Field(default=0, ge=0)
    priority: int = Field(default=50, ge=0, le=10000)
    json_mode: bool = True
    context_tokens: int = Field(default=128000, ge=1000)
    enabled: bool = True


def _apply_model(m: AIModel, body: ModelIn) -> None:
    m.model_id, m.label = body.model_id, body.label or body.model_id
    m.purposes = [x for x in body.purposes if x in TASKS or x == "general"] or ["general"]
    m.allow_confidential, m.priority, m.enabled = body.allow_confidential, body.priority, body.enabled
    m.cost_input_per_mtok, m.cost_output_per_mtok = body.cost_input_per_mtok, body.cost_output_per_mtok
    m.capabilities = {"json_mode": body.json_mode, "context_tokens": body.context_tokens}


@router.post("/providers/{pid}/models")
def register_model(pid: str, body: ModelIn, p: Principal = Depends(require_human("owner")), db: Session = Depends(get_db)):
    pr = db.get(AIProvider, pid)
    if not pr or pr.provider_type == "local":
        raise HTTPException(404, "Provider not found")
    m = AIModel(provider_id=pid)
    _apply_model(m, body)
    db.add(m)
    audit.record(db, None, p, "ai.model.create", body.model_id, provider=pr.name)
    db.commit()
    return provider_view(db, pr)


@router.put("/models/{mid}")
def update_model(mid: str, body: ModelIn, p: Principal = Depends(require_human("owner")), db: Session = Depends(get_db)):
    m = db.get(AIModel, mid)
    if not m:
        raise HTTPException(404, "Model not found")
    pr = db.get(AIProvider, m.provider_id)
    if pr.provider_type == "local":
        m.enabled, m.priority = body.enabled, body.priority
    else:
        _apply_model(m, body)
    audit.record(db, None, p, "ai.model.update", m.model_id)
    db.commit()
    return provider_view(db, pr)


@router.delete("/models/{mid}")
def delete_model(mid: str, p: Principal = Depends(require_human("owner")), db: Session = Depends(get_db)):
    m = db.get(AIModel, mid)
    if not m:
        raise HTTPException(404, "Model not found")
    pr = db.get(AIProvider, m.provider_id)
    if pr.provider_type == "local":
        raise HTTPException(409, "The built-in model cannot be deleted")
    db.delete(m)
    db.commit()
    return {"ok": True}


@router.get("/routing")
def routing(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    gw = Gateway(db, p.org_id)
    return {"tasks": [{"task": t, "structured": TASKS[t][0], "confidential": TASKS[t][1],
                       "candidates": [f"{pr.name} · {m.label or m.model_id}" for m, pr in gw.select_models(t)]} for t in TASKS],
            "month_spend": round(gw.month_spend(), 4), "budget": gw.budget()}


@router.get("/usage")
def usage(p: Principal = Depends(require_role("admin")), db: Session = Depends(get_db)):
    month = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    base = select(UsageRecord).where(UsageRecord.organization_id == p.org_id, UsageRecord.created_at >= month)
    rows = db.scalars(base).all()
    by: dict = {}
    for r in rows:
        key = (r.model_name, r.task)
        b = by.setdefault(key, {"model": r.model_name, "task": r.task, "calls": 0, "failures": 0, "prompt_tokens": 0,
                                "completion_tokens": 0, "cost": 0.0, "latency_ms": 0})
        b["calls"] += 1
        b["failures"] += 0 if r.success else 1
        b["prompt_tokens"] += r.prompt_tokens
        b["completion_tokens"] += r.completion_tokens
        b["cost"] += r.estimated_cost
        b["latency_ms"] += r.latency_ms
    for b in by.values():
        b["latency_ms"] = int(b["latency_ms"] / b["calls"])
        b["cost"] = round(b["cost"], 5)
    recent = db.scalars(base.order_by(UsageRecord.created_at.desc()).limit(25)).all()
    gw = Gateway(db, p.org_id)
    return {"month_spend": round(gw.month_spend(), 5), "budget": gw.budget(), "rows": sorted(by.values(), key=lambda b: -b["calls"]),
            "recent": [{"model": r.model_name, "task": r.task, "success": r.success, "error": r.error[:160], "latency_ms": r.latency_ms,
                        "tokens": r.prompt_tokens + r.completion_tokens, "cost": r.estimated_cost, "at": iso(r.created_at)} for r in recent]}
