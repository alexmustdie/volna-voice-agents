"""Сквозной прогон в мок-режиме: агент -> база -> кампания -> воркер -> звонки -> результаты."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db import get_sessionmaker
from app.main import app
from app.models import Call, CallStatus, Campaign, CampaignStatus, Contact, ContactStatus, Meeting
from app.security import media_token
from app.telephony import base as telephony_base
from app.telephony.fake_provider import FakeTelephonyProvider
from app.web import fake_routes
from app.worker.dialer import tick


@pytest.fixture
def provider():
    p = FakeTelephonyProvider(seed=42)
    telephony_base.set_provider(p)
    yield p
    telephony_base.set_provider(None)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _setup(client) -> tuple[int, int]:
    r = await client.post(
        "/agents",
        data={
            "name": "Анна",
            "gender": "female",
            "age": "28",
            "voice": "marin",
            "niche": "недвижимость",
            "company_description": "Агентство Дом",
            "call_goal": "договориться о встрече",
            "required_questions": "Какой бюджет?\nКогда планируете?",
            "extra_instructions": "",
        },
    )
    assert r.status_code == 303
    csv = (
        "phone,name\n+79991234567,Иван\n8 999 123 45 68,Пётр\n+79991234567,дубль\n"
        "abc,мусор\n+79991234569,\n"
    )
    r = await client.post(
        "/lists", data={"name": "Тест"}, files={"file": ("c.csv", csv.encode(), "text/csv")}
    )
    assert r.status_code == 303
    list_id = int(r.headers["location"].rsplit("/", 1)[1])
    r = await client.get(f"/lists/{list_id}")
    assert "Добавлено: 3, дубликатов: 1, невалидных: 1" in r.text

    r = await client.post(
        "/campaigns",
        data={
            "name": "Обзвон",
            "agent_id": "1",
            "list_id": str(list_id),
            "timezone": "UTC",
            "start_hour": "0",
            "end_hour": "24",
            "days_of_week": ["0", "1", "2", "3", "4", "5", "6"],
            "max_concurrent": "2",
        },
    )
    assert r.status_code == 303
    campaign_id = int(r.headers["location"].rsplit("/", 1)[1])
    return campaign_id, list_id


async def test_full_outbound_flow(client, provider):
    campaign_id, list_id = await _setup(client)
    r = await client.post(f"/campaigns/{campaign_id}/start")
    assert r.status_code == 303

    # первый тик: лимит 2 параллельных -> 2 звонка
    assert await tick() == 2
    async with get_sessionmaker()() as s:
        active = (
            (await s.execute(select(Call).where(Call.campaign_id == campaign_id))).scalars().all()
        )
        assert len(active) == 2
        calling = (
            (await s.execute(select(Contact).where(Contact.status == ContactStatus.calling)))
            .scalars()
            .all()
        )
        assert len(calling) == 2
    # пока звонки идут, слотов нет
    assert await tick() == 0
    await provider.wait_all()
    assert await tick() == 1
    await provider.wait_all()
    assert await tick() == 0  # кампания завершена

    async with get_sessionmaker()() as s:
        campaign = await s.get(Campaign, campaign_id)
        assert campaign.status == CampaignStatus.completed
        calls = (
            (await s.execute(select(Call).where(Call.campaign_id == campaign_id))).scalars().all()
        )
        assert len(calls) == 3
        assert all(c.status == CallStatus.completed for c in calls)
        assert all(c.transcript for c in calls)
        assert all(c.outcome_summary for c in calls)
        assert all(c.live_session_id for c in calls)
        contacts = (
            (await s.execute(select(Contact).where(Contact.list_id == list_id))).scalars().all()
        )
        assert all(c.status in (ContactStatus.done, ContactStatus.do_not_call) for c in contacts)
        meetings = (await s.execute(select(Meeting))).scalars().all()
        goal_calls = [c for c in calls if c.goal_achieved]
        assert len(meetings) == len(goal_calls)
        dnc_calls = [c for c in calls if c.do_not_call_requested]
        for c in dnc_calls:
            contact = await s.get(Contact, c.contact_id)
            assert contact.status == ContactStatus.do_not_call

    # страницы рендерятся
    for url in (
        "/",
        "/campaigns",
        f"/campaigns/{campaign_id}",
        "/agents",
        "/lists",
        f"/lists/{list_id}",
        "/inbound",
        "/calls/1",
        "/agents/1/prompt",
    ):
        r = await client.get(url)
        assert r.status_code == 200, url
    r = await client.get(f"/campaigns/{campaign_id}")
    assert "100.0%" in r.text


async def test_retry_then_failed(client, provider, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "fake_answer_rate", 0.0)
    monkeypatch.setattr(settings, "retry_interval_seconds", 0)
    monkeypatch.setattr(settings, "max_retries", 2)
    campaign_id, list_id = await _setup(client)
    await client.post(f"/campaigns/{campaign_id}/start")

    total_calls = 0
    for _ in range(10):
        n = await tick()
        total_calls += n
        await provider.wait_all()
        await asyncio.sleep(0.01)
    async with get_sessionmaker()() as s:
        contacts = (
            (await s.execute(select(Contact).where(Contact.list_id == list_id))).scalars().all()
        )
        assert all(c.status == ContactStatus.failed for c in contacts)
        assert all(c.attempts == 3 for c in contacts)  # 1 попытка + 2 повтора
        calls = (await s.execute(select(Call))).scalars().all()
        assert len(calls) == 9 and total_calls == 9
        assert all(
            c.status in (CallStatus.no_answer, CallStatus.busy, CallStatus.failed) for c in calls
        )
        campaign = await s.get(Campaign, campaign_id)
        assert campaign.status == CampaignStatus.completed


async def test_pause_and_window(client, provider):
    campaign_id, _ = await _setup(client)
    await client.post(f"/campaigns/{campaign_id}/start")
    await client.post(f"/campaigns/{campaign_id}/pause")
    assert await tick() == 0
    # окно не совпадает -> не звоним
    async with get_sessionmaker()() as s:
        c = await s.get(Campaign, campaign_id)
        c.status = CampaignStatus.running
        c.days_of_week = []
        await s.commit()
    assert await tick() == 0


async def test_inbound_flow(client, provider):
    await _setup(client)
    r = await client.post("/inbound", data={"phone": "+1 202 555 0123", "agent_id": "1"})
    assert r.status_code == 303
    r = await client.post(
        "/fake/inbound", data={"from_phone": "+79990000001", "to_phone": "+12025550123"}
    )
    assert r.status_code == 303
    call_id = int(r.headers["location"].rsplit("/", 1)[1])
    await fake_routes.wait_all()
    async with get_sessionmaker()() as s:
        call = await s.get(Call, call_id)
        assert call.status == CallStatus.completed
        assert call.direction.value == "inbound"
        assert call.transcript
    r = await client.get(f"/calls/{call_id}")
    assert r.status_code == 200 and "Входящий" in r.text
    # номер без агента
    r = await client.post(
        "/fake/inbound", data={"from_phone": "+79990000001", "to_phone": "+12025550999"}
    )
    assert r.status_code == 404


async def test_twilio_webhooks_twiml(client, provider):
    campaign_id, _ = await _setup(client)
    async with get_sessionmaker()() as s:
        campaign = await s.get(Campaign, campaign_id)
        contact = (await s.execute(select(Contact))).scalars().first()
        from app.services.calls import create_outbound_call

        call = await create_outbound_call(s, campaign, contact, "twilio")
        await s.commit()
        call_id = call.id
    r = await client.post(
        f"/twilio/voice/outbound/{call_id}", data={"CallSid": "CA1", "CallStatus": "in-progress"}
    )
    assert r.status_code == 200
    assert f"/twilio/media/{call_id}" in r.text and "<Connect><Stream" in r.text
    assert f'<Parameter name="token" value="{media_token(call_id)}"' in r.text
    r = await client.post(
        f"/twilio/status/{call_id}",
        data={"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "125"},
    )
    assert r.status_code == 204
    async with get_sessionmaker()() as s:
        call = await s.get(Call, call_id)
        assert call.status == CallStatus.completed and call.duration_seconds == 125
        contact = await s.get(Contact, call.contact_id)
        assert contact.status == ContactStatus.done
    # входящий на непривязанный номер
    r = await client.post(
        "/twilio/voice/inbound",
        data={"From": "+79990000001", "To": "+12025550999", "CallSid": "CA2"},
    )
    assert r.status_code == 200 and "<Hangup/>" in r.text


async def test_prompt_preview_follows_agent_engine(client):
    """Предпросмотр промпта должен совпадать с тем, что уйдёт в модель: под движок агента."""
    r = await client.post(
        "/agents",
        data={
            "name": "Артём",
            "gender": "male",
            "age": "30",
            "voice": "ermil",
            "niche": "",
            "company_description": "",
            "call_goal": "",
            "required_questions": "",
            "extra_instructions": "",
        },
    )
    assert r.status_code == 303
    preview = (await client.get("/agents/1/prompt")).text
    assert preview.startswith("Движок: yandex, голос: ermil")
    assert "вызови функцию book_meeting" in preview and "делегируй бэкенду" not in preview
    assert "=== Бэкенд-модель ===" not in preview
