#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Телеграм-бот для GitHub Actions: редактирует одно закреплённое сообщение,
показывая занятие, которое начинается через несколько минут.

В отличие от версии для VPS, этот скрипт выполняется ОДИН РАЗ за запуск
(GitHub Actions запускает его по расписанию каждые 5 минут) и завершается —
никакого бесконечного цикла. Состояние (id сообщения, какие занятия уже
анонсированы) хранится в файле bot_state.json и коммитится обратно в
репозиторий шагом workflow, чтобы следующий запуск (на свежей машине) его
увидел.

Расписание (даты/время/дисциплина/преподаватель/аудитория) лежит в
schedule.json в самом репозитории — в нём нет паролей и ссылок.
Пароли и ссылки на вебинары приходят из секрета CRED_JSON (переменная
окружения), который хранится в настройках репозитория и никогда не
попадает в код.
"""

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
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
LEAD_MINUTES = int(os.environ.get("LEAD_MINUTES", "5"))
# Окно шире, чем LEAD_MINUTES, — компенсирует то, что крон GitHub Actions
# запускается не идеально вовремя (может быть задержка в несколько минут).
PRE_WINDOW_MINUTES = int(os.environ.get("PRE_WINDOW_MINUTES", "7"))
START_WINDOW_MINUTES = int(os.environ.get("START_WINDOW_MINUTES", "7"))
NOTIFY_ON_START = os.environ.get("NOTIFY_ON_START", "true").lower() in ("1", "true", "yes")
PIN_MESSAGE = os.environ.get("PIN_MESSAGE", "true").lower() in ("1", "true", "yes")
TIMEZONE_NAME = os.environ.get("TIMEZONE", "Europe/Moscow")
STATE_PATH = os.environ.get("STATE_PATH", "bot_state.json")
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

    def key(self) -> str:
        return f"{self.date_str}_{self.time_str}_{self.room}"

    def format_message(self, status: str) -> str:
        pass_line = "" if not self.password or self.password.lower() in ("без пароля", "нет", "") \
            else f"🔑 Пароль: <code>{escape_html(self.password)}</code>\n"
        link_line = f'🔗 <a href="{escape_html(self.link)}">Подключиться к вебинару</a>' if self.link else ""
        return (
            f"{status}\n\n"
            f"📅 {escape_html(self.date_str)} ({escape_html(self.weekday)}), {escape_html(self.time_str)}\n"
            f"📘 {escape_html(self.lesson_type)}: {escape_html(self.discipline)}\n"
            f"👤 {escape_html(self.teacher)}\n"
            f"🏫 Ауд. {escape_html(self.room)}\n"
            f"{pass_line}"
            f"{link_line}"
        ).strip()


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
            # нет ссылки для этой аудитории — пропускаем, нечего показывать
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


def load_state() -> dict:
    p = Path(STATE_PATH)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("Файл состояния повреждён — начинаю заново")
    return {"message_id": None, "notified_pre": [], "notified_start": [], "finished_sent": False}


def save_state(state: dict) -> None:
    Path(STATE_PATH).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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


def ensure_message(state: dict) -> Optional[int]:
    if state.get("message_id"):
        return state["message_id"]
    message_id = send_message("🕐 Ожидание ближайшего занятия…\nЭто сообщение будет обновляться автоматически.")
    if message_id is not None:
        state["message_id"] = message_id
        if PIN_MESSAGE and not DRY_RUN:
            pin_message(message_id)
        log.info("Создано новое сообщение-статус, id=%s", message_id)
    return message_id


def main():
    if not DRY_RUN and (not BOT_TOKEN or not CHAT_ID):
        log.error("Не заданы BOT_TOKEN и/или CHAT_ID (секреты репозитория).")
        sys.exit(1)

    state = load_state()
    message_id = ensure_message(state)
    if message_id is None:
        log.error("Не удалось создать/найти сообщение — выхожу без изменений.")
        save_state(state)
        return

    now = get_now()
    today = now.date()

    lessons = load_lessons()
    if not lessons:
        log.info("В schedule.json нет занятий с подходящими ссылками.")
        save_state(state)
        return

    last_schedule_date = lessons[-1].start.date()

    if today > last_schedule_date:
        if not state.get("finished_sent"):
            edit_message(
                message_id,
                "✅ Расписание закончилось.\n"
                "Когда появится новое — обновите schedule.json (и, если нужно, секрет CRED_JSON) в репозитории.",
            )
            state["finished_sent"] = True
            log.info("Расписание завершено (последняя дата %s).", last_schedule_date)
        save_state(state)
        return

    today_lessons = [l for l in lessons if l.start.date() == today]
    changed = False

    for lesson in today_lessons:
        pre_trigger = lesson.start - timedelta(minutes=PRE_WINDOW_MINUTES)
        key = lesson.key()

        if pre_trigger <= now < lesson.start and key not in state["notified_pre"]:
            text = lesson.format_message(f"⏰ Через {LEAD_MINUTES} минут начинается:")
            if edit_message(message_id, text):
                state["notified_pre"].append(key)
                changed = True
                log.info("«Скоро начнётся»: %s %s %s", lesson.date_str, lesson.time_str, lesson.discipline)

        if NOTIFY_ON_START and lesson.start <= now < lesson.start + timedelta(minutes=START_WINDOW_MINUTES) \
                and key not in state["notified_start"]:
            text = lesson.format_message("🔴 Сейчас идёт:")
            if edit_message(message_id, text):
                state["notified_start"].append(key)
                changed = True
                log.info("«Началось»: %s %s %s", lesson.date_str, lesson.time_str, lesson.discipline)

    if len(state["notified_pre"]) > 300:
        state["notified_pre"] = state["notified_pre"][-150:]
    if len(state["notified_start"]) > 300:
        state["notified_start"] = state["notified_start"][-150:]

    save_state(state)
    if not changed:
        log.info("Сейчас (%s) ближайших триггеров нет, занятий сегодня: %d.", now.strftime("%H:%M"), len(today_lessons))


if __name__ == "__main__":
    main()
