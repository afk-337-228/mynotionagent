"""
Voice message transcription using faster-whisper (local, free).
"""
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Lazy-loaded model (tiny or base for CPU). None if faster_whisper not installed (e.g. Vercel).
_model = None
_model_name = "tiny"
_whisper_unavailable = False


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
    Returns recognized text or empty string on failure.
    """
    path = Path(file_path)
    if not path.exists():
        return ""
    model = _get_model()
    if model is None:
        return ""
    try:
        size_bytes = path.stat().st_size if path.exists() else 0
        segments, _ = model.transcribe(str(path), language="ru", beam_size=1)
        out = " ".join(s.text for s in segments if s.text).strip()
        logger.info("Transcribed: path=%s size_bytes=%s out_len=%s", path.name, size_bytes, len(out))
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
