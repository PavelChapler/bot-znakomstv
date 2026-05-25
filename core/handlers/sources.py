from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from config import load
from core.handlers.goal import get_dry_run, get_goal, get_threshold
from core.models import Decision
from core.registry import get_source_class
from core.runner import SessionController, SessionStats, run_session
from scorer.gemini import GeminiScorer

log = logging.getLogger(__name__)
router = Router()

# одна активная сессия (single user)
_current: SessionController | None = None


@router.callback_query(F.data.startswith("src:"))
async def on_source_chosen(query: CallbackQuery) -> None:
    global _current

    if not query.data:
        await query.answer()
        return

    name = query.data.split(":", 1)[1]
    cls = get_source_class(name)
    if cls is None:
        await query.answer("Источник не найден")
        return

    if _current is not None and _current.running:
        await query.answer("Уже идёт сессия. /stop чтобы остановить.", show_alert=True)
        return

    cfg = load()
    goal = await get_goal()
    threshold = await get_threshold()
    dry_run = await get_dry_run()

    try:
        source = cls()
    except Exception as e:
        log.exception("failed to instantiate source")
        if query.message:
            await query.message.answer(f"Не получилось создать источник: {e}")
        await query.answer()
        return

    scorer = GeminiScorer()
    controller = SessionController()
    _current = controller

    if not query.message:
        await query.answer()
        return

    chat_id = query.message.chat.id
    bot = query.bot

    async def on_progress(decision: Decision, stats: SessionStats) -> None:
        emoji = "❤️" if decision.action == "like" else "👎"
        prefix = "[DRY] " if decision.dry_run else ""
        bio_short = (decision.profile.bio or "").strip().replace("\n", " ")
        if len(bio_short) > 200:
            bio_short = bio_short[:197] + "..."
        text = (
            f"{prefix}{emoji} <b>score={decision.score.score}</b> "
            f"({stats.seen} | ❤️{stats.liked} 👎{stats.skipped})\n"
            f"<i>{escape(decision.score.reason)}</i>\n"
            f"{escape(bio_short)}"
        )
        try:
            if bot is not None:
                await bot.send_message(chat_id, text)
        except Exception:
            log.exception("failed to send progress")

    await query.message.answer(
        f"Запускаю «{cls.title}».\n"
        f"<b>Цель:</b> {escape(goal[:300])}\n"
        f"<b>Порог:</b> {threshold}, <b>dry-run:</b> {'ON' if dry_run else 'OFF'}, "
        f"<b>лимит:</b> {cfg.session_max_profiles}\n"
        f"/stop — остановить."
    )
    await query.answer()

    async def runner_task() -> None:
        try:
            await run_session(
                source=source,
                scorer=scorer,
                goal=goal,
                threshold=threshold,
                dry_run=dry_run,
                max_count=cfg.session_max_profiles,
                controller=controller,
                on_progress=on_progress,
            )
        finally:
            stats = controller.stats
            try:
                if bot is not None:
                    await bot.send_message(
                        chat_id,
                        f"Сессия завершена. Просмотрено: {stats.seen}, "
                        f"❤️ {stats.liked}, 👎 {stats.skipped}, ошибок: {stats.errors}.",
                    )
            except Exception:
                log.exception("failed to send finish message")

    controller.task = asyncio.create_task(runner_task())


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    global _current
    if _current is None or not _current.running:
        await message.answer("Сессия не активна.")
        return
    _current.request_stop()
    await message.answer("Останавливаю...")
