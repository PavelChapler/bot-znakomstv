"""/likes и /collect_likes.

/likes — браузер пула, карточка-плеер с edit_media (одно сообщение).
/collect_likes — однократный обход накопившихся уведомлений в выбранном
источнике без обычного скоринга.
"""

from __future__ import annotations

import asyncio
import logging
import os
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from core import db
from core.handlers import sources as sources_handler
from core.registry import all_sources, get_source_class

log = logging.getLogger(__name__)
router = Router()

KIND_LABEL = {"mutual": "✨ взаимная", "incoming": "💌 входящий"}

# Сколько максимум итераций next_profile() сделать в /collect_likes.
# Каждая итерация — это либо обработанное likes-уведомление, либо первый
# обычный профиль (после которого мы выходим).
COLLECT_MAX_ITERS = 30

_collecting: bool = False


@router.message(Command("likes"))
async def cmd_likes(message: Message) -> None:
    await _open_pool(message)


@router.callback_query(F.data == "likes:browse")
async def cb_browse(query: CallbackQuery) -> None:
    if query.message and isinstance(query.message, Message):
        await _open_pool(query.message)
    await query.answer()


async def _open_pool(message: Message) -> None:
    items = await db.list_liked()
    if not items:
        total, mutual, incoming = await db.count_liked()
        await message.answer(
            "Пул пуст. Лайки появятся после сессии в Leonardo или /collect_likes.\n"
            f"(всего: {total}, ✨ {mutual}, 💌 {incoming})"
        )
        return
    await _send_card(message, items, index=0)


@router.callback_query(F.data.startswith("likes:open:"))
async def cb_open(query: CallbackQuery) -> None:
    assert query.data is not None
    index = int(query.data.split(":")[2])
    items = await db.list_liked()
    if not items:
        await query.answer("Пул пуст", show_alert=True)
        return
    index = max(0, min(index, len(items) - 1))
    await _edit_card(query, items, index)


@router.callback_query(F.data.startswith("likes:del:"))
async def cb_delete(query: CallbackQuery) -> None:
    assert query.data is not None
    index = int(query.data.split(":")[2])
    items = await db.list_liked()
    if not items or index >= len(items):
        await query.answer("Уже удалено", show_alert=True)
        return
    victim = items[index]
    paths = await db.delete_liked(int(victim["id"]))
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass
    await query.answer("Удалено")

    items = await db.list_liked()
    if not items:
        if query.message and isinstance(query.message, Message):
            try:
                await query.message.delete()
            except Exception:
                pass
            await query.message.answer("Пул пуст.")
        return
    new_index = min(index, len(items) - 1)
    await _edit_card(query, items, new_index)


async def _send_card(message: Message, items: list[dict], index: int) -> None:
    item = items[index]
    photo = _first_photo(item)
    caption = _format_caption(item, index, len(items))
    kb = _kb(index, len(items))
    if photo is None:
        await message.answer(caption, reply_markup=kb)
    else:
        await message.answer_photo(
            FSInputFile(photo), caption=caption, reply_markup=kb
        )
    await db.mark_liked_viewed(int(item["id"]))


async def _edit_card(
    query: CallbackQuery, items: list[dict], index: int
) -> None:
    item = items[index]
    photo = _first_photo(item)
    caption = _format_caption(item, index, len(items))
    kb = _kb(index, len(items))
    msg = query.message
    if not isinstance(msg, Message):
        await query.answer()
        return
    try:
        if photo is not None and msg.photo:
            await msg.edit_media(
                InputMediaPhoto(media=FSInputFile(photo), caption=caption),
                reply_markup=kb,
            )
        elif photo is not None:
            # Старая карточка была текстовой — удалим и пришлём новую с фото.
            try:
                await msg.delete()
            except Exception:
                pass
            await msg.answer_photo(
                FSInputFile(photo), caption=caption, reply_markup=kb
            )
        else:
            await msg.edit_caption(caption=caption, reply_markup=kb)
    except Exception:
        log.exception("edit card failed; пересылаю новой")
        if photo is not None:
            await msg.answer_photo(
                FSInputFile(photo), caption=caption, reply_markup=kb
            )
        else:
            await msg.answer(caption, reply_markup=kb)
    await db.mark_liked_viewed(int(item["id"]))
    await query.answer()


def _kb(index: int, total: int) -> InlineKeyboardMarkup:
    prev_idx = (index - 1) % total
    next_idx = (index + 1) % total
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="← Назад", callback_data=f"likes:open:{prev_idx}"),
            InlineKeyboardButton(text=f"{index + 1}/{total}", callback_data="likes:noop"),
            InlineKeyboardButton(text="Вперёд →", callback_data=f"likes:open:{next_idx}"),
        ],
        [
            InlineKeyboardButton(text="🤖 В авточат", callback_data=f"likes:reuse:{index}"),
            InlineKeyboardButton(text="✖ Удалить", callback_data=f"likes:del:{index}"),
        ],
    ])


def _format_caption(item: dict, index: int, total: int) -> str:
    kind = item.get("kind", "")
    badge = KIND_LABEL.get(kind, kind)
    bio = (item.get("bio") or "").strip()
    if len(bio) > 600:
        bio = bio[:597] + "..."
    url = item.get("profile_url") or ""
    src = item.get("source", "")
    lines = [
        f"<b>{escape(badge)}</b>  ({escape(src)})  {index + 1}/{total}",
    ]
    if bio:
        lines.append(escape(bio))
    if url:
        lines.append(f'<a href="{escape(url)}">{escape(url)}</a>')
    return "\n\n".join(lines)


def _first_photo(item: dict) -> str | None:
    for p in item.get("photo_paths") or []:
        if isinstance(p, str) and os.path.exists(p):
            return p
    return None


@router.callback_query(F.data == "likes:noop")
async def cb_noop(query: CallbackQuery) -> None:
    await query.answer()


@router.callback_query(F.data.startswith("likes:reuse:"))
async def cb_reuse(query: CallbackQuery) -> None:
    """Кнопка «🤖 В авточат» в карточке пула.

    Орчестратор reuse_from_pool делает всё: импорт DM-истории, выбор
    opener/continuation, отправка. Здесь только маршрутизация + ack.
    """
    assert query.data is not None
    index = int(query.data.split(":")[2])
    items = await db.list_liked()
    if not items or index >= len(items):
        await query.answer("Запись пропала", show_alert=True)
        return
    pool_id = int(items[index]["id"])

    # Импорты внутри: модуль autochat можно удалить — пропадёт только эта
    # кнопка, остальная карточка работать продолжит.
    try:
        from autochat.engine import get_engine
        from autochat.reuse import reuse_from_pool
    except ImportError:
        await query.answer("autochat-модуль отсутствует", show_alert=True)
        return

    engine = get_engine()
    if engine is None:
        await query.answer("Autochat engine не запущен", show_alert=True)
        return

    await query.answer("Перезапускаю авточат...")
    msg = query.message
    if not isinstance(msg, Message):
        return
    chat_id = msg.chat.id
    bot = query.bot

    async def runner() -> None:
        try:
            result = await reuse_from_pool(pool_id, engine)
        except Exception as e:
            log.exception("reuse_from_pool crashed")
            if bot is not None:
                try:
                    await bot.send_message(
                        chat_id, f"🤖 Ошибка автореактивации: {e}",
                    )
                except Exception:
                    pass
            return
        if bot is None:
            return
        prefix = "🤖" if result.get("ok") else "⚠️"
        text = f"{prefix} {result.get('message', '')}"
        sent_text = result.get("sent_text")
        if sent_text:
            text += f"\n\nОтправлено:\n<code>{escape(str(sent_text))}</code>"
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.exception("autochat reuse notify failed")

    engine.schedule_side_task(runner())


@router.message(Command("collect_likes"))
async def cmd_collect_likes(message: Message) -> None:
    await _show_collect_menu(message)


@router.callback_query(F.data == "likes:menu")
async def cb_collect_menu(query: CallbackQuery) -> None:
    if query.message and isinstance(query.message, Message):
        await _show_collect_menu(query.message)
    await query.answer()


async def _show_collect_menu(message: Message) -> None:
    kb_rows = [
        [InlineKeyboardButton(
            text=cls.title, callback_data=f"likes:collect:{cls.name}"
        )]
        for cls in all_sources()
        if cls.name.startswith("leonardo_") or cls.name == "vk_dating"
    ]
    if not kb_rows:
        await message.answer("Нет источников для сбора лайков.")
        return
    await message.answer(
        "Из какого источника собрать накопившиеся лайки?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


@router.callback_query(F.data.startswith("likes:collect:"))
async def cb_collect(query: CallbackQuery) -> None:
    global _collecting
    assert query.data is not None
    name = query.data.split(":", 2)[2]
    cls = get_source_class(name)
    if cls is None:
        await query.answer("Источник не найден")
        return
    if _collecting:
        await query.answer("Сбор уже идёт", show_alert=True)
        return
    active = sources_handler._current
    if active is not None and active.running:
        await query.answer(
            "Сейчас идёт обычная сессия — сначала /stop", show_alert=True
        )
        return
    if not query.message or not isinstance(query.message, Message):
        await query.answer()
        return

    _collecting = True
    chat = query.message
    await query.answer()
    await chat.answer(f"Собираю лайки из «{cls.title}»...")

    try:
        source = cls()
    except Exception as e:
        log.exception("collect_likes: не создал источник")
        await chat.answer(f"Не получилось создать источник: {e}")
        _collecting = False
        return

    before_total, before_mutual, before_incoming = await db.count_liked()
    scan_diag: list[dict] = []
    scan_saved = 0
    scan_dups = 0
    feed_iters = 0
    stopped_reason = "не запускалась фидовая фаза"
    try:
        await source.start()

        # 1. История: сохранить все incoming, скопившиеся в чате
        scan_result = await source.scan_history_for_incoming()
        scan_saved = int(scan_result.get("saved", 0))
        scan_dups = int(scan_result.get("duplicates", 0))
        scan_diag = list(scan_result.get("diag", []))

        # 2. Свежий хвост: пройтись next_profile-ом, чтобы поймать mutual-
        #    уведомление наверху (и догнать ещё incoming, появившиеся
        #    непосредственно в текущем «положении» фида).
        stopped_reason = "лимит итераций"
        for _ in range(COLLECT_MAX_ITERS):
            feed_iters += 1
            profile = await source.next_profile()
            if profile is None:
                stopped_reason = "источник вернул None (тупик/нет нового)"
                break
            stopped_reason = (
                f"наткнулся на обычный профиль (id={profile.external_id}) — "
                "не трогаю, остановка"
            )
            break
    except Exception as e:
        log.exception("collect_likes: ошибка во время сбора")
        stopped_reason = f"ошибка: {e}"
    finally:
        try:
            await source.stop()
        except Exception:
            log.exception("collect_likes: source.stop failed")
        _collecting = False

    after_total, after_mutual, after_incoming = await db.count_liked()
    new_total = after_total - before_total
    new_mutual = after_mutual - before_mutual
    new_incoming = after_incoming - before_incoming

    summary = (
        f"<b>Готово.</b>\n"
        f"Скан истории: сохранено <b>{scan_saved}</b> incoming, "
        f"дублей {scan_dups}, просмотрено {len(scan_diag)} сообщений.\n"
        f"Фидовая фаза: итераций {feed_iters}, стоп: {escape(stopped_reason)}\n"
        f"Итого новых: <b>{new_total}</b> (✨ {new_mutual}, 💌 {new_incoming}).\n"
        f"Всего в пуле: {after_total} (✨ {after_mutual}, 💌 {after_incoming})."
    )
    await chat.answer(summary)

    # Диагностический хвост: последние 15 сообщений с классификацией —
    # помогает понять, почему ничего не нашлось (паттерны/тексты).
    if scan_diag:
        tail = scan_diag[-15:]
        lines = ["<b>Последние сообщения (диагностика):</b>"]
        for d in tail:
            mark = {
                "incoming": "💌",
                "mutual_notification": "✨?",
                "mutual_match": "✨",
                None: "·",
            }.get(d.get("kind"), "?")
            media = "📷" if d.get("has_media") else "  "
            lines.append(
                f"{mark} {media} <code>{d.get('id')}</code>  "
                f"{escape(d.get('text') or '(пусто)')}"
            )
        await chat.answer("\n".join(lines))
    await asyncio.sleep(0)
