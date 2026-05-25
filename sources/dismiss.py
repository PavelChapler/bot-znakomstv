"""Универсальная логика «закрыть рекламу / интерстициал и вернуться к ленте».

Применяется адаптерами, когда источник вместо очередной анкеты прислал что-то
другое: рекламу премиума, объявление, экран «бустани анкету» и т.п.

Логика не зависит от Telegram/VK/Android. Адаптер достаёт две вещи:
  - текст сообщения;
  - список текстов всех доступных кнопок;
и передаёт callback, который умеет «нажать» кнопку по её точному тексту
(в TG — `btn.click()` Telethon'а, в VK — отправить тот же текст сообщением).

Цепочка попыток (по нарастанию стоимости):
  1. heuristic — выбираем кнопку, в тексте которой есть слово/эмодзи из списка
     «отказ/закрыть/назад/позже/...».
  2. cache — заглядываем в SQLite: возможно, для такого экрана мы уже знаем
     нужную кнопку.
  3. LLM — спрашиваем Gemini «какую кнопку нажать чтобы вернуться к анкетам?»,
     результат сохраняем в кэш.
  4. hammer — последний рубеж: callable от источника, который делает что-то
     специфическое (например, для Леонардо TG — /myprofile + 1с + "1").
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Awaitable, Callable

import google.generativeai as genai

from config import load
from core import db

log = logging.getLogger(__name__)

# Подстроки, по наличию которых считаем кнопку «отказной/закрывающей».
# Регистронезависимо.
DISMISS_KEYWORDS = (
    "назад", "позже", "не сейчас", "пропустить", "пропуск",
    "отмена", "не интересно", "не надо", "не хочу", "вернуться",
    "понятно", "закрыть", "продолжить просмотр", "продолжить смотреть",
    "хорошо", "нет, спасибо", " нет", "нет ", "ок", "ok",
    "skip", "back", "later", "cancel", "close", "dismiss",
    "👎", "💔", "❌", "✖", "🚫",
)


async def attempt_dismiss(
    msg_text: str,
    button_texts: list[str],
    click_by_text: Callable[[str], Awaitable[bool]],
    hammer: Callable[[], Awaitable[None]] | None = None,
    source_name: str = "",
) -> bool:
    """Пробует «закрыть» текущий экран бота. True — если что-то нажали/послали.

    `click_by_text(text)` — callback источника: «нажать кнопку с таким текстом».
    Возвращает True, если получилось.

    `hammer` — корутина-«последний рубеж», специфичная для источника.
    """
    # 1. Эвристика
    picked = _pick_by_heuristic(button_texts)
    if picked is not None:
        log.info("dismiss[heuristic]: %r", picked)
        if await _try_click(click_by_text, picked):
            return True

    # 2. Кэш
    cache_key = _cache_key(msg_text, button_texts)
    cached = await db.get_dismiss_button(cache_key)
    if cached and cached in button_texts:
        log.info("dismiss[cache]: %r", cached)
        if await _try_click(click_by_text, cached):
            return True
    elif cached:
        log.info(
            "dismiss[cache]: записано %r, но такой кнопки нет — fall through",
            cached,
        )

    # 3. LLM
    llm_choice = await _ask_llm(msg_text, button_texts)
    if llm_choice and llm_choice in button_texts:
        log.info("dismiss[llm]: %r — клик и кэширую", llm_choice)
        await db.set_dismiss_button(cache_key, llm_choice, source_name)
        if await _try_click(click_by_text, llm_choice):
            return True

    # 4. Hammer
    if hammer is not None:
        log.info("dismiss[hammer]: запускаю последовательность источника")
        try:
            await hammer()
            return True
        except Exception:
            log.exception("dismiss[hammer] failed")

    return False


async def _try_click(
    click_by_text: Callable[[str], Awaitable[bool]], text: str
) -> bool:
    try:
        return await click_by_text(text)
    except Exception:
        log.exception("dismiss click failed for %r", text)
        return False


def _pick_by_heuristic(button_texts: list[str]) -> str | None:
    for text in button_texts:
        raw = text.strip()
        if not raw:
            continue
        low = raw.lower()
        for kw in DISMISS_KEYWORDS:
            if kw in low or kw in raw:
                return text
    return None


def _cache_key(msg_text: str, button_texts: list[str]) -> str:
    """Стабильный ключ кэша. Кнопки сортируем, чтобы перестановка не ломала."""
    sorted_btns = sorted(t.strip() for t in button_texts if t.strip())
    raw = (msg_text or "")[:200] + "||" + "|".join(sorted_btns)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


async def _ask_llm(msg_text: str, button_texts: list[str]) -> str | None:
    if not button_texts:
        return None

    cfg = load()
    if not cfg.gemini_api_key:
        return None

    genai.configure(api_key=cfg.gemini_api_key)
    model = genai.GenerativeModel(
        cfg.gemini_model,
        generation_config={
            "response_mime_type": "application/json",
            "temperature": 0.0,
        },
    )

    body = (msg_text or "(без текста)")[:1000]
    buttons_block = "\n".join(f"- {bt}" for bt in button_texts)
    prompt = (
        "Ты помогаешь автоматизировать просмотр анкет в боте знакомств.\n"
        "Вместо очередной анкеты бот прислал такое сообщение:\n\n"
        "---\n"
        f"{body}\n"
        "---\n\n"
        "Доступные кнопки (вернуть нужно ТОЧНЫЙ текст одной из них):\n"
        f"{buttons_block}\n\n"
        "Какую кнопку нажать, чтобы закрыть этот экран и вернуться к "
        "просмотру анкет?\n"
        'Если есть подходящая — верни JSON: {"button": "точный текст кнопки"}.\n'
        "Если ни одна не подходит (требует оплаты, не закрывает экран, и т.п.) "
        '— верни {"button": null}.\n'
    )

    try:
        resp = await model.generate_content_async(prompt)
        text = (resp.text or "").strip()
        data = json.loads(text)
        candidate = data.get("button")
        if isinstance(candidate, str) and candidate in button_texts:
            return candidate
    except Exception:
        log.exception("dismiss[llm] query failed")
    return None
