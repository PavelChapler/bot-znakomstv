"""Фоновый цикл автопереписки.

Раз в TICK_INTERVAL_SEC:
  1. Если выключено — спим дальше.
  2. Recovery: заполняем pending для записей liked_pool, у которых нет
     conversation (раз в RECOVERY_EVERY_SEC).
  3. pending с истёкшим scheduled_send_ts → opener.
  4. active → poll новых её сообщений → respond или close по сигналу brain.
  5. active с msg_count >= max_msgs → close(reason='cap').

Семафор `_send_lock` сериализует отправки + случайная пауза 5-10 сек
после каждой → анти-spam.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from aiogram import Bot
from telethon.errors import FloodWaitError

from autochat import config
from autochat import db as autochat_db
from autochat.brain import GeminiChatBrain
from autochat.chatters import get_chatter_class
from autochat.chatters.base import Chatter
from autochat.models import Conversation

log = logging.getLogger(__name__)

TICK_INTERVAL_SEC = 15
RECOVERY_EVERY_SEC = 60
RECOVERY_LOOKBACK_SEC = 24 * 3600
INTER_SEND_MIN_SEC = 5.0
INTER_SEND_MAX_SEC = 10.0
HISTORY_LIMIT = 50


class AutoChatEngine:
    def __init__(
        self, bot: Bot | None = None, notify_chat_id: int | None = None
    ) -> None:
        self.bot = bot
        self.notify_chat_id = notify_chat_id
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._chatters: dict[str, Chatter] = {}
        self._brain: GeminiChatBrain | None = None
        self._send_lock = asyncio.Lock()
        self._floodwait_until = 0
        self._last_recovery_ts = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        for ch in list(self._chatters.values()):
            try:
                await ch.stop()
            except Exception:
                log.exception("chatter stop failed")
        self._chatters.clear()

    async def _run(self) -> None:
        log.info("autochat engine started")
        while not self._stop_event.is_set():
            try:
                if await config.is_enabled():
                    await self._tick()
            except Exception:
                log.exception("autochat tick crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=TICK_INTERVAL_SEC
                )
            except asyncio.TimeoutError:
                pass
        log.info("autochat engine stopped")

    async def _tick(self) -> None:
        now = int(time.time())
        if now < self._floodwait_until:
            return

        # Recovery: дозаливаем pending по liked_pool
        if now - self._last_recovery_ts >= RECOVERY_EVERY_SEC:
            self._last_recovery_ts = now
            await self._recovery_scan(now)

        # Pending due → opener
        due = await autochat_db.find_due_pending(now, limit=10)
        for conv in due:
            if self._stop_event.is_set():
                return
            await self._fire_opener(conv)

        # Active → poll + respond
        active = await autochat_db.list_conversations(
            states=["active"], limit=50
        )
        max_msgs = await config.get_max_msgs()
        for conv in active:
            if self._stop_event.is_set():
                return
            if conv.msg_count >= max_msgs:
                await self._close(conv, reason="cap", failed=False)
                continue
            await self._poll_active(conv)

    async def _recovery_scan(self, now_ts: int) -> None:
        delay = await config.get_delay_sec()
        goal = await config.get_goal_prompt()
        style = await config.get_style_prompt()
        rows = await autochat_db.list_recent_pool_without_conv(
            now_ts - RECOVERY_LOOKBACK_SEC, limit=50
        )
        for r in rows:
            scheduled = (r["discovered_ts"] or now_ts) + delay
            conv_id = await autochat_db.create_conversation(
                source=r["source"],
                external_id=r["external_id"],
                profile_url=r["profile_url"],
                goal_prompt=goal,
                style_prompt=style,
                scheduled_send_ts=scheduled,
            )
            if conv_id is not None:
                log.info(
                    "autochat recovery: conv id=%d (%s/%s)",
                    conv_id, r["source"], r["external_id"],
                )

    async def _get_chatter(self, source: str) -> Chatter | None:
        ch = self._chatters.get(source)
        if ch is not None:
            return ch
        cls = get_chatter_class(source)
        if cls is None:
            return None
        try:
            ch = cls()
            await ch.start()
        except Exception:
            log.exception("chatter start failed for %s", source)
            return None
        self._chatters[source] = ch
        return ch

    async def _get_brain(self) -> GeminiChatBrain:
        if self._brain is None:
            self._brain = GeminiChatBrain()
        return self._brain

    async def _fire_opener(self, conv: Conversation) -> None:
        ch = await self._get_chatter(conv.source)
        if ch is None:
            await autochat_db.update_state(
                conv.id, "failed", done_reason="no_chatter"
            )
            await self._notify_done(conv, "no_chatter", failed=True)
            return
        ok, reason = await ch.can_write(conv.profile_url)
        if not ok:
            await autochat_db.update_state(
                conv.id, "failed", done_reason=reason or "cannot_write"
            )
            await self._notify_done(conv, reason or "cannot_write", failed=True)
            return
        peer = await ch.resolve_peer(conv.profile_url)
        if not peer:
            await autochat_db.update_state(
                conv.id, "failed", done_reason="no_peer"
            )
            await self._notify_done(conv, "no_peer", failed=True)
            return
        bio = await autochat_db.get_pool_bio(conv.source, conv.external_id)
        try:
            brain = await self._get_brain()
        except Exception as e:
            log.exception("brain init failed")
            await autochat_db.update_state(
                conv.id, "failed", done_reason=f"brain init: {e}"
            )
            return
        result = await brain.generate_opener(
            style=conv.style_prompt,
            goal=conv.goal_prompt,
            profile_bio=bio,
            profile_url=conv.profile_url,
        )
        if result.done or not result.reply:
            await autochat_db.update_state(
                conv.id, "failed",
                done_reason=result.done_reason or "opener_empty",
            )
            await self._notify_done(
                conv, result.done_reason or "opener_empty", failed=True,
            )
            return
        msg_id = await self._serialized_send(ch, peer, result.reply)
        if msg_id is None:
            await autochat_db.update_state(
                conv.id, "failed", done_reason="send_failed"
            )
            await self._notify_done(conv, "send_failed", failed=True)
            return
        await autochat_db.append_message(conv.id, "us", result.reply, msg_id)
        await autochat_db.update_after_outgoing(conv.id, msg_id)
        await autochat_db.update_state(conv.id, "active", peer_id=peer)
        log.info("autochat conv %d: opener sent, state=active", conv.id)

    async def _poll_active(self, conv: Conversation) -> None:
        if not conv.peer_id:
            return
        ch = await self._get_chatter(conv.source)
        if ch is None:
            return
        try:
            new_msgs = await ch.fetch_new_replies(
                conv.peer_id, conv.last_external_msg_id
            )
        except FloodWaitError as e:
            self._floodwait_until = int(time.time()) + int(e.seconds)
            log.warning(
                "autochat: FloodWait %ds on poll, паузим всё", e.seconds
            )
            return
        except Exception:
            log.exception("autochat poll failed conv %d", conv.id)
            return
        if not new_msgs:
            return
        for m in new_msgs:
            await autochat_db.append_message(
                conv.id, "her", m.text, m.external_msg_id, ts=m.ts,
            )
        last_id = new_msgs[-1].external_msg_id or ""
        await autochat_db.update_after_incoming(conv.id, last_id)

        bio = await autochat_db.get_pool_bio(conv.source, conv.external_id)
        history_msgs = await autochat_db.list_messages(
            conv.id, limit=HISTORY_LIMIT
        )
        history_pairs = [(m.role, m.text) for m in history_msgs]
        try:
            brain = await self._get_brain()
        except Exception:
            log.exception("brain init failed")
            return
        result = await brain.respond(
            style=conv.style_prompt,
            goal=conv.goal_prompt,
            profile_bio=bio,
            profile_url=conv.profile_url,
            history=history_pairs,
        )

        if result.reply:
            msg_id = await self._serialized_send(ch, conv.peer_id, result.reply)
            if msg_id is not None:
                await autochat_db.append_message(
                    conv.id, "us", result.reply, msg_id,
                )
                await autochat_db.update_after_outgoing(conv.id, msg_id)
            else:
                log.warning("autochat conv %d: ответ не отправился", conv.id)

        if result.done:
            await self._close(
                conv, reason=result.done_reason or "done", failed=False,
            )

    async def _close(
        self, conv: Conversation, reason: str, failed: bool
    ) -> None:
        state = "failed" if failed else "done"
        await autochat_db.update_state(conv.id, state, done_reason=reason)
        await self._notify_done(conv, reason, failed=failed)

    async def _serialized_send(
        self, ch: Chatter, peer: str, text: str
    ) -> str | None:
        async with self._send_lock:
            try:
                msg_id = await ch.send(peer, text)
            except FloodWaitError as e:
                self._floodwait_until = int(time.time()) + int(e.seconds)
                log.warning(
                    "autochat: FloodWait %ds на send, паузим", e.seconds,
                )
                return None
            except Exception:
                log.exception("autochat send failed peer=%s", peer)
                return None
            await asyncio.sleep(
                random.uniform(INTER_SEND_MIN_SEC, INTER_SEND_MAX_SEC)
            )
            return msg_id

    async def _notify_done(
        self, conv: Conversation, reason: str, failed: bool
    ) -> None:
        if self.bot is None or self.notify_chat_id is None:
            return
        emoji = "❌" if failed else "✅"
        text = (
            f"{emoji} <b>Авточат завершён</b>\n"
            f"Источник: {conv.source}\n"
            f"Профиль: {conv.profile_url}\n"
            f"Причина: <code>{reason}</code>\n"
            f"Сообщений: {conv.msg_count}"
        )
        try:
            await self.bot.send_message(self.notify_chat_id, text)
        except Exception:
            log.exception("autochat notify_done failed")
