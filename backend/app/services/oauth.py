"""Sign in with a provider ("Continue with Google"): the OAuth 2.0 authorization-code flow with PKCE.

An administrator registers an OAuth app at the provider once (Settings > Sign-in providers) and gives H.A.D.E.S its
client ID and secret. Anyone may then press "Continue with Google": they are redirected to the provider, approve
read access, and come back with a connection whose tokens live in the vault. Access tokens are refreshed before they
expire. The scopes asked for are those of the provider's data-source templates, so one sign-in can import them all.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.db import session_scope, utcnow
from ..models import Connection, Organization
from . import audit, egress, vault

log = logging.getLogger("hedes.oauth")

AUTH_TYPE = "oauth2_authorization_code"
STATE_TTL = 600
PROVIDERS: dict[str, dict] = {
    "google": {
        "label": "Google", "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth", "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo", "account_field": "email", "base_scopes": ["openid", "email"],
        "extra": {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
        "console": "https://console.cloud.google.com/apis/credentials",
        "setup": ["Google Cloud console > APIs & Services > Credentials > Create credentials > OAuth client ID > Web application.",
                  "Add the redirect URI below under Authorised redirect URIs.",
                  "Enable the APIs the data sources use (for Google Workspace: Admin SDK API)."],
    },
    "microsoft": {
        "label": "Microsoft", "authorize_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", "userinfo_url": "https://graph.microsoft.com/oidc/userinfo",
        "account_field": "email", "base_scopes": ["openid", "email", "offline_access"], "extra": {"prompt": "select_account"},
        "settings": {"tenant": {"label": "Tenant", "default": "organizations", "help": "Directory (tenant) ID, or 'organizations' for any work account"}},
        "console": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
        "setup": ["Microsoft Entra admin center > App registrations > New registration (platform: Web).",
                  "Add the redirect URI below, then create a client secret under Certificates & secrets.",
                  "Under API permissions add the delegated Microsoft Graph permissions and grant admin consent."],
    },
    "github": {
        "label": "GitHub", "authorize_url": "https://github.com/login/oauth/authorize", "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user", "account_field": "login", "base_scopes": [],
        "console": "https://github.com/settings/developers",
        "setup": ["GitHub > Settings > Developer settings > OAuth Apps > New OAuth App.",
                  "Use the redirect URI below as the Authorization callback URL, then generate a client secret."],
    },
    "atlassian": {
        "label": "Atlassian (Jira)", "authorize_url": "https://auth.atlassian.com/authorize", "token_url": "https://auth.atlassian.com/oauth/token",
        "userinfo_url": "https://api.atlassian.com/me", "account_field": "email", "base_scopes": ["offline_access", "read:me"],
        "extra": {"audience": "api.atlassian.com", "prompt": "consent"},
        "resources_url": "https://api.atlassian.com/oauth/token/accessible-resources",
        "console": "https://developer.atlassian.com/console/myapps/",
        "setup": ["developer.atlassian.com > Create > OAuth 2.0 integration.",
                  "Authorization: add the redirect URI below as the callback URL. Permissions: add Jira API with the read scopes.",
                  "Settings: copy the client ID and secret."],
    },
}

_STATES: dict[str, dict] = {}
_STATE_LOCK = threading.Lock()
_REFRESH_LOCKS: dict[str, threading.Lock] = {}


class OAuthError(Exception):
    pass


def redirect_uri() -> str:
    return get_settings().public_base_url.rstrip("/") + "/api/v1/oauth/callback"


# ----------------------------------------------------------------------------------------------- app configuration

def apps_of(org: Organization) -> dict:
    return dict((org.policy or {}).get("oauth_apps") or {})


def save_app(db: Session, org: Organization, provider: str, client_id: str, client_secret: str | None, settings: dict) -> None:
    apps = apps_of(org)
    app = dict(apps.get(provider) or {})
    if client_secret:
        vault.delete_secret(db, app.get("secret_ref"))
        app["secret_ref"] = vault.store_secret(db, org.id, {"client_secret": client_secret})
    spec = PROVIDERS[provider].get("settings") or {}
    app.update(client_id=client_id, settings={k: str(settings.get(k) or v.get("default", "")).strip() for k, v in spec.items()})
    apps[provider] = app
    org.policy = {**(org.policy or {}), "oauth_apps": apps}


def _app(db: Session, org_id: str, provider: str) -> tuple[dict, str]:
    org = db.get(Organization, org_id)
    app = apps_of(org).get(provider) or {}
    secret = (vault.read_secret(db, app.get("secret_ref"), org_id) or {}).get("client_secret", "") if app.get("secret_ref") else ""
    if not app.get("client_id") or not secret:
        raise OAuthError(f"{PROVIDERS[provider]['label']} sign-in is not set up. An administrator adds the OAuth app in Settings > Sign-in providers.")
    return app, secret


def _url(provider: str, key: str, app: dict) -> str:
    return PROVIDERS[provider][key].format(**{k: v for k, v in (app.get("settings") or {}).items()})


def scopes_for(provider: str) -> list[str]:
    """The provider's base scopes plus every scope its data-source templates need: one sign-in covers them all."""
    from . import catalog
    extra = [s for t in catalog.source_templates() if (t.get("credential") or {}).get("provider") == provider
             for s in (t["credential"].get("scopes") or [])]
    return list(dict.fromkeys(PROVIDERS[provider]["base_scopes"] + extra))


# ----------------------------------------------------------------------------------------------- the flow

def _check_replace(conn: Connection | None, name: str, user_id: str, admin: bool) -> None:
    """Signing in again replaces a credential. Like rotating a secret, only its creator or an administrator may do that:
    otherwise anyone could swap the organisation's connection for their own account."""
    if conn and conn.status != "revoked" and not admin and conn.created_by != user_id:
        raise OAuthError(f"A credential named {name!r} was connected by someone else. Only they or an administrator can replace it.")


def start(db: Session, org_id: str, user_id: str, provider: str, connection_name: str, admin: bool = False) -> tuple[str, str]:
    """Returns (authorize URL, browser nonce). The nonce goes into a cookie: only the browser that started the
    sign-in can finish it, so nobody can make you link their account by sending you a link."""
    if provider not in PROVIDERS:
        raise OAuthError("Unknown provider")
    app, _ = _app(db, org_id, provider)
    _check_replace(db.scalars(select(Connection).where(Connection.organization_id == org_id, Connection.name == connection_name)).first(),
                   connection_name, user_id, admin)
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    now = time.time()
    with _STATE_LOCK:
        for k in [k for k, v in _STATES.items() if now - v["at"] > STATE_TTL]:
            _STATES.pop(k, None)
        _STATES[state] = {"org_id": org_id, "user_id": user_id, "admin": admin, "provider": provider, "name": connection_name,
                          "verifier": verifier, "nonce": nonce, "at": now}
    params = {"response_type": "code", "client_id": app["client_id"], "redirect_uri": redirect_uri(), "state": state,
              "scope": " ".join(scopes_for(provider)), "code_challenge": challenge, "code_challenge_method": "S256",
              **(PROVIDERS[provider].get("extra") or {})}
    return _url(provider, "authorize_url", app) + "?" + urlencode(params), nonce


def _post_token(provider: str, app: dict, client_secret: str, data: dict) -> dict:
    url = _url(provider, "token_url", app)
    egress.check_url(url)
    r = httpx.post(url, data={**data, "client_id": app["client_id"], "client_secret": client_secret},
                   headers={"Accept": "application/json"}, timeout=20)
    try:
        body = r.json()
    except ValueError:
        raise OAuthError(f"{PROVIDERS[provider]['label']} answered HTTP {r.status_code} without JSON")
    if r.status_code >= 400 or not body.get("access_token"):  # GitHub reports errors with HTTP 200
        raise OAuthError(f"{PROVIDERS[provider]['label']} refused: {body.get('error_description') or body.get('error') or f'HTTP {r.status_code}'}")
    return body


def _get_json(url: str, token: str) -> object:
    egress.check_url(url)
    r = httpx.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=20)
    r.raise_for_status()
    return r.json()


def _tokens(provider: str, body: dict, old: dict | None = None) -> dict:
    out = {**(old or {}), "provider": provider, "access_token": body["access_token"],
           "scope": body.get("scope", (old or {}).get("scope", ""))}
    if body.get("refresh_token"):
        out["refresh_token"] = body["refresh_token"]
    out["expires_at"] = int(time.time()) + int(body["expires_in"]) if body.get("expires_in") else None
    return out


def finish(db: Session, state: str, code: str, nonce: str) -> Connection:
    with _STATE_LOCK:
        st = _STATES.pop(state, None)
    if not st or time.time() - st["at"] > STATE_TTL:
        raise OAuthError("This sign-in link has expired. Start again from Credentials.")
    if not nonce or not secrets.compare_digest(nonce, st["nonce"]):
        raise OAuthError("This sign-in was started in another browser. Start again from Credentials.")
    provider, spec = st["provider"], PROVIDERS[st["provider"]]
    app, client_secret = _app(db, st["org_id"], provider)
    secret = _tokens(provider, _post_token(provider, app, client_secret, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(), "code_verifier": st["verifier"]}))
    try:
        info = _get_json(spec["userinfo_url"], secret["access_token"])
        secret["account"] = str((info or {}).get(spec["account_field"]) or (info or {}).get("email") or "")[:200]
    except Exception as e:  # noqa: BLE001 - the account label is a nicety
        log.info("userinfo failed for %s: %s", provider, e)
    if spec.get("resources_url"):  # Atlassian: which Jira sites the person granted access to
        try:
            sites = _get_json(spec["resources_url"], secret["access_token"]) or []
            secret["discovered"] = {"cloud_id": sites[0]["id"], "site_url": sites[0].get("url", "")} if sites else {}
        except Exception as e:  # noqa: BLE001
            log.info("resource discovery failed for %s: %s", provider, e)
    conn = db.scalars(select(Connection).where(Connection.organization_id == st["org_id"], Connection.name == st["name"])).first()
    if conn and conn.auth_type != AUTH_TYPE and conn.status != "revoked":
        raise OAuthError(f"A credential named {st['name']!r} of another type already exists.")
    _check_replace(conn, st["name"], st["user_id"], st["admin"])  # again: it may have been created during the sign-in
    if conn:
        vault.delete_secret(db, conn.secret_ref)
    else:
        conn = Connection(organization_id=st["org_id"], name=st["name"], auth_type=AUTH_TYPE, created_by=st["user_id"], secret_ref="")
        db.add(conn)
    conn.auth_type, conn.status, conn.rotated_at, conn.expires_at = AUTH_TYPE, "active", utcnow(), None
    conn.secret_ref = vault.store_secret(db, st["org_id"], secret)
    conn.hint = (secret.get("account") or spec["label"])[:32]
    conn.test_url = spec["userinfo_url"]
    audit.record(db, st["org_id"], None, "connection.oauth", conn.name, provider=provider, account=secret.get("account", ""), user_id=st["user_id"])
    db.commit()
    return conn


def account_of(db: Session, conn: Connection) -> dict:
    """The non-secret parts of an OAuth connection: provider, account and discovered settings (for template variables)."""
    s = vault.read_secret(db, conn.secret_ref, conn.organization_id) or {}
    return {"provider": s.get("provider", ""), "account": s.get("account", ""), "discovered": s.get("discovered") or {}}


# ----------------------------------------------------------------------------------------------- refresh

def fresh_secret(db: Session, conn: Connection, secret: dict | None) -> dict | None:
    """The credential with a valid access token: refreshed (and saved) when it expires within a minute."""
    if conn.auth_type != AUTH_TYPE or not secret or not secret.get("expires_at") or time.time() < secret["expires_at"] - 60:
        return secret
    if not secret.get("refresh_token"):
        return secret  # the request fails with 401 and the alert says to reconnect
    lock = _REFRESH_LOCKS.setdefault(conn.id, threading.Lock())
    with lock:  # parallel steps refresh once: some providers invalidate a refresh token as soon as it is used
        with session_scope() as own:
            c = own.get(Connection, conn.id)
            current = vault.read_secret(own, c.secret_ref, c.organization_id) or {}
            if current.get("expires_at") and time.time() < current["expires_at"] - 60:
                fresh = current  # another thread refreshed it meanwhile
            else:
                try:
                    app, client_secret = _app(own, c.organization_id, current["provider"])
                    body = _post_token(current["provider"], app, client_secret, {"grant_type": "refresh_token", "refresh_token": current["refresh_token"]})
                except (OAuthError, httpx.HTTPError, egress.EgressDenied) as e:  # e.g. the token host does not resolve
                    log.warning("token refresh for %s failed: %s", c.name, e)
                    c.last_test_result = f"refresh failed: {str(e)[:170]}"
                    return secret
                fresh = _tokens(current["provider"], body, current)
                vault.delete_secret(own, c.secret_ref)
                c.secret_ref = vault.store_secret(own, c.organization_id, fresh)
    db.refresh(conn)
    return fresh
