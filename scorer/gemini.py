from __future__ import annotations

import json
import logging
import re
from io import BytesIO
from typing import Any

import google.generativeai as genai
from PIL import Image

from config import load
from core.models import Profile, ScoreResult
from scorer.base import Scorer
from scorer.prompts import SYSTEM, USER_TEMPLATE

log = logging.getLogger(__name__)

# Сколько фото из анкеты максимум кидаем в один запрос — экономим токены.
MAX_PHOTOS = 4
# Уменьшаем фото перед отправкой — Gemini Flash прекрасно видит и при 512px.
MAX_IMAGE_SIDE = 512


class GeminiScorer(Scorer):
    def __init__(self) -> None:
        cfg = load()
        if not cfg.gemini_api_key:
            raise RuntimeError("Не заполнен GEMINI_API_KEY в .env")
        genai.configure(api_key=cfg.gemini_api_key)
        self.model = genai.GenerativeModel(
            cfg.gemini_model,
            system_instruction=SYSTEM,
            generation_config={
                "response_mime_type": "application/json",
                "temperature": 0.2,
            },
        )

    async def score(self, profile: Profile, goal: str) -> ScoreResult:
        parts: list[Any] = [
            USER_TEMPLATE.format(goal=goal, bio=profile.bio or "(пусто)")
        ]

        for photo in profile.photos[:MAX_PHOTOS]:
            img = self._to_image(photo)
            if img is not None:
                parts.append(img)

        try:
            resp = await self.model.generate_content_async(parts)
        except Exception as e:
            log.exception("Gemini API error")
            return ScoreResult(score=0, reason=f"ошибка Gemini: {e}")

        text = (resp.text or "").strip()
        data = self._parse_json(text)
        if data is None:
            return ScoreResult(
                score=0, reason=f"не удалось распарсить ответ: {text[:200]}"
            )

        try:
            score_val = int(data.get("score", 0))
        except (TypeError, ValueError):
            score_val = 0
        score_val = max(0, min(100, score_val))
        reason = str(data.get("reason", "")).strip() or "(без комментария)"
        return ScoreResult(score=score_val, reason=reason)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # модель иногда оборачивает ответ в ```json ... ```
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _to_image(photo: bytes | str) -> Image.Image | None:
        try:
            if isinstance(photo, bytes):
                img = Image.open(BytesIO(photo))
            else:
                # URL'ы пока не поддерживаем — адаптеры должны давать bytes.
                # Для будущих VK/web-адаптеров добавим скачивание здесь.
                return None
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            if img.mode != "RGB":
                img = img.convert("RGB")
            return img
        except Exception:
            log.exception("failed to load image")
            return None
