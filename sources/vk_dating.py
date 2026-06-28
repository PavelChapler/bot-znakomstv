"""Адаптер «VK Знакомства» (сервис vk.com/dating).

В отличие от Леонардо это НЕ бот-сообщество: у VK Знакомств свой бэкенд
`dating.vk.ru/api/<method>` (тело — multipart/form-data). Поэтому общий
`core.vk_throttle.vk_post` (он бьёт в api.vk.com/method) здесь не подходит —
у нас собственный httpx-клиент со своим троттлом.

Авторизация: `auth.signIn(launch_url)` → сессионный `_token`, которым
подписаны все остальные вызовы. `launch_url` — это подписанные VK launch-
параметры мини-приложения; они живут ограниченное время и снимаются из
браузера (см. инструкцию в .env.example). Сгенерировать их из обычного
VK-токена нельзя (проверено: VK ID web-флоу требует cookie-сессию).

Поток анкет: `dating.getRecommendedUsers` → карточки с `id`, `name`, `age`,
фото в `stories[].url`, анкетой в `form` и телеметрией `extra.meta`
(её обязательно передавать в like/dislike). `dating.like` / `dating.dislike`
принимают `user_id` + `meta`; в ответе like есть `remaining` — дневной лимит.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from config import load
from core.models import Profile
from sources.base import DatingSource

log = logging.getLogger(__name__)

API_BASE = "https://dating.vk.ru/api"
API_V = "1.13"
# Экран мини-аппа — сервер ждёт это поле в вызовах с _token. Главная страница.
SCREEN = '{"pageId":"/","params":{}}'
# Origin/Referer мини-приложения (сняты из рабочего HAR). Если VK сменит
# домен и сервер начнёт ругаться — поправить здесь.
APP_ORIGIN = "https://stage-app7058363-409ba0d0d24a.pages.vk-apps.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
# Шаблон _agent, если VK_DATING_AGENT не задан. {vkid} подставляется из
# launch_url. Версии клиента некритичны для работы API.
AGENT_TEMPLATE = (
    "love1 version:1.1.0 build:70 commit:44a6350114 env:production "
    "platform:desktop_web client:0.0/0/web/none lang:ru tz:10800 "
    "vkid:{vkid} screen:d/1280x720/1.0"
)

# Сколько анкет тянуть за один запрос ленты.
FEED_COUNT = 20
# Максимум фото из анкеты качаем (scorer всё равно смотрит первые несколько).
MAX_PHOTOS = 4
# Защита от зацикливания на негодных карточках за один next_profile.
MAX_SCAN_PER_CALL = 60
# Минимальный интервал между вызовами dating.vk.ru (≈3 req/sec).
MIN_INTERVAL_SEC = 0.34


class VKDatingSource(DatingSource):
    name = "vk_dating"
    title = "VK Знакомства"

    def __init__(self) -> None:
        cfg = load()
        if not cfg.vk_dating_launch_url:
            raise RuntimeError(
                "VK_DATING_LAUNCH_URL не заполнен в .env (как достать — "
                "см. комментарий в .env.example)."
            )
        self._launch_url = cfg.vk_dating_launch_url
        self._city_id = cfg.vk_dating_city_id or 0
        m = re.search(r"vk_user_id=(\d+)", self._launch_url)
        self._uid = m.group(1) if m else "0"
        self._agent = cfg.vk_dating_agent or AGENT_TEMPLATE.format(vkid=self._uid)

        self._http: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._session: str | None = None
        self._feed: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._remaining: int | None = None
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    # ───────── lifecycle ─────────

    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=20.0, headers={
            "Origin": APP_ORIGIN,
            "Referer": APP_ORIGIN + "/",
            "User-Agent": USER_AGENT,
        })
        self._feed = []
        self._current = None
        self._remaining = None
        await self._sign_in()

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _sign_in(self) -> None:
        self._session = f"{self._uid}_{int(time.time() * 1000)}"
        data = await self._call("auth.signIn", {
            "launch_url": self._launch_url,
            "_v": API_V,
            "_agent": self._agent,
            "_session": self._session,
        })
        if not isinstance(data, dict) or "token" not in data:
            err = data.get("error") if isinstance(data, dict) else None
            if "launch_url" in str(err):
                raise RuntimeError(
                    "VK Знакомства: launch_url протух/невалиден. Обнови "
                    "VK_DATING_LAUNCH_URL в .env (инструкция — в .env.example)."
                )
            raise RuntimeError(f"VK Знакомства: signIn не удался: {err or data}")
        self._token = data["token"]
        user = data.get("user") or {}
        log.info("VK Знакомства: signIn ok, user=%s %s",
                 user.get("id"), user.get("name"))

    # ───────── feed ─────────

    async def next_profile(self) -> Profile | None:
        for _ in range(MAX_SCAN_PER_CALL):
            if not self._feed:
                await self._load_feed()
            if not self._feed:
                log.info("VK Знакомства: лента пуста — останавливаюсь")
                return None

            card = self._feed.pop(0)
            uid = card.get("id")
            if not uid:
                continue
            meta = (card.get("extra") or {}).get("meta", "")
            # Запоминаем текущую анкету: like()/skip() подействуют на неё.
            self._current = {"user_id": uid, "meta": meta}

            if card.get("is_deleted") or card.get("is_blocked"):
                # Негодную карточку дизлайкаем, чтобы убрать из ленты.
                await self._react("dating.dislike", uid, meta)
                continue

            profile = await self._parse_card(card)
            if profile is None:
                log.info("VK Знакомства: анкета %s без фото — auto-skip", uid)
                await self._react("dating.dislike", uid, meta)
                continue
            return profile

        log.warning("VK Знакомства: %d карточек подряд негодны — стоп",
                    MAX_SCAN_PER_CALL)
        return None

    async def _load_feed(self) -> None:
        # Simple-лента не требует city_id и отдаёт полные карточки (age, form,
        # extra.meta, stories) — берём её по умолчанию. Полная лента
        # (getRecommendedUsers) без city_id отвечает request_invalid, поэтому
        # её используем только когда город задан явно в конфиге.
        if self._city_id:
            method = "dating.getRecommendedUsers"
            fields: dict[str, Any] = {
                "count": FEED_COUNT, "city_id": self._city_id,
                **self._auth_fields(),
            }
        else:
            method = "dating.getRecommendedUsersSimple"
            fields = {"count": FEED_COUNT, **self._auth_fields()}
        data = await self._call(method, fields)
        if not isinstance(data, dict):
            self._feed = []
            return
        if "remaining" in data:
            self._remaining = data.get("remaining")
        users = data.get("users") or []
        self._feed = [u for u in users if u.get("id")]
        log.info("VK Знакомства: лента +%d (remaining=%s)",
                 len(self._feed), self._remaining)

    async def _parse_card(self, card: dict[str, Any]) -> Profile | None:
        photos: list[bytes] = []
        for story in (card.get("stories") or []):
            if len(photos) >= MAX_PHOTOS:
                break
            if story.get("type") != "photo":
                continue
            url = story.get("url") or story.get("large_url") or story.get("medium_url")
            if not url:
                continue
            blob = await self._download(url)
            if blob:
                photos.append(blob)
        if not photos:
            return None
        return Profile(
            source=self.name,
            external_id=str(card.get("id")),
            bio=self._build_bio(card),
            photos=photos,
        )

    @staticmethod
    def _build_bio(card: dict[str, Any]) -> str:
        parts: list[str] = []
        name = (card.get("name") or "").strip()
        age = card.get("age")
        head = f"{name}, {age}" if name and age else (name or "")
        if head:
            parts.append(head)

        form = card.get("form") or {}
        about = (form.get("about") or "").strip()
        if about:
            parts.append(about)

        lines: list[str] = []
        if form.get("height"):
            lines.append(f"Рост: {form['height']}")
        if form.get("target"):
            lines.append(f"Цель: {form['target']}")
        attrs = [
            f"{label}: {form[key]}"
            for key, label in (
                ("family", "семья"), ("kids", "дети"),
                ("smoking", "курит"), ("alcohol", "алкоголь"),
            )
            if form.get(key)
        ]
        if attrs:
            lines.append(", ".join(attrs))
        for key, label in (("work", "Работа"), ("movies", "Фильмы"),
                           ("books", "Книги")):
            val = (form.get(key) or "").strip()
            if val:
                lines.append(f"{label}: {val}")
        interests = form.get("interests") or []
        if interests:
            lines.append("Интересы: " + ", ".join(map(str, interests)))
        if lines:
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    # ───────── actions ─────────

    async def like(self, message: str | None = None) -> None:
        cur = self._current
        if not cur:
            log.warning("VK Знакомства: like без текущей анкеты")
            return
        if message:
            # У VK Знакомств в наблюдаемом API лайка-с-сообщением нет —
            # деградируем до обычного лайка (контракт DatingSource это разрешает).
            log.info("VK Знакомства: лайк-с-сообщением не поддерживается — "
                     "обычный лайк")
        if self._remaining is not None and self._remaining <= 0:
            log.warning("VK Знакомства: дневной лимит лайков исчерпан — "
                        "лайк не отправлен")
            return
        data = await self._react("dating.like", cur["user_id"], cur["meta"])
        if isinstance(data, dict) and "remaining" in data:
            self._remaining = data.get("remaining")

    async def skip(self) -> None:
        cur = self._current
        if not cur:
            log.warning("VK Знакомства: skip без текущей анкеты")
            return
        await self._react("dating.dislike", cur["user_id"], cur["meta"])

    async def _react(self, method: str, uid: Any, meta: str) -> dict[str, Any] | None:
        return await self._call(method, {
            "user_id": str(uid),
            "meta": meta or "",
            **self._auth_fields(),
            "_screen": SCREEN,
        })

    # ───────── http helpers ─────────

    def _auth_fields(self) -> dict[str, Any]:
        return {
            "_token": self._token or "",
            "_v": API_V,
            "_agent": self._agent,
            "_session": self._session or "",
            "_screen": SCREEN,
        }

    async def _download(self, url: str) -> bytes | None:
        assert self._http is not None
        try:
            resp = await self._http.get(url)
            if resp.status_code == 200:
                return resp.content
            log.warning("VK Знакомства: фото HTTP %d", resp.status_code)
        except Exception:
            log.exception("VK Знакомства: не скачал фото")
        return None

    async def _call(self, method: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Throttled multipart-вызов dating.vk.ru. Возвращает распарсенный
        JSON (с ключом 'error' при ошибке) или None при сетевом сбое."""
        assert self._http is not None
        await self._throttle()
        # multipart/form-data из обычных полей: значение как (None, str).
        files = {k: (None, str(v)) for k, v in fields.items()}
        try:
            resp = await self._http.post(f"{API_BASE}/{method}", files=files)
        except Exception:
            log.exception("VK Знакомства: сетевой сбой в %s", method)
            return None
        try:
            data = resp.json()
        except Exception:
            log.error("VK Знакомства: ответ %s не JSON (HTTP %d)",
                      method, resp.status_code)
            return None
        if isinstance(data, dict) and data.get("error"):
            log.error("VK Знакомства: ошибка в %s: %r", method, data.get("error"))
        return data

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = MIN_INTERVAL_SEC - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
