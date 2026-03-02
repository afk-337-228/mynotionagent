"""
Vercel serverless: receive Telegram webhook POST, process update, return 200.
"""
import asyncio
import json
import logging
import os
import sys

# Ensure project root is on path (Vercel runs from project root)
if os.path.dirname(os.path.dirname(os.path.abspath(__file__))) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def _process_update_sync(body: bytes) -> None:
    from telegram import Update
    from bot.main import build_application
    app = build_application()
    data = json.loads(body.decode("utf-8"))
    update = Update.de_json(data, app.bot)
    asyncio.run(app.process_update(update))


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b""
            if body:
                _process_update_sync(body)
        except Exception as e:
            logger.exception("Webhook error: %s", e)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Notion Telegram Bot webhook endpoint. Use POST.")

    def log_message(self, format, *args):
        logger.debug("%s - %s", self.address_string(), format % args)
