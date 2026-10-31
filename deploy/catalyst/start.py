"""Catalyst AppSail entry point: run H.A.D.E.S on the port AppSail assigns.

Dependencies are bundled in ./deps (built for Linux). With HEDES_DIAGNOSE=1, a failed start serves the error on the
port instead of exiting, so it can be read without console access (turn it off once the app runs).
"""
import io
import logging
import os
import platform
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ROOT, "deps"), os.path.join(ROOT, "backend")]
os.chdir(os.path.join(ROOT, "backend"))
PORT = int(os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT", "9000"))

captured = io.StringIO()
logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout), logging.StreamHandler(captured)])

error = ""
try:
    import uvicorn

    from app.main import app
    uvicorn.run(app, host="0.0.0.0", port=PORT, proxy_headers=True, forwarded_allow_ips="*")
    error = "uvicorn exited (startup failed?)"
except BaseException:  # noqa: BLE001 - reported below
    error = traceback.format_exc()

print(error, flush=True)
if os.environ.get("HEDES_DIAGNOSE") != "1":
    sys.exit(1)

from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

report = (f"H.A.D.E.S failed to start\npython {sys.version} on {platform.machine()} {platform.platform()}\ncwd {os.getcwd()}\n\n"
          f"{error}\n--- log ---\n{captured.getvalue()[-20000:]}")
for secret in ("DATABASE_URL", "JWT_SECRET", "SECRET_ENCRYPTION_KEY"):
    if os.environ.get(secret):
        report = report.replace(os.environ[secret], f"<{secret}>")


class Report(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = report.encode()
        self.send_response(500)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("0.0.0.0", PORT), Report).serve_forever()
