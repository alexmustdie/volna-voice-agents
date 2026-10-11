"""Конкурентность и идемпотентность: claim воркера, CAS-переходы, inbox вебхуков, квота номера.

Гонки воспроизводятся только на PostgreSQL (маркер pg): SQLite выполняет операторы по одному и
блокировок строк не держит. Остальные тесты идут на обеих базах и проверяют сами инварианты.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.config import get_settings
from app.db import get_sessionmaker
from app.main import app
from app.models import (
    Agent,
    Call,
    CallDirection,
    CallEvent,
    CallStatus,
    Contact,
    ContactList,
    ContactStatus,
    Gender,
    Meeting,
    NumberDailyUsage,
)
from app.services import quota
from app.services.calls import finalize_call, handle_tool_call, set_call_status
from app.telephony import base as telephony_base
from app.telephony.fake_provider import FakeTelephonyProvider
from app.worker.dialer import tick
from tests.test_flow import _setup


@pytest.fixture
def provider():
    p = FakeTelephonyProvider(seed=5)
    telephony_base.set_provider(p)
    yield p
    telephony_base.set_provider(None)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _make_call(status: CallStatus = CallStatus.in_progress) -> int:
    """Агент, база, контакт в calling (одна попытка) и звонок в заданном статусе."""
    async with get_sessionmaker()() as s:
        agent = Agent(
            name="Анна", gender=Gender.female, age=30, voice="marin", required_questions=[]
        )
        lst = ContactList(name="l")
        s.add_all([agent, lst])
        await s.flush()
        contact = Contact(
            list_id=lst.id, phone="+79991234567", status=ContactStatus.calling, attempts=1
        )
        s.add(contact)
        await s.flush()
        call = Call(
            agent_id=agent.id,
            contact_id=contact.id,
            direction=CallDirection.outbound,
            phone=contact.phone,
            status=status,
            provider="twilio",
            started_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        s.add(call)
        await s.commit()
        return call.id


async def _call(call_id: int) -> Call:
    async with get_sessionmaker()() as s:
        return await s.get(Call, call_id)


@pytest.mark.pg
async def test_parallel_ticks_respect_slots_and_claim_each_contact_once(
    client, provider, monkeypatch
):
    """Два воркера разом: лимит параллельных звонков не превышен, контакт набран один раз."""
    monkeypatch.setattr(get_settings(), "fake_call_seconds", 2.0)  # звонки идут, пока тики спорят
    campaign_id, list_id = await _setup(client)  # 3 контакта, max_concurrent = 2
    await client.post(f"/campaigns/{campaign_id}/start")

    started = await asyncio.gather(tick(), tick())
    assert sum(started) == 2
    async with get_sessionmaker()() as s:
        calls = (
            (await s.execute(select(Call).where(Call.campaign_id == campaign_id))).scalars().all()
        )
        assert len(calls) == 2 and len({c.contact_id for c in calls}) == 2
        contacts = (
            (await s.execute(select(Contact).where(Contact.list_id == list_id))).scalars().all()
        )
        assert sorted(c.attempts for c in contacts) == [0, 1, 1]
    await provider.wait_all()


async def test_parallel_finalize_gives_one_transition(provider):
    """Teardown сокета и колбэк провайдера финализируют разом: переход один, контакт один раз."""
    call_id = await _make_call()
    await asyncio.gather(
        finalize_call(call_id, CallStatus.completed, error="teardown"),
        finalize_call(call_id, CallStatus.no_answer, error="callback"),
    )
    call = await _call(call_id)
    assert call.status in (CallStatus.completed, CallStatus.no_answer)
    assert call.error in ("teardown", "callback")  # ошибка победителя, без дописывания второй
    assert call.ended_at is not None and 9 <= call.duration_seconds <= 11
    async with get_sessionmaker()() as s:
        contact = await s.get(Contact, call.contact_id)
    expected = ContactStatus.done if call.status == CallStatus.completed else ContactStatus.new
    assert contact.status == expected and contact.attempts == 1


async def test_repeated_finalize_only_refines_duration(provider):
    call_id = await _make_call()
    await finalize_call(call_id, CallStatus.completed)
    await finalize_call(call_id, CallStatus.failed, duration=77, error="поздно")
    call = await _call(call_id)
    assert call.status == CallStatus.completed and call.duration_seconds == 77
    assert call.error == ""


async def test_status_never_goes_back(provider):
    """ringing после completed и queued после in_progress не откатывают статус."""
    call_id = await _make_call(CallStatus.in_progress)
    await set_call_status(call_id, CallStatus.queued, provider_call_id="CA7")
    call = await _call(call_id)
    assert call.status == CallStatus.in_progress and call.provider_call_id == "CA7"
    await finalize_call(call_id, CallStatus.completed)
    await asyncio.gather(
        set_call_status(call_id, CallStatus.ringing), set_call_status(call_id, CallStatus.queued)
    )
    assert (await _call(call_id)).status == CallStatus.completed


async def test_in_progress_sets_started_at_once(provider):
    call_id = await _make_call(CallStatus.queued)
    async with get_sessionmaker()() as s:
        call = await s.get(Call, call_id)
        call.started_at = None
        await s.commit()
    await asyncio.gather(
        set_call_status(call_id, CallStatus.in_progress),
        set_call_status(call_id, CallStatus.in_progress),
    )
    first = (await _call(call_id)).started_at
    assert first is not None
    await set_call_status(call_id, CallStatus.in_progress)
    assert (await _call(call_id)).started_at == first


async def test_duplicate_status_callback_is_dropped(client, provider):
    """Повтор вебхука с тем же ключом ничего не меняет и отвечает 204."""
    call_id = await _make_call(CallStatus.in_progress)
    headers = {"I-Twilio-Idempotency-Token": "tok-1"}
    data = {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "100"}
    r = await client.post(f"/twilio/status/{call_id}", data=data, headers=headers)
    assert r.status_code == 204
    assert (await _call(call_id)).duration_seconds == 100

    # тот же токен с другой длительностью: повтор доставки, уточнять нечего
    r = await client.post(
        f"/twilio/status/{call_id}", data={**data, "CallDuration": "999"}, headers=headers
    )
    assert r.status_code == 204
    assert (await _call(call_id)).duration_seconds == 100
    # новое событие (свой SequenceNumber) принимается и уточняет длительность
    data = {**data, "CallDuration": "150", "SequenceNumber": "3"}
    for _ in range(2):
        assert (await client.post(f"/twilio/status/{call_id}", data=data)).status_code == 204
    assert (await _call(call_id)).duration_seconds == 150
    async with get_sessionmaker()() as s:
        events = (
            (await s.execute(select(CallEvent).where(CallEvent.call_id == call_id))).scalars().all()
        )
    assert sorted(e.provider_event_id for e in events) == ["CA1:3", "tok-1"]
    assert all(e.payload["CallSid"] == "CA1" for e in events)


async def test_parallel_duplicate_callbacks_record_one_event(client, provider):
    call_id = await _make_call(CallStatus.in_progress)
    data = {"CallSid": "CA1", "CallStatus": "in-progress", "SequenceNumber": "2"}
    responses = await asyncio.gather(
        *(client.post(f"/twilio/status/{call_id}", data=data) for _ in range(5))
    )
    assert {r.status_code for r in responses} == {204}
    async with get_sessionmaker()() as s:
        count = (
            await s.execute(select(func.count(CallEvent.id)).where(CallEvent.call_id == call_id))
        ).scalar_one()
    assert count == 1


async def test_status_callback_checks_call_and_sid(client, provider):
    call_id = await _make_call(CallStatus.in_progress)
    r = await client.post(f"/twilio/status/{call_id + 100}", data={"CallStatus": "completed"})
    assert r.status_code == 404
    r = await client.post(f"/twilio/voice/outbound/{call_id + 100}", data={"CallSid": "CA1"})
    assert r.status_code == 404
    assert (
        await client.post(
            f"/twilio/status/{call_id}", data={"CallSid": "CA1", "CallStatus": "ringing"}
        )
    ).status_code == 204
    r = await client.post(
        f"/twilio/status/{call_id}", data={"CallSid": "CA-other", "CallStatus": "completed"}
    )
    assert r.status_code == 400
    assert (await _call(call_id)).status == CallStatus.in_progress


async def test_ringing_callback_after_completed_keeps_status(client, provider):
    call_id = await _make_call(CallStatus.in_progress)
    await client.post(
        f"/twilio/status/{call_id}",
        data={"CallSid": "CA1", "CallStatus": "completed", "SequenceNumber": "3"},
    )
    r = await client.post(
        f"/twilio/status/{call_id}",
        data={"CallSid": "CA1", "CallStatus": "ringing", "SequenceNumber": "1"},
    )
    assert r.status_code == 204
    assert (await _call(call_id)).status == CallStatus.completed


async def test_inbound_webhook_is_get_or_create(client, provider):
    await _setup(client)
    await client.post("/inbound", data={"phone": "+1 202 555 0123", "agent_id": "1"})
    data = {"From": "+79990000001", "To": "+12025550123", "CallSid": "CA9"}
    first = await client.post("/twilio/voice/inbound", data=data)
    second = await client.post("/twilio/voice/inbound", data=data)
    assert first.status_code == 200 and first.text == second.text
    async with get_sessionmaker()() as s:
        calls = (
            (await s.execute(select(Call).where(Call.provider_call_id == "CA9"))).scalars().all()
        )
    assert len(calls) == 1 and calls[0].direction == CallDirection.inbound


async def test_book_meeting_twice_creates_one_meeting(provider):
    call_id = await _make_call()
    args = {
        "datetime": "2026-09-15T14:00:00+03:00",
        "contact_phone": "9215550123",
        "messenger": "telegram",
        "comment": "офис",
    }
    first = await handle_tool_call(call_id, "book_meeting", args)
    second = await handle_tool_call(call_id, "book_meeting", {**args, "comment": "повтор"})
    assert first["ok"] and second["ok"] and first["meeting_id"] == second["meeting_id"]
    async with get_sessionmaker()() as s:
        meetings = (
            (await s.execute(select(Meeting).where(Meeting.call_id == call_id))).scalars().all()
        )
        assert len(meetings) == 1 and meetings[0].comment == "офис"
        assert (await s.get(Call, call_id)).goal_achieved


async def test_daily_quota_is_not_exceeded_by_parallel_reservations(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "daily_calls_per_number", 3)
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

    async def reserve() -> bool:
        async with get_sessionmaker()() as s:
            ok = await quota.reserve_daily_slot(s, "+7999", now)
            await s.commit()
            return ok

    results = await asyncio.gather(*(reserve() for _ in range(10)))
    assert sum(results) == 3
    async with get_sessionmaker()() as s:
        used = (
            await s.execute(
                select(NumberDailyUsage.used).where(NumberDailyUsage.from_phone == "+7999")
            )
        ).scalar_one()
        assert used == 3
        # другой номер и другой день считаются отдельно
        assert await quota.reserve_daily_slot(s, "+7888", now)
        assert await quota.reserve_daily_slot(s, "+7999", now + timedelta(days=1))
        assert not await quota.reserve_daily_slot(s, "+7999", now)
        await s.rollback()
    monkeypatch.setattr(settings, "daily_calls_per_number", 0)
    async with get_sessionmaker()() as s:
        assert await quota.reserve_daily_slot(s, "+7999", now)  # без лимита резерв не нужен
