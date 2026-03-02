"""
Notion API: create/ensure databases, add pages, query, archive.
Retry with exponential backoff (Notion ~3 req/s).
"""
import logging
import re
import time
from typing import Any

from notion_client import Client
from notion_client.errors import APIResponseError

logger = logging.getLogger(__name__)

# All 18 categories (exact names for Notion)
CATEGORIES = [
    "Книги к прочтению",
    "YouTube / Видео",
    "Идеи для стартапа",
    "Улучшения текущего проекта",
    "Планы долгосрочные (5 лет)",
    "Задачи на сегодня/завтра",
    "Финансы",
    "Ссылки / Статьи",
    "Разное",
    "Полезные сайты",
    "Спорт",
    "Саморазвитие",
    "Учёба",
    "Вайбкодинг",
    "Предпринимательство",
    "Тг посты",
    "Фильмы / Сериалы",
    "Крипта",
]

# Aliases for fuzzy match (user says "книги" -> "Книги к прочтению")
CATEGORY_ALIASES: dict[str, str] = {
    "книги": "Книги к прочтению",
    "книга": "Книги к прочтению",
    "видео": "YouTube / Видео",
    "youtube": "YouTube / Видео",
    "стартап": "Идеи для стартапа",
    "стартапы": "Идеи для стартапа",
    "идеи": "Идеи для стартапа",
    "улучшения": "Улучшения текущего проекта",
    "проект": "Улучшения текущего проекта",
    "планы": "Планы долгосрочные (5 лет)",
    "долгосрочные": "Планы долгосрочные (5 лет)",
    "задачи": "Задачи на сегодня/завтра",
    "сегодня": "Задачи на сегодня/завтра",
    "завтра": "Задачи на сегодня/завтра",
    "финансы": "Финансы",
    "деньги": "Финансы",
    "ссылки": "Ссылки / Статьи",
    "статьи": "Ссылки / Статьи",
    "разное": "Разное",
    "сайты": "Полезные сайты",
    "полезные сайты": "Полезные сайты",
    "спорт": "Спорт",
    "саморазвитие": "Саморазвитие",
    "учёба": "Учёба",
    "учеба": "Учёба",
    "вайбкодинг": "Вайбкодинг",
    "предпринимательство": "Предпринимательство",
    "тг посты": "Тг посты",
    "посты": "Тг посты",
    "телеграм посты": "Тг посты",
    "фильмы": "Фильмы / Сериалы",
    "сериалы": "Фильмы / Сериалы",
    "крипта": "Крипта",
    "крипту": "Крипта",
    "крипто": "Крипта",
}


def _retry(fn, *args, max_retries: int = 5, **kwargs) -> Any:
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except APIResponseError as e:
            if getattr(e, "code", None) == "rate_limited" and attempt < max_retries - 1:
                wait = 2**attempt
                logger.warning("Notion rate limit, retry in %s sec", wait)
                time.sleep(wait)
                continue
            logger.error("Notion API error: %s", e)
            raise
        except Exception:
            raise
    return None


# --- Schema definitions (property name -> Notion type and options) ---

def _prop_title() -> dict:
    return {"title": {}}


def _prop_rich_text() -> dict:
    return {"rich_text": {}}


def _prop_date() -> dict:
    return {"date": {}}


def _prop_select(options: list[str]) -> dict:
    return {"select": {"options": [{"name": o} for o in options]}}


def _prop_url() -> dict:
    return {"url": {}}


# Universal schema: Name (title), Date Added (date), Source (select), Notes (rich_text)
def _universal_properties() -> dict:
    return {
        "Name": _prop_title(),
        "Date Added": _prop_date(),
        "Source": _prop_select(["Telegram"]),
        "Notes": _prop_rich_text(),
    }


# Books
def _books_properties() -> dict:
    return {
        "Name": _prop_title(),
        "Author": _prop_rich_text(),
        "Date Added": _prop_date(),
        "Status": _prop_select(["Хочу прочитать", "Читаю", "Прочитана"]),
        "Notes": _prop_rich_text(),
    }


# Tasks
def _tasks_properties() -> dict:
    return {
        "Name": _prop_title(),
        "Date Added": _prop_date(),
        "Due Date": _prop_date(),
        "Status": _prop_select(["Не начата", "В процессе", "Выполнена"]),
        "Notes": _prop_rich_text(),
    }


# Films
def _films_properties() -> dict:
    return {
        "Name": _prop_title(),
        "Date Added": _prop_date(),
        "Status": _prop_select(["Хочу посмотреть", "Смотрю", "Посмотрел"]),
        "Notes": _prop_rich_text(),
    }


# Links / Articles and Полезные сайты
def _links_properties() -> dict:
    return {
        "Name": _prop_title(),
        "URL": _prop_url(),
        "Date Added": _prop_date(),
        "Notes": _prop_rich_text(),
    }


def _schema_for_category(category: str) -> dict:
    if category == "Книги к прочтению":
        return _books_properties()
    if category == "Задачи на сегодня/завтра":
        return _tasks_properties()
    if category == "Фильмы / Сериалы":
        return _films_properties()
    if category in ("Ссылки / Статьи", "Полезные сайты"):
        return _links_properties()
    return _universal_properties()


def normalize_category(user_input: str) -> str | None:
    """Match user input to exact category name. Fuzzy via aliases and substring."""
    s = user_input.strip().lower()
    if not s:
        return None
    if s in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[s]
    for cat in CATEGORIES:
        if cat.lower() == s or s in cat.lower():
            return cat
    return None


def extract_url_from_text(text: str) -> str | None:
    m = re.search(r"https?://[^\s]+", text or "")
    return m.group(0).rstrip(".,;:)") if m else None


class NotionClient:
    def __init__(self, api_key: str, parent_page_id: str):
        self._client = Client(auth=api_key)
        self._parent_page_id = parent_page_id.strip()
        self._db_ids: dict[str, str] = {}  # category title -> database_id
        self._db_ids_loaded = False

    def _ensure_db_ids(self) -> None:
        if self._db_ids_loaded:
            return
        _init_databases(self._client, self._parent_page_id, self._db_ids)
        self._db_ids_loaded = True

    def get_database_id(self, category: str) -> str | None:
        self._ensure_db_ids()
        return self._db_ids.get(category)

    def create_page(
        self,
        category: str,
        title: str,
        notes: str = "",
        *,
        url: str | None = None,
        author: str | None = None,
        status: str | None = None,
        due_date: str | None = None,
    ) -> dict | None:
        """Create a page in the category database. Returns dict with page_id, url, etc."""
        self._ensure_db_ids()
        db_id = self._db_ids.get(category)
        if not db_id:
            return None
        props = _build_properties(
            category, title, notes, url=url, author=author, status=status, due_date=due_date
        )
        try:
            page = _retry(
                self._client.pages.create,
                parent={"database_id": db_id},
                properties=props,
            )
            return {
                "id": page["id"],
                "url": page.get("url"),
                "database_id": db_id,
                "database_title": category,
            }
        except Exception as e:
            logger.exception("Notion create_page failed for category=%s", category, exc_info=False)
            return None

    def get_recent_pages(self, limit: int = 5) -> list[dict]:
        """Get last added pages across all DBs (by created_time)."""
        self._ensure_db_ids()
        all_pages: list[tuple[str, dict]] = []
        for cat, db_id in self._db_ids.items():
            try:
                r = _retry(
                    self._client.databases.query,
                    database_id=db_id,
                    page_size=min(limit * 2, 100),
                    sorts=[{"timestamp": "created_time", "direction": "descending"}],
                )
                for p in r.get("results", []):
                    title = _page_title(p)
                    if title:
                        all_pages.append((p["created_time"], {"page_id": p["id"], "title": title, "database_id": db_id, "database_title": cat}))
            except Exception as e:
                logger.warning("Query DB %s failed: %s", cat, e)
        all_pages.sort(key=lambda x: x[0], reverse=True)
        return [p[1] for p in all_pages[:limit]]

    def find_page_by_title_fragment(self, fragment: str) -> dict | None:
        """Search in all DBs for a page whose title contains fragment."""
        self._ensure_db_ids()
        fragment = fragment.strip().lower()
        if not fragment:
            return None
        for cat, db_id in self._db_ids.items():
            try:
                r = _retry(
                    self._client.databases.query,
                    database_id=db_id,
                    page_size=100,
                )
                for p in r.get("results", []):
                    title = _page_title(p)
                    if title and fragment in title.lower():
                        return {"page_id": p["id"], "title": title, "database_id": db_id, "database_title": cat}
            except Exception as e:
                logger.warning("Query DB %s failed: %s", cat, e)
        return None

    def archive_page(self, page_id: str) -> bool:
        try:
            _retry(self._client.blocks.delete, block_id=page_id)
            return True
        except Exception as e:
            logger.exception("Notion archive_page failed: %s", e)
            return False

    def init_databases(self) -> dict[str, str]:
        """Force (re)create missing databases. Returns category -> database_id."""
        _init_databases(self._client, self._parent_page_id, self._db_ids)
        self._db_ids_loaded = True
        return dict(self._db_ids)


def _page_title(page: dict) -> str:
    props = page.get("properties", {})
    name = props.get("Name") or props.get("name")
    if not name:
        return ""
    t = name.get("title")
    if not t or not isinstance(t, list):
        return ""
    return "".join((b.get("plain_text") or "") for b in t)


def _build_properties(
    category: str,
    title: str,
    notes: str,
    *,
    url: str | None = None,
    author: str | None = None,
    status: str | None = None,
    due_date: str | None = None,
) -> dict:
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")

    def title_val(t: str) -> list:
        return [{"type": "text", "text": {"content": (t or " ")[:2000]}}]

    def rich_val(t: str) -> list:
        return [{"type": "text", "text": {"content": (t or "")[:2000]}}]

    def date_val(d: str) -> dict:
        return {"start": d}

    def select_val(s: str) -> dict:
        return {"name": s}

    base = {
        "Name": {"title": title_val(title)},
        "Date Added": {"date": date_val(today)},
        "Notes": {"rich_text": rich_val(notes)},
    }

    if category == "Книги к прочтению":
        base["Author"] = {"rich_text": rich_val(author or "")}
        base["Status"] = {"select": select_val(status or "Хочу прочитать")}
        return base
    if category == "Задачи на сегодня/завтра":
        base["Due Date"] = {"date": date_val(due_date or today)}
        base["Status"] = {"select": select_val(status or "Не начата")}
        return base
    if category == "Фильмы / Сериалы":
        base["Status"] = {"select": select_val(status or "Хочу посмотреть")}
        return base
    if category in ("Ссылки / Статьи", "Полезные сайты"):
        if url:
            base["URL"] = {"url": url}
        return base
    # Universal
    base["Source"] = {"select": select_val("Telegram")}
    return base


def _init_databases(client: Client, parent_page_id: str, out: dict[str, str]) -> None:
    """List existing DBs under parent, create missing ones."""
    try:
        children = _retry(
            client.blocks.children.list,
            block_id=parent_page_id,
            page_size=100,
        )
    except Exception as e:
        logger.exception("List blocks under parent failed: %s", e)
        return
    for block in children.get("results", []):
        if block.get("type") == "child_database":
            raw = (block.get("child_database") or {}).get("title")
            if isinstance(raw, str):
                title = raw
            elif isinstance(raw, list):
                title = "".join(t.get("plain_text", "") for t in raw)
            else:
                title = ""
            if title and block.get("id"):
                out[title] = block["id"]
    for category in CATEGORIES:
        if category in out:
            continue
        schema = _schema_for_category(category)
        try:
            db = _retry(
                client.databases.create,
                parent={"type": "page_id", "page_id": parent_page_id},
                title=[{"type": "text", "text": {"content": category}}],
                properties=schema,
            )
            out[category] = db["id"]
            logger.info("Created Notion database: %s", category)
        except Exception as e:
            logger.exception("Create database %s failed: %s", category, e)
