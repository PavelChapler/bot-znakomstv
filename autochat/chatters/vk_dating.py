"""Chatter для «VK Знакомства»: переписка с мэтчами через мессенджер
бэкенда dating.vk.ru (messenger.*).

В отличие от Леонардо (api.vk.com/messages) тут собственный протокол:
адресация по dating-`user_id`, msg_id — UUID-строка, текст в `content`,
своя/её реплика — флаг `is_my`. Авторизация — та же, что у source
(`sources.vk_dating`): launch_url → auth.signIn → сессионный `_token`.
Source и chatter держат отдельные клиенты/сессии (как в Леонардо).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import httpx

from autochat.chatters.base import Chatter
from autochat.models import ConvMessage
from config import load
from sources.vk_dating import (
    AGENT_TEMPLATE,
    API_BASE,
    API_V,
    APP_ORIGIN,
    MIN_INTERVAL_SEC,
    USER_AGENT,
)

log = logging.getLogger(__name__)

SCREEN_CHATS = '{"pageId":"/chats","params":{}}'
FETCH_LIMIT = 50
# Префикс profile_url для vk_dating-мэтчей: см. sources.vk_dating scan.
PEER_PREFIX = "vkdating:"


class VKDatingChatter(Chatter):
    name = "vk_dating"

    def __init__(self) -> None:
        cfg = load()
        if not cfg.vk_dating_launch_url:
            raise RuntimeError("VK_DATING_LAUNCH_URL не заполнен в .env")
        self._launch_url = cfg.vk_dating_launch_url
        import re
        m = re.search(r"vk_user_id=(\d+)", self._launch_url)
        self._uid = m.group(1) if m else "0"
        self._agent = cfg.vk_dating_agent or AGENT_TEMPLATE.format(vkid=self._uid)
        self._http: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._session: str | None = None
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def start(self) -> None:
        if self._http is not None:
            return
        self._http = httpx.AsyncClient(timeout=20.0, headers={
            "Origin": APP_ORIGIN,
            "Referer": APP_ORIGIN + "/",
            "User-Agent": USER_AGENT,
        })
        await self._sign_in()

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _sign_in(self) -> None:
        self._session = f"{self._uid}_{int(time.time() * 1000)}"
        data = await self._call("auth.signIn", {
            "launch_url": self._launch_url,
            "_v": API_V,
            "_agent": self._agent,
            "_session": self._session,
        })
        if not isinstance(data, dict) or "token" not in data:
            err = data.get("error") if isinstance(data, dict) else None
            if "launch_url" in str(err):
                raise RuntimeError(
                    "VK Знакомства: launch_url протух/невалиден — обнови "
                    "VK_DATING_LAUNCH_URL в .env."
                )
            raise RuntimeError(f"VK Знакомства chatter: signIn не удался: {err or data}")
        self._token = data["token"]

    # ───────── Chatter API ─────────

    async def resolve_peer(self, profile_url: str) -> str | None:
        s = (profile_url or "").strip()
        if s.startswith(PEER_PREFIX):
            s = s[len(PEER_PREFIX):]
        return s if s.isdigit() else None

    async def can_write(self, profile_url: str) -> tuple[bool, str | None]:
        peer = await self.resolve_peer(profile_url)
        if not peer:
            return False, f"bad_url: {profile_url!r}"
        data = await self._call("messenger.getChat", {
            "user_id": peer, **self._auth_fields(),
        })
        if not isinstance(data, dict):
            return False, "getChat failed"
        if data.get("error"):
            return False, f"getChat error: {data.get('error')}"
        if not data.get("chat"):
            return False, "no_chat (не мэтч/анмэтч?)"
        return True, None

    async def send(self, peer: str, text: str) -> str | None:
        data = await self._call("messenger.send", {
            "user_id": peer,
            "text": text,
            "is_ignore_advice": "false",
            "attachments": "[]",
            **self._auth_fields(),
        })
        if not isinstance(data, dict) or data.get("error"):
            log.error("VK Знакомства send error: %r",
                      (data or {}).get("error") if isinstance(data, dict) else data)
            return None
        msg = data.get("message") or {}
        mid = msg.get("id")
        return str(mid) if mid else None

    async def fetch_new_replies(
        self, peer: str, after_msg_id: str | None
    ) -> list[ConvMessage]:
        msgs = await self._history(peer, FETCH_LIMIT)
        out: list[ConvMessage] = []
        seen_after = after_msg_id is None
        for m in msgs:
            mid = m.get("id")
            if not seen_after:
                if mid == after_msg_id:
                    seen_after = True
                continue
            text = self._content_text(m)
            if not text:
                continue
            out.append(ConvMessage(
                id=0, conversation_id=0,
                ts=self._iso_ts(m.get("created_at")),
                role="us" if m.get("is_my") else "her",
                text=text, external_msg_id=str(mid),
            ))
        # after не найден в окне (диалог длиннее лимита / старый id) — не
        # рискуем дублями, ждём следующего тика.
        if after_msg_id is not None and not seen_after:
            return []
        out.sort(key=lambda x: x.ts)
        return out

    async def fetch_full_history(
        self, peer: str, limit: int = 50
    ) -> list[ConvMessage]:
        msgs = await self._history(peer, limit)
        out: list[ConvMessage] = []
        for m in msgs:
            mid = m.get("id")
            text = self._content_text(m)
            if not mid or not text:
                continue
            out.append(ConvMessage(
                id=0, conversation_id=0,
                ts=self._iso_ts(m.get("created_at")),
                role="us" if m.get("is_my") else "her",
                text=text, external_msg_id=str(mid),
            ))
        out.sort(key=lambda x: x.ts)
        return out

    # ───────── helpers ─────────

    async def _history(self, peer: str, limit: int) -> list[dict[str, Any]]:
        data = await self._call("messenger.getHistory", {
            "user_id": peer, "limit": limit, "offset": 0,
            **self._auth_fields(),
        })
        if not isinstance(data, dict):
            return []
        return data.get("messages") or []

    @staticmethod
    def _content_text(m: dict[str, Any]) -> str:
        """Текст реплики. Не-текстовый контент помечаем, чтобы brain видел
        факт сообщения (войсы/фото в dating-мессенджере без удобного аудио)."""
        content = (m.get("content") or "").strip()
        ctype = m.get("content_type") or "text"
        if ctype != "text" and not content:
            return f"[{ctype}]"
        return content

    @staticmethod
    def _iso_ts(s: str | None) -> int:
        if not s:
            return 0
        try:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
        except Exception:
            return 0

    def _auth_fields(self) -> dict[str, Any]:
        return {
            "_token": self._token or "",
            "_v": API_V,
            "_agent": self._agent,
            "_session": self._session or "",
            "_screen": SCREEN_CHATS,
        }

    async def _call(self, method: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        assert self._http is not None
        await self._throttle()
        files = {k: (None, str(v)) for k, v in fields.items()}
        try:
            resp = await self._http.post(f"{API_BASE}/{method}", files=files)
        except Exception:
            log.exception("VK Знакомства chatter: сетевой сбой %s", method)
            return None
        try:
            data = resp.json()
        except Exception:
            log.error("VK Знакомства chatter: %s не JSON (HTTP %d)",
                      method, resp.status_code)
            return None
        if isinstance(data, dict) and data.get("error"):
            log.error("VK Знакомства chatter: ошибка %s: %r",
                      method, data.get("error"))
        return data

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = MIN_INTERVAL_SEC - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
