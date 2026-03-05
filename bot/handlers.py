"""
Telegram message handlers: commands, create/delete/edit note, move, voice.

Access control: бот отвечает ИСКЛЮЧИТЕЛЬНО пользователю с TELEGRAM_USER_ID.
Rate limit: 30 запросов/мин. Память диалога не используется — один запрос к модели на сообщение
(дешево). Состояние: последние заметки (для «удали/измени последнюю») и выбор категории (pending).
"""
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
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

# Hints that message is NOT a simple "create note" and should go through full intent parsing.
_INTENT_HINTS = (
    "удали", "удалить", "убери", "убрать", "отмени", "отменить",
    "перенеси", "перенести", "перемести", "переместить",
    "найди", "найти", "поиск", "ищи", "искать", "покажи",
    "исправ", "исправь", "замени", "заменить", "поменяй", "поменять",
    "измен", "редакт", "обнови", "обновить",
    "выполнено", "сделано", "готово", "done",
    "закрой", "закрыть", "заверши", "завершить", "отметь", "отметить",
)
_DATE_HINTS = (
    "сегодня", "завтра", "послезавтра",
    "понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскрес",
    "today", "tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

_TASK_HINTS = (
    "купить", "сделать", "сделай", "позвонить", "позвони", "написать", "напиши",
    "отправить", "отправь", "оплатить", "оплати", "заплатить", "заплати",
    "заказать", "закажи", "проверить", "проверь", "забрать", "сдать",
    "созвон", "встреч", "записаться", "запишись", "сходить", "съездить",
    "куплю", "заказ", "покупк",
)


def _should_use_intent_llm(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _INTENT_HINTS) or any(h in t for h in _DATE_HINTS)


def _heuristic_route(text: str) -> tuple[str, str | None] | None:
    """Return (category, url_or_none) if simple heuristics match, else None."""
    s = (text or "").strip()
    if not s:
        return None
    lower = s.lower()

    url = extract_url_from_text(s)
    if url:
        u = url.lower()
        if "github.com" in u or "gitlab.com" in u:
            return ("Гитхаб репы", url)
        if "youtube.com" in u or "youtu.be" in u:
            return ("YouTube / Видео", url)
        if "t.me/" in u or "telegram.me/" in u:
            return ("Тг посты", url)
        return ("Ссылки / Статьи", url)

    if "github.com" in lower or "gitlab.com" in lower:
        return ("Гитхаб репы", None)
    if "youtube.com" in lower or "youtu.be" in lower:
        return ("YouTube / Видео", None)
    if "t.me/" in lower or "telegram.me/" in lower:
        return ("Тг посты", None)
    if "http://" in lower or "https://" in lower or "www." in lower:
        return ("Ссылки / Статьи", None)

    if any(h in lower for h in _TASK_HINTS):
        return ("Задачи на сегодня/завтра", None)

    return None

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
    # В Name — короткая подпись для строки таблицы; полный текст — в Notes (доп. свойство)
    short_title = (text[:40] + "…") if len(text) > 40 else (text.strip() or "Заметка")
    if not short_title.strip():
        short_title = "Заметка"
    page = NOTION.create_page(
        category,
        title=short_title,
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
        short_title,
        page["database_id"],
        page["database_title"],
    )
    link = page.get("url") or ""
    if category == "Задачи на сегодня/завтра":
        due = due_date or "сегодня"
        msg = (
            "✅ Записано!\n\n"
            f"📁 Категория: {category}\n"
            f"📝 Задача: {short_title}\n"
            f"📅 Срок: {due}\n"
            f"🏷 Статус: {status or 'Не начата'}\n"
            f"🔗 Открыть в Notion: {link}"
        )
    else:
        today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
        msg = (
            "✅ Записано!\n\n"
            f"📁 Категория: {category}\n"
            f"📝 Заметка: «{short_title}»\n"
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
    "запиши в [категория]: текст", "добавь в задачи: ...", "в крипту: текст", "[спорт]: текст"
    Returns (category, rest) or None.
    """
    text = text.strip()
    lower = text.lower()
    # "[категория]: текст"
    if text.startswith("[") and "]:" in text:
        bracket, _, rest = text.partition("]:")
        cat_part = bracket[1:].strip()
        note = rest.strip()
        cat = normalize_category(cat_part)
        if cat and note:
            return (cat, note)
    # Префиксы: запиши в, добавь в, сохрани в, внеси в, в [категория]
    for prefix in ("запиши в ", "добавь в ", "сохрани в ", "внеси в ", "в "):
        if lower.startswith(prefix):
            rest = text[len(prefix):].strip()
            if ":" in rest:
                cat_part, _, note = rest.partition(":")
                cat = normalize_category(cat_part.strip())
                if cat and note.strip():
                    return (cat, note.strip())
            else:
                # "в задачи купить молоко" или "добавь в крипту ТОН ДНС"
                for cat_name in CATEGORIES:
                    if rest.lower().startswith(cat_name.lower() + " ") or rest.lower() == cat_name.lower():
                        note = rest[len(cat_name):].strip()
                        if note:
                            return (cat_name, note)
                cat = normalize_category(rest.split()[0] if rest.split() else rest)
                if cat and len(rest.split()) > 1:
                    note = rest.split(maxsplit=1)[1]
                    if note:
                        return (cat, note)
            break
    # "задачи: купить молоко" или "крипта: ТОН ДНС"
    if ":" in text:
        head, _, note = text.partition(":")
        cat = normalize_category(head.strip())
        if cat and note.strip():
            return (cat, note.strip())
    return None


def _parse_move_command(text: str) -> tuple[str | None, str | None] | None:
    """
    "перенеси последнюю заметку в [категория]", "перекинь в [категория]", "перемести X в Y"
    Returns (fragment_or_none, category) or None.
    """
    text = text.strip()
    lower = text.lower()
    if lower.startswith("перенеси последнюю заметку в "):
        cat = normalize_category(text[30:].strip())
        if cat:
            return (None, cat)
    if lower.startswith("перенеси последнюю в "):
        cat = normalize_category(text[21:].strip())
        if cat:
            return (None, cat)
    if lower.startswith("перекинь последнюю в ") or lower.startswith("перекинь в "):
        rest = text[21:].strip() if lower.startswith("перекинь последнюю в ") else text[11:].strip()
        cat = normalize_category(rest)
        if cat:
            return (None, cat)
    if (lower.startswith("перемести ") or lower.startswith("перенеси ")) and " в " in lower:
        rest = text[10:] if lower.startswith("перемести ") else text[9:]
        if " в " in rest:
            fragment, _, cat_part = rest.rpartition(" в ")
            cat = normalize_category(cat_part.strip())
            if cat and fragment.strip():
                return (fragment.strip(), cat)
    return None


def _parse_delete_command(text: str) -> str | tuple[str, str] | None:
    """Удалить: последнюю, заметку про X, её, из [категория] удали. Returns "last", fragment, ("last_in_category", cat), or None."""
    # Убираем вводные "а ", "и " чтобы распознать "а из ссылки удали ее"
    s = text.strip()
    for prefix in ("а ", "и "):
        if s.lower().startswith(prefix):
            s = s[len(prefix):].strip()
            break
    t = s.lower()
    # "удали ее", "удали её", "убери её"
    if t in ("удали ее", "удали её", "убери ее", "убери её", "удали последнюю", "удали последнюю заметку", "убери последнюю", "отмени последнюю"):
        return "last"
    for prefix in ("удали последнюю", "удалить последнюю", "убери последнюю", "отмени последнюю"):
        if t.startswith(prefix) and (len(t) == len(prefix) or t[len(prefix):len(prefix)+1] in " \n"):
            return "last"
    # "из [категория] удали", "из ссылки/статьи удали ее"
    if t.startswith("из ") and " удали" in t:
        rest = s[3:].strip()
        idx = rest.lower().find(" удали")
        if idx != -1:
            cat_part = rest[:idx].strip()
            cat = normalize_category(cat_part)
            if not cat and cat_part:
                cat = normalize_category(cat_part.split("/")[0].strip()) or normalize_category(cat_part.split()[0])
            if cat:
                return ("last_in_category", cat)
    # "удали из [категория]", "убери из крипты"
    if (t.startswith("удали из ") or t.startswith("убери из ")) and len(t) > 9:
        prefix = "удали из " if t.startswith("удали из ") else "убери из "
        cat_part = s[len(prefix):].strip()
        cat = normalize_category(cat_part)
        if not cat and cat_part:
            cat = normalize_category(cat_part.split()[0])
        if cat:
            return ("last_in_category", cat)
    # "удали заметку про X", "убери заметку X"
    for prefix in ("удали заметку", "убери заметку"):
        if t.startswith(prefix) and len(t) > len(prefix):
            rest = s[len(prefix):].strip()
            if rest.lower().startswith("про "):
                rest = rest[4:].strip()
            return rest or "last"
    return None


def _parse_edit_command(text: str) -> tuple[str, str] | None:
    """Изменить последнюю на X: измени/исправь/поменяй/замени последнюю на ..."""
    t = text.strip().lower()
    if " последнюю " in t and " на " in t:
        for prefix in (
            "измени последнюю на ", "изменить последнюю на ", "исправь последнюю на ",
            "поменяй последнюю на ", "замени последнюю на ", "правка последней на ",
        ):
            if t.startswith(prefix):
                new_text = text.strip()[len(prefix):].strip()
                if new_text:
                    return ("last", new_text)
    return None


def _resolve_due_date_from_intent(intent: dict, category: str) -> str | None:
    """Convert intent['due_date_relative'] to YYYY-MM-DD for tasks. Returns None if not tasks or no hint."""
    if category != "Задачи на сегодня/завтра":
        return None
    rel = intent.get("due_date_relative")
    if not rel or not isinstance(rel, str):
        return None
    rel = rel.strip().lower()
    if not rel or rel in ("null", "none"):
        return None
    today = datetime.now(timezone.utc).date()
    if rel == "today":
        return today.isoformat()
    if rel == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if rel == "day_after_tomorrow":
        return (today + timedelta(days=2)).isoformat()
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    if rel in weekdays:
        # 0=Monday .. 6=Sunday
        target_wd = weekdays.index(rel)
        current_wd = today.weekday()  # 0=Monday
        days_ahead = (target_wd - current_wd) % 7
        if days_ahead == 0:
            days_ahead = 7
        return (today + timedelta(days=days_ahead)).isoformat()
    return None


def _parse_search_command(text: str) -> str | None:
    """Поиск: найди заметки про X, поищи про X, ищи про X, поиск X."""
    t = text.strip().lower()
    for prefix, length in (
        ("найди заметки про ", 18),
        ("найди всё про ", 14),
        ("найди про ", 10),
        ("поищи заметки про ", 18),
        ("поищи про ", 10),
        ("ищи про ", 8),
        ("ищи заметки про ", 16),
        ("поиск ", 6),
        ("найди ", 6),
    ):
        if t.startswith(prefix) and len(t) > length:
            return text.strip()[length:].strip()
    if t.startswith("найди ") and len(t) > 6:
        return text.strip()[6:].strip()
    return None


def _parse_done_command(text: str) -> str | None:
    """Выполнено: выполнено, отметь выполненной, закрой последнюю, заверши."""
    t = text.strip().lower()
    if t in ("выполнено", "сделано", "готово", "done", "отмечено"):
        return "last"
    if t.startswith("отметь последнюю как выполненную") or t.startswith("отметить последнюю как выполненную"):
        return "last"
    if t.startswith("пометь последнюю выполненной") or t.startswith("пометь выполненной"):
        return "last"
    if t.startswith("закрой последнюю") or t.startswith("заверши последнюю"):
        return "last"
    if t.startswith("отметь как выполненную") or t.startswith("отметить как выполненную"):
        rest = text.strip()[23:].strip() if t.startswith("отметь как выполненную") else text.strip()[24:].strip()
        if rest:
            return rest
        return "last"
    if t.startswith("выполни ") and len(t) > 8:
        return text.strip()[8:].strip()
    return None


@_require_allowed_and_rate_limit
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Command /start user_id=%s", update.effective_user.id if update.effective_user else 0)
    await update.message.reply_text(
        "Привет! Я бот для заметок в Notion.\n\n"
        "Отправь текст или голосовое — я определю категорию и сохраню в Notion.\n"
        "Или напиши: запиши в [категория]: текст\n\n"
        "Команды: /help, /categories, /last, /today, /init"
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
        "/today — задачи на сегодня\n"
        "/init — создать базы в Notion\n\n"
        "Поиск: найди заметки про [фрагмент] · найди про [фрагмент]\n\n"
        "Записать: просто текст или запиши в [категория]: текст\n\n"
        "Выполнено (отметить и удалить): выполнено · отметь последнюю как выполненную · отметь как выполненную [фрагмент]\n\n"
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
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show tasks due today from 'Задачи на сегодня/завтра'."""
    logger.info("Command /today user_id=%s", update.effective_user.id if update.effective_user else 0)
    if not NOTION:
        await update.message.reply_text("⚠️ Notion не настроен.")
        return
    tasks = NOTION.get_tasks_due_today(limit=15)
    if not tasks:
        await update.message.reply_text("На сегодня задач нет. Добавь: запиши в задачи: [текст] или укажи срок в Notion.")
        return
    lines = ["📅 Задачи на сегодня:"] + [
        f"• {t['title'][:55]}{'…' if len(t['title']) > 55 else ''}\n  {t.get('url') or ''}"
        for t in tasks[:15]
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
        # Принимаем любую категорию по названию (крипта → Крипта), не только из предложенных
        chosen = normalize_category(text)
        if not chosen and options:
            for o in options:
                if o.lower() == text.lower():
                    chosen = o
                    break
    if not chosen:
        await update.message.reply_text(
            "❓ Категория не найдена. Напиши номер из списка, название категории (например крипта, задачи) или /categories."
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
    await _save_note_and_respond(update, context, target_category, title)
    return True


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str | tuple[str, str]) -> bool:
    """Delete note: target is 'last', search fragment, or ('last_in_category', category). Returns True if handled."""
    if not NOTION:
        await update.message.reply_text("⚠️ Notion не настроен.")
        return True
    user_id = update.effective_user.id
    page_id = None
    title = None
    if isinstance(target, tuple):
        # Удалить последнюю заметку в указанной категории
        _, category = target
        pages = NOTION.get_recent_pages_in_category(category, limit=1)
        if not pages:
            await update.message.reply_text(f"В категории «{category}» нет заметок для удаления.")
            return True
        page_id = pages[0]["page_id"]
        title = pages[0]["title"]
    elif (isinstance(target, str) and target.strip().lower() in ("last", "последнюю", "")) or target is None:
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


async def handle_done(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str) -> bool:
    """Mark note as done (set Status) and archive it. target is 'last' or search fragment. Returns True if handled."""
    if not NOTION:
        await update.message.reply_text("⚠️ Notion не настроен.")
        return True
    user_id = update.effective_user.id
    category = None
    if target.strip().lower() in ("last", "последнюю", ""):
        last = get_last_notes(user_id)
        if not last:
            await update.message.reply_text("Нет последней заметки для отметки как выполненную.")
            return True
        page_id = last[0]["page_id"]
        title = last[0]["title"]
        category = last[0].get("database_title")
    else:
        found = NOTION.find_page_by_title_fragment(target.strip())
        if not found:
            await update.message.reply_text("Заметка с таким текстом не найдена.")
            return True
        page_id = found["page_id"]
        title = found["title"]
        category = found.get("database_title")
    if not NOTION.mark_done_and_archive(page_id, category=category):
        await update.message.reply_text("⚠️ Не удалось отметить и удалить в Notion.\n\n" + NOTION_CONNECTION_HINT)
        return True
    remove_last_note_by_page_id(user_id, page_id)
    preview = (title[:60] + "…") if len(title) > 60 else title
    await update.message.reply_text(f"✅ Выполнено и удалено из списка: «{preview}»")
    return True


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> bool:
    """Search notes in Notion and reply with list of links. Returns True if handled."""
    if not NOTION:
        await update.message.reply_text("⚠️ Notion не настроен.")
        return True
    q = query.strip()
    if not q:
        await update.message.reply_text("Напиши, что искать: найди заметки про [фрагмент]")
        return True
    results = NOTION.search_pages(q, limit=10)
    if not results:
        await update.message.reply_text(f"По запросу «{q[:50]}» ничего не найдено.")
        return True
    lines = [f"🔍 Найдено по «{q[:40]}{'…' if len(q) > 40 else ''}»:"] + [
        f"• {r['title'][:50]}{'…' if len(r['title']) > 50 else ''} — {r.get('database_title', '')}\n  {r.get('url') or ''}"
        for r in results
    ]
    await update.message.reply_text("\n".join(lines))
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

    search_query = _parse_search_command(text)
    if search_query is not None:
        await handle_search(update, context, search_query)
        return

    explicit = _parse_explicit_category(text)
    if explicit:
        category, note_text = explicit
        url = extract_url_from_text(note_text) if category in ("Ссылки / Статьи", "Полезные сайты", "Гитхаб репы") else None
        await _save_note_and_respond(update, context, category, note_text, url=url)
        return

    done_target = _parse_done_command(text)
    if done_target is not None:
        await handle_done(update, context, done_target)
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

    if not _should_use_intent_llm(text):
        heur = _heuristic_route(text)
        if heur:
            category, url = heur
            await _save_note_and_respond(update, context, category, text, url=url)
            return

    if not _should_use_intent_llm(text):
        cls = classify(
            text,
            api_key=OPENROUTER_API_KEY,
            base_url=OPENROUTER_BASE_URL,
        )
        if cls and cls.get("category"):
            category = cls.get("category")
            confidence = cls.get("confidence", 0.7)
            note_text = text
            if confidence < CONFIDENCE_THRESHOLD:
                opts = [category] + [c for c in CATEGORIES if c != category][:2]
                set_pending_category(update.effective_user.id, note_text, opts)
                await update.message.reply_text(
                    "рџ¤” РќРµ СѓРІРµСЂРµРЅ. Р’Р°СЂРёР°РЅС‚С‹:\n"
                    + "\n".join(f"{i+1}. {o}" for i, o in enumerate(opts))
                    + "\nРќР°РїРёС€Рё РЅРѕРјРµСЂ РёР»Рё РЅР°Р·РІР°РЅРёРµ."
                )
                return
            url = extract_url_from_text(note_text) if category in ("РЎСЃС‹Р»РєРё / РЎС‚Р°С‚СЊРё", "РџРѕР»РµР·РЅС‹Рµ СЃР°Р№С‚С‹", "Р“РёС‚С…Р°Р± СЂРµРїС‹") else None
            await _save_note_and_respond(update, context, category, note_text, url=url)
            return

    intent = understand_message(
        text,
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
    )
    if not intent:
        await update.message.reply_text(
            "⚠️ ИИ временно недоступен. Запиши явно: запиши в [категория]: текст. "
            "Или: удали последнюю, измени последнюю на [текст], найди заметки про [фрагмент]. Команды: /help"
        )
        return

    action = intent.get("action", "create")
    if action == "done":
        await handle_done(update, context, intent.get("done_target") or "last")
        return
    if action == "search":
        await handle_search(update, context, intent.get("search_query") or text.strip())
        return
    if action == "delete":
        if intent.get("delete_category"):
            await handle_delete(update, context, ("last_in_category", intent["delete_category"]))
        else:
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
    url = extract_url_from_text(note_text) if category in ("Ссылки / Статьи", "Полезные сайты", "Гитхаб репы") else None
    due_date = _resolve_due_date_from_intent(intent, category)
    await _save_note_and_respond(update, context, category, note_text, url=url, due_date=due_date)


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
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("init", cmd_init))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
