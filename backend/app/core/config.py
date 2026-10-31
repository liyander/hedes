"""Application settings, loaded from the environment (and an optional .env file)."""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = BACKEND_DIR.parent
DATA_DIR = BACKEND_DIR / "data"
EXAMPLES_DIR = ROOT_DIR / "examples"
SHARED_DIR = ROOT_DIR / "shared"
STATIC_DIR = BACKEND_DIR / "app" / "static"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(ROOT_DIR / ".env", BACKEND_DIR / ".env"), extra="ignore")

    database_url: str = f"sqlite:///{(DATA_DIR / 'integra.db').as_posix()}"
    public_base_url: str = "http://127.0.0.1:8000"

    # Signing key for session tokens and the key that encrypts stored credentials.
    # When unset, random keys are generated once and kept in backend/data (development only).
    jwt_secret: str = ""
    secret_encryption_key: str = ""
    session_hours: int = 12
    cookie_secure: bool = False

    # Password for the seeded demo accounts. When unset a random one is generated and logged once.
    seed_password: str = ""

    # URL prefixes on private networks that integrations may call (normally only the bundled mock APIs).
    # Comma separated. Defaults to <public_base_url>/mock/.
    private_egress_allowlist: str = ""

    # Optional AI providers seeded on first start. The built-in offline model is always available.
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # Amazon Bedrock (Converse API). Set BEDROCK_MODEL_ID plus AWS keys (or AWS_BEARER_TOKEN_BEDROCK) to register it at first start;
    # everything can also be done later in AI Providers.
    bedrock_model_id: str = ""
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""
    aws_bearer_token_bedrock: str = ""

    ai_timeout_seconds: int = 60
    run_scheduler: bool = True

    # Off by default: the product runs against real services only. DEMO_MODE=true additionally mounts the bundled mock
    # APIs (/mock/*), seeds sample integrations against them and enables the demo controls. Used by the test suite.
    demo_mode: bool = False

    @property
    def egress_allowlist(self) -> list[str]:
        raw = self.private_egress_allowlist or f"{self.public_base_url.rstrip('/')}/mock/"
        return [p.strip() for p in raw.split(",") if p.strip()]


def _persistent_key(name: str, factory) -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / name
    if path.exists():
        return path.read_text().strip()
    value = factory()
    path.write_text(value)
    return value


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.jwt_secret:
        s.jwt_secret = _persistent_key(".jwt-secret", lambda: secrets.token_urlsafe(48))
    if not s.secret_encryption_key:
        s.secret_encryption_key = _persistent_key(".vault-key", lambda: Fernet.generate_key().decode())
    return s
