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
from core.vk_throttle import vk_post

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
LIKE_WITH_MSG_TEXT = "2"   # «лайк с сообщением» в numeric-меню VK Леонардо
SKIP_TEXT = "3"
HAMMER_STEP_1 = "4"   # выход в главное меню
HAMMER_STEP_2 = "1"   # из главного меню в просмотр анкет
# В диалоге «Ты понравился N девушке, показать её?» утвердительная кнопка
# обычно "1". Если в keyboard нашлось что-то с keyword «показать» — используем
# её label; иначе fallback вот на это.
SHOW_FALLBACK_TEXT = "1"

# Сколько ждать после "2", чтобы Леонардо успел спросить текст сообщения.
MESSAGE_PROMPT_DELAY_SEC = 1.5
# Маркер приглашения Леонардо ввести текст лайка-с-сообщением (ответ на "2").
# Пока его не увидим — текст НЕ шлём: иначе он улетит в экран анкеты как
# неверная команда и рассинхронит весь цикл лайков.
MSG_PROMPT_MARKER = "напиши сообщение"
# Сколько раз опросить входящие, ожидая это приглашение.
MSG_PROMPT_POLLS = 3

POLL_INTERVAL_SEC = 1.5
RESPONSE_TIMEOUT_SEC = 25
HAMMER_DELAY_SEC = 3.0

# Сколько последних сообщений тянуть в scan_history_for_incoming.
SCAN_LOOKBACK = 100

# После «1» (show) на mutual_notification ждём, чтобы Леонардо успел
# прислать и анкету, и служебное меню — потом забираем БАТЧ и ищем нужное.
MUTUAL_SETTLE_SEC = 2.5
MUTUAL_BATCH_LIMIT = 10


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

            if not has_media and likes_recognizer.is_exhausted(msg.get("text") or ""):
                log.info("VK: дневной лимит лайков, останавливаюсь")
                return None

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
            # State мутируется кликом «1», поэтому возвращаем True всегда —
            # outer loop не должен трактовать оригинальный текст как dismiss.
            button_texts = self._extract_button_texts(msg)
            show_btn = (
                likes_recognizer.pick_show_button(button_texts)
                or SHOW_FALLBACK_TEXT
            )
            log.info("VK mutual-notification: отправляю %r", show_btn)
            await self._send_text(show_btn)
            # Леонардо обычно шлёт пачкой: анкета-мэтч + служебное меню.
            # Берём батч и выбираем нужное — НЕ полагаемся на «самое свежее».
            await asyncio.sleep(MUTUAL_SETTLE_SEC)
            new_msgs = await self._fetch_new_inbound(limit=MUTUAL_BATCH_LIMIT)
            if not new_msgs:
                log.warning("VK mutual: бот не ответил после 'show'")
                return True
            self._last_seen_id = max(
                m.get("id", 0) or 0 for m in new_msgs
            )
            match_msg = next(
                (m for m in new_msgs
                 if likes_recognizer.is_mutual_match(m.get("text") or "")),
                None,
            )
            if match_msg is None:
                # Запасной: первое с фото-аттачем (вдруг текст без ключевых слов).
                match_msg = next(
                    (m for m in new_msgs if any(
                        a.get("type") == "photo"
                        for a in (m.get("attachments") or [])
                    )),
                    None,
                )
            if match_msg is None:
                log.warning(
                    "VK mutual: не нашёл match среди %d новых: %r",
                    len(new_msgs),
                    [(m.get("id"), (m.get("text") or "")[:60]) for m in new_msgs],
                )
                return True
            saved = await self._save_mutual_match(match_msg)
            log.info(
                "VK mutual %s id=%s", "saved" if saved else "skipped",
                match_msg.get("id"),
            )
            # self.like() не зовём: «1» уже отработало как подтверждение лайка.
            return True

        return False

    async def _fetch_new_inbound(self, limit: int = 10) -> list[dict[str, Any]]:
        """Все входящие из чата с id > self._last_seen_id, по возрастанию id.
        last_seen_id здесь НЕ обновляем — это делает caller."""
        data = await self._api(
            "messages.getHistory", peer_id=self._peer_id, count=limit
        )
        items = (data or {}).get("items") or []
        out: list[dict[str, Any]] = []
        for m in items:
            if m.get("out", 0) != 0:
                continue
            mid = m.get("id")
            if mid is None:
                continue
            if self._last_seen_id is None or mid > self._last_seen_id:
                out.append(m)
        out.sort(key=lambda m: m.get("id") or 0)
        return out

    async def _save_mutual_match(self, msg: dict[str, Any]) -> bool:
        """Сохранить mutual-подтверждение в пул. В отличие от _parse_profile
        не требует фото — bio это сам текст. Защита: требуется хотя бы один
        признак профиля (match-фраза, фото или URL), иначе откажемся —
        чтобы случайно не записать сервисное меню Леонардо."""
        text = (msg.get("text") or "").strip()
        attachments = msg.get("attachments") or []
        if not text and not attachments:
            return False
        external_id = str(msg.get("id") or "")
        if not external_id:
            return False
        has_photo = any(a.get("type") == "photo" for a in attachments)
        url = (
            likes_recognizer.extract_profile_url(text)
            or self._url_from_attachments(msg)
        )
        if not (likes_recognizer.is_mutual_match(text) or has_photo or url):
            log.info(
                "VK mutual_match: пропускаю — нет признаков профиля: %r",
                text[:80],
            )
            return False
        photos: list[bytes] = []
        for att in attachments:
            if att.get("type") == "photo":
                p = await self._download_photo(att.get("photo") or {})
                if p:
                    photos.append(p)
        profile = Profile(
            source=self.name,
            external_id=external_id,
            bio=text,
            photos=photos,
        )
        return await likes_pool.save_profile(profile, "mutual", url)

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

    async def like(self, message: str | None = None) -> None:
        if message:
            log.info("VK: лайк-с-сообщением — шлю %r и жду приглашение",
                     LIKE_WITH_MSG_TEXT)
            await self._send_text(LIKE_WITH_MSG_TEXT)
            # Подтверждаем, что Леонардо реально перешёл в режим ввода
            # сообщения, и ТОЛЬКО тогда шлём текст. Иначе при асинхронных
            # вставках Леонардо («Ты понравился…», промо) "2" не открывает
            # ввод, текст уходит в экран анкеты как неверная команда и весь
            # цикл рассинхронивается.
            new_msgs: list[dict[str, Any]] = []
            for _ in range(MSG_PROMPT_POLLS):
                await asyncio.sleep(MESSAGE_PROMPT_DELAY_SEC)
                batch = await self._fetch_new_inbound(limit=MUTUAL_BATCH_LIMIT)
                if batch:
                    new_msgs = batch
                if any(MSG_PROMPT_MARKER in (m.get("text") or "").lower()
                       for m in new_msgs):
                    break
            if not any(MSG_PROMPT_MARKER in (m.get("text") or "").lower()
                       for m in new_msgs):
                # Десинк: приглашения нет. Текст НЕ шлём и last_seen НЕ
                # двигаем — пусть next_profile сам разберёт/задисмиссит
                # пришедшие экраны на следующем витке.
                log.warning(
                    "VK: после %r не пришло «%s» (пришло: %r) — текст не шлю",
                    LIKE_WITH_MSG_TEXT, MSG_PROMPT_MARKER,
                    [(m.get("text") or "")[:40] for m in new_msgs],
                )
                return
            # Приглашение есть — потребляем эти сообщения и шлём текст.
            self._last_seen_id = max(m.get("id", 0) or 0 for m in new_msgs)
            try:
                await self._send_text(message)
            except Exception:
                log.exception("VK: не удалось отправить текст сообщения")
            return
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
        """Прочесть последние SCAN_LOOKBACK сообщений и сохранить в пул:
        - incoming-лайки (классифицируется через classify())
        - mutual-match-подтверждения (через is_mutual_match() — обычно
          text-only «Есть взаимная симпатия! …»).
        Действий не совершаем — только запись с дедупом по msg id."""
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
            is_match = likes_recognizer.is_mutual_match(text)
            diag_kind = kind or ("mutual_match" if is_match else None)
            diag.append({
                "id": msg.get("id"),
                "has_media": has_media,
                "kind": diag_kind,
                "text": text[:120].replace("\n", " "),
            })

            if kind == "incoming":
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
            elif is_match:
                if await self._save_mutual_match(msg):
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
        data = await vk_post(self._http, method, params)
        if data is None:
            return None
        if "error" in data:
            err = data["error"]
            log.error(
                "VK API error in %s: code=%s msg=%s",
                method, err.get("error_code"), err.get("error_msg"),
            )
            return None
        return data.get("response")
