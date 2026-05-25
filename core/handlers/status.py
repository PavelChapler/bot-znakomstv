from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from core import db
from core.handlers.goal import get_dry_run, get_goal, get_threshold

router = Router()


def _format_decisions(rows: list[dict]) -> str:
    if not rows:
        return "Истории пока нет."
    lines = []
    for r in rows[:10]:
        emoji = "❤️" if r["action"] == "like" else "👎"
        dr = "[DRY] " if r["dry_run"] else ""
        bio = (r["bio"] or "").replace("\n", " ")[:60]
        lines.append(
            f"{dr}{emoji} {r['score']} | {r['source']} | {escape(bio)}"
        )
    return "\n".join(lines)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    goal = await get_goal()
    threshold = await get_threshold()
    dry = await get_dry_run()
    rows = await db.recent_decisions(limit=10)
    text = (
        f"<b>Цель:</b> {escape(goal[:400])}\n"
        f"<b>Порог:</b> {threshold}, <b>dry-run:</b> {'ON' if dry else 'OFF'}\n\n"
        f"<b>Последние решения:</b>\n{_format_decisions(rows)}"
    )
    await message.answer(text)


@router.callback_query(F.data == "settings:status")
async def cb_status(query: CallbackQuery) -> None:
    if query.message and isinstance(query.message, Message):
        await cmd_status(query.message)
    await query.answer()
