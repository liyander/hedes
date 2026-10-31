"""Catalyst AppSail entry point: run H.A.D.E.S on the port AppSail assigns.

Dependencies are bundled in ./deps (built for Linux). With HEDES_DIAGNOSE=1, a failed start serves the error on the
port instead of exiting, so it can be read without console access (turn it off once the app runs).
"""
import collections
import logging
import os
import platform
import re
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ROOT, "deps"), os.path.join(ROOT, "backend")]
os.chdir(os.path.join(ROOT, "backend"))
PORT = int(os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT", "9000"))
# Settings without which a redeploy silently loses data: the local disk does not survive it, and missing keys would be
# generated there - every session and every vault-encrypted secret would become unreadable after the next deploy.
REQUIRED = ("DATABASE_URL", "JWT_SECRET", "SECRET_ENCRYPTION_KEY")


class Tail(logging.Handler):
    """The most recent log lines, for the diagnose page. Bounded: the handler stays attached while the app runs."""

    def __init__(self, size: int = 400):
        super().__init__()
        self.lines = collections.deque(maxlen=size)

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


tail = Tail()
logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout), tail])

error = ""
try:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Set " + ", ".join(missing) + " in app-config.json (env_variables) or the AppSail console")
    if not os.environ.get("PUBLIC_BASE_URL"):
        logging.warning("PUBLIC_BASE_URL is not set: OAuth redirect URIs, invitation links and alert links will point at 127.0.0.1")
    import uvicorn

    from app.main import app
    # log_config=None: uvicorn's own loggers reach the handlers above, so a failed startup shows up on the diagnose page.
    # FORWARDED_ALLOW_IPS: the proxy addresses whose X-Forwarded-For is trusted for the client address ("*" = any).
    uvicorn.run(app, host="0.0.0.0", port=PORT, proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
                log_config=None)
    error = "uvicorn exited (startup failed?)"
except BaseException:  # noqa: BLE001 - reported below
    error = traceback.format_exc()

print(error, flush=True)
if os.environ.get("HEDES_DIAGNOSE") != "1":
    sys.exit(1)

from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

report = (f"H.A.D.E.S failed to start\npython {sys.version} on {platform.machine()} {platform.platform()}\ncwd {os.getcwd()}\n\n"
          f"{error}\n--- log ---\n{chr(10).join(tail.lines)[-20000:]}")
for name, value in os.environ.items():  # mask every secret-looking setting, not only the database URL and the keys
    if len(value) >= 8 and re.search(r"KEY|SECRET|TOKEN|PASSWORD|DATABASE_URL", name):
        report = report.replace(value, f"<{name}>")


class Report(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = report.encode()
        self.send_response(500)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("0.0.0.0", PORT), Report).serve_forever()
