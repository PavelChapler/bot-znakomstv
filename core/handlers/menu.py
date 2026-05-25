from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.registry import all_sources

router = Router()


def main_menu_kb() -> InlineKeyboardMarkup:
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
            InlineKeyboardButton(text="💌 Пул лайков", callback_data="likes:browse"),
            InlineKeyboardButton(text="Собрать лайки", callback_data="likes:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "Главное меню.\nВыбери источник, чтобы запустить сессию, или настройку:",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("menu"))
async def on_menu(message: Message) -> None:
    await message.answer("Меню:", reply_markup=main_menu_kb())
