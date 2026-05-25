from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import load
from core import db

router = Router()

KEY_GOAL = "goal"
KEY_THRESHOLD = "threshold"
KEY_DRY_RUN = "dry_run"
KEY_MESSAGE_ENABLED = "message_enabled"
KEY_MESSAGE_STYLE = "message_style"

DEFAULT_MESSAGE_STYLE = (
    "Дружелюбно, искренне, с лёгким юмором, без шаблонов. "
    "1-2 предложения. Зацепка из bio или фото. На «ты». "
    "Без приветствий типа «Привет!»."
)


async def get_goal() -> str:
    cfg = load()
    val = await db.get_setting(KEY_GOAL)
    return val if val else cfg.default_goal


async def get_threshold() -> int:
    cfg = load()
    raw = await db.get_setting(KEY_THRESHOLD)
    if raw is None:
        return cfg.default_threshold
    try:
        return int(raw)
    except ValueError:
        return cfg.default_threshold


async def get_dry_run() -> bool:
    raw = await db.get_setting(KEY_DRY_RUN, "1")
    return raw == "1"


async def get_message_enabled() -> bool:
    raw = await db.get_setting(KEY_MESSAGE_ENABLED, "0")
    return raw == "1"


async def get_message_style() -> str:
    val = await db.get_setting(KEY_MESSAGE_STYLE)
    return val if val else DEFAULT_MESSAGE_STYLE


class SetGoal(StatesGroup):
    waiting = State()


class SetThreshold(StatesGroup):
    waiting = State()


class SetStyle(StatesGroup):
    waiting = State()


@router.message(Command("goal"))
async def cmd_goal(message: Message, state: FSMContext) -> None:
    cur = await get_goal()
    await message.answer(
        f"Текущая цель:\n\n{escape(cur)}\n\nОтправь новый текст, чтобы заменить, или /cancel."
    )
    await state.set_state(SetGoal.waiting)


@router.callback_query(F.data == "settings:goal")
async def cb_goal(query: CallbackQuery, state: FSMContext) -> None:
    cur = await get_goal()
    if query.message:
        await query.message.answer(
            f"Текущая цель:\n\n{escape(cur)}\n\nОтправь новый текст, чтобы заменить, или /cancel."
        )
    await state.set_state(SetGoal.waiting)
    await query.answer()


@router.message(SetGoal.waiting, Command("cancel"))
async def cancel_goal(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отмена.")


@router.message(SetGoal.waiting)
async def save_goal(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой текст. Попробуй ещё раз или /cancel.")
        return
    await db.set_setting(KEY_GOAL, text)
    await state.clear()
    await message.answer("Цель сохранена.")


@router.message(Command("threshold"))
async def cmd_threshold(message: Message, state: FSMContext) -> None:
    cur = await get_threshold()
    await message.answer(
        f"Текущий порог: {cur}\nОтправь число от 0 до 100, или /cancel."
    )
    await state.set_state(SetThreshold.waiting)


@router.callback_query(F.data == "settings:threshold")
async def cb_threshold(query: CallbackQuery, state: FSMContext) -> None:
    cur = await get_threshold()
    if query.message:
        await query.message.answer(
            f"Текущий порог: {cur}\nОтправь число от 0 до 100, или /cancel."
        )
    await state.set_state(SetThreshold.waiting)
    await query.answer()


@router.message(SetThreshold.waiting, Command("cancel"))
async def cancel_threshold(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отмена.")


@router.message(SetThreshold.waiting)
async def save_threshold(message: Message, state: FSMContext) -> None:
    try:
        v = int((message.text or "").strip())
    except ValueError:
        await message.answer("Не число. Попробуй ещё раз или /cancel.")
        return
    if not 0 <= v <= 100:
        await message.answer("Должно быть от 0 до 100.")
        return
    await db.set_setting(KEY_THRESHOLD, str(v))
    await state.clear()
    await message.answer(f"Порог сохранён: {v}")


@router.message(Command("dry_run"))
async def cmd_dry_run(message: Message) -> None:
    cur = await get_dry_run()
    new = "0" if cur else "1"
    await db.set_setting(KEY_DRY_RUN, new)
    await message.answer(f"Dry-run: {'ON' if new == '1' else 'OFF'}")


@router.callback_query(F.data == "settings:dry_run")
async def cb_dry_run(query: CallbackQuery) -> None:
    cur = await get_dry_run()
    new = "0" if cur else "1"
    await db.set_setting(KEY_DRY_RUN, new)
    if query.message:
        await query.message.answer(f"Dry-run: {'ON' if new == '1' else 'OFF'}")
    await query.answer()


@router.message(Command("toggle_message"))
async def cmd_toggle_message(message: Message) -> None:
    cur = await get_message_enabled()
    new = "0" if cur else "1"
    await db.set_setting(KEY_MESSAGE_ENABLED, new)
    await message.answer(
        f"Генерация сообщений: {'ON' if new == '1' else 'OFF'}"
    )


@router.callback_query(F.data == "settings:toggle_message")
async def cb_toggle_message(query: CallbackQuery) -> None:
    cur = await get_message_enabled()
    new = "0" if cur else "1"
    await db.set_setting(KEY_MESSAGE_ENABLED, new)
    if query.message:
        await query.message.answer(
            f"Генерация сообщений: {'ON' if new == '1' else 'OFF'}"
        )
    await query.answer()


@router.message(Command("style"))
async def cmd_style(message: Message, state: FSMContext) -> None:
    cur = await get_message_style()
    await message.answer(
        f"Текущий стиль сообщений:\n\n{escape(cur)}\n\n"
        "Отправь новый текст, чтобы заменить, или /cancel."
    )
    await state.set_state(SetStyle.waiting)


@router.callback_query(F.data == "settings:style")
async def cb_style(query: CallbackQuery, state: FSMContext) -> None:
    cur = await get_message_style()
    if query.message:
        await query.message.answer(
            f"Текущий стиль сообщений:\n\n{escape(cur)}\n\n"
            "Отправь новый текст, чтобы заменить, или /cancel."
        )
    await state.set_state(SetStyle.waiting)
    await query.answer()


@router.message(SetStyle.waiting, Command("cancel"))
async def cancel_style(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отмена.")


@router.message(SetStyle.waiting)
async def save_style(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой текст. Попробуй ещё раз или /cancel.")
        return
    await db.set_setting(KEY_MESSAGE_STYLE, text)
    await state.clear()
    await message.answer("Стиль сохранён.")
