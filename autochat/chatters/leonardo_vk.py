"""Chatter для Leonardo VK: ЛС напрямую пользователю vk.com/idN.

Использует свой httpx-клиент с user access token из .env. VK API
stateless — без конкуренции с LeonardoVKSource.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

import httpx

from autochat import config as ac_config
from autochat.chatters.base import Chatter
from autochat.models import ConvMessage
from autochat.transcribe import transcribe_audio
from config import load
from core.vk_throttle import vk_post

log = logging.getLogger(__name__)

VK_API_BASE = "https://api.vk.com/method"
VK_API_VERSION = "5.131"
FETCH_LIMIT = 30


class VKChatter(Chatter):
    name = "leonardo_vk"

    def __init__(self) -> None:
        cfg = load()
        if not cfg.vk_access_token:
            raise RuntimeError("VK_ACCESS_TOKEN не заполнен в .env")
        self._token = cfg.vk_access_token
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15.0)

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def can_write(self, profile_url: str) -> tuple[bool, str | None]:
        uid = await self.resolve_peer(profile_url)
        if not uid:
            return False, f"bad_url: {profile_url!r}"

        # 1. Базовая проверка: жив ли аккаунт + глобальный privacy-флаг.
        data = await self._api(
            "users.get",
            user_ids=uid,
            fields="can_write_private_message",
        )
        if not data:
            return False, "users.get failed"
        info = data[0] if isinstance(data, list) and data else None
        if not info:
            return False, "no info"
        if info.get("deactivated"):
            return False, f"deactivated:{info['deactivated']}"
        can_global = info.get("can_write_private_message")

        # 2. Авторитет: messages.getConversationsById. Если уже есть диалог
        # (она писала первой / мы уже общались), VK ставит can_write.allowed,
        # независимо от закрытости/приватности её страницы.
        conv_data = await self._api(
            "messages.getConversationsById", peer_ids=int(uid)
        )
        items = (conv_data or {}).get("items") or [] if isinstance(conv_data, dict) else []
        if items:
            cw = (items[0].get("can_write") or {})
            allowed = cw.get("allowed")
            reason = cw.get("reason")
            log.info(
                "VK can_write conv: peer=%s allowed=%s reason=%s global=%s",
                uid, allowed, reason, can_global,
            )
            if allowed:
                return True, None
            # Диалог есть, но писать запрещено (заблокированы и т.п.).
            return False, f"conv_blocked:reason={reason}"

        # 3. Диалога нет — полагаемся на глобальный privacy.
        log.info(
            "VK can_write no_conv: peer=%s global=%s", uid, can_global,
        )
        if can_global is False:
            return False, "cannot_write_first (closed profile)"
        # can_global True / None / отсутствует — пробуем.
        return True, None

    async def resolve_peer(self, profile_url: str) -> str | None:
        uid = _parse_vk_id(profile_url)
        if uid:
            return uid
        screen = _parse_vk_screen_name(profile_url)
        if not screen:
            return None
        data = await self._api("utils.resolveScreenName", screen_name=screen)
        if not data or data.get("type") != "user":
            return None
        oid = data.get("object_id")
        return str(oid) if isinstance(oid, int) else None

    async def send(self, peer: str, text: str) -> str | None:
        data = await self._api(
            "messages.send",
            peer_id=int(peer),
            message=text,
            random_id=random.randint(1, 2**31 - 1),
            return_raw=True,
        )
        if isinstance(data, dict):
            err = data.get("error") or {}
            code = err.get("error_code")
            if code in (901, 902, 7):
                log.info("VK send blocked (code=%s) for %s", code, peer)
                return None
            if code == 9:
                log.warning("VK send FloodWait-like (code=9) for %s", peer)
                return None
            if code:
                log.error("VK send error %s: %s", code, err.get("error_msg"))
                return None
        if isinstance(data, int):
            return str(data)
        if isinstance(data, dict) and isinstance(data.get("response"), int):
            return str(data["response"])
        return None

    async def fetch_new_replies(
        self, peer: str, after_msg_id: str | None
    ) -> list[ConvMessage]:
        after_int = int(after_msg_id) if after_msg_id else None
        data = await self._api(
            "messages.getHistory", peer_id=int(peer), count=FETCH_LIMIT
        )
        items = (data or {}).get("items") or []
        new: list[ConvMessage] = []
        for m in items:
            if m.get("out", 0) != 0:
                continue
            mid = m.get("id")
            if mid is None:
                continue
            if after_int is not None and mid <= after_int:
                continue
            text = await self._resolve_text(m)
            if not text:
                continue
            new.append(ConvMessage(
                id=0, conversation_id=0,
                ts=m.get("date") or 0,
                role="her",
                text=text,
                external_msg_id=str(mid),
            ))
        new.sort(key=lambda x: x.ts)
        return new

    async def fetch_full_history(
        self, peer: str, limit: int = 50
    ) -> list[ConvMessage]:
        data = await self._api(
            "messages.getHistory", peer_id=int(peer), count=limit
        )
        items = (data or {}).get("items") or []
        out: list[ConvMessage] = []
        for m in items:
            mid = m.get("id")
            if mid is None:
                continue
            text = await self._resolve_text(m)
            if not text:
                continue
            out.append(ConvMessage(
                id=0, conversation_id=0,
                ts=m.get("date") or 0,
                role="us" if m.get("out") else "her",
                text=text,
                external_msg_id=str(mid),
            ))
        out.sort(key=lambda x: x.ts)
        return out

    async def _resolve_text(self, msg: dict[str, Any]) -> str:
        """Текст реплики: msg['text'] + расшифровки голосовых через
        встроенную VK-транскрипцию. Gemini сюда не привлекается."""
        text = (msg.get("text") or "").strip()
        voice_parts: list[str] = []
        enabled = await ac_config.is_transcribe_voice_enabled()
        msg_id = msg.get("id")
        for att in msg.get("attachments") or []:
            if att.get("type") != "audio_message":
                continue
            am = att.get("audio_message") or {}
            if not enabled:
                voice_parts.append("[голосовое сообщение]")
                continue
            transcript = await self._resolve_vk_transcript(am, msg_id)
            voice_parts.append(
                f"[голосовое] {transcript}" if transcript
                else "[голосовое, не распознано]"
            )
        if text and voice_parts:
            return text + " " + " ".join(voice_parts)
        if voice_parts:
            return " ".join(voice_parts)
        return text

    async def _resolve_vk_transcript(
        self, am: dict[str, Any], msg_id: int | None
    ) -> str | None:
        """Расшифровка голосового.

        VK API в 5.131 не отдаёт поле `transcript` сторонним клиентам с
        user-токеном (проверено логами — keys = access_key, duration,
        id, link_mp3, link_ogg, owner_id, waveform). На случай если VK
        когда-нибудь откатит — проверяем поле один раз. Иначе сразу
        фоллбэк: скачиваем link_ogg и шлём в Gemini Flash.
        """
        ready = _extract_transcript(am)
        if ready is not None:
            return ready

        url = am.get("link_ogg") or am.get("link_mp3")
        if not url:
            log.info("VK voice без link_ogg/link_mp3 (msg_id=%s)", msg_id)
            return None
        mime = "audio/ogg" if url == am.get("link_ogg") else "audio/mpeg"
        audio = await self._download_audio(url)
        if not audio:
            return None
        return await transcribe_audio(audio, mime_type=mime)

    async def _download_audio(self, url: str) -> bytes | None:
        assert self._http is not None
        try:
            resp = await self._http.get(url)
            if resp.status_code == 200:
                return resp.content
            log.warning(
                "VK voice download HTTP %d for %s", resp.status_code, url,
            )
        except Exception:
            log.exception("VK voice download failed %s", url)
        return None

    async def _api(
        self,
        method: str,
        *,
        return_raw: bool = False,
        **params: Any,
    ) -> Any:
        assert self._http is not None, "call start() first"
        params = {**params, "access_token": self._token, "v": VK_API_VERSION}
        data = await vk_post(self._http, method, params)
        if data is None:
            return None if not return_raw else {}
        if "error" in data:
            err = data["error"]
            log.error(
                "VK API error in %s: code=%s msg=%s",
                method, err.get("error_code"), err.get("error_msg"),
            )
            return data if return_raw else None
        return data.get("response")


def _extract_transcript(am: dict[str, Any]) -> str | None:
    """Универсальный экстрактор: разные версии VK API кладут текст в
    `transcript` или в `text`; state бывает 'done'/'error' или вовсе
    отсутствует. Возвращает: непустую строку (готово), либо None
    если ещё не готово / error / нечего достать.

    Sentinel ''  (пустая строка) — внешне не отличаем от None, считаем
    «не готово»: всегда возвращаем None.
    """
    state = (am.get("transcript_state") or "").lower()
    if state == "error":
        return None
    text = (am.get("transcript") or am.get("text") or "").strip()
    if text and (state == "done" or not state):
        return text
    return None


_VK_ID_RE = re.compile(r"(?:^|/)id(\d+)\b", re.IGNORECASE)
_VK_SCREEN_RE = re.compile(r"vk\.com/([A-Za-z0-9_.]+)", re.IGNORECASE)


def _parse_vk_id(url: str) -> str | None:
    s = (url or "").strip()
    m = _VK_ID_RE.search(s)
    if m:
        return m.group(1)
    if s.isdigit():
        return s
    return None


def _parse_vk_screen_name(url: str) -> str | None:
    s = (url or "").strip()
    m = _VK_SCREEN_RE.search(s)
    if not m:
        return None
    name = m.group(1)
    if name.lower().startswith("id") and name[2:].isdigit():
        return None  # это id, обработано отдельно
    return name
