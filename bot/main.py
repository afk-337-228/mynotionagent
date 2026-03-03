"""
Entry point: load config, create Notion client, setup Telegram handlers.
Supports polling (local/Docker) and webhook (Vercel).
"""
import logging
import os
import sys

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes

from bot.handlers import setup_handlers
from bot.notion_client import NotionClient

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
logging.basicConfig(
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT,
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger(__name__)

_app: Application | None = None


def build_application() -> Application:
    """Build and return configured Application (idempotent per process)."""
    global _app
    if _app is not None:
        return _app
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    user_id_str = os.getenv("TELEGRAM_USER_ID")
    notion_key = os.getenv("NOTION_API_KEY")
    notion_parent = os.getenv("NOTION_PARENT_PAGE_ID")
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    openrouter_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    try:
        allowed_user_id = int(user_id_str or "0")
    except ValueError:
        allowed_user_id = 0
    if not allowed_user_id:
        raise ValueError("TELEGRAM_USER_ID is required")
    if not notion_key or not notion_parent:
        raise ValueError("NOTION_API_KEY and NOTION_PARENT_PAGE_ID are required")
    parent_clean = notion_parent.strip().replace("-", "")
    if len(parent_clean) != 32 or not all(c in "0123456789abcdefABCDEF" for c in parent_clean):
        raise ValueError("NOTION_PARENT_PAGE_ID must be a 32-char hex UUID (with or without dashes)")
    if not openrouter_key:
        raise ValueError("OPENROUTER_API_KEY is required")
    notion = NotionClient(api_key=notion_key, parent_page_id=notion_parent.strip())
    application = Application.builder().token(token).build()
    setup_handlers(
        application,
        allowed_user_id=allowed_user_id,
        notion=notion,
        openrouter_api_key=openrouter_key,
        openrouter_base_url=openrouter_url,
    )
    _app = application
    logger.info(
        "Application built: allowed_user_id=%s, notion_parent=%s...",
        allowed_user_id,
        notion_parent.strip()[:8] + "..." if len(notion_parent.strip()) > 8 else notion_parent.strip(),
    )
    return application


def main() -> None:
    """Run bot in polling mode (local or Docker)."""
    app = build_application()
    logger.info("Bot starting (polling), log_level=%s", LOG_LEVEL)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logger.info("Bot stopped (Ctrl+C)")
        sys.exit(0)


if __name__ == "__main__":
    main()
