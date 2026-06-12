"""Расшифровка голосовых сообщений через Gemini Flash.

Gemini принимает аудио на вход (`audio/ogg`, `audio/mp3` и др.) inline'ом
до ~8 МБ. Для типичного войса (10-60 сек) это копейки: ~32 токена/сек +
сотня overhead.
"""

from __future__ import annotations

import logging

import google.generativeai as genai

from config import load

log = logging.getLogger(__name__)

# Inline-лимит Gemini ~20 МБ; режем раньше, чтобы зря не гонять.
MAX_AUDIO_BYTES = 8 * 1024 * 1024

PROMPT = (
    "Расшифруй это голосовое сообщение на русском. Верни ТОЛЬКО текст "
    "реплики без префиксов, кавычек и комментариев. Если речь невнятная — "
    "верни лучшее, что смог разобрать."
)


async def transcribe_audio(
    audio_bytes: bytes, mime_type: str = "audio/ogg"
) -> str | None:
    if not audio_bytes:
        return None
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        log.warning(
            "voice: %d байт > лимита %d, пропускаю",
            len(audio_bytes), MAX_AUDIO_BYTES,
        )
        return None
    cfg = load()
    if not cfg.gemini_api_key:
        return None
    genai.configure(api_key=cfg.gemini_api_key)
    model = genai.GenerativeModel(
        cfg.gemini_model,
        generation_config={"temperature": 0.0},
    )
    try:
        resp = await model.generate_content_async([
            {"mime_type": mime_type, "data": audio_bytes},
            PROMPT,
        ])
    except Exception:
        log.exception("voice transcribe call failed")
        return None
    text = (getattr(resp, "text", "") or "").strip()
    return text or None
