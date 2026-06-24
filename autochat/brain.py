"""Claude-обёртка для генерации сообщений автопереписки.

На Anthropic Claude переведена ТОЛЬКО автопереписка. Скоринг анкет,
сообщения к лайкам (`scorer/`) и расшифровка голосовых (`transcribe.py`)
остаются на Gemini — у Claude нет аудио-входа, поэтому войсы по-прежнему
расшифровывает Gemini, а текст уже отдаётся сюда.

Stateless: каждый вызов отдаёт системный промпт + style + goal + полную
историю. Источник истины — БД, in-memory state модели не используем —
после рестарта потеряется, а БД переживёт.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic

from autochat.prompts import (
    SYSTEM,
    build_continue_user_message,
    build_opener_user_message,
    build_reply_user_message,
)
from config import load

log = logging.getLogger(__name__)

# Реплики короткие (1-3 предложения) + маленький JSON — 1024 с запасом.
MAX_TOKENS = 1024
# Лёгкая вариативность, чтобы реплики не были шаблонными. Sonnet 4.6 и
# Haiku 4.5 ещё принимают temperature (в отличие от Opus 4.7+/Fable 5).
TEMPERATURE = 0.8

# Structured outputs: гарантируем валидный JSON нужной формы. Поля строковые
# (пустая строка = «нечего слать» / «нет причины») — так обходимся без
# nullable-типов, которые structured outputs не гарантирует.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {
            "type": "string",
            "description": (
                "текст следующего сообщения; пустая строка, если done "
                "и слать нечего"
            ),
        },
        "done": {"type": "boolean"},
        "done_reason": {
            "type": "string",
            "description": "краткая причина, если done=true, иначе пустая строка",
        },
    },
    "required": ["reply", "done", "done_reason"],
    "additionalProperties": False,
}


@dataclass
class BrainResult:
    reply: str | None
    done: bool
    done_reason: str | None


class ClaudeChatBrain:
    def __init__(self) -> None:
        cfg = load()
        if not cfg.anthropic_api_key:
            raise RuntimeError(
                "Не задан ANTHROPIC_API_KEY (в окружении или .env)"
            )
        self.model = cfg.anthropic_model
        self.client = AsyncAnthropic(api_key=cfg.anthropic_api_key)

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
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                system=SYSTEM,
                messages=[{"role": "user", "content": user_text}],
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": _RESPONSE_SCHEMA,
                    }
                },
            )
        except Exception as e:
            log.exception("Claude autochat error")
            return BrainResult(
                reply=None, done=True, done_reason=f"claude error: {e}"
            )

        # Контент-модерация: stop_reason="refusal" → content может не
        # соответствовать схеме, читать его нельзя. Закрываем диалог.
        if resp.stop_reason == "refusal":
            return BrainResult(
                reply=None, done=True, done_reason="refusal (safety)"
            )

        text = next(
            (b.text for b in resp.content if getattr(b, "type", None) == "text"),
            "",
        ).strip()
        if not text:
            return BrainResult(
                reply=None,
                done=True,
                done_reason=f"empty response (stop={resp.stop_reason})",
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
