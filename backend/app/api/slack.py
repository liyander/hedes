"""Settings > Slack: connect a Slack app so people can talk to the AI assistant from Slack."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.db import get_db
from ..core.security import Principal, current_principal, require_human
from ..models import Organization, User
from ..services import audit, slack_bot, vault

router = APIRouter(prefix="/api/v1/settings/slack", tags=["slack"])


def _view(db: Session, org: Organization, p: Principal) -> dict:
    cfg = slack_bot.config_of(org)
    links = cfg.get("links") or {}
    out = {"enabled": bool(cfg.get("enabled")), "configured": bool(cfg.get("bot_ref") and cfg.get("app_ref")),
           "team": cfg.get("team", ""), "bot_name": cfg.get("bot_name", ""), "status": slack_bot.STATUS.get(org.id, {"state": "stopped"}),
           "my_links": [s for s, u in links.items() if u == p.id], "manifest": slack_bot.MANIFEST,
           "missing_scopes": cfg.get("missing_scopes") or {}, "last_event": slack_bot.LAST_EVENT.get(org.id)}
    if p.at_least("admin"):
        out["links"] = [{"slack_user": s, "user_id": u, "username": (db.get(User, u).username if db.get(User, u) else "?")} for s, u in links.items()]
    return out


@router.get("")
def get_slack(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return _view(db, db.get(Organization, p.org_id), p)


class SlackIn(BaseModel):
    enabled: bool = False
    bot_token: str | None = Field(default=None, max_length=300)  # xoxb-…, omitted = keep
    app_token: str | None = Field(default=None, max_length=300)  # xapp-… with connections:write, omitted = keep


@router.put("")
def save_slack(body: SlackIn, p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    org = db.get(Organization, p.org_id)
    cfg = slack_bot.config_of(org)
    if body.bot_token:
        tok = body.bot_token.strip()
        if not tok.startswith("xoxb-"):
            raise HTTPException(422, "The bot token must be a Bot User OAuth Token starting with xoxb- (OAuth & Permissions).")
        try:
            who = slack_bot.call(tok, "auth.test")
        except slack_bot.SlackError as e:
            raise HTTPException(422, f"Slack rejected the bot token: {e}")
        vault.delete_secret(db, cfg.get("bot_ref"))
        cfg.update(bot_ref=vault.store_secret(db, p.org_id, {"token": tok}), team=who.get("team", ""), bot_name=who.get("user", ""),
                   bot_user_id=who.get("user_id", ""))
    if body.app_token:
        tok = body.app_token.strip()
        if not tok.startswith("xapp-"):
            raise HTTPException(422, "The app-level token must start with xapp- (Basic Information > App-Level Tokens, scope connections:write).")
        vault.delete_secret(db, cfg.get("app_ref"))
        cfg["app_ref"] = vault.store_secret(db, p.org_id, {"token": tok})
    if body.enabled and not (cfg.get("bot_ref") and cfg.get("app_ref")):
        raise HTTPException(422, "Add both the bot token and the app-level token before turning Slack on.")
    cfg["enabled"] = body.enabled
    if cfg.get("bot_ref"):  # re-checked on every save, so "reinstall the app, then Save" confirms the fix
        bot, _ = slack_bot.tokens(db, org) if not body.bot_token else (body.bot_token.strip(), None)
        cfg["missing_scopes"] = slack_bot.missing_scopes(bot)
    slack_bot.save_config(db, org, cfg)
    audit.record(db, p.org_id, p, "settings.slack", org.name, enabled=body.enabled, bot_rotated=bool(body.bot_token), app_rotated=bool(body.app_token))
    db.commit()
    slack_bot.sync_bridges()
    return _view(db, org, p)


@router.post("/link-code")
def link_code(p: Principal = Depends(require_human("admin")), db: Session = Depends(get_db)):
    """A one-time code (10 minutes) that links the sender's Slack user to this account when sent to the bot as `link <code>`."""
    org = db.get(Organization, p.org_id)
    if not slack_bot.config_of(org).get("enabled"):
        raise HTTPException(409, "Slack is not connected yet. An administrator must set it up first.")
    return {"code": slack_bot.new_link_code(p.org_id, p.id), "expires_in": slack_bot.CODE_TTL}


@router.delete("/links/{slack_user}")
def unlink(slack_user: str, p: Principal = Depends(require_human("user")), db: Session = Depends(get_db)):
    org = db.get(Organization, p.org_id)
    cfg = slack_bot.config_of(org)
    links = dict(cfg.get("links") or {})
    if slack_user not in links:
        raise HTTPException(404, "Not linked")
    if links[slack_user] != p.id and not p.at_least("admin"):
        raise HTTPException(403, "You can only unlink your own Slack account")
    links.pop(slack_user)
    cfg["links"] = links
    slack_bot.save_config(db, org, cfg)
    audit.record(db, p.org_id, p, "slack.unlink", slack_user)
    db.commit()
    return _view(db, org, p)
