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
from scorer.prompts import SYSTEM, build_user_message

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
            # Анкеты знакомств — легитимный контент; снимаем штатные блокировки,
            # иначе Gemini возвращает PROHIBITED_CONTENT по фото и анкета теряется.
            # Жёсткий prompt-level блок всё равно возможен — его гасит _safe_text.
            safety_settings=[
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
        )

    async def score(
        self,
        profile: Profile,
        goal: str,
        style: str | None = None,
        gen_message_if_score_ge: int | None = None,
    ) -> ScoreResult:
        parts: list[Any] = [
            build_user_message(
                goal=goal,
                bio=profile.bio or "(пусто)",
                style=style,
                threshold=gen_message_if_score_ge,
            )
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

        text = self._safe_text(resp)
        if text is None:
            reason = self._block_reason(resp)
            log.warning("Gemini заблокировал ответ: %s", reason)
            return ScoreResult(score=0, reason=f"Gemini не оценил ({reason})")
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
        message_raw = data.get("message")
        message = (
            str(message_raw).strip()
            if isinstance(message_raw, str) and message_raw.strip()
            else None
        )
        return ScoreResult(score=score_val, reason=reason, message=message)

    @staticmethod
    def _safe_text(resp: Any) -> str | None:
        """resp.text бросает ValueError, когда Gemini заблокировал промпт/ответ
        (кандидатов/Part нет). Возвращаем None вместо краша."""
        try:
            return (resp.text or "").strip()
        except Exception:
            return None

    @staticmethod
    def _block_reason(resp: Any) -> str:
        """Причина блокировки для лога/UI (prompt_feedback / finish_reason)."""
        try:
            pf = getattr(resp, "prompt_feedback", None)
            br = getattr(pf, "block_reason", None) if pf else None
            if br:
                return f"prompt={br}"
            cands = getattr(resp, "candidates", None) or []
            if cands and getattr(cands[0], "finish_reason", None):
                return f"finish={cands[0].finish_reason}"
        except Exception:
            pass
        return "blocked"

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
