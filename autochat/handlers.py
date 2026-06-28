"""Aiogram-роутер автопереписки: меню, переключатель, промпты, список,
управление отдельными диалогами."""

from __future__ import annotations

import logging
import time
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from autochat import config
from autochat import db as autochat_db
from core.registry import all_sources, get_source_class

log = logging.getLogger(__name__)
router = Router()

STATE_LABEL = {
    "pending": "⏳ pending",
    "active": "💬 active",
    "paused": "⏸ paused",
    "done": "✅ done",
    "failed": "❌ failed",
}


def _source_kb(prefix: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора источника для per-source настройки авточата."""
    rows = [
        [InlineKeyboardButton(text=cls.title, callback_data=f"{prefix}:{cls.name}")]
        for cls in all_sources() if cls.name != "twinby"
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _source_title(source: str) -> str:
    cls = get_source_class(source)
    return cls.title if cls else source


class SetAutochatGoal(StatesGroup):
    waiting = State()


class SetAutochatStyle(StatesGroup):
    waiting = State()


async def _menu_kb() -> InlineKeyboardMarkup:
    enabled = await config.is_enabled()
    voice_on = await config.is_transcribe_voice_enabled()
    counts = await autochat_db.count_by_state()
    total = sum(counts.values())
    pending = counts.get("pending", 0)
    active = counts.get("active", 0)
    done = counts.get("done", 0)
    failed = counts.get("failed", 0)
    toggle_label = f"Автопереписка: {'ON 🟢' if enabled else 'OFF ⚪'}"
    voice_label = f"🎙 Голос: {'ON' if voice_on else 'OFF'}"
    list_label = (
        f"📋 Диалоги ({active} активн., {pending} ждут, {done}✅, {failed}❌)"
    )
    rows = [
        [InlineKeyboardButton(text=toggle_label, callback_data="autochat:toggle")],
        [
            InlineKeyboardButton(text="🎯 Цель", callback_data="autochat:goal"),
            InlineKeyboardButton(text="🎭 Стиль", callback_data="autochat:style"),
        ],
        [InlineKeyboardButton(text=voice_label, callback_data="autochat:toggle_voice")],
        [InlineKeyboardButton(text=list_label, callback_data="autochat:list")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ───────── меню ─────────

@router.message(Command("autochat"))
async def cmd_autochat(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        f"<b>🤖 Автопереписка</b>\n\n"
        f"Бот пишет первый месседж через {await config.get_delay_sec()} сек "
        f"после mutual, потом ведёт диалог через Claude (отвечает с паузой "
        f"~{await config.get_reply_delay_sec()} сек) пока он не сигналит "
        f"«цель достигнута». Лимит: {await config.get_max_msgs()} наших сообщений.",
        reply_markup=await _menu_kb(),
    )


@router.callback_query(F.data == "autochat:menu")
async def cb_menu(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if query.message and isinstance(query.message, Message):
        await query.message.answer(
            "🤖 Автопереписка:", reply_markup=await _menu_kb(),
        )
    await query.answer()


@router.callback_query(F.data == "autochat:toggle")
async def cb_toggle(query: CallbackQuery) -> None:
    cur = await config.is_enabled()
    await config.set_enabled(not cur)
    new_state = "ON" if not cur else "OFF"
    if query.message and isinstance(query.message, Message):
        await query.message.answer(
            f"Автопереписка: <b>{new_state}</b>",
            reply_markup=await _menu_kb(),
        )
    await query.answer()


@router.callback_query(F.data == "autochat:toggle_voice")
async def cb_toggle_voice(query: CallbackQuery) -> None:
    cur = await config.is_transcribe_voice_enabled()
    await config.set_transcribe_voice(not cur)
    new_state = "ON" if not cur else "OFF"
    if query.message and isinstance(query.message, Message):
        await query.message.answer(
            f"🎙 Расшифровка голосовых: <b>{new_state}</b>",
            reply_markup=await _menu_kb(),
        )
    await query.answer()


# ───────── цель ─────────

@router.message(Command("autochat_goal"))
async def cmd_goal(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🎯 Цель автопереписки — для какого источника?",
        reply_markup=_source_kb("autochat:goalsrc"),
    )


@router.callback_query(F.data == "autochat:goal")
async def cb_goal(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if query.message and isinstance(query.message, Message):
        await query.message.answer(
            "🎯 Цель автопереписки — для какого источника?",
            reply_markup=_source_kb("autochat:goalsrc"),
        )
    await query.answer()


@router.callback_query(F.data.startswith("autochat:goalsrc:"))
async def cb_goalsrc(query: CallbackQuery, state: FSMContext) -> None:
    assert query.data is not None
    source = query.data.split(":", 2)[2]
    cur = await config.get_goal_prompt(source)
    await state.update_data(source=source)
    await state.set_state(SetAutochatGoal.waiting)
    if query.message and isinstance(query.message, Message):
        await query.message.answer(
            f"Цель автопереписки для «{_source_title(source)}»:\n\n{escape(cur)}\n\n"
            "Отправь новый текст или /cancel."
        )
    await query.answer()


@router.message(SetAutochatGoal.waiting, Command("cancel"))
async def cancel_goal(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отмена.")


@router.message(SetAutochatGoal.waiting)
async def save_goal(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пусто. Попробуй ещё раз или /cancel.")
        return
    from core import db
    data = await state.get_data()
    source = data.get("source")
    await db.set_setting(
        f"{config.KEY_GOAL_PROMPT}:{source}" if source else config.KEY_GOAL_PROMPT,
        text,
    )
    await state.clear()
    suffix = f" для «{_source_title(source)}»" if source else ""
    await message.answer(f"Цель сохранена{suffix}.")


# ───────── стиль ─────────

@router.message(Command("autochat_style"))
async def cmd_style(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🎭 Стиль автопереписки — для какого источника?",
        reply_markup=_source_kb("autochat:stylesrc"),
    )


@router.callback_query(F.data == "autochat:style")
async def cb_style(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if query.message and isinstance(query.message, Message):
        await query.message.answer(
            "🎭 Стиль автопереписки — для какого источника?",
            reply_markup=_source_kb("autochat:stylesrc"),
        )
    await query.answer()


@router.callback_query(F.data.startswith("autochat:stylesrc:"))
async def cb_stylesrc(query: CallbackQuery, state: FSMContext) -> None:
    assert query.data is not None
    source = query.data.split(":", 2)[2]
    cur = await config.get_style_prompt(source)
    await state.update_data(source=source)
    await state.set_state(SetAutochatStyle.waiting)
    if query.message and isinstance(query.message, Message):
        await query.message.answer(
            f"Стиль автопереписки для «{_source_title(source)}»:\n\n{escape(cur)}\n\n"
            "Отправь новый текст или /cancel."
        )
    await query.answer()


@router.message(SetAutochatStyle.waiting, Command("cancel"))
async def cancel_style(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отмена.")


@router.message(SetAutochatStyle.waiting)
async def save_style(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пусто. Попробуй ещё раз или /cancel.")
        return
    from core import db
    data = await state.get_data()
    source = data.get("source")
    await db.set_setting(
        f"{config.KEY_STYLE_PROMPT}:{source}" if source else config.KEY_STYLE_PROMPT,
        text,
    )
    await state.clear()
    suffix = f" для «{_source_title(source)}»" if source else ""
    await message.answer(f"Стиль сохранён{suffix}.")


# ───────── список диалогов ─────────

@router.message(Command("autochat_list"))
async def cmd_list(message: Message) -> None:
    await _show_list(message)


@router.callback_query(F.data == "autochat:list")
async def cb_list(query: CallbackQuery) -> None:
    if query.message and isinstance(query.message, Message):
        await _show_list(query.message)
    await query.answer()


async def _show_list(message: Message) -> None:
    convs = await autochat_db.list_conversations(
        states=["pending", "active", "paused"], limit=30,
    )
    if not convs:
        await message.answer(
            "Активных диалогов нет.\n"
            "Они появятся после mutual/incoming-лайков при включённой "
            "автопереписке."
        )
        return
    lines = ["<b>Диалоги автопереписки:</b>\n"]
    rows: list[list[InlineKeyboardButton]] = []
    now = int(time.time())
    for c in convs:
        ago = _humanize_ago(now - c.last_activity_ts)
        st = STATE_LABEL.get(c.state, c.state)
        url_short = c.profile_url[:40]
        lines.append(
            f"#{c.id}  {st}  <code>{escape(url_short)}</code>  "
            f"msgs={c.msg_count}  {ago}"
        )
        rows.append([
            InlineKeyboardButton(text=f"#{c.id}", callback_data=f"autochat:conv:{c.id}"),
        ])
    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        disable_web_page_preview=True,
    )


# ───────── карточка одного диалога ─────────

@router.callback_query(F.data.startswith("autochat:conv:"))
async def cb_conv(query: CallbackQuery) -> None:
    assert query.data is not None
    conv_id = int(query.data.split(":")[2])
    conv = await autochat_db.get_conversation(conv_id)
    if conv is None:
        await query.answer("Диалог не найден", show_alert=True)
        return
    if query.message and isinstance(query.message, Message):
        await query.message.answer(
            _format_conv_card(conv),
            reply_markup=_conv_kb(conv.id, conv.state),
            disable_web_page_preview=True,
        )
    await query.answer()


@router.callback_query(F.data.startswith("autochat:pause:"))
async def cb_pause(query: CallbackQuery) -> None:
    assert query.data is not None
    conv_id = int(query.data.split(":")[2])
    conv = await autochat_db.get_conversation(conv_id)
    if conv is None or conv.state != "active":
        await query.answer("Нельзя поставить на паузу", show_alert=True)
        return
    await autochat_db.update_state(conv_id, "paused", done_reason=None)
    await query.answer("Поставлен на паузу")
    if query.message and isinstance(query.message, Message):
        updated = await autochat_db.get_conversation(conv_id)
        if updated is not None:
            await query.message.answer(
                _format_conv_card(updated),
                reply_markup=_conv_kb(updated.id, updated.state),
                disable_web_page_preview=True,
            )


@router.callback_query(F.data.startswith("autochat:resume:"))
async def cb_resume(query: CallbackQuery) -> None:
    assert query.data is not None
    conv_id = int(query.data.split(":")[2])
    conv = await autochat_db.get_conversation(conv_id)
    if conv is None or conv.state != "paused":
        await query.answer("Нельзя возобновить", show_alert=True)
        return
    await autochat_db.update_state(conv_id, "active", done_reason=None)
    await query.answer("Возобновлён")
    if query.message and isinstance(query.message, Message):
        updated = await autochat_db.get_conversation(conv_id)
        if updated is not None:
            await query.message.answer(
                _format_conv_card(updated),
                reply_markup=_conv_kb(updated.id, updated.state),
                disable_web_page_preview=True,
            )


@router.callback_query(F.data.startswith("autochat:end:"))
async def cb_end(query: CallbackQuery) -> None:
    assert query.data is not None
    conv_id = int(query.data.split(":")[2])
    conv = await autochat_db.get_conversation(conv_id)
    if conv is None or conv.state in ("done", "failed"):
        await query.answer("Уже закрыт", show_alert=True)
        return
    await autochat_db.update_state(conv_id, "done", done_reason="manual")
    await query.answer("Закрыт")
    if query.message and isinstance(query.message, Message):
        updated = await autochat_db.get_conversation(conv_id)
        if updated is not None:
            await query.message.answer(
                _format_conv_card(updated),
                reply_markup=_conv_kb(updated.id, updated.state),
                disable_web_page_preview=True,
            )


@router.callback_query(F.data.startswith("autochat:history:"))
async def cb_history(query: CallbackQuery) -> None:
    assert query.data is not None
    conv_id = int(query.data.split(":")[2])
    msgs = await autochat_db.list_messages(conv_id, limit=50)
    if not msgs:
        await query.answer("История пуста", show_alert=True)
        return
    lines = [f"<b>История диалога #{conv_id}:</b>\n"]
    for m in msgs:
        prefix = "🟦 Я" if m.role == "us" else "🟪 Она"
        text = (m.text or "").strip()
        if len(text) > 500:
            text = text[:497] + "…"
        lines.append(f"{prefix}: {escape(text)}")
    out = "\n\n".join(lines)
    if query.message and isinstance(query.message, Message):
        # Telegram cap 4096
        if len(out) > 4000:
            out = out[-4000:]
        await query.message.answer(out, disable_web_page_preview=True)
    await query.answer()


def _format_conv_card(conv) -> str:
    now = int(time.time())
    ago = _humanize_ago(now - conv.last_activity_ts)
    st = STATE_LABEL.get(conv.state, conv.state)
    if getattr(conv, "manual", False):
        st += " · 🖐 ручной"
    sched = ""
    if conv.state == "pending" and conv.scheduled_send_ts:
        delta = conv.scheduled_send_ts - now
        sched = (
            f"\nОтправка через: {delta}с" if delta > 0 else "\nГотов к отправке"
        )
    done = (
        f"\nПричина: <code>{escape(conv.done_reason)}</code>"
        if conv.done_reason else ""
    )
    return (
        f"<b>Диалог #{conv.id}</b>\n"
        f"Источник: {conv.source}\n"
        f"Профиль: {escape(conv.profile_url)}\n"
        f"Состояние: {st}\n"
        f"Сообщений: {conv.msg_count}\n"
        f"Активность: {ago}"
        f"{sched}{done}"
    )


def _conv_kb(conv_id: int, state: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if state == "active":
        rows.append([
            InlineKeyboardButton(text="⏸ Пауза", callback_data=f"autochat:pause:{conv_id}"),
            InlineKeyboardButton(text="⏹ Стоп", callback_data=f"autochat:end:{conv_id}"),
        ])
    elif state == "paused":
        rows.append([
            InlineKeyboardButton(text="▶ Возобновить", callback_data=f"autochat:resume:{conv_id}"),
            InlineKeyboardButton(text="⏹ Стоп", callback_data=f"autochat:end:{conv_id}"),
        ])
    elif state == "pending":
        rows.append([
            InlineKeyboardButton(text="⏹ Отменить", callback_data=f"autochat:end:{conv_id}"),
        ])
    rows.append([
        InlineKeyboardButton(text="📜 История", callback_data=f"autochat:history:{conv_id}"),
        InlineKeyboardButton(text="← Список", callback_data="autochat:list"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _humanize_ago(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}с назад"
    if seconds < 3600:
        return f"{seconds // 60}мин назад"
    if seconds < 86400:
        return f"{seconds // 3600}ч назад"
    return f"{seconds // 86400}д назад"
