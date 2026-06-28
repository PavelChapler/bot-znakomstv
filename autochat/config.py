"""Ключи настроек автопереписки и их дефолты.

Хранятся в общей таблице `settings` через `core.db.get_setting/set_setting`.
Тогглы — строки "0"/"1", тексты — как есть, числа — строковое десятичное.
"""

from __future__ import annotations

from core import db

KEY_ENABLED = "autochat_enabled"
KEY_DELAY_SEC = "autochat_delay_sec"
KEY_REPLY_DELAY_SEC = "autochat_reply_delay_sec"
KEY_MAX_MSGS = "autochat_max_msgs"
KEY_GOAL_PROMPT = "autochat_goal_prompt"
KEY_STYLE_PROMPT = "autochat_style_prompt"
KEY_TRANSCRIBE_VOICE = "autochat_transcribe_voice"

DEFAULT_DELAY_SEC = 300  # 5 минут — пауза перед первым сообщением (opener)
DEFAULT_REPLY_DELAY_SEC = 300  # 5 минут — пауза перед ответом в активном диалоге
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


async def get_reply_delay_sec() -> int:
    """Пауза перед нашим ответом в активном диалоге — чтобы не отвечать
    мгновенно, как робот. Отсчитывается от её последнего сообщения."""
    raw = await db.get_setting(KEY_REPLY_DELAY_SEC)
    try:
        return int(raw) if raw else DEFAULT_REPLY_DELAY_SEC
    except ValueError:
        return DEFAULT_REPLY_DELAY_SEC


async def get_max_msgs() -> int:
    raw = await db.get_setting(KEY_MAX_MSGS)
    try:
        return int(raw) if raw else DEFAULT_MAX_MSGS
    except ValueError:
        return DEFAULT_MAX_MSGS


async def _scoped(key: str, source: str | None) -> str | None:
    """Значение с фоллбэком: ключ для источника → общий ключ."""
    if source:
        v = await db.get_setting(f"{key}:{source}")
        if v:
            return v
    return await db.get_setting(key)


async def get_goal_prompt(source: str | None = None) -> str:
    val = await _scoped(KEY_GOAL_PROMPT, source)
    return val if val else DEFAULT_GOAL_PROMPT


async def get_style_prompt(source: str | None = None) -> str:
    val = await _scoped(KEY_STYLE_PROMPT, source)
    return val if val else DEFAULT_STYLE_PROMPT


async def is_transcribe_voice_enabled() -> bool:
    raw = await db.get_setting(KEY_TRANSCRIBE_VOICE, "1")  # default ON
    return raw == "1"


async def set_transcribe_voice(value: bool) -> None:
    await db.set_setting(KEY_TRANSCRIBE_VOICE, "1" if value else "0")
