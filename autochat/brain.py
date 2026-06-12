"""Gemini-обёртка для генерации сообщений автопереписки.

Stateless: каждый вызов отдаёт системный промпт + style + goal + полную
историю. Источник истины — БД, in-memory state Gemini'а не используем
(start_chat) — после рестарта потеряется, а БД переживёт.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import google.generativeai as genai

from autochat.prompts import (
    SYSTEM,
    build_continue_user_message,
    build_opener_user_message,
    build_reply_user_message,
)
from config import load

log = logging.getLogger(__name__)


@dataclass
class BrainResult:
    reply: str | None
    done: bool
    done_reason: str | None


class GeminiChatBrain:
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
                "temperature": 0.7,
            },
        )

    async def generate_opener(
        self,
        *,
        style: str,
        goal: str,
        profile_bio: str,
        profile_url: str,
    ) -> BrainResult:
        user = build_opener_user_message(
            style=style,
            goal=goal,
            profile_bio=profile_bio,
            profile_url=profile_url,
            now_ts=int(time.time()),
        )
        return await self._call(user)

    async def respond(
        self,
        *,
        style: str,
        goal: str,
        profile_bio: str,
        profile_url: str,
        history: list[tuple[str, str, int]],
    ) -> BrainResult:
        user = build_reply_user_message(
            style=style,
            goal=goal,
            profile_bio=profile_bio,
            profile_url=profile_url,
            history=history,
            now_ts=int(time.time()),
        )
        return await self._call(user)

    async def continue_conversation(
        self,
        *,
        style: str,
        goal: str,
        profile_bio: str,
        profile_url: str,
        history: list[tuple[str, str, int]],
    ) -> BrainResult:
        user = build_continue_user_message(
            style=style,
            goal=goal,
            profile_bio=profile_bio,
            profile_url=profile_url,
            history=history,
            now_ts=int(time.time()),
        )
        return await self._call(user)

    async def _call(self, user_text: str) -> BrainResult:
        try:
            resp = await self.model.generate_content_async(user_text)
        except Exception as e:
            log.exception("Gemini autochat error")
            return BrainResult(
                reply=None, done=True, done_reason=f"gemini error: {e}"
            )

        # safety-фильтр: ответ пустой, finish_reason ≠ STOP
        try:
            finish_reason = resp.candidates[0].finish_reason.name  # type: ignore[union-attr]
        except Exception:
            finish_reason = "UNKNOWN"
        text = (getattr(resp, "text", "") or "").strip()
        if not text:
            return BrainResult(
                reply=None,
                done=True,
                done_reason=f"empty response (finish={finish_reason})",
            )

        data = _parse_json(text)
        if data is None:
            return BrainResult(
                reply=None,
                done=True,
                done_reason=f"bad json: {text[:200]}",
            )

        reply_raw = data.get("reply")
        reply = (
            str(reply_raw).strip()
            if isinstance(reply_raw, str) and reply_raw.strip()
            else None
        )
        done = bool(data.get("done"))
        reason_raw = data.get("done_reason")
        reason = (
            str(reason_raw).strip()
            if isinstance(reason_raw, str) and reason_raw.strip()
            else None
        )
        return BrainResult(reply=reply, done=done, done_reason=reason)


def _parse_json(text: str) -> dict[str, Any] | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
