"""
Voice: OpenRouter (audio-capable model), OpenAI Whisper API, or faster-whisper (local).
Priority: OpenRouter (if OPENROUTER_API_KEY) → OpenAI (if OPENAI_API_KEY) → local faster-whisper.
"""
import base64
import logging
import os
import tempfile
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

OPENAI_WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_API_TIMEOUT = 30.0
OPENROUTER_CHAT_TIMEOUT = 30.0
OPENROUTER_VOICE_MODEL = "google/gemini-2.5-flash"

# Lazy-loaded model (tiny or base for CPU). None if faster_whisper not installed (e.g. Vercel).
_model = None
_model_name = "tiny"
_whisper_unavailable = False


def _transcribe_via_openrouter(file_path: str | Path) -> str:
    """Transcribe via OpenRouter chat with audio input (e.g. Gemini). Uses OPENROUTER_API_KEY."""
    api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    base_url = (os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
    if not api_key:
        return ""
    path = Path(file_path)
    if not path.exists():
        return ""
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError as e:
        logger.warning("OpenRouter voice: read failed %s", e)
        return ""
    model = os.getenv("OPENROUTER_VOICE_MODEL", OPENROUTER_VOICE_MODEL).strip() or OPENROUTER_VOICE_MODEL
    url = f"{base_url}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Transcribe this voice message to text. Reply with only the transcription, in the same language as the audio (e.g. Russian). No other commentary.",
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {"data": b64, "format": "ogg"},
                    },
                ],
            }
        ],
    }
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=OPENROUTER_CHAT_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices") or [{}])[0].get("message") or {}
        out = (text.get("content") or "").strip()
        logger.info("Transcribed via OpenRouter: path=%s out_len=%s", path.name, len(out))
        return out
    except Exception as e:
        logger.warning("OpenRouter voice failed: %s", e)
        return ""


def _transcribe_via_openai(file_path: str | Path) -> str:
    """Transcribe via OpenAI Whisper API. Returns text or empty string."""
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return ""
    path = Path(file_path)
    if not path.exists():
        return ""
    try:
        with open(path, "rb") as f:
            files = {"file": (path.name, f, "audio/ogg")}
            data = {"model": "whisper-1", "language": "ru"}
            r = httpx.post(
                OPENAI_WHISPER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files=files,
                data=data,
                timeout=WHISPER_API_TIMEOUT,
            )
        r.raise_for_status()
        out = (r.json().get("text") or "").strip()
        logger.info("Transcribed via OpenAI: path=%s out_len=%s", path.name, len(out))
        return out
    except Exception as e:
        logger.warning("OpenAI Whisper API failed: %s", e)
        return ""


def _get_model():
    global _model, _whisper_unavailable
    if _whisper_unavailable:
        return None
    if _model is None:
        try:
            from faster_whisper import WhisperModel
            _model = WhisperModel(_model_name, device="cpu", compute_type="int8")
        except ImportError:
            logger.info("faster_whisper not installed; voice messages will not be transcribed (e.g. on Vercel)")
            _whisper_unavailable = True
            return None
    return _model


def set_model_size(size: str) -> None:
    """Set model size: tiny, base, small, etc. Call before first transcribe."""
    global _model_name, _model
    _model_name = size
    _model = None


def transcribe_file(file_path: str | Path) -> str:
    """
    Transcribe audio file (ogg, mp3, etc.).
    Priority: OpenRouter (OPENROUTER_API_KEY) → OpenAI (OPENAI_API_KEY) → local faster-whisper.
    Returns recognized text or empty string on failure.
    """
    path = Path(file_path)
    if not path.exists():
        return ""
    if os.getenv("OPENROUTER_API_KEY", "").strip():
        out = _transcribe_via_openrouter(path)
        if out:
            return out
    if os.getenv("OPENAI_API_KEY", "").strip():
        out = _transcribe_via_openai(path)
        if out:
            return out
    model = _get_model()
    if model is None:
        return ""
    try:
        size_bytes = path.stat().st_size
        segments, _ = model.transcribe(str(path), language="ru", beam_size=1)
        out = " ".join(s.text for s in segments if s.text).strip()
        logger.info("Transcribed (local): path=%s size_bytes=%s out_len=%s", path.name, size_bytes, len(out))
        return out
    except Exception as e:
        logger.warning("Transcribe failed: path=%s error=%s", path, e)
        return ""


def transcribe_bytes(data: bytes, suffix: str = ".ogg") -> str:
    """Transcribe from in-memory audio bytes. Writes to temp file."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        path = f.name
    try:
        return transcribe_file(path)
    finally:
        Path(path).unlink(missing_ok=True)
