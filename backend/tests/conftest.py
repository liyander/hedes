"""Starts a real server (the engine calls the bundled mock APIs over HTTP) on a free port with a temp database."""
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="integra-test-")
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{Path(_tmp, 'test.db').as_posix()}",
    "PUBLIC_BASE_URL": BASE,
    "SEED_PASSWORD": "test-password-123",
    "JWT_SECRET": "test-secret-" + "x" * 32,
    "SECRET_ENCRYPTION_KEY": "uLpQ4cW7l1PZxH6m0bQy3VqB2T9a8kq1xK3lJf0Hn5Y=",
    "RUN_SCHEDULER": "false", "DEMO_MODE": "true", "CONNECTION_ENV_VARS": "SOME_VAR",
    "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": "",
    # a developer's .env must not register a real Bedrock provider in the test run
    "BEDROCK_MODEL_ID": "", "AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": "", "AWS_SESSION_TOKEN": "", "AWS_BEARER_TOKEN_BEDROCK": "",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app  # noqa: E402
from app.services import docs_search  # noqa: E402


def _offline(url, limit):
    raise docs_search.DocsError("offline in tests")


docs_search.real_get = docs_search._get
docs_search._get = _offline  # no test may reach the internet for documentation; tests that need it install a fake


@pytest.fixture(scope="session")
def server():
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            if httpx.get(BASE + "/api/v1/health").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield BASE
    srv.should_exit = True


def login(base: str, username: str) -> httpx.Client:
    c = httpx.Client(base_url=base, headers={"X-Requested-With": "integra"}, timeout=30)
    r = c.post("/api/v1/auth/login", json={"username": username, "password": "test-password-123"})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture(scope="session")
def builder(server):
    return login(server, "builder")


@pytest.fixture(scope="session")
def admin(server):
    return login(server, "admin")


@pytest.fixture(scope="session")
def owner(server):
    return login(server, "owner")
