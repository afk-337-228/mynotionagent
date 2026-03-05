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

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
)
logger = logging.getLogger(__name__)


def _sender_id_from_update(data: dict) -> int | None:
    """Extract sender user id from Telegram update (message, callback_query, etc.)."""
    for key in ("message", "edited_message", "callback_query", "channel_post"):
        obj = data.get(key)
        if not obj:
            continue
        from_obj = obj.get("from") if isinstance(obj, dict) else None
        if from_obj and isinstance(from_obj.get("id"), (int, float)):
            return int(from_obj["id"])
    return None


def _process_update_sync(data: dict) -> None:
    try:
        allowed_id_str = os.getenv("TELEGRAM_USER_ID", "").strip()
        allowed_id = int(allowed_id_str) if allowed_id_str else None
    except ValueError:
        allowed_id = None
    sender_id = _sender_id_from_update(data)
    update_id = data.get("update_id", "?")
    if allowed_id is None or sender_id is None or sender_id != allowed_id:
        logger.info("Ignoring update_id=%s from user_id=%s (allowed_id=%s)", update_id, sender_id, allowed_id)
        print(f"[webhook] Ignoring update_id={update_id} from user_id={sender_id} (allowed={allowed_id})", flush=True)
        return
    logger.info("Processing update_id=%s from user_id=%s", update_id, sender_id)
    print(f"[webhook] Processing update_id={update_id} user_id={sender_id}", flush=True)
    try:
        from telegram import Update
        from bot.main import build_application
        app = build_application()

        async def _run() -> None:
            await app.initialize()
            update = Update.de_json(data, app.bot)
            await app.process_update(update)
            await app.shutdown()

        asyncio.run(_run())
        logger.info("Update %s processed OK", update_id)
    except Exception as e:
        logger.exception("Process update_id=%s failed: %s", update_id, e)
        raise


class handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.end_headers()

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b""
            logger.info("Webhook POST body_size=%s", len(body))
            print(f"[webhook] POST body_size={len(body)}", flush=True)
            if body:
                data = json.loads(body.decode("utf-8"))
                _process_update_sync(data)
        except Exception as e:
            logger.exception("Webhook error: %s", e)
            print(f"[webhook] ERROR: {e}", flush=True)
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
