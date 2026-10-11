"""Суточные лимиты и темп звонков с одного нашего номера.

Зачем: площадки ограничивают число звонков с одного номера в сутки (у площадок объявлений такие
лимиты есть),
а операторский антифрод реагирует на всплески. Лимит считается по календарным суткам рабочей
таймзоны (DISPLAY_TIMEZONE), а не по UTC: ограничение площадки местное.

Настройки: DAILY_CALLS_PER_NUMBER (0 = без лимита), MIN_SECONDS_BETWEEN_CALLS (0 = без паузы).

Лимит резервируется атомарно в number_daily_usage (reserve_daily_slot), чтобы параллельные воркеры
не превысили его. count(*) по звонкам (calls_today, number_budget) остаётся для отчётов и как
оценка остатка, когда лимит выключен.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import dialect_insert
from app.models import Call, CallDirection, NumberDailyUsage


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def day_start(now: datetime | None = None) -> datetime:
    """Начало текущих суток рабочей таймзоны, в UTC."""
    now = (now or datetime.now(UTC)).astimezone(_tz())
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def usage_day(now: datetime | None = None) -> date:
    """Календарные сутки рабочей таймзоны: ключ суточной квоты."""
    return (now or datetime.now(UTC)).astimezone(_tz()).date()


async def reserve_daily_slot(
    session: AsyncSession, from_phone: str | None, now: datetime | None = None
) -> bool:
    """Резервирует один звонок с номера на сегодня. False означает отказ: лимит исчерпан.

    Один оператор: INSERT ... ON CONFLICT (from_phone, day) DO UPDATE SET used = used + 1
    WHERE used < лимит RETURNING used. Пустой результат это отказ. Строка блокируется базой, поэтому
    два параллельных резервирования не пройдут оба на последнем слоте. Без лимита резервирование
    не нужно и таблица не ведётся. Коммит на вызывающей стороне: резерв живёт в той же транзакции,
    что и claim контакта.
    """
    limit = get_settings().daily_calls_per_number
    if limit <= 0:
        return True
    usage = NumberDailyUsage.__table__
    stmt = (
        dialect_insert(session, NumberDailyUsage)
        .values(from_phone=from_phone or "", day=usage_day(now), used=1)
        .on_conflict_do_update(
            index_elements=["from_phone", "day"],
            set_={"used": usage.c.used + 1},
            where=usage.c.used < limit,
        )
        .returning(usage.c.used)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def calls_today(
    session: AsyncSession, from_phone: str | None, now: datetime | None = None
) -> int:
    stmt = select(func.count(Call.id)).where(
        Call.direction == CallDirection.outbound, Call.created_at >= day_start(now)
    )
    stmt = (
        stmt.where(Call.from_phone == from_phone)
        if from_phone
        else stmt.where(Call.from_phone.is_(None))
    )
    return (await session.execute(stmt)).scalar_one()


async def seconds_since_last_call(
    session: AsyncSession, from_phone: str | None, now: datetime | None = None
) -> float | None:
    """Сколько прошло с прошлого исходящего с этого номера. None, если звонков ещё не было."""
    stmt = select(func.max(Call.created_at)).where(Call.direction == CallDirection.outbound)
    stmt = (
        stmt.where(Call.from_phone == from_phone)
        if from_phone
        else stmt.where(Call.from_phone.is_(None))
    )
    last = (await session.execute(stmt)).scalar_one_or_none()
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - last).total_seconds()


async def pause_elapsed(
    session: AsyncSession, from_phone: str | None, now: datetime | None = None
) -> bool:
    """Выдержана ли пауза MIN_SECONDS_BETWEEN_CALLS после прошлого исходящего с этого номера."""
    settings = get_settings()
    if settings.min_seconds_between_calls <= 0:
        return True
    since = await seconds_since_last_call(session, from_phone, now)
    return since is None or since >= settings.min_seconds_between_calls


async def number_budget(
    session: AsyncSession, from_phone: str | None, now: datetime | None = None
) -> tuple[int | None, bool]:
    """Сколько звонков с этого номера осталось на сегодня и можно ли звонить прямо сейчас.

    Возвращает (остаток или None без лимита, пауза выдержана). Оценка по count(*) для отчётов;
    воркер резервирует слот через reserve_daily_slot.
    """
    settings = get_settings()
    remaining: int | None = None
    if settings.daily_calls_per_number > 0:
        remaining = max(
            0, settings.daily_calls_per_number - await calls_today(session, from_phone, now)
        )
    return remaining, await pause_elapsed(session, from_phone, now)


def next_day_start(now: datetime | None = None) -> datetime:
    """Когда лимит обнулится: начало следующих суток рабочей таймзоны."""
    return day_start(now) + timedelta(days=1)
