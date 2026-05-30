"""Ключи настроек автопереписки и их дефолты.

Хранятся в общей таблице `settings` через `core.db.get_setting/set_setting`.
Тогглы — строки "0"/"1", тексты — как есть, числа — строковое десятичное.
"""

from __future__ import annotations

from core import db

KEY_ENABLED = "autochat_enabled"
KEY_DELAY_SEC = "autochat_delay_sec"
KEY_MAX_MSGS = "autochat_max_msgs"
KEY_GOAL_PROMPT = "autochat_goal_prompt"
KEY_STYLE_PROMPT = "autochat_style_prompt"

DEFAULT_DELAY_SEC = 300  # 5 минут
DEFAULT_MAX_MSGS = 15

DEFAULT_GOAL_PROMPT = (
    "Познакомиться, найти общие интересы и аккуратно вывести на обмен "
    "контактом (Telegram/WhatsApp) или предложение встретиться. Когда "
    "пользовательница согласилась на контакт или встречу — цель достигнута."
)

DEFAULT_STYLE_PROMPT = (
    "Дружелюбно, искренне, с лёгким налётом юмора. Без шаблонов и "
    "пикапа. Короткие реплики (1-3 предложения). На «ты». Не рассыпайся "
    "в комплиментах сразу, веди диалог через интересы из её bio."
)


async def is_enabled() -> bool:
    return (await db.get_setting(KEY_ENABLED, "0")) == "1"


async def set_enabled(value: bool) -> None:
    await db.set_setting(KEY_ENABLED, "1" if value else "0")


async def get_delay_sec() -> int:
    raw = await db.get_setting(KEY_DELAY_SEC)
    try:
        return int(raw) if raw else DEFAULT_DELAY_SEC
    except ValueError:
        return DEFAULT_DELAY_SEC


async def get_max_msgs() -> int:
    raw = await db.get_setting(KEY_MAX_MSGS)
    try:
        return int(raw) if raw else DEFAULT_MAX_MSGS
    except ValueError:
        return DEFAULT_MAX_MSGS


async def get_goal_prompt() -> str:
    val = await db.get_setting(KEY_GOAL_PROMPT)
    return val if val else DEFAULT_GOAL_PROMPT


async def get_style_prompt() -> str:
    val = await db.get_setting(KEY_STYLE_PROMPT)
    return val if val else DEFAULT_STYLE_PROMPT
