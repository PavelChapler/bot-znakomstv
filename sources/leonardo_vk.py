"""Адаптер «Леонардо Дайвинчик в VK».

Работает от лица VK-пользователя по user access token. Никаких внешних
VK-либ — общается напрямую с VK API через httpx. Архитектурно повторяет
sources/leonardo_tg.py: те же 5 методов DatingSource, тот же dismiss-конвейер
для рекламы/интерстициалов.

Чтобы добавить аналогичный VK-бот (например, VK Знакомства) — этот файл
почти полностью повторяет, меняется только VK_LEONARDO_GROUP и, возможно,
эмодзи кнопок.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from config import load
from core import likes_pool
from core.models import Profile
from sources import dismiss, likes_recognizer
from sources.base import DatingSource
from sources.media import extract_video_frames

log = logging.getLogger(__name__)

VK_API_BASE = "https://api.vk.com/method"
VK_API_VERSION = "5.131"

# VK Leonardo не отдаёт keyboard через messages.getHistory, и взаимодействие
# с ним идёт цифрами:
#   в режиме просмотра анкет: 1 — лайк, 3 — дизлайк
#   из любого подменю: 4 — вернуться в главное меню
#   из главного меню: 1 — «Смотреть анкеты»
# Если бот сменит схему — поменяй цифры здесь.
LIKE_TEXT = "1"
SKIP_TEXT = "3"
HAMMER_STEP_1 = "4"   # выход в главное меню
HAMMER_STEP_2 = "1"   # из главного меню в просмотр анкет
# В диалоге «Ты понравился N девушке, показать её?» утвердительная кнопка
# обычно "1". Если в keyboard нашлось что-то с keyword «показать» — используем
# её label; иначе fallback вот на это.
SHOW_FALLBACK_TEXT = "1"

POLL_INTERVAL_SEC = 1.5
RESPONSE_TIMEOUT_SEC = 25
HAMMER_DELAY_SEC = 3.0

# Сколько последних сообщений тянуть в scan_history_for_incoming.
SCAN_LOOKBACK = 100


class LeonardoVKSource(DatingSource):
    name = "leonardo_vk"
    title = "Леонардо в VK"

    def __init__(self) -> None:
        cfg = load()
        if not cfg.vk_access_token:
            raise RuntimeError("VK_ACCESS_TOKEN не заполнен в .env")
        if not cfg.vk_leonardo_group:
            raise RuntimeError("VK_LEONARDO_GROUP не заполнен в .env")
        self._token = cfg.vk_access_token
        self._screen_name = cfg.vk_leonardo_group
        self._http: httpx.AsyncClient | None = None
        self._peer_id: int | None = None
        self._last_seen_id: int | None = None

    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=15.0)
        resolved = await self._api("utils.resolveScreenName", screen_name=self._screen_name)
        if not resolved or "object_id" not in resolved:
            raise RuntimeError(f"VK: не нашёл сообщество/пользователя {self._screen_name!r}")
        obj_id = int(resolved["object_id"])
        obj_type = resolved.get("type", "group")
        # Для группы peer_id отрицательный (это особенность VK API).
        self._peer_id = -obj_id if obj_type == "group" else obj_id
        log.info(
            "VK Leonardo connected: %s (type=%s, peer_id=%s)",
            self._screen_name, obj_type, self._peer_id,
        )
        self._last_seen_id = None

    async def next_profile(self) -> Profile | None:
        max_no_media_streak = 5
        max_unparseable_streak = 5
        no_media_streak = 0
        unparseable_streak = 0

        while (
            no_media_streak < max_no_media_streak
            and unparseable_streak < max_unparseable_streak
        ):
            msg = await self._wait_for_new_message()
            if msg is None:
                return None

            attachments = msg.get("attachments") or []
            has_media = any(
                a.get("type") in ("photo", "video", "doc") for a in attachments
            )

            handled = await self._try_handle_likes(msg, has_media)
            if handled:
                no_media_streak = 0
                unparseable_streak = 0
                continue

            if has_media:
                no_media_streak = 0
                profile = await self._parse_profile(msg)
                if profile is not None:
                    return profile
                unparseable_streak += 1
                log.warning(
                    "VK: media есть, но не распарсилось (streak %d/%d) — auto-skip",
                    unparseable_streak, max_unparseable_streak,
                )
                await self.skip()
                continue

            unparseable_streak = 0
            no_media_streak += 1
            log.info(
                "VK: non-media (streak %d/%d): %r — пробую dismiss",
                no_media_streak, max_no_media_streak,
                (msg.get("text") or "")[:100],
            )
            button_texts = self._extract_button_texts(msg)
            dismissed = await dismiss.attempt_dismiss(
                msg_text=msg.get("text") or "",
                button_texts=button_texts,
                click_by_text=self._click_button_by_text,
                hammer=self._hammer,
                source_name=self.name,
            )
            if not dismissed:
                log.warning("VK: не смог закрыть экран — стоп")
                return None

        log.info(
            "VK: stopping next_profile: no_media_streak=%d, unparseable_streak=%d",
            no_media_streak, unparseable_streak,
        )
        return None

    async def _try_handle_likes(
        self, msg: dict[str, Any], has_media: bool
    ) -> bool:
        """Распознать likes-уведомление, сохранить в пул, ответить."""
        text = msg.get("text") or ""
        kind = likes_recognizer.classify(text, has_media)
        if kind is None:
            return False

        if kind == "incoming":
            profile = await self._parse_profile(msg)
            if profile is None:
                log.warning("VK incoming-like: анкета не распарсилась")
                return False
            url = (
                likes_recognizer.extract_profile_url(text)
                or self._url_from_attachments(msg)
            )
            saved = await likes_pool.save_profile(profile, "incoming", url)
            log.info(
                "VK incoming like %s id=%s",
                "saved" if saved else "duplicate", profile.external_id,
            )
            await self.like()
            return True

        if kind == "mutual_notification":
            button_texts = self._extract_button_texts(msg)
            show_btn = (
                likes_recognizer.pick_show_button(button_texts)
                or SHOW_FALLBACK_TEXT
            )
            log.info("VK mutual-notification: отправляю %r", show_btn)
            await self._send_text(show_btn)
            profile_msg = await self._wait_for_new_message()
            if profile_msg is None:
                log.warning("VK mutual-notification: бот не прислал анкету")
                return False
            profile = await self._parse_profile(profile_msg)
            if profile is None:
                log.warning("VK mutual-notification: анкета не распарсилась")
                return False
            url = (
                likes_recognizer.extract_profile_url(profile_msg.get("text") or "")
                or self._url_from_attachments(profile_msg)
            )
            saved = await likes_pool.save_profile(profile, "mutual", url)
            log.info(
                "VK mutual like %s id=%s",
                "saved" if saved else "duplicate", profile.external_id,
            )
            await self.like()
            return True

        return False

    @staticmethod
    def _url_from_attachments(msg: dict[str, Any]) -> str | None:
        """Достать vk.com/id<N> из owner_id вложенного фото/видео."""
        for att in msg.get("attachments") or []:
            kind = att.get("type")
            obj = att.get(kind) if kind else None
            if not isinstance(obj, dict):
                continue
            owner_id = obj.get("owner_id")
            if isinstance(owner_id, int) and owner_id > 0:
                return f"https://vk.com/id{owner_id}"
        return None

    async def like(self) -> None:
        log.info("VK: like (отправляю %r)", LIKE_TEXT)
        await self._send_text(LIKE_TEXT)

    async def skip(self) -> None:
        log.info("VK: skip (отправляю %r)", SKIP_TEXT)
        await self._send_text(SKIP_TEXT)

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def scan_history_for_incoming(self) -> dict[str, Any]:
        """Прочесть последние SCAN_LOOKBACK сообщений и сохранить incoming-
        лайки. Действий не совершаем."""
        saved = 0
        duplicates = 0
        diag: list[dict[str, Any]] = []
        data = await self._api(
            "messages.getHistory", peer_id=self._peer_id, count=SCAN_LOOKBACK
        )
        items = list((data or {}).get("items") or [])
        items.reverse()  # API даёт от новых к старым; идём хронологически
        for msg in items:
            if msg.get("out"):
                continue
            text = msg.get("text") or ""
            attachments = msg.get("attachments") or []
            has_media = any(
                a.get("type") in ("photo", "video", "doc") for a in attachments
            )
            kind = likes_recognizer.classify(text, has_media)
            diag.append({
                "id": msg.get("id"),
                "has_media": has_media,
                "kind": kind,
                "text": text[:120].replace("\n", " "),
            })
            if kind != "incoming":
                continue
            profile = await self._parse_profile(msg)
            if profile is None:
                continue
            url = (
                likes_recognizer.extract_profile_url(text)
                or self._url_from_attachments(msg)
            )
            if await likes_pool.save_profile(profile, "incoming", url):
                saved += 1
            else:
                duplicates += 1
        return {"saved": saved, "duplicates": duplicates, "diag": diag}

    # ───────── helpers ─────────

    async def _hammer(self) -> None:
        """Возврат в режим просмотра анкет VK Леонардо: цифра "4" (вернуться
        в главное меню) → пауза → "1" («Смотреть анкеты»)."""
        await self._send_text(HAMMER_STEP_1)
        await asyncio.sleep(HAMMER_DELAY_SEC)
        await self._send_text(HAMMER_STEP_2)

    async def _click_button_by_text(self, target: str) -> bool:
        """В VK «нажать кнопку» = отправить текст её label."""
        try:
            await self._send_text(target)
            return True
        except Exception:
            log.exception("VK click_by_text failed")
            return False

    @staticmethod
    def _extract_button_texts(msg: dict[str, Any]) -> list[str]:
        out: list[str] = []
        kb = msg.get("keyboard") or {}
        for row in kb.get("buttons", []):
            for btn in row:
                label = ((btn.get("action") or {}).get("label") or "").strip()
                if label:
                    out.append(label)
        return out

    async def _parse_profile(self, msg: dict[str, Any]) -> Profile | None:
        text = (msg.get("text") or "").strip()
        photos: list[bytes] = []

        for att in msg.get("attachments") or []:
            kind = att.get("type")
            if kind == "photo":
                photo_bytes = await self._download_photo(att.get("photo") or {})
                if photo_bytes:
                    photos.append(photo_bytes)
            elif kind in ("video", "doc"):
                video_bytes = await self._download_video_or_doc(att)
                if video_bytes:
                    photos.extend(extract_video_frames(video_bytes, n=3))

        if not photos:
            return None

        return Profile(
            source=self.name,
            external_id=str(msg.get("id", "")),
            bio=text,
            photos=photos,
        )

    async def _download_photo(self, photo: dict[str, Any]) -> bytes | None:
        sizes = photo.get("sizes") or []
        if not sizes:
            return None
        # самый большой по площади
        largest = max(
            sizes,
            key=lambda s: (s.get("width") or 0) * (s.get("height") or 0),
        )
        url = largest.get("url")
        if not url:
            return None
        try:
            assert self._http is not None
            resp = await self._http.get(url)
            if resp.status_code == 200:
                return resp.content
            log.warning("VK photo download HTTP %d", resp.status_code)
        except Exception:
            log.exception("VK photo download failed")
        return None

    async def _download_video_or_doc(self, att: dict[str, Any]) -> bytes | None:
        """Видео в VK скачивается не так просто (нужны отдельные права),
        для doc'ов (gif и пр.) URL обычно прямо в attachment'е."""
        if att.get("type") == "doc":
            url = (att.get("doc") or {}).get("url")
        else:
            url = None
        if not url:
            return None
        try:
            assert self._http is not None
            resp = await self._http.get(url)
            if resp.status_code == 200:
                return resp.content
        except Exception:
            log.exception("VK doc download failed")
        return None

    async def _wait_for_new_message(self) -> dict[str, Any] | None:
        deadline = time.monotonic() + RESPONSE_TIMEOUT_SEC
        while time.monotonic() < deadline:
            data = await self._api("messages.getHistory", peer_id=self._peer_id, count=1)
            if data and data.get("items"):
                msg = data["items"][0]
                if msg.get("out", 0) == 0:
                    msg_id = msg.get("id")
                    if self._last_seen_id is None or (
                        msg_id is not None and msg_id > self._last_seen_id
                    ):
                        self._last_seen_id = msg_id
                        return msg
            await asyncio.sleep(POLL_INTERVAL_SEC)
        return None

    async def _send_text(self, text: str) -> None:
        await self._api(
            "messages.send",
            peer_id=self._peer_id,
            message=text,
            random_id=random.randint(1, 2**31 - 1),
        )

    async def _api(self, method: str, **params: Any) -> dict[str, Any] | None:
        assert self._http is not None
        params = {**params, "access_token": self._token, "v": VK_API_VERSION}
        try:
            resp = await self._http.post(f"{VK_API_BASE}/{method}", data=params)
            data = resp.json()
        except Exception:
            log.exception("VK call %s failed", method)
            return None
        if "error" in data:
            err = data["error"]
            log.error(
                "VK API error in %s: code=%s msg=%s",
                method, err.get("error_code"), err.get("error_msg"),
            )
            return None
        return data.get("response")
