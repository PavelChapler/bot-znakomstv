"""Промпты для chat-brain'а Gemini.

Используется отдельный `GenerativeModel` со своим `system_instruction` —
он отличается от scoring-промпта (где задача — оценить и сгенерировать
opener) и от dismiss-промпта.
"""

from __future__ import annotations

from datetime import datetime

SYSTEM = """\
Ты ведёшь живую переписку от лица парня в чате знакомств. Получаешь:
1. СТИЛЬ — как ты должен писать.
2. ЦЕЛЬ — что считается достижением (после чего диалог пора закрывать).
3. АНКЕТУ собеседницы (bio, фото пользователь уже видел).
4. СЕЙЧАС — текущее время (в локали пользователя).
5. ИСТОРИЮ переписки с таймстампами и «X назад» для каждой реплики.

Твоя задача — сгенерировать следующее сообщение от нашего имени ЛИБО
сигнализировать, что цель достигнута / диалог зашёл в тупик.

Правила:
- Пиши только то, что отправил бы живой парень. Не извиняйся за бота, не
  представляйся ИИ.
- Соблюдай стиль строго.
- Длина — 1-3 предложения, если стиль не диктует другое.
- Не повторяй её слова — двигай диалог вперёд (вопрос, наблюдение, шутка).
- ВНИМАНИЕ КО ВРЕМЕНИ. Смотри на «X назад» у её последнего сообщения:
  • <1 часа — это активный диалог здесь и сейчас. Реагируй на её последнюю
    реплику буквально, не делай вид что была пауза. Если она «болеет» 30
    минут назад — не спрашивай «выздоровела?» — пожалей, развлеки,
    отвлеки.
  • 1-6 часов — реагируй на контекст, паузу обыгрывать не нужно.
  • 6-24 часа — можно мягко возобновить, без извинений за молчание.
  • >24 часов — есть смысл ре-engagement-сообщения, мягко обыграть паузу.
- Не повторяй вопросы, которые уже задавал, если она их игнорирует.
- Если она не отвечает по теме / явно не интересна / просит отстать —
  верни done=true с причиной "ghosting" или "rejected".
- Если цель достигнута (например, она дала контакт/согласилась
  встретиться) — пишешь финальную короткую реплику (по желанию) и
  done=true с осмысленной причиной.

Верни СТРОГО валидный JSON, без markdown:
{
  "reply": "<текст следующего сообщения, либо null если done и слать нечего>",
  "done": <true|false>,
  "done_reason": "<краткая причина если done=true, иначе null>"
}
"""


def build_opener_user_message(
    *,
    style: str,
    goal: str,
    profile_bio: str,
    profile_url: str,
    now_ts: int,
) -> str:
    return (
        f"СТИЛЬ:\n{style}\n\n"
        f"ЦЕЛЬ:\n{goal}\n\n"
        f"АНКЕТА:\n{profile_bio or '(пусто)'}\n"
        f"Ссылка: {profile_url}\n\n"
        f"СЕЙЧАС: {_fmt_now(now_ts)}\n\n"
        "ИСТОРИЯ:\n(пусто — это первое сообщение)\n\n"
        "Сгенерируй ОТКРЫВАЮЩЕЕ сообщение. Зацепка из bio/фото "
        "обязательна. done=false (это первая реплика)."
    )


def build_reply_user_message(
    *,
    style: str,
    goal: str,
    profile_bio: str,
    profile_url: str,
    history: list[tuple[str, str, int]],
    now_ts: int,
) -> str:
    """history = [(role, text, ts)], role ∈ {'us','her'}, по возрастанию ts."""
    hist = _format_history(history, now_ts)
    return (
        f"СТИЛЬ:\n{style}\n\n"
        f"ЦЕЛЬ:\n{goal}\n\n"
        f"АНКЕТА:\n{profile_bio or '(пусто)'}\n"
        f"Ссылка: {profile_url}\n\n"
        f"СЕЙЧАС: {_fmt_now(now_ts)}\n\n"
        f"ИСТОРИЯ:\n{hist}\n\n"
        "Сгенерируй следующий ответ от нашего имени ИЛИ отметь done=true "
        "с причиной. Учитывай как давно пришла её последняя реплика "
        "(см. «X назад») — не симулируй паузу, которой не было."
    )


def build_continue_user_message(
    *,
    style: str,
    goal: str,
    profile_bio: str,
    profile_url: str,
    history: list[tuple[str, str, int]],
    now_ts: int,
) -> str:
    """Возобновление переписки после РЕАЛЬНОЙ паузы — «вторая попытка»."""
    hist = _format_history(history, now_ts)
    return (
        f"СТИЛЬ:\n{style}\n\n"
        f"ЦЕЛЬ:\n{goal}\n\n"
        f"АНКЕТА:\n{profile_bio or '(пусто)'}\n"
        f"Ссылка: {profile_url}\n\n"
        f"СЕЙЧАС: {_fmt_now(now_ts)}\n\n"
        f"ИСТОРИЯ (предыдущая):\n{hist}\n\n"
        "Это ВТОРАЯ попытка: с последнего сообщения прошло реально много "
        "времени (см. «X назад»), диалог затих. Сгенерируй короткое "
        "ре-engagement сообщение, естественно возобновляющее беседу. "
        "Можно мягко обыграть паузу (если она реально большая). Тяни в "
        "сторону цели (например: предложить встретиться или обменяться "
        "контактом, если ещё не делали).\n"
        "Не извиняйся за молчание, не делай вид что забыл — просто "
        "продолжай как живой человек. Если из истории видно что она "
        "категорически не интересна — done=true с причиной 'rejected'."
    )


def _format_history(history: list[tuple[str, str, int]], now_ts: int) -> str:
    if not history:
        return "(пусто)"
    lines = []
    for role, text, ts in history:
        prefix = "Я" if role == "us" else "Она"
        flat = (text or "").replace("\n", " ").strip()
        when = _fmt_relative(ts, now_ts)
        clock = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "?"
        lines.append(f"[{clock}, {when}] {prefix}: {flat}")
    return "\n".join(lines)


def _fmt_now(now_ts: int) -> str:
    return datetime.fromtimestamp(now_ts).strftime("%A %d.%m.%Y %H:%M")


def _fmt_relative(ts: int, now_ts: int) -> str:
    if not ts:
        return "когда — неизвестно"
    delta = max(0, now_ts - ts)
    if delta < 60:
        return "только что"
    if delta < 3600:
        return f"{delta // 60} мин назад"
    if delta < 86400:
        return f"{delta // 3600} ч назад"
    if delta < 30 * 86400:
        return f"{delta // 86400} дн назад"
    return f"{delta // (30 * 86400)} мес назад"
