"""Распознавание сообщений Леонардо про лайки/симпатии.

Чистая функция: текст + список кнопок → kind. Никаких I/O.

Два типа уведомлений:
  - 'incoming'             — «Кому-то понравилась твоя анкета: …» вместе с
                              анкетой во вложении. Само сообщение И ЕСТЬ
                              профиль той, кто нас лайкнул.
  - 'mutual_notification'  — «Ты понравился N девушке, показать её?» без
                              медиа. Нужно нажать "показать" — следующее
                              сообщение бот пришлёт уже с анкетой.

Если ни одно не подошло — возвращаем None и адаптер дальше идёт по
обычному пути (профиль / dismiss).
"""

from __future__ import annotations

import re

INCOMING_PATTERNS = (
    re.compile(r"кому[- ]?то понрав", re.IGNORECASE),
    re.compile(r"тво[яе]\s*анкет", re.IGNORECASE),
    re.compile(r"тебе пришла симпат", re.IGNORECASE),
)

MUTUAL_NOTIF_PATTERNS = (
    re.compile(r"ты понрав[а-яё]*\s+\S+\s+девушк", re.IGNORECASE),
    re.compile(r"показать\s+(её|ее|их)", re.IGNORECASE),
    re.compile(r"взаимн", re.IGNORECASE),
)

SHOW_BUTTON_KEYWORDS = ("показать", "посмотреть", "смотреть", "show", "view", "да")

URL_PATTERNS = (
    re.compile(r"https?://vk\.com/\S+", re.IGNORECASE),
    re.compile(r"vk\.com/[A-Za-z0-9_.]+"),
    re.compile(r"https?://t\.me/\S+", re.IGNORECASE),
    re.compile(r"t\.me/[A-Za-z0-9_]+"),
    re.compile(r"@[A-Za-z0-9_]{4,}"),
)


def classify(msg_text: str, has_media: bool) -> str | None:
    """'incoming' | 'mutual_notification' | None."""
    text = msg_text or ""
    if has_media and _any_match(text, INCOMING_PATTERNS):
        return "incoming"
    if not has_media and _any_match(text, MUTUAL_NOTIF_PATTERNS):
        return "mutual_notification"
    return None


def pick_show_button(button_texts: list[str]) -> str | None:
    """Найти подходящую кнопку «показать»/«посмотреть» в клавиатуре."""
    for text in button_texts:
        low = (text or "").strip().lower()
        if not low:
            continue
        for kw in SHOW_BUTTON_KEYWORDS:
            if kw in low:
                return text
    return None


def extract_profile_url(msg_text: str) -> str | None:
    """Первая встретившаяся ссылка/@username в тексте."""
    text = msg_text or ""
    for pat in URL_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


def _any_match(text: str, patterns: tuple[re.Pattern, ...]) -> bool:
    return any(p.search(text) for p in patterns)
