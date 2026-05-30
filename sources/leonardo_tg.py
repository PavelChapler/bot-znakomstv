from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from typing import Any

from telethon.tl.custom import Message

from config import load
from core import likes_pool
from core.models import Profile
from core.telethon_conn import get_shared_client
from sources import dismiss, likes_recognizer
from sources.base import DatingSource
from sources.media import extract_video_frames

log = logging.getLogger(__name__)

# Username бота "Леонардо Дайвинчик" в Telegram. При необходимости поправь.
BOT_USERNAME = "leomatchbot"

# Эмодзи, по которым ищем кнопки в клавиатуре Леонардо. Текст самой кнопки
# может содержать что-то вокруг (цифры, слова) — главное, чтобы был один
# из этих символов. Если бот сменит UI на нечто радикально другое — расширь
# эти кортежи.
LIKE_EMOJIS = (
    "❤", "♥", "💕", "💖", "💗", "💓", "💝", "💘", "💞", "💟",
    "❣", "🩷", "💜", "🧡", "💛", "💚", "💙", "🤍", "👍", "🔥",
)
SKIP_EMOJIS = ("👎", "💔", "✖", "❌")
# Кнопка «лайк с текстовым сообщением» в UI Леонардо. 📹 — это видео-
# сообщение, его не берём.
MESSAGE_EMOJIS = ("💌", "✉", "📨", "✍")

# Fallback-текст, если кнопок вообще не нашли в истории.
FALLBACK_LIKE_TEXT = "❤"
FALLBACK_SKIP_TEXT = "👎"

# Сколько ждать после клика «💌», чтобы Леонардо успел спросить текст.
MESSAGE_PROMPT_DELAY_SEC = 1.5

# Сколько последних сообщений сканировать в поисках клавиатуры/inline-кнопок.
BUTTON_LOOKBACK = 10

# Сколько ждать ответа бота с новой анкетой после нашего действия.
RESPONSE_TIMEOUT_SEC = 25
POLL_INTERVAL_SEC = 1.0

# Хаммер для dismiss-логики (последний рубеж): сначала открываем главное
# меню через /myprofile, ждём, чтобы Леонардо успел отрисовать клавиатуру,
# потом отправляем "1" — пункт «Смотреть анкеты».
HAMMER_DELAY_SEC = 1.0

# Сколько последних сообщений просматривать в scan_history_for_incoming.
SCAN_LOOKBACK = 80

# После клика «показать» на mutual_notification ждём, чтобы Леонардо успел
# прислать пачку (анкета-мэтч + меню), потом забираем батч и ищем нужное.
MUTUAL_SETTLE_SEC = 2.5
MUTUAL_BATCH_LIMIT = 10


class LeonardoTGSource(DatingSource):
    name = "leonardo_tg"
    title = "Леонардо в TG"

    def __init__(self) -> None:
        cfg = load()
        if not cfg.telethon_api_id or not cfg.telethon_api_hash:
            raise RuntimeError(
                "Не заполнены TELETHON_API_ID / TELETHON_API_HASH в .env"
            )
        self._cfg = cfg
        # Клиент берём из shared singleton — иначе autochat.TelethonChatter
        # и эта сессия конкурировали бы за один .session файл.
        self.client = None  # type: ignore[assignment]
        self.entity = None
        self._last_seen_id: int | None = None

    async def start(self) -> None:
        self.client = await get_shared_client()
        log.info("Telethon connected (Leonardo TG)")
        self.entity = await self.client.get_entity(BOT_USERNAME)
        # _last_seen_id оставляем None, чтобы на первой итерации забрать
        # сообщение, которое бот уже показывает в режиме просмотра анкет.
        self._last_seen_id = None

    async def next_profile(self) -> Profile | None:
        # - Уведомление про входящий/взаимный лайк → ловим в пул, авто-лайк,
        #   continue (см. _try_handle_likes).
        # - Есть медиа → парсим анкету; если медиа битое — auto-skip.
        # - Нет медиа → реклама/интерстициал/«не понял»; пытаемся аккуратно
        #   закрыть через dismiss (эвристика → кэш → LLM → hammer).
        # - Если dismiss за один раз ничего не нашёл — это реальный тупик,
        #   стопимся, чтобы юзер посмотрел руками.
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

            has_media = bool(
                msg.photo
                or msg.video
                or msg.video_note
                or msg.gif
                or msg.grouped_id
            )

            handled = await self._try_handle_likes(msg, has_media)
            if handled:
                no_media_streak = 0
                unparseable_streak = 0
                continue

            if not has_media and likes_recognizer.is_exhausted(msg.message or ""):
                log.info("TG: дневной лимит лайков, останавливаюсь")
                return None

            if has_media:
                no_media_streak = 0
                profile = await self._parse_profile(msg)
                if profile is not None:
                    return profile
                unparseable_streak += 1
                log.warning(
                    "media present but unparseable in msg id=%d "
                    "(unparseable streak %d/%d) — auto-skip",
                    msg.id, unparseable_streak, max_unparseable_streak,
                )
                await self.skip()
                continue

            unparseable_streak = 0
            no_media_streak += 1
            log.info(
                "non-media from bot (no-media streak %d/%d): %r — пробую dismiss",
                no_media_streak, max_no_media_streak,
                (msg.message or "")[:100],
            )
            button_texts = self._extract_button_texts(msg)
            dismissed = await dismiss.attempt_dismiss(
                msg_text=msg.message or "",
                button_texts=button_texts,
                click_by_text=self._click_button_by_text,
                hammer=self._hammer,
                source_name=self.name,
            )
            if not dismissed:
                log.warning(
                    "не смог закрыть экран (ни эвристика, ни кэш, ни LLM, "
                    "ни хаммер не сработали) — стоп"
                )
                return None

        log.info(
            "stopping next_profile: no_media_streak=%d, unparseable_streak=%d",
            no_media_streak, unparseable_streak,
        )
        return None

    async def _try_handle_likes(self, msg: Message, has_media: bool) -> bool:
        """Распознать likes-уведомление и сохранить в пул. True — обработано."""
        kind = likes_recognizer.classify(msg.message or "", has_media)
        if kind is None:
            return False

        if kind == "incoming":
            # Сам msg — это анкета той, что нас лайкнула.
            profile = await self._parse_profile(msg)
            if profile is None:
                log.warning("incoming-like: не смог распарсить анкету, пропускаю")
                return False
            url = likes_recognizer.extract_profile_url(msg.message or "")
            saved = await likes_pool.save_profile(profile, "incoming", url)
            log.info(
                "incoming like %s id=%s",
                "saved" if saved else "duplicate", profile.external_id,
            )
            await self.like()
            return True

        if kind == "mutual_notification":
            button_texts = self._extract_button_texts(msg)
            show_btn = likes_recognizer.pick_show_button(button_texts)
            if not show_btn:
                log.info(
                    "mutual-notification: show-кнопки нет в %r, отдаю dismiss-у",
                    button_texts,
                )
                return False
            log.info("mutual-notification: жму %r", show_btn)
            if not await self._click_button_by_text(show_btn):
                return False
            # State мутирован — всегда возвращаем True.
            await asyncio.sleep(MUTUAL_SETTLE_SEC)
            new_msgs = await self._fetch_new_inbound(limit=MUTUAL_BATCH_LIMIT)
            if not new_msgs:
                log.warning("mutual-notification: бот не ответил после show")
                return True
            self._last_seen_id = max(m.id for m in new_msgs)
            match_msg = next(
                (m for m in new_msgs
                 if likes_recognizer.is_mutual_match(m.message or "")),
                None,
            )
            if match_msg is None:
                # Запасной: первое сообщение с медиа (это «pending» анкета
                # без характерной взаимка-фразы).
                match_msg = next(
                    (m for m in new_msgs if (
                        m.photo or m.video or m.video_note
                        or m.gif or m.grouped_id
                    )),
                    None,
                )
            if match_msg is None:
                log.warning(
                    "mutual: не нашёл match среди %d новых: %r",
                    len(new_msgs),
                    [(m.id, (m.message or "")[:60]) for m in new_msgs],
                )
                return True
            saved = await self._save_mutual_match(match_msg)
            log.info(
                "TG mutual %s id=%s", "saved" if saved else "skipped", match_msg.id,
            )
            # Лайк после mutual только если ответ — анкета со swipe-кнопками
            # (pending-очередь), а не итоговое «взаимная симпатия» текстом.
            has_media = bool(
                match_msg.photo or match_msg.video or match_msg.video_note
                or match_msg.gif or match_msg.grouped_id
            )
            if has_media and not likes_recognizer.is_mutual_match(
                match_msg.message or ""
            ):
                await self.like()
            return True

        return False

    async def _fetch_new_inbound(self, limit: int = 10) -> list[Message]:
        """Все входящие из чата с id > self._last_seen_id, по возрастанию id.
        last_seen_id НЕ обновляем — это делает caller."""
        new: list[Message] = []
        async for m in self.client.iter_messages(self.entity, limit=limit):
            if m.out:
                continue
            if self._last_seen_id is not None and m.id <= self._last_seen_id:
                continue
            new.append(m)
        new.sort(key=lambda m: m.id)
        return new

    async def _save_mutual_match(self, msg: Message) -> bool:
        """Сохранить mutual-подтверждение в пул. Допускает text-only ответы:
        в TG итоговое «Есть взаимная симпатия! @user» может приходить без
        медиа, в pending-очереди — наоборот, с фото.

        Защита: требуется хотя бы один признак профиля (match-фраза, медиа
        или URL), иначе откажемся — чтобы не записать сервисное меню."""
        text = msg.message or ""
        has_any_media = bool(
            msg.photo or msg.video or msg.video_note or msg.gif or msg.grouped_id
        )
        if not text.strip() and not has_any_media:
            return False
        url = likes_recognizer.extract_profile_url(text)
        is_match = likes_recognizer.is_mutual_match(text)
        if not (is_match or has_any_media or url):
            log.info(
                "TG mutual_match: пропускаю — нет признаков профиля: %r",
                text[:80],
            )
            return False
        external_id = str(msg.id)
        photos: list[bytes] = []
        if has_any_media:
            parsed = await self._parse_profile(msg)
            if parsed is not None:
                photos = [p for p in parsed.photos if isinstance(p, bytes)]
                if not text.strip():
                    text = parsed.bio
        profile = Profile(
            source=self.name,
            external_id=external_id,
            bio=text.strip(),
            photos=photos,
        )
        return await likes_pool.save_profile(profile, "mutual", url)

    async def like(self, message: str | None = None) -> None:
        if message:
            if await self._click_button(MESSAGE_EMOJIS):
                log.info("TG: лайк-с-сообщением, текст %d симв.", len(message))
                await asyncio.sleep(MESSAGE_PROMPT_DELAY_SEC)
                try:
                    await self.client.send_message(self.entity, message)
                    return
                except Exception:
                    log.exception("TG: не удалось отправить текст сообщения")
                    # Падать не будем — обычный лайк уже не поставить
                    # (мы уже в режиме «введи сообщение»). Останавливаемся.
                    return
            log.warning(
                "TG: кнопки 💌 не нашёл, деградирую до обычного лайка"
            )
        if not await self._click_button(LIKE_EMOJIS):
            log.warning(
                "like button not found, sending fallback text %r", FALLBACK_LIKE_TEXT
            )
            await self.client.send_message(self.entity, FALLBACK_LIKE_TEXT)

    async def skip(self) -> None:
        if not await self._click_button(SKIP_EMOJIS):
            log.warning(
                "skip button not found, sending fallback text %r", FALLBACK_SKIP_TEXT
            )
            await self.client.send_message(self.entity, FALLBACK_SKIP_TEXT)

    @staticmethod
    def _extract_button_texts(msg: Message) -> list[str]:
        """Все тексты кнопок данного сообщения (любые ряды клавиатуры)."""
        out: list[str] = []
        if not msg.buttons:
            return out
        for row in msg.buttons:
            for btn in row:
                text = (btn.text or "").strip()
                if text:
                    out.append(text)
        return out

    async def _click_button_by_text(self, target: str) -> bool:
        """Найти в недавних сообщениях кнопку с точно таким текстом и нажать."""
        async for msg in self.client.iter_messages(
            self.entity, limit=BUTTON_LOOKBACK
        ):
            if not msg.buttons:
                continue
            for row in msg.buttons:
                for btn in row:
                    if (btn.text or "").strip() == target:
                        try:
                            await btn.click()
                            return True
                        except Exception:
                            log.exception("button click failed")
                            return False
            return False
        return False

    async def _click_button(self, emojis: tuple[str, ...]) -> bool:
        """Найти в недавних сообщениях бота кнопку с одним из заданных
        эмодзи и нажать её. Telethon корректно обрабатывает и inline-кнопки
        (callback), и reply-клавиатуру (отправка текста кнопки)."""
        async for msg in self.client.iter_messages(
            self.entity, limit=BUTTON_LOOKBACK
        ):
            if not msg.buttons:
                continue
            all_button_texts: list[str] = []
            for row in msg.buttons:
                for btn in row:
                    text = (btn.text or "").strip()
                    if text:
                        all_button_texts.append(text)
                    if text and any(e in text for e in emojis):
                        log.info("clicking Leonardo button %r", text)
                        try:
                            await btn.click()
                            return True
                        except Exception:
                            log.exception("button click failed")
                            return False
            # Сообщение с кнопками нашли, но нужного эмодзи нет.
            # Логируем все доступные тексты — по ним легко расширить
            # LIKE_EMOJIS / SKIP_EMOJIS под конкретный UI бота.
            log.warning(
                "no button matched %r; available button texts: %r",
                emojis, all_button_texts,
            )
            return False
        return False

    async def stop(self) -> None:
        # Не дисконнектим shared-клиент — он живёт на всё время работы бота
        # и используется autochat-engine'ом. Просто отпускаем локальную ссылку.
        self.client = None  # type: ignore[assignment]
        self.entity = None

    async def scan_history_for_incoming(self) -> dict[str, Any]:
        """Прочесть последние SCAN_LOOKBACK сообщений Леонардо и сохранить
        в пул: incoming-лайки + mutual-match-подтверждения. Действий не
        совершаем — для авто-лайка дальше вызывающий запустит next_profile."""
        saved = 0
        duplicates = 0
        diag: list[dict[str, Any]] = []
        msgs: list[Message] = []
        async for m in self.client.iter_messages(self.entity, limit=SCAN_LOOKBACK):
            if m.out:
                continue
            msgs.append(m)
        # iter_messages даёт от новых к старым — переворачиваем для
        # хронологического порядка обработки/диагностики.
        msgs.reverse()

        for m in msgs:
            has_media = bool(
                m.photo or m.video or m.video_note or m.gif or m.grouped_id
            )
            text = m.message or ""
            kind = likes_recognizer.classify(text, has_media)
            is_match = likes_recognizer.is_mutual_match(text)
            diag_kind = kind or ("mutual_match" if is_match else None)
            diag.append({
                "id": m.id,
                "has_media": has_media,
                "kind": diag_kind,
                "text": text[:120].replace("\n", " "),
            })
            if kind == "incoming":
                profile = await self._parse_profile(m)
                if profile is None:
                    continue
                url = likes_recognizer.extract_profile_url(text)
                if await likes_pool.save_profile(profile, "incoming", url):
                    saved += 1
                else:
                    duplicates += 1
            elif is_match:
                if await self._save_mutual_match(m):
                    saved += 1
                else:
                    duplicates += 1
        return {"saved": saved, "duplicates": duplicates, "diag": diag}

    async def _hammer(self) -> None:
        """Возвращаемся в главное меню Леонардо и заходим в просмотр анкет.
        /myprofile открывает главное меню с reply-клавиатурой, затем "1"
        соответствует пункту «Смотреть анкеты»."""
        await self.client.send_message(self.entity, "/myprofile")
        await asyncio.sleep(HAMMER_DELAY_SEC)
        await self.client.send_message(self.entity, "1")

    async def _wait_for_new_message(self) -> Message | None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + RESPONSE_TIMEOUT_SEC
        while loop.time() < deadline:
            async for msg in self.client.iter_messages(self.entity, limit=1):
                if (
                    self._last_seen_id is None
                    or msg.id > self._last_seen_id
                ) and not msg.out:
                    self._last_seen_id = msg.id
                    return msg
                break
            await asyncio.sleep(POLL_INTERVAL_SEC)
        return None

    async def _parse_profile(self, msg: Message) -> Profile | None:
        # Возвращает None, если медиа было, но извлечь его не вышло.
        # «Это вообще не анкета» определяется уровнем выше (next_profile).
        text = msg.message or ""
        photos: list[bytes] = []

        if msg.grouped_id:
            async for m in self.client.iter_messages(self.entity, limit=10):
                if m.grouped_id != msg.grouped_id:
                    continue
                photos.extend(await self._extract_media(m))
                if not text and m.message:
                    text = m.message
        else:
            photos.extend(await self._extract_media(msg))

        if not photos:
            return None

        return Profile(
            source=self.name,
            external_id=str(msg.id),
            bio=text,
            photos=photos,
        )

    async def _extract_media(self, m: Message) -> list[bytes]:
        """Возвращает список картинок-байтов: для фото — само фото,
        для видео/гифки/кружочка — несколько вытащенных кадров."""
        if m.photo:
            buf = BytesIO()
            await m.download_media(file=buf)
            return [buf.getvalue()]
        if m.video or m.video_note or m.gif:
            buf = BytesIO()
            await m.download_media(file=buf)
            frames = extract_video_frames(buf.getvalue(), n=3)
            if frames:
                log.info("extracted %d frame(s) from video msg id=%s", len(frames), m.id)
            return frames
        return []
