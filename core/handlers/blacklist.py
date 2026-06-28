"""Хендлеры чёрного списка: кнопка «🚫 В ЧС» из потока сессии и
команда/меню /blacklist (просмотр + удаление).

callback'и: bladd:<source>:<external_id> — добавить; bllist — показать;
bldel:<id> — удалить. (Префиксы разные, чтобы startswith не пересекался.)
"""

from __future__ import annotations

from html import escape
from io import BytesIO

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

from core import blacklist, db
from core.registry import get_source_class

router = Router()


class BanManual(StatesGroup):
    waiting = State()


def _src_title(source: str) -> str:
    cls = get_source_class(source)
    return cls.title if cls else source


@router.callback_query(F.data.startswith("bladd:"))
async def cb_add(query: CallbackQuery) -> None:
    assert query.data is not None
    _, source, external_id = query.data.split(":", 2)
    added, label = await blacklist.add(source, external_id)
    await query.answer(
        f"🚫 «{label}» в ЧС — дизлайк при встрече" if added
        else f"«{label}» уже в ЧС",
        show_alert=False,
    )


@router.message(Command("blacklist"))
async def cmd_list(message: Message) -> None:
    await _show(message)


@router.callback_query(F.data == "bllist")
async def cb_list(query: CallbackQuery) -> None:
    if query.message and isinstance(query.message, Message):
        await _show(query.message)
    await query.answer()


async def _show(message: Message) -> None:
    rows = await db.blacklist_list()
    manual_btn = InlineKeyboardButton(
        text="➕ Добавить вручную (по описанию)", callback_data="blman"
    )
    if not rows:
        await message.answer(
            "Чёрный список пуст.\n"
            "Добавляй кнопкой «🚫 В ЧС» под анкетой во время сессии, либо "
            "«➕ вручную» — по описанию (имя + возраст + ключевые слова), "
            "когда у старой анкеты кнопки нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[manual_btn]]),
        )
        return
    lines = [f"<b>Чёрный список ({len(rows)}):</b>\n"]
    kb_rows: list[list[InlineKeyboardButton]] = [[manual_btn]]
    for r in rows[:50]:
        name = r.get("name") or r["external_id"]
        age = f", {r['age']}" if r.get("age") else ""
        lines.append(
            f"#{r['id']} · {escape(str(name))}{escape(age)} · "
            f"<i>{escape(_src_title(r['source']))}</i> · "
            f"<code>{escape(r['external_id'])}</code>"
        )
        kb_rows.append([InlineKeyboardButton(
            text=f"✖ удалить #{r['id']} ({name})",
            callback_data=f"bldel:{r['id']}",
        )])
    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("bldel:"))
async def cb_del(query: CallbackQuery) -> None:
    assert query.data is not None
    bl_id = int(query.data.split(":")[1])
    await db.blacklist_delete(bl_id)
    await query.answer("Удалено из ЧС")
    if query.message and isinstance(query.message, Message):
        await _show(query.message)


# ───────── ручное добавление по описанию (fuzzy) ─────────

_BAN_PROMPT = (
    "Вставь описание анкеты для ЧС (VK Знакомства) — имя и возраст в начале, "
    "например:\n<code>Соня, 18, обожаю дарк романы, сдаю на категорию А, "
    "трудоголик</code>\n\n"
    "Сматчу в ленте по совпадению имени + возраста + ключевых слов. /cancel — отмена."
)


@router.message(Command("ban"))
async def cmd_ban(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(_BAN_PROMPT)
    await state.set_state(BanManual.waiting)


@router.callback_query(F.data == "blman")
async def cb_ban_manual(query: CallbackQuery, state: FSMContext) -> None:
    if query.message and isinstance(query.message, Message):
        await query.message.answer(_BAN_PROMPT)
    await state.set_state(BanManual.waiting)
    await query.answer()


@router.message(BanManual.waiting, Command("cancel"))
async def cancel_ban(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отмена.")


async def _download_tg_photo(message: Message) -> bytes | None:
    if not message.photo or message.bot is None:
        return None
    try:
        buf = BytesIO()
        file = await message.bot.get_file(message.photo[-1].file_id)
        await message.bot.download_file(file.file_path, buf)
        return buf.getvalue()
    except Exception:
        return None


@router.message(BanManual.waiting)
async def save_ban(message: Message, state: FSMContext) -> None:
    # Фото или форвард анкеты — баним по перцептивному хэшу (user_id не нужен).
    if message.photo:
        blob = await _download_tg_photo(message)
        if not blob:
            await message.answer("Не смог скачать фото. Попробуй ещё или /cancel.")
            return
        added, label = await blacklist.add_photos("vk_dating", [blob])
        await state.clear()
        head = "🚫 Добавлено в ЧС по фото" if added else "ℹ️ Это фото уже в ЧС"
        await message.answer(
            f"{head}: <b>{escape(label)}</b> (VK Знакомства).\n"
            "В ленте дизлайкну анкету с похожим фото."
        )
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пусто. Вставь описание, пришли фото или /cancel.")
        return
    name, age, keywords = blacklist.parse_description(text)
    if not name or age is None:
        await message.answer(
            "Не разобрал имя/возраст — нужно имя и возраст в начале "
            "(«Соня, 18, ...»). Попробуй ещё раз или /cancel."
        )
        return
    added, _ = await blacklist.add_manual("vk_dating", name, age, keywords)
    await state.clear()
    need = min(3, len(keywords)) if keywords else 0
    kw = ", ".join(keywords[:8]) or "—"
    head = "🚫 Добавлено в ЧС" if added else "ℹ️ Уже было в ЧС"
    await message.answer(
        f"{head}: <b>{escape(name)}, {age}</b> (VK Знакомства)\n"
        f"Ключевые слова: <i>{escape(kw)}</i>\n\n"
        f"В ленте дизлайкну анкету с тем же именем и возрастом, где совпадёт "
        f"≥{need} из этих слов." if keywords else
        f"{head}: <b>{escape(name)}, {age}</b> (VK Знакомства)\n\n"
        f"⚠️ Ключевых слов нет — дизлайкну ЛЮБУЮ «{escape(name)}, {age}» "
        f"(возможны ложные баны однофамилиц). Лучше добавь описание."
    )
