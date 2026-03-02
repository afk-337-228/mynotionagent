"""
Voice message transcription using faster-whisper (local, free).
"""
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Lazy-loaded model (tiny or base for CPU)
_model = None
_model_name = "tiny"


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(_model_name, device="cpu", compute_type="int8")
    return _model


def set_model_size(size: str) -> None:
    """Set model size: tiny, base, small, etc. Call before first transcribe."""
    global _model_name, _model
    _model_name = size
    _model = None


def transcribe_file(file_path: str | Path) -> str:
    """
    Transcribe audio file (ogg, mp3, etc.).
    Returns recognized text or empty string on failure.
    """
    path = Path(file_path)
    if not path.exists():
        return ""
    try:
        model = _get_model()
        segments, _ = model.transcribe(str(path), language="ru", beam_size=1)
        return " ".join(s.text for s in segments if s.text).strip()
    except Exception as e:
        logger.warning("Transcribe failed: %s", e)
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
