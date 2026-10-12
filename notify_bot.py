#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Телеграм-бот для GitHub Actions: редактирует одно закреплённое сообщение,
всегда показывая либо текущее идущее занятие (со ссылкой/паролем), либо,
если сейчас перерыв, время и тему следующего.

Запускается по расписанию (cron в .github/workflows/notify.yml) — каждый
запуск одноразовый: прочитал расписание, посчитал статус, отредактировал
сообщение, вышел. Расписание — в schedule.json (без паролей/ссылок,
можно хранить в публичном репозитории). Пароли и ссылки — в секрете
CRED_JSON (переменная окружения), никогда не попадают в код.
"""

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
CRED_JSON = os.environ.get("CRED_JSON", "").strip()
SCHEDULE_PATH = os.environ.get("SCHEDULE_PATH", "schedule.json")
SCHEDULE_YEAR = int(os.environ.get("SCHEDULE_YEAR", "2026"))
# Сколько минут считать занятие "идущим", если не знаем точного времени
# окончания (в schedule.json его нет) — используется только для ПОСЛЕДНЕГО
# занятия дня; для остальных конец = начало следующего занятия в тот же день.
DEFAULT_DURATION_MINUTES = int(os.environ.get("DEFAULT_DURATION_MINUTES", "100"))
MESSAGE_ID = os.environ.get("MESSAGE_ID", "").strip()
PIN_MESSAGE = os.environ.get("PIN_MESSAGE", "true").lower() in ("1", "true", "yes")
TIMEZONE_NAME = os.environ.get("TIMEZONE", "Europe/Moscow")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")
FAKE_NOW = os.environ.get("FAKE_NOW", "").strip()

TZ = ZoneInfo(TIMEZONE_NAME) if ZoneInfo else None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("notify-bot")

API_BASE = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class Lesson:
    date_str: str
    weekday: str
    time_str: str
    lesson_type: str
    discipline: str
    teacher: str
    room: str
    password: str
    link: str
    start: datetime

    def details(self) -> str:
        """Полный блок с данными занятия (дата, предмет, пароль, ссылка)."""
        pass_line = "" if not self.password or self.password.lower() in ("без пароля", "нет", "") \
            else f"🔑 Пароль: <code>{escape_html(self.password)}</code>\n"
        link_line = f'🔗 <a href="{escape_html(self.link)}">Подключиться к вебинару</a>\n' if self.link else ""
        return (
            f"📅 {escape_html(self.date_str)} ({escape_html(self.weekday)}), {escape_html(self.time_str)}\n"
            f"📘 {escape_html(self.lesson_type)}: {escape_html(self.discipline)}\n"
            f"👤 {escape_html(self.teacher)}\n"
            f"🏫 Ауд. {escape_html(self.room)}\n"
            f"{pass_line}"
            f"{link_line}"
        ).strip()

    def card(self, status: str, next_line: str = "") -> str:
        tail = f"\n\n{next_line}" if next_line else ""
        return f"{status}\n\n{self.details()}{tail}".strip()


def escape_html(text) -> str:
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_lessons() -> list:
    creds = json.loads(CRED_JSON) if CRED_JSON else {}
    with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    lessons = []
    for item in raw:
        date_str = item["date"]
        time_str = item["time"]
        room = item["room"]
        cred = creds.get(room, {})
        link = cred.get("link", "")
        if not link:
            continue
        try:
            day, month = (int(x) for x in date_str.split("."))
            hour, minute = (int(x) for x in time_str.split(":"))
            start = datetime(SCHEDULE_YEAR, month, day, hour, minute, tzinfo=TZ)
        except (ValueError, IndexError):
            log.warning("Пропускаю строку с некорректной датой/временем: %r %r", date_str, time_str)
            continue

        lessons.append(Lesson(
            date_str=date_str,
            weekday=item.get("day", ""),
            time_str=time_str,
            lesson_type=item.get("type", ""),
            discipline=item.get("discipline", ""),
            teacher=item.get("teacher", ""),
            room=room,
            password=cred.get("password", ""),
            link=link,
            start=start,
        ))

    lessons.sort(key=lambda l: l.start)
    return lessons


def tg_call(method: str, **params):
    if DRY_RUN:
        log.info("[DRY_RUN] %s(%s)", method, {k: v for k, v in params.items() if k != "text"})
        if "text" in params:
            log.info("[DRY_RUN] текст сообщения:\n%s", params["text"])
        return {"ok": True, "result": {"message_id": params.get("message_id") or -1}}

    url = API_BASE.format(token=BOT_TOKEN, method=method)
    resp = requests.post(url, json=params, timeout=15)
    data = resp.json()
    if not data.get("ok"):
        desc = str(data.get("description", ""))
        if "message is not modified" in desc.lower():
            # текст не изменился с прошлого раза — это не ошибка
            return {"ok": True, "result": {"message_id": params.get("message_id")}, "unchanged": True}
        log.error("Telegram API ошибка (%s): %s", method, data)
    return data


def send_message(text: str) -> Optional[int]:
    data = tg_call("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    return data["result"]["message_id"] if data.get("ok") else None


def edit_message(message_id: int, text: str) -> bool:
    data = tg_call("editMessageText", chat_id=CHAT_ID, message_id=message_id, text=text,
                    parse_mode="HTML", disable_web_page_preview=True)
    return bool(data.get("ok"))


def pin_message(message_id: int) -> None:
    tg_call("pinChatMessage", chat_id=CHAT_ID, message_id=message_id, disable_notification=True)


def get_now() -> datetime:
    if FAKE_NOW:
        return datetime.strptime(FAKE_NOW, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    return datetime.now(TZ)


def ensure_message() -> Optional[int]:
    """Берём id сообщения из переменной MESSAGE_ID (зафиксирована один раз
    в настройках репозитория). Если она не задана — создаём и закрепляем
    новое сообщение и выводим его id в лог, чтобы сохранить в MESSAGE_ID."""
    if MESSAGE_ID:
        try:
            return int(MESSAGE_ID)
        except ValueError:
            log.error("MESSAGE_ID=%r — не число.", MESSAGE_ID)
            return None

    message_id = send_message("🕐 Загружаю расписание…")
    if message_id is not None:
        if PIN_MESSAGE and not DRY_RUN:
            pin_message(message_id)
        log.info(
            "MESSAGE_ID не задан — создано новое сообщение id=%s. "
            "Сохраните его в репозитории: Settings → Secrets and variables → "
            "Actions → Variables → New repository variable → MESSAGE_ID = %s, "
            "иначе каждый запуск будет создавать новое сообщение.",
            message_id, message_id,
        )
    return message_id


def lesson_end(lesson: Lesson, lessons: list) -> datetime:
    """Момент, когда занятие считается закончившимся: не позже, чем через
    DEFAULT_DURATION_MINUTES, и не позже начала следующего занятия в тот
    же день (если пары идут подряд без большого перерыва — не захватываем
    чужое время)."""
    by_duration = lesson.start + timedelta(minutes=DEFAULT_DURATION_MINUTES)
    idx = lessons.index(lesson)
    if idx + 1 < len(lessons):
        nxt = lessons[idx + 1]
        if nxt.start.date() == lesson.start.date():
            return min(by_duration, nxt.start)
    return by_duration


def build_status_text(lessons: list, now: datetime) -> Optional[str]:
    if not lessons:
        return None

    current = None
    nxt = None
    nxt_idx = None
    for i, lesson in enumerate(lessons):
        if lesson.start <= now < lesson_end(lesson, lessons):
            current = lesson
            nxt = lessons[i + 1] if i + 1 < len(lessons) else None
            nxt_idx = i + 1 if nxt else None
            break
        if lesson.start > now:
            nxt = lesson
            nxt_idx = i
            break

    if current is not None:
        next_line = f"➡️ Далее:\n{nxt.details()}" if nxt else "✅ Это последнее занятие по расписанию."
        return current.card("🔴 Сейчас идёт:", next_line)

    if nxt is not None:
        mins = int((nxt.start - now).total_seconds() // 60)
        if nxt.start.date() == now.date() and 0 <= mins < 24 * 60:
            status = f"⏳ Следующее занятие сегодня (через {mins} мин):"
        else:
            status = "⏳ Следующее занятие:"

        after_next = lessons[nxt_idx + 1] if nxt_idx + 1 < len(lessons) else None
        next_line = f"➡️ А после:\n{after_next.details()}" if after_next else ""
        return nxt.card(status, next_line)

    return None  # занятий больше нет — расписание закончилось


def main():
    if not DRY_RUN and (not BOT_TOKEN or not CHAT_ID):
        log.error("Не заданы BOT_TOKEN и/или CHAT_ID (секреты репозитория).")
        sys.exit(1)

    message_id = ensure_message()
    if message_id is None:
        log.error("Не удалось создать/найти сообщение — выхожу без изменений.")
        return

    now = get_now()
    lessons = load_lessons()
    text = build_status_text(lessons, now)

    if text is None:
        edit_message(message_id, "✅ Расписание закончилось.\n"
                                  "Когда появится новое — обновите schedule.json (и CRED_JSON, если нужно).")
        log.info("Расписание завершено.")
        return

    if edit_message(message_id, text):
        log.info("Сообщение (id=%s) обновлено (%s).", message_id, now.strftime("%H:%M"))


if __name__ == "__main__":
    main()
