from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from core.handlers.goal import get_message_enabled
from core.registry import all_sources

router = Router()

# Текст постоянной кнопки внизу экрана. Нажатие = пользователь шлёт этот
# текст боту, который мы перехватываем и показываем inline-меню.
MENU_BUTTON_TEXT = "📋 Меню"


def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MENU_BUTTON_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
    )


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
    rows.append(
        [
            InlineKeyboardButton(text="🤖 Авточат", callback_data="autochat:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    # Первое сообщение прибивает постоянную кнопку «📋 Меню» к чату.
    await message.answer(
        "Кнопка 📋 Меню теперь всегда снизу — можно не печатать /start.",
        reply_markup=main_reply_kb(),
    )
    await message.answer(
        "Главное меню. Выбери источник или настройку:",
        reply_markup=await main_menu_kb(),
    )


@router.message(Command("menu"))
async def on_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Меню:", reply_markup=await main_menu_kb())


# StateFilter('*') — ловим нажатие даже когда FSM ждёт ввод (цели/порога/
# стиля), и сначала аккуратно сбрасываем состояние.
@router.message(StateFilter("*"), F.text == MENU_BUTTON_TEXT)
async def on_menu_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Меню:", reply_markup=await main_menu_kb())
