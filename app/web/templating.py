from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.live.factory import ENGINE_LABELS, voices_by_engine
from app.live.voices import mismatch as voice_mismatch
from app.live.voices import voice_note
from app.prompts import OUTCOMES

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["voices_by_engine"] = voices_by_engine()
templates.env.globals["engine_labels"] = ENGINE_LABELS
templates.env.globals["voice_note"] = voice_note
templates.env.globals["voice_mismatch"] = voice_mismatch
templates.env.globals["settings"] = get_settings()
templates.env.globals["weekdays"] = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def display_tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def to_display_tz(value: datetime) -> datetime:
    """Время в БД хранится в UTC. Панель показывает его в рабочей таймзоне, иначе встреча,
    о которой агент договорился на 12:00 по Москве, читается как 09:00."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(display_tz())


def fmt_dt(value) -> str:
    if value is None:
        return "–"
    if not isinstance(value, datetime):
        return str(value)
    return to_display_tz(value).strftime("%d.%m.%Y %H:%M")


def fmt_duration(seconds) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


templates.env.filters["dt"] = fmt_dt
templates.env.filters["duration"] = fmt_duration
templates.env.globals["display_timezone"] = lambda: get_settings().display_timezone
templates.env.globals["outcomes"] = OUTCOMES
