"""Chatter для Leonardo VK: ЛС напрямую пользователю vk.com/idN.

Использует свой httpx-клиент с user access token из .env. VK API
stateless — без конкуренции с LeonardoVKSource.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any

import httpx

from autochat.chatters.base import Chatter
from autochat.models import ConvMessage
from config import load

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
        can = info.get("can_write_private_message")
        if can is None:
            # privacy не возвращён — пробуем, разберёмся по send
            return True, None
        if not can:
            return False, "cannot_write"
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
            new.append(ConvMessage(
                id=0, conversation_id=0,
                ts=m.get("date") or 0,
                role="her",
                text=m.get("text") or "",
                external_msg_id=str(mid),
            ))
        new.sort(key=lambda x: x.ts)
        return new

    async def _api(
        self,
        method: str,
        *,
        return_raw: bool = False,
        **params: Any,
    ) -> Any:
        assert self._http is not None, "call start() first"
        params = {**params, "access_token": self._token, "v": VK_API_VERSION}
        try:
            resp = await self._http.post(f"{VK_API_BASE}/{method}", data=params)
            data = resp.json()
        except Exception:
            log.exception("VK call %s failed", method)
            return None if not return_raw else {}
        if "error" in data:
            err = data["error"]
            log.error(
                "VK API error in %s: code=%s msg=%s",
                method, err.get("error_code"), err.get("error_msg"),
            )
            return data if return_raw else None
        return data.get("response")


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
