from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.live.openai_live import normalize_server_event
from app.models import Agent, CallDirection, Campaign, Gender
from app.phone import normalize_phone
from app.prompts import build_voice_instructions
from app.services.campaigns import campaign_in_window, clamp_concurrency
from app.services.contacts import parse_csv


def test_normalize_phone():
    assert normalize_phone("8 (999) 123-45-67") == "+79991234567"
    assert normalize_phone("+7 999 123 45 67") == "+79991234567"
    assert normalize_phone("9991234567") == "+79991234567"
    assert normalize_phone("+1 202 555 0123") == "+12025550123"
    assert normalize_phone("12345") is None
    assert normalize_phone("") is None
    assert normalize_phone("abc") is None


def test_parse_csv_variants():
    """Контракт parse_csv: словарь на строку; старые файлы phone,name читаются по-прежнему."""
    rows = parse_csv(b"phone,name\n+79991234567,Ivan\n89991234568,\n")
    assert [(r["phone"], r["name"]) for r in rows] == [
        ("+79991234567", "Ivan"),
        ("89991234568", None),
    ]
    ru = parse_csv("﻿phone;name\n+79991234567;Иван\n".encode())
    assert [(r["phone"], r["name"]) for r in ru] == [("+79991234567", "Иван")]
    no_header = parse_csv(b"+79991234567\n+79991234568,Petr\n")
    assert [(r["phone"], r["name"]) for r in no_header] == [
        ("+79991234567", None),
        ("+79991234568", "Petr"),
    ]
    assert parse_csv(b"") == []


def test_prompt_contains_mandatory_rules():
    agent = Agent(
        name="Анна",
        gender=Gender.female,
        age=28,
        voice="marin",
        niche="недвижимость",
        company_description="Агентство Дом",
        call_goal="договориться о встрече",
        required_questions=["Какой бюджет?", "Когда планируете покупку?"],
        extra_instructions="Не дави.",
    )
    text = build_voice_instructions(agent, CallDirection.outbound, "Иван")
    assert "только по-русски" in text
    assert "не беспокоить" in text
    assert "book_meeting" in text
    assert "1. Какой бюджет?" in text and "2. Когда планируете покупку?" in text
    assert "Иван" in text
    assert "женщина примерно 28 лет" in text
    inbound = build_voice_instructions(agent, CallDirection.inbound)
    assert "входящий" in inbound
    assert "делегируй бэкенду фиксацию встречи (book_meeting)" in text
    ya = build_voice_instructions(agent, CallDirection.outbound, "Иван", engine="yandex")
    assert "вызови функцию book_meeting" in ya and "Делегирование бэкенду" not in ya
    assert "по московскому времени" in ya


def test_campaign_window():
    c = Campaign(timezone="Europe/Moscow", start_hour=10, end_hour=19, days_of_week=[0, 1, 2, 3, 4])
    # понедельник 14.09.2026 12:00 MSK = 09:00 UTC
    assert campaign_in_window(c, datetime(2026, 9, 14, 9, 0, tzinfo=UTC))
    # 20:00 MSK
    assert not campaign_in_window(c, datetime(2026, 9, 14, 17, 0, tzinfo=UTC))
    # воскресенье
    assert not campaign_in_window(c, datetime(2026, 9, 13, 9, 0, tzinfo=UTC))
    assert clamp_concurrency(0) == 1 and clamp_concurrency(50) == 20 and clamp_concurrency(5) == 5


def test_normalize_live_events():
    assert normalize_server_event(
        {"type": "session.started", "session": {"id": "live_1"}}
    ).data == {"session_id": "live_1"}
    assert (
        normalize_server_event({"type": "session.output_audio.delta", "delta": "AAA="}).type
        == "audio"
    )
    tr = normalize_server_event(
        {"type": "session.input_transcript.delta", "delta": "Да", "start_ms": 100, "end_ms": 300}
    )
    assert tr.type == "input_transcript" and tr.data["delta"] == "Да"
    fc = normalize_server_event(
        {
            "type": "response.event",
            "delegation_id": "item_1",
            "event": {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "book_meeting",
                    "arguments": '{"datetime": "2026-09-15T14:00:00+03:00", "comment": "офис"}',
                },
            },
        }
    )
    assert fc.type == "function_call"
    assert fc.data["name"] == "book_meeting" and fc.data["arguments"]["comment"] == "офис"
    closed = normalize_server_event(
        {"type": "session.closed", "reason": "remote_hangup", "usage": {"seconds": 42}}
    )
    assert closed.type == "closed" and closed.data["seconds"] == 42
    assert (
        normalize_server_event(
            {"type": "response.event", "event": {"type": "response.output_text.delta"}}
        ).type
        == "other"
    )


def test_settings_require_session_secret_in_real_mode():
    """В реальном режиме пустой или короткий SESSION_SECRET это ошибка запуска."""
    with pytest.raises(ValidationError, match="SESSION_SECRET"):
        Settings(session_secret="short", fake_calls=False, _env_file=None)
    assert Settings(session_secret="", fake_calls=True, _env_file=None).fake_calls
    assert not Settings(session_secret="x" * 16, fake_calls=False, _env_file=None).fake_calls
