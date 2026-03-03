"""
Vercel serverless: one-time GET to register Telegram webhook URL.
Call after deploy: https://YOUR_VERCEL_URL/api/set_webhook?secret=YOUR_WEBHOOK_SECRET
"""
import os
import sys

if os.path.dirname(os.path.dirname(os.path.abspath(__file__))) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


def _set_webhook(token: str, url: str) -> bool:
    import json
    import logging
    import urllib.request
    log = logging.getLogger(__name__)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setWebhook",
        data=json.dumps({"url": url}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = r.status == 200
            if ok:
                log.info("setWebhook success: url=%s", url)
            else:
                log.warning("setWebhook unexpected status: url=%s status=%s", url, r.status)
            return ok
    except Exception as e:
        log.warning("setWebhook failed: url=%s error=%s", url, e)
        return False


class handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.end_headers()

    def do_GET(self):
        secret = os.getenv("WEBHOOK_SECRET", "").strip()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        # Prefer PRODUCTION_DOMAIN (set in Vercel) so webhook URL is always correct, no typo from Host
        production_domain = (os.getenv("PRODUCTION_DOMAIN") or "").strip()
        if production_domain:
            host = production_domain
        else:
            host = (self.headers.get("Host") or "").strip()
            if not host:
                host = (os.getenv("VERCEL_PROJECT_PRODUCTION_URL") or os.getenv("VERCEL_URL") or "").strip()
        base = f"https://{host}" if host else ""
        webhook_url = f"{base}/api/webhook" if base else ""

        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        got_secret = (qs.get("secret") or [""])[0].strip()

        if not secret:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"WEBHOOK_SECRET is not set in this project. "
                b"Add it in Vercel: Settings -> Environment Variables."
            )
            return
        if secret != got_secret:
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Invalid secret (value in URL does not match WEBHOOK_SECRET)")
            return
        if not token or not webhook_url:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Missing TELEGRAM_BOT_TOKEN or VERCEL_URL")
            return
        ok = _set_webhook(token, webhook_url)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if ok:
            self.wfile.write(("Webhook set: " + webhook_url).encode("utf-8"))
        else:
            self.wfile.write(b"Webhook set failed")
