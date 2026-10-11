"""Суточный лимит звонков с одного номера и пауза между звонками."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.db import get_sessionmaker
from app.main import app
from app.models import Call, CallDirection, CallStatus
from app.services import quota
from app.telephony import base as telephony_base
from app.telephony.fake_provider import FakeTelephonyProvider
from tests.test_flow import _setup  # noqa: F401  тот же сценарий: агент, база, кампания

MSK = timezone(timedelta(hours=3))


@pytest.fixture
def provider():
    p = FakeTelephonyProvider(seed=11)
    telephony_base.set_provider(p)
    yield p
    telephony_base.set_provider(None)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def test_day_start_uses_working_timezone(monkeypatch):
    """Сутки считаются местные: лимит площадки обнуляется в полночь по Москве, не по UTC."""
    monkeypatch.setattr(get_settings(), "display_timezone", "Europe/Moscow")
    # 25.09.2026 22:30 UTC это уже 26.09 по Москве
    late = datetime(2026, 9, 25, 22, 30, tzinfo=UTC)
    assert quota.day_start(late).astimezone(MSK).day == 26
    assert quota.next_day_start(late).astimezone(MSK).day == 27
    early = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    assert quota.day_start(early).astimezone(MSK).day == 25


async def _add_call(from_phone: str | None, created_at: datetime) -> None:
    async with get_sessionmaker()() as s:
        s.add(
            Call(
                agent_id=1,
                direction=CallDirection.outbound,
                phone="+79990000001",
                status=CallStatus.completed,
                provider="fake",
                from_phone=from_phone,
                created_at=created_at,
            )
        )
        await s.commit()


async def test_budget_counts_only_this_number_and_today(monkeypatch, client, provider):
    await _setup(client)  # создаёт агента, базу и кампанию
    settings = get_settings()
    monkeypatch.setattr(settings, "daily_calls_per_number", 3)
    monkeypatch.setattr(settings, "min_seconds_between_calls", 0)
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

    await _add_call("+7999", now - timedelta(hours=2))
    await _add_call("+7999", now - timedelta(hours=1))
    await _add_call("+7888", now - timedelta(minutes=30))  # другой номер
    await _add_call("+7999", now - timedelta(days=1))  # вчера

    async with get_sessionmaker()() as s:
        assert await quota.calls_today(s, "+7999", now) == 2
        remaining, pause_ok = await quota.number_budget(s, "+7999", now)
        assert remaining == 1 and pause_ok
        remaining_other, _ = await quota.number_budget(s, "+7888", now)
        assert remaining_other == 2


async def test_pause_between_calls(monkeypatch, client, provider):
    await _setup(client)
    settings = get_settings()
    monkeypatch.setattr(settings, "daily_calls_per_number", 0)
    monkeypatch.setattr(settings, "min_seconds_between_calls", 60)
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    await _add_call("+7999", now - timedelta(seconds=30))
    async with get_sessionmaker()() as s:
        _, pause_ok = await quota.number_budget(s, "+7999", now)
        assert not pause_ok
        _, pause_ok_later = await quota.number_budget(s, "+7999", now + timedelta(seconds=40))
        assert pause_ok_later


async def test_dialer_respects_daily_limit(monkeypatch, client, provider):
    """Воркер не превышает суточный лимит и пишет в звонок наш номер."""
    from app.worker.dialer import tick

    settings = get_settings()
    monkeypatch.setattr(settings, "twilio_phone_number", "+79990000000")
    monkeypatch.setattr(settings, "daily_calls_per_number", 2)
    monkeypatch.setattr(settings, "min_seconds_between_calls", 0)

    campaign_id, _ = await _setup(client)
    await client.post(f"/campaigns/{campaign_id}/start")

    started = await tick()
    await provider.wait_all()
    started += await tick()
    await provider.wait_all()
    assert started == 2  # третий контакт остался на завтра

    async with get_sessionmaker()() as s:
        calls = (await s.execute(__import__("sqlalchemy").select(Call))).scalars().all()
        assert len(calls) == 2
        assert all(c.from_phone == "+79990000000" for c in calls)
