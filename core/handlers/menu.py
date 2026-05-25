from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.handlers.goal import get_message_enabled
from core.registry import all_sources

router = Router()


async def main_menu_kb() -> InlineKeyboardMarkup:
    msg_on = await get_message_enabled()
    msg_label = f"✉️ Сообщение: {'ON' if msg_on else 'OFF'}"
    rows: list[list[InlineKeyboardButton]] = []
    for cls in all_sources():
        rows.append(
            [InlineKeyboardButton(text=cls.title, callback_data=f"src:{cls.name}")]
        )
    rows.append(
        [
            InlineKeyboardButton(text="Цель", callback_data="settings:goal"),
            InlineKeyboardButton(text="Порог", callback_data="settings:threshold"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="Dry-run", callback_data="settings:dry_run"),
            InlineKeyboardButton(text="Статус", callback_data="settings:status"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text=msg_label, callback_data="settings:toggle_message"),
            InlineKeyboardButton(text="Стиль", callback_data="settings:style"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="💌 Пул лайков", callback_data="likes:browse"),
            InlineKeyboardButton(text="Собрать лайки", callback_data="likes:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "Главное меню.\nВыбери источник, чтобы запустить сессию, или настройку:",
        reply_markup=await main_menu_kb(),
    )


@router.message(Command("menu"))
async def on_menu(message: Message) -> None:
    await message.answer("Меню:", reply_markup=await main_menu_kb())
