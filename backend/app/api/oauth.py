"""Sign in with a provider ("Continue with Google") and import every data source that provider offers."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.db import get_db
from ..core.security import Principal, current_principal, require_human, require_role
from ..models import Connection, Integration, Organization
from ..services import audit, catalog, oauth
from .templates import create_template_integration

router = APIRouter(prefix="/api/v1/oauth", tags=["sign-in providers"])
NONCE_COOKIE = "hedes_oauth"


def _templates(provider: str) -> list[dict]:
    return [t for t in catalog.source_templates() if (t.get("credential") or {}).get("provider") == provider]


@router.get("/providers")
def providers(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    org = db.get(Organization, p.org_id)
    apps = oauth.apps_of(org)
    installed = {i.slug for i in db.scalars(select(Integration).where(Integration.organization_id == p.org_id)).all()}
    out = []
    for pid, spec in oauth.PROVIDERS.items():
        app = apps.get(pid) or {}
        conn = db.scalars(select(Connection).where(Connection.organization_id == p.org_id, Connection.name == pid,
                                                   Connection.auth_type == oauth.AUTH_TYPE)).first()
        info = oauth.account_of(db, conn) if conn else {}
        row = {"id": pid, "label": spec["label"], "configured": bool(app.get("client_id") and app.get("secret_ref")),
               "connection": {"id": conn.id, "status": conn.status, "account": info.get("account", ""), "discovered": info.get("discovered", {})} if conn else None,
               "templates": [{"id": t["id"], "title": t["title"], "summary": t.get("summary", ""), "installed": t["id"] in installed,
                              "variables": [{k: v[k] for k in ("name", "label", "placeholder", "help") if k in v} for v in t.get("variables") or []]}
                             for t in _templates(pid)]}
        if p.at_least("admin"):
            row.update(client_id=app.get("client_id", ""), settings=app.get("settings") or {}, setting_fields=spec.get("settings") or {},
                       redirect_uri=oauth.redirect_uri(), console=spec["console"], setup=spec["setup"], scopes=oauth.scopes_for(pid))
        out.append(row)
    return out


class AppIn(BaseModel):
    client_id: str = Field(min_length=4, max_length=300)
    client_secret: str | None = Field(default=None, max_length=1000)  # omitted = keep
    settings: dict[str, str] = Field(default_factory=dict)


@router.put("/providers/{pid}")
def save_provider(pid: str, body: AppIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    if pid not in oauth.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    org = db.get(Organization, p.org_id)
    if not body.client_secret and not (oauth.apps_of(org).get(pid) or {}).get("secret_ref"):
        raise HTTPException(422, "Add the client secret")
    oauth.save_app(db, org, pid, body.client_id.strip(), (body.client_secret or "").strip() or None, body.settings)
    audit.record(db, p.org_id, p, "settings.oauth_app", pid, client_id=body.client_id.strip())
    db.commit()
    return {"ok": True}


class StartIn(BaseModel):
    connection_name: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")


@router.post("/{pid}/start")
def start(pid: str, body: StartIn, p: Principal = Depends(require_human("user")), db: Session = Depends(get_db)):
    try:
        url, nonce = oauth.start(db, p.org_id, p.id, pid, body.connection_name or pid)
    except oauth.OAuthError as e:
        raise HTTPException(409, str(e))
    resp = JSONResponse({"url": url})
    # Lax (not Strict like the session) so it comes back on the provider's redirect; it only proves "same browser"
    resp.set_cookie(NONCE_COOKIE, nonce, max_age=oauth.STATE_TTL, httponly=True, samesite="lax",
                    secure=get_settings().cookie_secure, path="/api/v1/oauth")
    return resp


@router.get("/callback")
def callback(request: Request, state: str = "", code: str = "", error: str = "", error_description: str = "", db: Session = Depends(get_db)):
    """Where the provider sends the browser back. The session cookie is SameSite=Strict and is not sent on this
    cross-site redirect, so the one-time state (bound to this browser by the nonce cookie) identifies the sign-in."""
    def back(fragment: str) -> RedirectResponse:
        r = RedirectResponse(f"/#/credentials/{fragment}", status_code=303)
        r.delete_cookie(NONCE_COOKIE, path="/api/v1/oauth")
        return r
    if error or not code:
        return back("oauth-error/" + quote((error_description or error or "The sign-in was cancelled")[:200], safe=""))
    try:
        conn = oauth.finish(db, state, code, request.cookies.get(NONCE_COOKIE, ""))
    except oauth.OAuthError as e:
        return back("oauth-error/" + quote(str(e)[:200], safe=""))
    except Exception as e:  # noqa: BLE001 - never show a stack trace page in the middle of a sign-in
        return back("oauth-error/" + quote(f"Sign-in failed: {type(e).__name__}", safe=""))
    return back("connected/" + quote(conn.name, safe=""))


class ImportIn(BaseModel):
    templates: list[str] = Field(min_length=1, max_length=20)
    variables: dict[str, dict[str, str]] = Field(default_factory=dict)  # per template id


@router.post("/{pid}/import")
def import_sources(pid: str, body: ImportIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    """Create a draft integration (with its data sources) for each chosen template of this provider. Each still goes
    through the safe tests and administrator approval like any other integration."""
    offered = {t["id"] for t in _templates(pid)}
    conn = db.scalars(select(Connection).where(Connection.organization_id == p.org_id, Connection.name == pid,
                                               Connection.auth_type == oauth.AUTH_TYPE)).first()
    if not conn:
        raise HTTPException(409, f"Sign in with {oauth.PROVIDERS.get(pid, {}).get('label', pid)} first")
    discovered = oauth.account_of(db, conn).get("discovered") or {}
    results = []
    for tid in body.templates:
        if tid not in offered:
            results.append({"template": tid, "ok": False, "error": "Not a template of this provider"})
            continue
        try:
            r = create_template_integration(db, p, tid, {**discovered, **(body.variables.get(tid) or {})})
            results.append({"template": tid, "ok": True, "integration": r["integration"], "tests": r["tests"]})
        except HTTPException as e:
            db.rollback()
            results.append({"template": tid, "ok": False, "error": str(e.detail)})
    return {"results": results}
