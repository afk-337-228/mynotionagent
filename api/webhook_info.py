"""
Vercel: GET /api/webhook_info?secret=... — returns Telegram getWebhookInfo (to verify webhook URL).
"""
import json
import os
import sys
import urllib.request

if os.path.dirname(os.path.dirname(os.path.abspath(__file__))) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        secret = os.getenv("WEBHOOK_SECRET", "").strip()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        got = (qs.get("secret") or [""])[0].strip()
        if not secret or secret != got:
            self.send_response(403)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"Invalid or missing secret"}')
            return
        if not token:
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"TELEGRAM_BOT_TOKEN not set"}')
            return
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/getWebhookInfo",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, ensure_ascii=False).encode())
