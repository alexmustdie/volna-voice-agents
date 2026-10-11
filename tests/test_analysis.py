"""Разбор звонка после разговора: парсинг ответа модели и дополнение результатов без перезаписи."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import (
    Agent,
    Call,
    CallDirection,
    CallStatus,
    Contact,
    ContactList,
    ContactStatus,
    Gender,
    Meeting,
)
from app.services import analysis


def test_parse_analysis_extracts_json_from_text():
    text = (
        'Вот результат:\n```json\n{"summary": "Встреча", "outcome": "meeting_booked", '
        '"meeting": {"datetime": "2026-09-15T15:00:00+03:00", "comment": "офис"}, '
        '"contact_phone": "9215550123", "messenger": "макс", "do_not_call": false}\n```'
    )
    r = analysis.parse_analysis(text)
    assert r["outcome"] == "meeting_booked" and r["meeting"]["datetime"].startswith(
        "2026-09-15T15:00"
    )
    assert r["contact_phone"] == "9215550123" and r["messenger"] == "макс"
    empty = analysis.parse_analysis(
        '{"summary": "", "outcome": "придуманный", "meeting": {"datetime": ""}, "do_not_call": '
        "true}"
    )
    assert empty["meeting"] is None and empty["outcome"] is None  # исход вне списка не принимаем
    with pytest.raises(ValueError):
        analysis.parse_analysis("без json")


async def _make_call(**kw) -> int:
    async with get_sessionmaker()() as s:
        agent = Agent(
            name="Анна",
            gender=Gender.female,
            age=28,
            voice="dasha",
            required_questions=["Бюджет?"],
            call_goal="встреча",
        )
        lst = ContactList(name="l")
        s.add_all([agent, lst])
        await s.flush()
        contact = Contact(list_id=lst.id, phone="+79991234567", status=ContactStatus.calling)
        s.add(contact)
        await s.flush()
        call = Call(
            agent_id=agent.id,
            contact_id=contact.id,
            direction=CallDirection.outbound,
            phone=contact.phone,
            status=CallStatus.completed,
            started_at=datetime(2026, 9, 13, 18, 0, tzinfo=UTC),
            transcript=[
                {"role": "agent", "text": "Когда удобно?"},
                {"role": "user", "text": "послезавтра в три"},
            ],
            **kw,
        )
        s.add(call)
        await s.commit()
        return call.id


async def test_analyze_call_adds_missing_meeting_and_dnc(monkeypatch):
    call_id = await _make_call()
    prompts = []

    async def fake_complete(system, user):
        prompts.append(user)
        return (
            '{"summary": "Договорились", "outcome": "meeting_booked", '
            '"meeting": {"datetime": "2026-09-15T15:00:00+03:00", "comment": "офис"}, '
            '"contact_phone": "8 921 555-01-23", "messenger": "телеграм", "do_not_call": true}'
        )

    monkeypatch.setattr(analysis, "complete", fake_complete)
    result = await analysis.analyze_call(call_id)
    assert result["meeting"]
    assert "воскресенье, 13.09.2026" in prompts[0] and "Клиент: послезавтра в три" in prompts[0]
    async with get_sessionmaker()() as s:
        call = await s.get(Call, call_id)
        assert (
            call.goal_achieved
            and call.outcome_summary == "Договорились"
            and call.do_not_call_requested
        )
        assert call.direct_phone == "+79215550123" and call.messenger == "telegram"
        meetings = (
            (await s.execute(select(Meeting).where(Meeting.call_id == call_id))).scalars().all()
        )
        assert len(meetings) == 1 and meetings[0].scheduled_at.day == 15
        contact = await s.get(Contact, call.contact_id)
        assert contact.status == ContactStatus.do_not_call


async def test_analyze_call_keeps_tool_results(monkeypatch):
    call_id = await _make_call(outcome_summary="из инструмента")
    async with get_sessionmaker()() as s:
        s.add(
            Meeting(
                call_id=call_id,
                scheduled_at=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
                comment="book_meeting",
            )
        )
        await s.commit()

    async def fake_complete(system, user):
        return (
            '{"goal_achieved": true, "summary": "другое", '
            '"meeting": {"datetime": "2026-09-15T15:00:00+03:00", "comment": "x"}, '
            '"do_not_call": false}'
        )

    monkeypatch.setattr(analysis, "complete", fake_complete)
    await analysis.analyze_call(call_id)
    async with get_sessionmaker()() as s:
        call = await s.get(Call, call_id)
        assert call.outcome_summary == "из инструмента"
        meetings = (
            (await s.execute(select(Meeting).where(Meeting.call_id == call_id))).scalars().all()
        )
        assert [m.comment for m in meetings] == ["book_meeting"]


async def test_analyze_call_failure_is_recorded(monkeypatch):
    call_id = await _make_call()

    async def broken(system, user):
        raise RuntimeError("сеть")

    monkeypatch.setattr(analysis, "complete", broken)
    assert await analysis.analyze_call(call_id) is None
    async with get_sessionmaker()() as s:
        assert "разбор звонка: сеть" in (await s.get(Call, call_id)).error


async def test_meeting_without_contact_is_not_a_goal(monkeypatch):
    """Встреча без номера для мессенджера целью не считается: приглашение прислать некуда."""
    call_id = await _make_call()

    async def fake_complete(system, user):
        return (
            '{"summary": "Время есть, номер не дал", "outcome": "meeting_booked", '
            '"meeting": {"datetime": "2026-09-15T15:00:00+03:00", "comment": "офис"}, '
            '"contact_phone": null, "messenger": null, "do_not_call": false}'
        )

    monkeypatch.setattr(analysis, "complete", fake_complete)
    await analysis.analyze_call(call_id)
    async with get_sessionmaker()() as s:
        call = await s.get(Call, call_id)
        assert not call.goal_achieved
        assert (
            len(
                (await s.execute(select(Meeting).where(Meeting.call_id == call_id))).scalars().all()
            )
            == 1
        )


async def test_callback_later_and_aggressive(monkeypatch):
    call_id = await _make_call()

    async def later(system, user):
        return (
            '{"summary": "Сначала продаст", "outcome": "callback_later", "meeting": null, '
            '"callback_after": "через месяц", "callback_reason": "спросить, как идёт продажа", '
            '"do_not_call": false}'
        )

    monkeypatch.setattr(analysis, "complete", later)
    await analysis.analyze_call(call_id)
    async with get_sessionmaker()() as s:
        call = await s.get(Call, call_id)
        assert call.outcome == "callback_later" and call.callback_after == "через месяц"
        assert call.callback_reason == "спросить, как идёт продажа" and not call.goal_achieved

    rude_id = await _make_call()

    async def rude(system, user):
        return (
            '{"summary": "Нагрубил", "outcome": "aggressive", "meeting": null, '
            '"do_not_call": false}'
        )

    monkeypatch.setattr(analysis, "complete", rude)
    await analysis.analyze_call(rude_id)
    async with get_sessionmaker()() as s:
        call = await s.get(Call, rude_id)
        assert call.outcome == "aggressive" and call.do_not_call_requested  # грубость = не звонить
