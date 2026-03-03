"""
Telegram message handlers: commands, create/delete/edit note, move, voice.

Access control: бот отвечает ИСКЛЮЧИТЕЛЬНО пользователю с TELEGRAM_USER_ID.
Rate limit: 30 запросов/мин. Память диалога не используется — один запрос к модели на сообщение
(дешево). Состояние: последние заметки (для «удали/измени последнюю») и выбор категории (pending).
"""
import logging
import os
import tempfile
from datetime import datetime
from functools import wraps
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from bot.classifier import classify, understand_message
from bot.notion_client import (
    CATEGORIES,
    NotionClient,
    normalize_category,
    extract_url_from_text,
)
from bot.state import (
    check_rate_limit,
    get_pending_category,
    set_pending_category,
    clear_pending_category,
    get_last_notes,
    append_last_note,
    remove_last_note_by_page_id,
)
from bot.voice_handler import transcribe_file

logger = logging.getLogger(__name__)

# Injected by main
ALLOWED_USER_ID: int = 0
NOTION: NotionClient | None = None
OPENROUTER_API_KEY: str = ""
OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
CONFIDENCE_THRESHOLD = 0.6

# Подсказка при 404 / block not found: «Connections» в меню «…», не в Share
NOTION_CONNECTION_HINT = (
    "Если ошибка из-за доступа: открой страницу в Notion → нажми «…» (три точки) вверху → "
    "прокрути меню вниз до «Connections» → «Add connections» → выбери интеграцию (ключ из NOTION_API_KEY)."
)


def _allowed(update: Update) -> bool:
    """Единственный разрешённый пользователь — по TELEGRAM_USER_ID. Остальным не отвечаем."""
    if not update.effective_user:
        return False
    return int(update.effective_user.id) == ALLOWED_USER_ID


def _require_allowed_and_rate_limit(handler):
    """Decorator: skip if not allowed user; if rate limit exceeded, reply and skip."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _allowed(update):
            return
        user_id = update.effective_user.id if update.effective_user else 0
        if not check_rate_limit(user_id):
            logger.warning("Rate limit exceeded: user_id=%s", user_id)
            if update.message:
                await update.message.reply_text(
                    "⚠️ Слишком много запросов. Подожди около минуты."
                )
            return
        return await handler(update, context)

    return wrapper


async def _save_note_and_respond(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    category: str,
    text: str,
    *,
    url: str | None = None,
    author: str | None = None,
    status: str | None = None,
    due_date: str | None = None,
) -> bool:
    if not NOTION:
        await update.message.reply_text("⚠️ Notion не настроен.")
        return False
    title = (text[:200] + "…") if len(text) > 200 else text
    if not title.strip():
        title = "Заметка"
    page = NOTION.create_page(
        category,
        title=title,
        notes=text,
        url=url,
        author=author,
        status=status,
        due_date=due_date,
    )
    if not page:
        await update.message.reply_text(
            "⚠️ Не удалось подключиться к Notion. Попробуй позже.\n\n" + NOTION_CONNECTION_HINT
        )
        return False
    user_id = update.effective_user.id if update.effective_user else 0
    append_last_note(
        user_id,
        page["id"],
        title,
        page["database_id"],
        page["database_title"],
    )
    link = page.get("url") or ""
    if category == "Задачи на сегодня/завтра":
        due = due_date or "сегодня"
        msg = (
            "✅ Записано!\n\n"
            f"📁 Категория: {category}\n"
            f"📝 Задача: {title}\n"
            f"📅 Срок: {due}\n"
            f"🏷 Статус: {status or 'Не начата'}\n"
            f"🔗 Открыть в Notion: {link}"
        )
    else:
        today = datetime.utcnow().strftime("%d.%m.%Y")
        msg = (
            "✅ Записано!\n\n"
            f"📁 Категория: {category}\n"
            f"📝 Заметка: «{title}»\n"
            f"📅 Дата: {today}\n"
            f"🔗 Открыть в Notion: {link}"
        )
    logger.info(
        "Note saved: category=%s user_id=%s page_id=%s",
        category, user_id, page["id"][:8] + "..." if len(page["id"]) > 8 else page["id"],
    )
    await update.message.reply_text(msg)
    return True


def _parse_explicit_category(text: str) -> tuple[str | None, str] | None:
    """
    "запиши в [категория]: текст", "в крипту: текст" или "[спорт]: текст"
    Returns (category, rest) or None.
    """
    text = text.strip()
    # "[категория]: текст"
    if text.startswith("[") and "]:" in text:
        bracket, _, rest = text.partition("]:")
        cat_part = bracket[1:].strip()
        note = rest.strip()
        cat = normalize_category(cat_part)
        if cat and note:
            return (cat, note)
    for prefix in ("запиши в ", "в "):
        if text.lower().startswith(prefix):
            rest = text[len(prefix):].strip()
            if ":" in rest:
                cat_part, _, note = rest.partition(":")
                cat = normalize_category(cat_part.strip())
                if cat and note.strip():
                    return (cat, note.strip())
            break
    return None


def _parse_move_command(text: str) -> tuple[str | None, str | None] | None:
    """
    "перенеси последнюю заметку в [категория]" -> (None, category)
    "перемести [фрагмент] в [категория]" -> (fragment, category)
    Returns (fragment_or_none, category) or None.
    """
    text = text.strip()
    lower = text.lower()
    if lower.startswith("перенеси последнюю заметку в "):
        cat = normalize_category(text[30:].strip())
        if cat:
            return (None, cat)
    if lower.startswith("перемести ") and " в " in lower:
        rest = text[10:]
        if " в " in rest:
            fragment, _, cat_part = rest.rpartition(" в ")
            cat = normalize_category(cat_part.strip())
            if cat and fragment.strip():
                return (fragment.strip(), cat)
    return None


def _parse_delete_command(text: str) -> str | None:
    """"удали последнюю [заметку]", "удали заметку про X" -> "last" or fragment. None if not delete."""
    t = text.strip().lower()
    if t.startswith("удали последнюю") or t in ("удали последнюю", "удали последнюю заметку"):
        return "last"
    if t.startswith("удали заметку") and len(t) > 14:
        rest = text.strip()[14:].strip()  # "про X" or "X"
        if rest.lower().startswith("про "):
            rest = rest[4:].strip()
        return rest or "last"
    if t.startswith("удалить последнюю"):
        return "last"
    return None


def _parse_edit_command(text: str) -> tuple[str, str] | None:
    """"измени последнюю на X", "исправь последнюю на X" -> ("last", "X"). None if not edit."""
    t = text.strip().lower()
    if " последнюю " in t and " на " in t:
        for prefix in ("измени последнюю на ", "изменить последнюю на ", "исправь последнюю на "):
            if t.startswith(prefix):
                new_text = text.strip()[len(prefix):].strip()
                if new_text:
                    return ("last", new_text)
    return None


@_require_allowed_and_rate_limit
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Command /start user_id=%s", update.effective_user.id if update.effective_user else 0)
    await update.message.reply_text(
        "Привет! Я бот для заметок в Notion.\n\n"
        "Отправь текст или голосовое — я определю категорию и сохраню в Notion.\n"
        "Или напиши: запиши в [категория]: текст\n\n"
        "Команды: /help, /categories, /last, /init"
    )


@_require_allowed_and_rate_limit
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Command /help user_id=%s", update.effective_user.id if update.effective_user else 0)
    await update.message.reply_text(
        "Команды:\n"
        "/start — приветствие\n"
        "/help — эта справка\n"
        "/categories — список категорий\n"
        "/last — последние 5 заметок\n"
        "/init — создать базы в Notion\n\n"
        "Записать: просто текст или запиши в [категория]: текст\n\n"
        "Удалить: удали последнюю · удали заметку про [фрагмент]\n\n"
        "Изменить: измени последнюю на [новый текст]\n\n"
        "Перенос: перенеси последнюю заметку в [категория] · перемести [фрагмент] в [категория]"
    )


@_require_allowed_and_rate_limit
async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Command /categories user_id=%s", update.effective_user.id if update.effective_user else 0)
    lines = ["Категории Notion:"] + [f"• {c}" for c in CATEGORIES]
    await update.message.reply_text("\n".join(lines))


@_require_allowed_and_rate_limit
async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Command /last user_id=%s", update.effective_user.id if update.effective_user else 0)
    if not NOTION:
        await update.message.reply_text("⚠️ Notion не настроен.")
        return
    user_id = update.effective_user.id
    last = get_last_notes(user_id)
    if not last:
        # Fallback: query Notion for recent
        pages = NOTION.get_recent_pages(limit=5)
        if not pages:
            await update.message.reply_text("Пока нет добавленных заметок.")
            return
        for p in pages:
            append_last_note(user_id, p["page_id"], p["title"], p["database_id"], p["database_title"])
        last = get_last_notes(user_id)
    lines = ["Последние заметки:"] + [
        f"• {n['title'][:60]} — {n['database_title']}" for n in last[:5]
    ]
    await update.message.reply_text("\n".join(lines))


@_require_allowed_and_rate_limit
async def cmd_init(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else 0
    logger.info("Command /init user_id=%s", user_id)
    if not NOTION:
        await update.message.reply_text("⚠️ Notion не настроен.")
        return
    NOTION.init_databases()
    await update.message.reply_text("✅ Базы Notion проверены/созданы.")


async def handle_pending_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If user was asked to choose category, parse reply and save. Returns True if handled."""
    if not _allowed(update) or not update.message or not update.message.text:
        return False
    user_id = update.effective_user.id
    pending = get_pending_category(user_id)
    if not pending:
        return False
    text = update.message.text.strip()
    options = pending.get("options") or []
    note_text = pending.get("text") or ""
    chosen = None
    if text.isdigit() and 1 <= int(text) <= len(options):
        chosen = options[int(text) - 1]
    else:
        chosen = normalize_category(text)
        if chosen and chosen not in options:
            chosen = None
        if not chosen and options:
            for o in options:
                if o.lower() == text.lower():
                    chosen = o
                    break
    if not chosen:
        await update.message.reply_text(
            "❓ Категория не найдена. Напиши номер или название из списка выше, или /categories."
        )
        return True
    clear_pending_category(user_id)
    await _save_note_and_respond(update, context, chosen, note_text)
    return True


async def handle_move(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not _allowed(update) or not NOTION:
        return False
    parsed = _parse_move_command(update.message.text or "")
    if not parsed:
        return False
    fragment, target_category = parsed
    user_id = update.effective_user.id
    if fragment is None:
        last = get_last_notes(user_id)
        if not last:
            await update.message.reply_text("Нет последней заметки для переноса. Сначала добавь заметку.")
            return True
        page_id = last[0]["page_id"]
        title = last[0]["title"]
        db_title = last[0]["database_title"]
    else:
        found = NOTION.find_page_by_title_fragment(fragment)
        if not found:
            await update.message.reply_text("Заметка с таким текстом не найдена.")
            return True
        page_id = found["page_id"]
        title = found["title"]
        db_title = found["database_title"]
    if not NOTION.archive_page(page_id):
        await update.message.reply_text(
            "⚠️ Не удалось удалить запись в Notion.\n\n" + NOTION_CONNECTION_HINT
        )
        return True
    remove_last_note_by_page_id(user_id, page_id)
    logger.info(
        "Move: user_id=%s page_id=%s target=%s title_preview=%s",
        user_id, page_id[:8] + "..." if len(page_id) > 8 else page_id,
        target_category, (title[:40] + "…") if len(title) > 40 else title,
    )
    await _save_note_and_respond(update, context, target_category, title, notes=title)
    return True


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str) -> bool:
    """Delete note: target is 'last' or search fragment. Returns True if handled."""
    if not NOTION:
        await update.message.reply_text("⚠️ Notion не настроен.")
        return True
    user_id = update.effective_user.id
    if target.strip().lower() in ("last", "последнюю", ""):
        last = get_last_notes(user_id)
        if not last:
            await update.message.reply_text("Нет последней заметки для удаления.")
            return True
        page_id = last[0]["page_id"]
        title = last[0]["title"]
    else:
        found = NOTION.find_page_by_title_fragment(target.strip())
        if not found:
            await update.message.reply_text("Заметка с таким текстом не найдена.")
            return True
        page_id = found["page_id"]
        title = found["title"]
    if not NOTION.archive_page(page_id):
        await update.message.reply_text("⚠️ Не удалось удалить в Notion.\n\n" + NOTION_CONNECTION_HINT)
        return True
    remove_last_note_by_page_id(user_id, page_id)
    await update.message.reply_text(f"🗑 Удалил: «{title[:60]}{'…' if len(title) > 60 else ''}»")
    return True


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str, new_title: str, new_notes: str = "") -> bool:
    """Edit note: target is 'last' or fragment; new_title/new_notes — new content. Returns True if handled."""
    if not NOTION:
        await update.message.reply_text("⚠️ Notion не настроен.")
        return True
    user_id = update.effective_user.id
    if target.strip().lower() in ("last", "последнюю", ""):
        last = get_last_notes(user_id)
        if not last:
            await update.message.reply_text("Нет последней заметки для редактирования.")
            return True
        page_id = last[0]["page_id"]
        title = last[0]["title"]
    else:
        found = NOTION.find_page_by_title_fragment(target.strip())
        if not found:
            await update.message.reply_text("Заметка не найдена.")
            return True
        page_id = found["page_id"]
        title = found["title"]
    if not new_title and not new_notes:
        await update.message.reply_text("Напиши новый текст: измени последнюю на [текст]")
        return True
    if not NOTION.update_page(page_id, title=new_title or None, notes=new_notes or None):
        await update.message.reply_text("⚠️ Не удалось обновить в Notion.\n\n" + NOTION_CONNECTION_HINT)
        return True
    preview = (new_title or new_notes or title)[:60]
    await update.message.reply_text(f"✏️ Обновил: «{preview}{'…' if len(preview) >= 60 else ''}»")
    return True


async def _process_note_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Обработка: pending → move → explicit → delete/edit (regex) → LLM intent (create/delete/edit). Без памяти диалога."""
    if not text or not text.strip():
        return
    text = text.strip()
    if await handle_pending_reply(update, context):
        return
    if await handle_move(update, context):
        return

    explicit = _parse_explicit_category(text)
    if explicit:
        category, note_text = explicit
        url = extract_url_from_text(note_text) if category in ("Ссылки / Статьи", "Полезные сайты") else None
        await _save_note_and_respond(update, context, category, note_text, url=url)
        return

    delete_target = _parse_delete_command(text)
    if delete_target is not None:
        await handle_delete(update, context, delete_target)
        return

    edit_parsed = _parse_edit_command(text)
    if edit_parsed is not None:
        target, new_text = edit_parsed
        await handle_edit(update, context, target, new_title=new_text)
        return

    intent = understand_message(
        text,
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
    )
    if not intent:
        await update.message.reply_text(
            "⚠️ ИИ временно недоступен. Укажи категорию: запиши в [категория]: текст. Или удали/измени: удали последнюю, измени последнюю на текст."
        )
        return

    action = intent.get("action", "create")
    if action == "delete":
        await handle_delete(update, context, intent.get("delete_target") or "last")
        return
    if action == "edit":
        await handle_edit(
            update, context,
            intent.get("edit_target") or "last",
            new_title=intent.get("edit_new_title") or "",
            new_notes=intent.get("edit_new_notes") or "",
        )
        return

    category = intent.get("category")
    note_text = intent.get("note_text", text)
    confidence = intent.get("confidence", 0.7)
    if not category or category not in CATEGORIES:
        await update.message.reply_text(
            "Не определил категорию. Напиши: запиши в [категория]: текст или /categories."
        )
        return
    if confidence < CONFIDENCE_THRESHOLD:
        opts = [category] + [c for c in CATEGORIES if c != category][:2]
        set_pending_category(update.effective_user.id, note_text, opts)
        await update.message.reply_text(
            "🤔 Не уверен. Варианты:\n" + "\n".join(f"{i+1}. {o}" for i, o in enumerate(opts)) + "\nНапиши номер или название."
        )
        return
    url = extract_url_from_text(note_text) if category in ("Ссылки / Статьи", "Полезные сайты") else None
    await _save_note_and_respond(update, context, category, note_text, url=url)


@_require_allowed_and_rate_limit
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text
    logger.debug("Text message: user_id=%s len=%s preview=%s", update.effective_user.id if update.effective_user else 0, len(text), (text[:60] + "…") if len(text) > 60 else text)
    await _process_note_text(update, context, text)


@_require_allowed_and_rate_limit
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.voice:
        return
    voice = update.message.voice
    try:
        tg_file = await voice.get_file()
        path = tempfile.mktemp(suffix=".ogg")
        await tg_file.download_to_drive(path)
        try:
            text = transcribe_file(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    except Exception as e:
        logger.warning("Voice download failed: user_id=%s error=%s", update.effective_user.id if update.effective_user else 0, e)
        await update.message.reply_text(
            "🎙 Не удалось распознать голос. Попробуй ещё раз или напиши текстом."
        )
        return
    if not text or not text.strip():
        logger.info("Voice empty result: user_id=%s", update.effective_user.id if update.effective_user else 0)
        await update.message.reply_text(
            "🎙 Голос здесь не распознаётся (на сервере нет распознавания). Напиши текстом или запусти бота локально с pip install -r requirements-voice.txt"
        )
        return
    logger.info("Voice transcribed: user_id=%s text_len=%s", update.effective_user.id if update.effective_user else 0, len(text))
    await _process_note_text(update, context, text)


def setup_handlers(
    application: Any,
    *,
    allowed_user_id: int,
    notion: NotionClient,
    openrouter_api_key: str,
    openrouter_base_url: str,
) -> None:
    global ALLOWED_USER_ID, NOTION, OPENROUTER_API_KEY, OPENROUTER_BASE_URL
    ALLOWED_USER_ID = allowed_user_id
    NOTION = notion
    OPENROUTER_API_KEY = openrouter_api_key
    OPENROUTER_BASE_URL = openrouter_base_url or "https://openrouter.ai/api/v1"

    from telegram.ext import CommandHandler, MessageHandler, filters

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("categories", cmd_categories))
    application.add_handler(CommandHandler("last", cmd_last))
    application.add_handler(CommandHandler("init", cmd_init))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
