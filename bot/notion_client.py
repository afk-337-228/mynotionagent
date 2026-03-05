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

# All categories (exact names for Notion)
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
    "Гитхаб репы",
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
    "крипты": "Крипта",
    "крипто": "Крипта",
    "гитхаб репы": "Гитхаб репы",
    "гитхаб": "Гитхаб репы",
    "репы": "Гитхаб репы",
    "репо": "Гитхаб репы",
    "репозитории": "Гитхаб репы",
    "github": "Гитхаб репы",
}


def _retry(fn, *args, max_retries: int = 5, **kwargs) -> Any:
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except APIResponseError as e:
            code = getattr(e, "code", None)
            if code == "rate_limited" and attempt < max_retries - 1:
                wait = 2**attempt
                logger.warning(
                    "Notion rate limit (attempt %s/%s), retry in %s sec",
                    attempt + 1, max_retries, wait,
                )
                time.sleep(wait)
                continue
            logger.error(
                "Notion API error: code=%s message=%s",
                code, getattr(e, "message", str(e)),
            )
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
    if category in ("Ссылки / Статьи", "Полезные сайты", "Гитхаб репы"):
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

    def _get_database_schema(self, db_id: str) -> dict[str, str]:
        """Return dict: property_name -> type (e.g. 'title', 'rich_text', 'date', 'select').
        Supports Notion API 2025: schema is in data_sources[].retrieve, not in database.properties.
        """
        try:
            db = _retry(self._client.databases.retrieve, database_id=db_id)
            raw_props = db.get("properties") or getattr(db, "properties", None) or {}
            if not raw_props and db.get("data_sources"):
                ds_list = db["data_sources"]
                if isinstance(ds_list, list) and ds_list and isinstance(ds_list[0], dict):
                    ds_id = ds_list[0].get("id")
                    if ds_id:
                        ds = _retry(self._client.data_sources.retrieve, data_source_id=ds_id)
                        raw_props = ds.get("properties") or {}
                        logger.debug("DB %s: schema from data_source %s", db_id[:8], ds_id[:8])
            if not isinstance(raw_props, dict):
                logger.warning("DB %s properties is not a dict: type=%s", db_id[:8], type(raw_props).__name__)
                return {}
            out = {}
            for key, cfg in raw_props.items():
                if not isinstance(cfg, dict) or "type" not in cfg:
                    continue
                prop_type = cfg.get("type")
                display_name = cfg.get("name") or key
                if isinstance(display_name, list):
                    display_name = "".join(t.get("plain_text", "") for t in display_name) if display_name else key
                if isinstance(display_name, str) and display_name:
                    out[display_name] = prop_type
                if key != display_name and isinstance(key, str):
                    out[key] = prop_type
            if not out and raw_props:
                logger.warning(
                    "Schema empty for db %s but properties had %s keys (first: %s)",
                    db_id[:8], len(raw_props), list(raw_props.keys())[:5],
                )
            else:
                logger.info("Schema for db %s: %s props, names=%s", db_id[:8], len(out), list(out.keys())[:15])
            return out
        except Exception as e:
            logger.warning("Could not retrieve database schema for db_id=%s: %s", db_id[:8], e)
            return {}

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
        raw_props = _build_properties(
            category, title, notes, url=url, author=author, status=status, due_date=due_date
        )
        schema = self._get_database_schema(db_id)
        props = _align_properties_to_schema(raw_props, schema)
        if not props:
            schema_preview = dict(list(schema.items())[:10])
            logger.warning(
                "No matching properties: category=%s db_id=%s schema=%s",
                category, db_id[:8], schema_preview,
            )
            return None
        if schema and set(props.keys()) != set(raw_props.keys()):
            logger.debug("Aligned props for %s: %s -> %s", category, list(raw_props.keys()), list(props.keys()))
        try:
            page = _retry(
                self._client.pages.create,
                parent={"database_id": db_id},
                properties=props,
            )
            page_id = page["id"]
            logger.info(
                "Page created: category=%s page_id=%s db_id=%s",
                category, page_id[:8] + "..." if len(page_id) > 8 else page_id, db_id[:8] + "...",
            )
            return {
                "id": page_id,
                "url": page.get("url"),
                "database_id": db_id,
                "database_title": category,
            }
        except Exception as e:
            logger.exception(
                "Notion create_page failed: category=%s db_id=%s error=%s",
                category, db_id[:8], e, exc_info=False,
            )
            return None

    def get_recent_pages_in_category(self, category: str, limit: int = 1) -> list[dict]:
        """Get the most recent page(s) in a given category database."""
        self._ensure_db_ids()
        db_id = self._db_ids.get(category)
        if not db_id:
            return []
        try:
            r = _retry(
                self._client.databases.query,
                database_id=db_id,
                page_size=limit,
                sorts=[{"timestamp": "created_time", "direction": "descending"}],
            )
            out = []
            for p in r.get("results", []):
                title = _page_title(p)
                if title:
                    out.append({
                        "page_id": p["id"],
                        "title": title,
                        "database_id": db_id,
                        "database_title": category,
                    })
            return out
        except Exception as e:
            logger.warning("get_recent_pages_in_category failed: category=%s error=%s", category[:20], e)
            return []

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
                logger.warning("Query DB failed: category=%s db_id=%s error=%s", cat, db_id[:8], e)
        all_pages.sort(key=lambda x: x[0], reverse=True)
        return [p[1] for p in all_pages[:limit]]

    def get_tasks_due_today(self, limit: int = 15) -> list[dict]:
        """Get tasks from 'Задачи на сегодня/завтра' with Due Date = today."""
        from datetime import datetime, timezone
        self._ensure_db_ids()
        db_id = self._db_ids.get("Задачи на сегодня/завтра")
        if not db_id:
            return []
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            r = _retry(
                self._client.databases.query,
                database_id=db_id,
                page_size=limit,
                filter={
                    "property": "Due Date",
                    "date": {"equals": today},
                },
                sorts=[{"timestamp": "created_time", "direction": "descending"}],
            )
            out = []
            for p in r.get("results", []):
                title = _page_title(p)
                if title:
                    out.append({
                        "page_id": p["id"],
                        "title": title,
                        "url": p.get("url"),
                        "database_title": "Задачи на сегодня/завтра",
                    })
            return out
        except Exception as e:
            logger.warning("get_tasks_due_today failed: db_id=%s error=%s", db_id[:8], e)
            return []

    def search_pages(self, query: str, limit: int = 10) -> list[dict]:
        """Search across workspace, return only pages from our category databases."""
        self._ensure_db_ids()
        our_db_ids = {db_id.replace("-", "") for db_id in self._db_ids.values()}
        if not query or not our_db_ids:
            return []
        try:
            r = _retry(
                self._client.search,
                query=query.strip(),
                filter={"property": "object", "value": "page"},
                page_size=min(limit * 2, 25),
            )
            out = []
            for p in r.get("results", []):
                parent = p.get("parent") or {}
                if parent.get("type") != "database_id":
                    continue
                db_id = (parent.get("database_id") or "").replace("-", "")
                if db_id not in our_db_ids:
                    continue
                title = _page_title(p)
                if title:
                    cat = next((c for c, did in self._db_ids.items() if (did or "").replace("-", "") == db_id), "")
                    out.append({
                        "page_id": p["id"],
                        "title": title,
                        "url": p.get("url"),
                        "database_title": cat,
                    })
                if len(out) >= limit:
                    break
            return out
        except Exception as e:
            logger.warning("search_pages failed: query=%s error=%s", query[:50], e)
            return []

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
                logger.warning("Query DB failed: category=%s db_id=%s error=%s", cat, db_id[:8], e)
        return None

    def archive_page(self, page_id: str) -> bool:
        try:
            _retry(self._client.blocks.delete, block_id=page_id)
            logger.info("Page archived: page_id=%s", page_id[:8] + "..." if len(page_id) > 8 else page_id)
            return True
        except Exception as e:
            logger.exception("Notion archive_page failed: page_id=%s error=%s", page_id[:8], e)
            return False

    def update_page(
        self,
        page_id: str,
        *,
        title: str | None = None,
        notes: str | None = None,
        status: str | None = None,
    ) -> bool:
        """Update page title, notes and/or status. Uses standard property names Name, Notes, Status."""
        props = {}
        if title is not None:
            props["Name"] = {"title": [{"type": "text", "text": {"content": (title or " ")[:2000]}}]}
        if notes is not None:
            props["Notes"] = {"rich_text": [{"type": "text", "text": {"content": (notes or "")[:2000]}}]}
        if status is not None:
            props["Status"] = {"select": {"name": status}}
        if not props:
            return True
        try:
            _retry(self._client.pages.update, page_id=page_id, properties=props)
            logger.info("Page updated: page_id=%s", page_id[:8] + "..." if len(page_id) > 8 else page_id)
            return True
        except Exception as e:
            logger.exception("Notion update_page failed: page_id=%s error=%s", page_id[:8], e)
            return False

    def _get_done_status_value(self, category: str) -> str | None:
        """Return the 'done' status option for this category, or None if no Status/done."""
        if category == "Задачи на сегодня/завтра":
            return "Выполнена"
        if category == "Книги к прочтению":
            return "Прочитана"
        if category == "Фильмы / Сериалы":
            return "Посмотрел"
        return None

    def _get_category_by_page_id(self, page_id: str) -> str | None:
        """Get category (database title) for a page. Returns None if not found."""
        try:
            page = _retry(self._client.pages.retrieve, page_id=page_id)
            parent = page.get("parent") or {}
            if parent.get("type") != "database_id":
                return None
            db_id = parent.get("database_id") or ""
            if not db_id:
                return None
            self._ensure_db_ids()
            for cat, did in self._db_ids.items():
                if (did or "").replace("-", "") == (db_id or "").replace("-", ""):
                    return cat
            return None
        except Exception as e:
            logger.warning("Could not get category for page_id=%s: %s", page_id[:8] if page_id else "", e)
            return None

    def mark_done_and_archive(self, page_id: str, category: str | None = None) -> bool:
        """
        Mark note as done (set Status to done value where applicable) and archive the page.
        If category is None, it is resolved from the page's parent database.
        """
        cat = category
        if cat is None:
            cat = self._get_category_by_page_id(page_id)
        if cat:
            done_value = self._get_done_status_value(cat)
            if done_value:
                try:
                    self.update_page(page_id, status=done_value)
                except Exception:
                    pass  # DB may have no Status; archive anyway
        try:
            return self.archive_page(page_id)
        except Exception:
            return False

    def init_databases(self) -> dict[str, str]:
        """Force (re)create missing databases. Returns category -> database_id."""
        _init_databases(self._client, self._parent_page_id, self._db_ids)
        self._db_ids_loaded = True
        return dict(self._db_ids)


def _is_likely_uuid(s: str) -> bool:
    """True if string looks like a Notion property id (hex with optional dashes)."""
    if not s or len(s) < 20:
        return False
    clean = s.replace("-", "")
    return len(clean) == 32 and all(c in "0123456789abcdefABCDEF" for c in clean)


def _align_properties_to_schema(raw_props: dict, schema: dict[str, str]) -> dict:
    """
    Map our property names to the actual DB property names by type.
    Handles DBs created in UI or with different locale (e.g. "Название" instead of "Name").
    Prefers display names over UUID keys when sending to API.
    When schema is empty we do not send raw_props (would cause 400); caller should handle None/create_page fails.
    """
    if not schema:
        return {}
    by_type: dict[str, list[str]] = {}
    for name, t in schema.items():
        by_type.setdefault(t, []).append(name)
    # Prefer non-UUID keys (display names) so API gets names, not ids
    for t, names in by_type.items():
        by_type[t] = sorted(names, key=lambda n: (1 if _is_likely_uuid(str(n)) else 0, n))
    result = {}
    used: dict[str, int] = {}
    for our_key, our_value in raw_props.items():
        if not isinstance(our_value, dict):
            continue
        prop_type = next(iter(our_value.keys()), None)
        if not prop_type or prop_type not in by_type:
            continue
        candidates = by_type[prop_type]
        if our_key in candidates:
            actual_key = our_key
        else:
            idx = used.get(prop_type, 0)
            if idx >= len(candidates):
                continue
            actual_key = candidates[idx]
            used[prop_type] = idx + 1
        result[actual_key] = our_value
    has_title = any(
        isinstance(v, dict) and "title" in v
        for v in result.values()
    )
    return result if has_title else {}


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
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
    if category in ("Ссылки / Статьи", "Полезные сайты", "Гитхаб репы"):
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
        logger.exception("List blocks under parent failed: parent_id=%s error=%s", parent_page_id[:8], e)
        return
    results = children.get("results", [])
    for block in results:
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
    existing = len(out)
    logger.debug("Found %s existing databases under parent %s", existing, parent_page_id[:8])
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
            logger.info("Created Notion database: category=%s db_id=%s", category, db["id"][:8] + "...")
        except Exception as e:
            logger.exception("Create database failed: category=%s error=%s", category, e)
