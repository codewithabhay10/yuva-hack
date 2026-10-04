"""Voice notes to text (P2, P14) through an OpenAI-compatible speech-to-text endpoint.

Free options: Groq's hosted Whisper (``GROQ_API_KEY``, free tier), or a Whisper server on your
own computer that speaks the same API (for example faster-whisper-server), so audio never leaves
it. ``UNITWATT_STT`` picks one (``groq``, ``local`` or ``none``); ``UNITWATT_STT_BASE_URL``,
``UNITWATT_STT_MODEL`` and ``UNITWATT_STT_API_KEY`` point it anywhere else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpeechToText:
    label: str
    base_url: str
    model: str
    key_env: tuple[str, ...] = ()

    @property
    def api_key(self) -> str | None:
        return next((os.environ[k] for k in self.key_env if os.environ.get(k)), None)


PRESETS = {
    "groq": SpeechToText("Groq Whisper (free tier)", "https://api.groq.com/openai/v1", "whisper-large-v3", ("GROQ_API_KEY",)),
    "local": SpeechToText("Whisper on this computer", "http://localhost:8000/v1", "Systran/faster-whisper-small"),
}

_EXTENSIONS = {"audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
               "audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/aac": "m4a", "audio/wav": "wav", "audio/x-wav": "wav",
               "audio/webm": "webm", "audio/amr": "amr"}


def configured() -> SpeechToText | None:
    """The speech-to-text service the environment points at, or None."""
    choice = os.environ.get("UNITWATT_STT", "").lower()
    if choice == "none":
        return None
    if os.environ.get("UNITWATT_STT_BASE_URL"):
        return SpeechToText("Speech-to-text", os.environ["UNITWATT_STT_BASE_URL"].rstrip("/"),
                            os.environ.get("UNITWATT_STT_MODEL", "whisper-1"), ("UNITWATT_STT_API_KEY",))
    if choice in PRESETS:
        return PRESETS[choice]
    if os.environ.get("GROQ_API_KEY"):
        return PRESETS["groq"]
    return None


def transcribe(content: bytes, mime: str, stt: SpeechToText | None = None, prompt: str = "",
               session=None, timeout: float = 60.0) -> str:
    """Audio bytes -> text. ``prompt`` biases the vocabulary (product names, 'tonne', 'shift')."""
    import requests

    stt = stt or configured()
    if stt is None:
        raise TranscriptionError("No speech-to-text service is set up: set GROQ_API_KEY (free) or UNITWATT_STT=local.")
    if stt.key_env and not stt.api_key:
        raise TranscriptionError(f"Set {stt.key_env[0]} to use {stt.label}.")
    base_mime = (mime or "audio/ogg").split(";")[0].strip().lower()
    data = {"model": stt.model, "response_format": "json", "temperature": "0"}
    if prompt:
        data["prompt"] = prompt[:800]
    headers = {"Authorization": f"Bearer {stt.api_key}"} if stt.api_key else {}
    try:
        response = (session or requests).post(
            f"{stt.base_url}/audio/transcriptions", data=data, headers=headers, timeout=timeout,
            files={"file": (f"voice.{_EXTENSIONS.get(base_mime, 'ogg')}", content, base_mime)},
        )
    except requests.RequestException as exc:
        raise TranscriptionError(f"Could not reach {stt.label} at {stt.base_url} ({exc.__class__.__name__}).") from exc
    if response.status_code == 429:
        raise TranscriptionError(f"{stt.label} free-tier limit reached; try again in a minute.")
    if response.status_code != 200:
        raise TranscriptionError(f"{stt.label} returned HTTP {response.status_code}: {response.text[:200]}")
    try:
        text = str(response.json().get("text", "")).strip()
    except ValueError:
        text = response.text.strip()
    if not text:
        raise TranscriptionError("The voice note came back empty; please speak a little longer or type it.")
    return text
