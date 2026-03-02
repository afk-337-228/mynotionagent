"""
AI classification of notes via OpenRouter (Gemini/Llama).
Returns category, confidence, reasoning. No dialog history.
"""
import json
import logging
from typing import Any

import httpx

from bot.notion_client import CATEGORIES

logger = logging.getLogger(__name__)

# Prefer free models
OPENROUTER_MODEL = "google/gemini-2.5-flash"
FALLBACK_MODEL = "meta-llama/llama-3.1-8b-instruct"
MAX_TOKENS = 50
TEMPERATURE = 0.0
TIMEOUT = 10.0

SYSTEM_PROMPT = """Ты — ассистент для классификации личных заметок.
Отвечай ТОЛЬКО валидным JSON, без markdown и пояснений."""


def _categories_text() -> str:
    return "\n".join(f"- {c}" for c in CATEGORIES)


def classify(
    text: str,
    *,
    api_key: str,
    base_url: str = "https://openrouter.ai/api/v1",
    model: str = OPENROUTER_MODEL,
) -> dict[str, Any] | None:
    """
    Returns {"category": "...", "confidence": 0.85, "reasoning": "..."} or None on error.
    """
    user_content = f"""Доступные категории:
{_categories_text()}

Заметка пользователя:
\"\"\"{text[:3000]}\"\"\"

Ответь ТОЛЬКО в формате JSON:
{{ "category": "точное название категории из списка", "confidence": 0.85, "reasoning": "краткое объяснение" }}
Если не уверен — укажи confidence ниже 0.6."""

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/notion-telegram-bot",
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
        r.raise_for_status()
        data = r.json()
        choice = (data.get("choices") or [None])[0]
        if not choice:
            return None
        content = (choice.get("message") or {}).get("content") or ""
        content = content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()
        out = json.loads(content)
        cat = out.get("category") or ""
        conf = float(out.get("confidence") or 0)
        if cat not in CATEGORIES:
            return None
        return {
            "category": cat,
            "confidence": max(0, min(1, conf)),
            "reasoning": str(out.get("reasoning") or ""),
        }
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Classifier error: %s", e)
        return None
