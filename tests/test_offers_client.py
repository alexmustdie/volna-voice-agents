"""Клиент сервиса офферов: контракт ответа, озвучка полей, поведение при best: null и при сбое."""

import pytest

from app.config import get_settings
from app.models import Agent, CallDirection, Gender
from app.prompts import AGENT_TOOLS, build_offer_block, build_voice_instructions
from app.services import offers_client
from app.services.contacts import parse_budget, parse_csv, parse_rooms

BLOCK_BEST = {
    "jk_name": "Лесная гавань",
    "title": None,
    "price_or_threshold": "от 8,2 млн, 2-к.кв",
    "condition": "ПВ 15%, есть рассрочка от застройщика",
    "theme": None,
    "distance_hint": "у метро Купчино, 15 мин",
    # поля вне контракта: волна обязана их отбросить
    "source": "example-aggregator",
    "url": "https://example.com/block/123",
    "fetched_at": "2026-09-19T10:00:00+03:00",
    "score": 12.5,
}
COLLECTION_BEST = {
    "jk_name": None,
    "title": "Семейная ипотека без первоначального взноса",
    "price_or_threshold": "от 4,3%",
    "condition": "до конца месяца",
    "theme": "без первоначального взноса",
    "distance_hint": None,
    "source": "example-catalog",
    "url": None,
    "fetched_at": None,
    "score": 7.0,
}


def test_offer_keeps_only_six_fields():
    offer = offers_client.UtpOffer.from_api(BLOCK_BEST)
    assert set(offer.to_dict()) == set(offers_client.OFFER_FIELDS)
    for dropped in ("source", "url", "fetched_at", "score"):
        assert dropped not in offer.to_dict()
    assert offer.jk_name == "Лесная гавань" and not offer.is_collection
    assert offers_client.UtpOffer.from_api(COLLECTION_BEST).is_collection
    assert offers_client.UtpOffer.from_api(None) is None
    assert offers_client.UtpOffer.from_api({"jk_name": None, "title": None}) is None


def test_speech_expands_abbreviations():
    assert (
        offers_client.to_speech("от 8,2 млн, 2-к.кв")
        == "от 8 миллионов 200 тысяч рублей, двухкомнатная квартира"
    )
    assert offers_client.to_speech("от 12 млн, 3Е-к.кв") == (
        "от 12 миллионов рублей, евротрёшка, две спальни и кухня-гостиная"
    )
    assert offers_client.to_speech("ПВ 15%") == "первоначальный взнос 15 процентов"
    assert offers_client.to_speech("от 4,3%") == "ставка от 4,3 процента"
    assert offers_client.to_speech("студия, от 5 млн") == "студия, от 5 миллионов рублей"
    assert offers_client.to_speech(None) == ""


def test_offer_block_rules():
    offer = offers_client.UtpOffer.from_api(BLOCK_BEST)
    with_offer = "\n".join(build_offer_block(offer))
    assert "Лесная гавань" not in with_offer  # название ЖК модели не показываем никогда
    assert "название" in with_offer and "сообщит коллега" in with_offer
    assert "от 8 миллионов 200 тысяч рублей, двухкомнатная квартира" in with_offer
    assert "первоначальный взнос 15 процентов" in with_offer
    assert "не называй" in with_offer.lower() or "не называй" in with_offer

    empty = "\n".join(build_offer_block(None))
    assert "общими словами" in empty
    assert "ничего не выдумывай" in empty

    collection = "\n".join(build_offer_block(offers_client.UtpOffer.from_api(COLLECTION_BEST)))
    assert "называть нельзя" in collection  # конкретный ЖК из подборки произносить запрещено


def test_prompt_includes_offer_and_tool():
    agent = Agent(
        name="Анна",
        gender=Gender.female,
        age=28,
        voice="marin",
        niche="недвижимость",
        required_questions=[],
        company_description="",
        call_goal="",
        extra_instructions="",
    )
    offer = offers_client.UtpOffer.from_api(BLOCK_BEST)
    text = build_voice_instructions(agent, CallDirection.outbound, "Иван", offer=offer)
    assert "Лесная гавань" not in text and "find_offer" in text
    assert "от 8 миллионов 200 тысяч рублей" in text  # оффер в промпте есть, но без названия
    assert "https://" not in text and "example-aggregator" not in text  # ничего вне шести полей
    plain = build_voice_instructions(agent, CallDirection.outbound, "Иван")
    assert "Предложение для клиента" not in plain
    assert any(tool["name"] == "find_offer" for tool in AGENT_TOOLS)


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    """Подменяет httpx.AsyncClient: проверяем параметры запроса без сети."""

    calls: list[dict] = []
    payload: object = {"best": BLOCK_BEST, "blocks": [], "collections": []}
    status = 200
    raise_on_get: Exception | None = None

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        type(self).calls.append({"url": url, "params": params or {}, "timeout": self.timeout})
        if type(self).raise_on_get is not None:
            raise type(self).raise_on_get
        return _FakeResponse(type(self).payload, type(self).status)


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.payload = {"best": BLOCK_BEST, "blocks": [], "collections": []}
    _FakeClient.status = 200
    _FakeClient.raise_on_get = None
    monkeypatch.setattr(offers_client.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(get_settings(), "offers_url", "http://offers:8010")
    return _FakeClient


async def test_fetch_offer_sends_only_known_params(fake_http):
    offer = await offers_client.fetch_offer(
        budget=9_000_000, rooms=2, district="Невский", motive="ищет побольше"
    )
    assert offer.jk_name == "Лесная гавань"
    call = fake_http.calls[0]
    assert call["url"] == "http://offers:8010/utp/match"
    assert call["params"] == {
        "budget": 9_000_000,
        "rooms": 2,
        "district": "Невский",
        "motive": "ищет побольше",
    }
    assert "metro" not in call["params"]  # пустые параметры не отправляем
    assert call["timeout"] == get_settings().offers_timeout_seconds


async def test_fetch_offer_null_best_is_honest_answer(fake_http):
    fake_http.payload = {"best": None, "blocks": [], "collections": []}
    assert await offers_client.fetch_offer(district="Купчино") is None


async def test_fetch_offer_survives_timeout_and_errors(fake_http):
    fake_http.raise_on_get = TimeoutError()
    assert await offers_client.fetch_offer(budget=8_000_000) is None
    fake_http.raise_on_get = None
    fake_http.status = 500
    assert await offers_client.fetch_offer(budget=8_000_000) is None
    fake_http.status = 200
    fake_http.payload = ["не объект"]
    assert await offers_client.fetch_offer(budget=8_000_000) is None


async def test_fetch_offer_disabled_without_url(monkeypatch, fake_http):
    monkeypatch.setattr(get_settings(), "offers_url", "")
    assert await offers_client.fetch_offer(budget=8_000_000) is None
    assert fake_http.calls == []  # без адреса в сеть не ходим вовсе


def test_csv_lead_profile_columns():
    csv = (
        "phone;name;district;metro;budget;rooms\n"
        "+79991234567;Иван;Невский;Купчино;8 200 000;2\n"
        "89991234568;Пётр;;;12,5 млн;студия\n"
        "+79991234569;;;;;\n"
    )
    rows = parse_csv(csv.encode())
    assert rows[0] == {
        "phone": "+79991234567",
        "name": "Иван",
        "district": "Невский",
        "metro": "Купчино",
        "budget": 8_200_000,
        "rooms": 2,
    }
    assert rows[1]["budget"] == 12_500_000 and rows[1]["rooms"] == 0
    assert rows[2]["district"] is None and rows[2]["budget"] is None
    # старый файл без заголовка по-прежнему читается
    assert parse_csv(b"+79991234567,Ivan\n")[0]["name"] == "Ivan"
    # русские заголовки
    ru = parse_csv("телефон,имя,район,бюджет\n+79991234567,Иван,Невский,9млн\n".encode())
    assert ru[0]["district"] == "Невский" and ru[0]["budget"] == 9_000_000


def test_budget_and_rooms_parsers():
    assert parse_budget("8 200 000") == 8_200_000
    assert parse_budget("8,2 млн") == 8_200_000
    assert parse_budget("12000000 руб") == 12_000_000
    assert parse_budget("9") == 9_000_000  # без единиц это миллионы
    assert parse_budget("") is None and parse_budget("не указан") is None
    assert parse_rooms("2") == 2 and parse_rooms("двушка") == 2
    assert parse_rooms("студия") == 0 and parse_rooms("3Е") == 3
    assert parse_rooms("99") is None and parse_rooms(None) is None


async def test_prefetch_puts_offer_into_call_and_prompt(fake_http):
    """УТП запрашивается до набора номера, ложится в звонок и попадает в промпт агента."""
    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.models import Call, CallStatus, Contact, ContactList
    from app.services import calls as call_service

    async with get_sessionmaker()() as session:
        agent = Agent(
            name="Анна",
            gender=Gender.female,
            age=28,
            voice="marin",
            niche="недвижимость",
            required_questions=[],
            company_description="",
            call_goal="",
            extra_instructions="",
        )
        contact_list = ContactList(name="Лиды")
        session.add_all([agent, contact_list])
        await session.flush()
        contact = Contact(
            list_id=contact_list.id,
            phone="+79991234567",
            name="Иван",
            district="Невский",
            metro="Купчино",
            budget=9_000_000,
            rooms=2,
        )
        session.add(contact)
        await session.flush()
        call = Call(
            agent_id=agent.id,
            contact_id=contact.id,
            direction=CallDirection.outbound,
            phone=contact.phone,
            status=CallStatus.queued,
            provider="fake",
        )
        session.add(call)
        await session.commit()
        call_id, contact_id = call.id, contact.id

    async with get_sessionmaker()() as session:
        contact = await session.get(Contact, contact_id)
        saved = await call_service.prefetch_offer(call_id, contact)

    assert saved["jk_name"] == "Лесная гавань"
    assert fake_http.calls[0]["params"]["district"] == "Невский"
    assert fake_http.calls[0]["timeout"] == get_settings().offers_prefetch_timeout_seconds

    async with get_sessionmaker()() as session:
        stored = (
            await session.execute(select(Call.utp_offer).where(Call.id == call_id))
        ).scalar_one()
    assert stored["price_or_threshold"] == "от 8,2 млн, 2-к.кв"
    assert "url" not in stored and "score" not in stored

    config = await call_service.build_live_config(call_id)
    assert stored["jk_name"] == "Лесная гавань"  # в базе звонка название остаётся для коллеги
    assert "Лесная гавань" not in config.instructions
    assert "от 8 миллионов 200 тысяч рублей" in config.instructions
    assert any(tool["name"] == "find_offer" for tool in config.tools)


async def test_find_offer_tool_updates_call(fake_http):
    """Инструмент во время разговора: обновляет оффер и честно сообщает, когда его нет."""
    from app.db import get_sessionmaker
    from app.models import Call, CallStatus
    from app.services import calls as call_service

    async with get_sessionmaker()() as session:
        agent = Agent(
            name="Анна",
            gender=Gender.female,
            age=28,
            voice="marin",
            required_questions=[],
            niche="",
            company_description="",
            call_goal="",
            extra_instructions="",
        )
        session.add(agent)
        await session.flush()
        call = Call(
            agent_id=agent.id,
            direction=CallDirection.inbound,
            phone="+79991234567",
            status=CallStatus.in_progress,
            provider="fake",
        )
        session.add(call)
        await session.commit()
        call_id = call.id

    result = await call_service.handle_tool_call(
        call_id, "find_offer", {"budget": "9000000", "rooms": "2", "district": "Невский"}
    )
    # модель названия ЖК не получает, в базе звонка оно остаётся для коллеги
    assert result["ok"] and "jk_name" not in result["offer"]
    assert "Лесная гавань" not in str(result)
    assert fake_http.calls[-1]["params"] == {"budget": 9000000, "rooms": 2, "district": "Невский"}

    fake_http.payload = {"best": None}
    empty = await call_service.handle_tool_call(call_id, "find_offer", {"district": "Купчино"})
    assert empty["ok"] and empty["offer"] is None and "ничего не выдумывай" in empty["message"]

    async with get_sessionmaker()() as session:
        call = await session.get(Call, call_id)
    assert (
        call.utp_offer is None
    )  # пустой ответ затирает прежний оффер, чтобы агент его не повторял


def test_prompt_tempo_and_promises():
    """Правки после разбора звонка 19.09.2026.

    Темп, запрет проговаривать поиск и обещать отправку.
    """
    agent = Agent(
        name="Марина",
        gender=Gender.female,
        age=30,
        voice="bossa",
        niche="новостройки",
        required_questions=[],
        company_description="",
        call_goal="",
        extra_instructions="",
    )
    text = build_voice_instructions(agent, CallDirection.outbound, "Иван")
    low = text.lower()
    assert "не задавай больше двух уточняющих" in low
    # после звонка 5 запрет сформулирован как принцип, а не список: модель обходила список
    # синонимами
    assert "не объявляй свои действия" in low and "в любой форме" in low
    assert "не обещай того, чего не делаешь" in low


def test_prompt_rules_for_cold_call_20_09():
    """Решения после разбора звонков 21.09.2026.

    Выводы плейбука, вынесенные в общую часть промпта.
    """
    agent = Agent(
        name="Марина",
        gender=Gender.female,
        age=30,
        voice="bossa",
        niche="новостройки",
        required_questions=[],
        company_description="ассистент брокера",
        call_goal="",
        extra_instructions="",
    )
    text = build_voice_instructions(agent, CallDirection.outbound, "Иван")
    low = text.lower()
    # на прямой вопрос агент честно отвечает, что он виртуальный ассистент, и не выдаёт себя
    # за человека
    assert "живой сотрудник" not in low
    assert "честно ответь, что ты виртуальный ассистент" in low
    assert "не утверждай, что ты живой человек" in low
    # сначала ответ на вопрос собеседника, реакции по делу
    assert "сначала" in low and "честно ответь" in low
    assert "одобрение" in low and "только в ответ на согласие" in low
    # договорённость: окна, предварительно, прямой номер с повтором, итог с подтверждением
    assert "два-три ближайших конкретных окна" in low and "предварительн" in low
    assert "может быть временным" in low and "повтори номер вслух" in low
    assert "подведи итог" in low
    # имя в именительном
    assert "в именительном падеже" in low


async def test_book_meeting_requires_direct_contact():
    """Встреча пишется только с прямым номером и мессенджером, цель считается по фактам."""
    from app.db import get_sessionmaker
    from app.models import Call, CallStatus
    from app.services import calls as call_service

    async with get_sessionmaker()() as session:
        agent = Agent(
            name="Марина",
            gender=Gender.female,
            age=30,
            voice="bossa",
            required_questions=[],
            niche="",
            company_description="",
            call_goal="",
            extra_instructions="",
        )
        session.add(agent)
        await session.flush()
        call = Call(
            agent_id=agent.id,
            direction=CallDirection.outbound,
            phone="+79990000000",
            status=CallStatus.in_progress,
            provider="fake",
        )
        session.add(call)
        await session.commit()
        call_id = call.id

    when = "2026-09-22T12:00:00+03:00"
    bad_phone = await call_service.handle_tool_call(
        call_id,
        "book_meeting",
        {"datetime": when, "contact_phone": "12345", "messenger": "telegram", "comment": "двушка"},
    )
    assert not bad_phone["ok"] and "переспроси" in bad_phone["error"]
    bad_messenger = await call_service.handle_tool_call(
        call_id,
        "book_meeting",
        {
            "datetime": when,
            "contact_phone": "921 555 01 23",
            "messenger": "почта",
            "comment": "двушка",
        },
    )
    assert not bad_messenger["ok"] and "мессенджер" in bad_messenger["error"]

    ok = await call_service.handle_tool_call(
        call_id,
        "book_meeting",
        {
            "datetime": when,
            "contact_phone": "8 (921) 555-01-23",
            "messenger": "макс",
            "comment": "двушка",
        },
    )
    assert ok["ok"] and ok["contact_phone"] == "+79215550123" and ok["messenger"] == "max"

    # модель не может "отчитаться" о цели: outcome вне списка превращается в other, цель из фактов
    await call_service.handle_tool_call(
        call_id, "report_outcome", {"outcome": "победа", "summary": "всё"}
    )
    async with get_sessionmaker()() as session:
        call = await session.get(Call, call_id)
    assert (
        call.goal_achieved
        and call.outcome == "meeting_booked"
        and call.direct_phone == "+79215550123"
    )


async def test_report_outcome_contact_only_is_not_goal():
    from app.db import get_sessionmaker
    from app.models import Call, CallStatus
    from app.services import calls as call_service

    async with get_sessionmaker()() as session:
        agent = Agent(
            name="Марина",
            gender=Gender.female,
            age=30,
            voice="bossa",
            required_questions=[],
            niche="",
            company_description="",
            call_goal="",
            extra_instructions="",
        )
        session.add(agent)
        await session.flush()
        call = Call(
            agent_id=agent.id,
            direction=CallDirection.outbound,
            phone="+79990000000",
            status=CallStatus.in_progress,
            provider="fake",
        )
        session.add(call)
        await session.commit()
        call_id = call.id

    r = await call_service.handle_tool_call(
        call_id,
        "report_outcome",
        {
            "outcome": "contact_only",
            "summary": "дал номер, встречу позже",
            "contact_phone": "+7 921 555 01 23",
            "messenger": "telegram",
        },
    )
    assert r["outcome"] == "contact_only"
    async with get_sessionmaker()() as session:
        call = await session.get(Call, call_id)
    assert (
        call.direct_phone == "+79215550123"
        and call.messenger == "telegram"
        and not call.goal_achieved
    )


async def test_engine_follows_agent_voice(monkeypatch):
    """Движок принадлежит агенту и определяется голосом: ermil это Яндекс, bossa это OpenAI."""
    from app.db import get_sessionmaker
    from app.live.factory import create_live_session, engine_for_voice
    from app.live.openai_live import OpenAILiveSession
    from app.live.yandex_live import YandexLiveSession
    from app.models import Call, CallStatus
    from app.services import calls as call_service

    assert engine_for_voice("ermil") == "yandex" and engine_for_voice("bossa") == "openai"
    assert engine_for_voice("нет такого") is None

    settings = get_settings()
    monkeypatch.setattr(settings, "voice_engine", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "k")
    monkeypatch.setattr(settings, "yandex_api_key", "k")
    monkeypatch.setattr(settings, "yandex_folder_id", "f")
    assert isinstance(create_live_session("yandex"), YandexLiveSession)
    assert isinstance(create_live_session("openai"), OpenAILiveSession)

    async with get_sessionmaker()() as session:
        agent = Agent(
            name="Артём",
            gender=Gender.male,
            age=30,
            voice="ermil",
            engine="yandex",
            required_questions=[],
            niche="",
            company_description="",
            call_goal="",
            extra_instructions="",
        )
        session.add(agent)
        await session.flush()
        call = Call(
            agent_id=agent.id,
            direction=CallDirection.outbound,
            phone="+79990000000",
            status=CallStatus.in_progress,
            provider="fake",
        )
        session.add(call)
        await session.commit()
        call_id = call.id

    # приложение по умолчанию на OpenAI, но звонок агента с ermil идёт через Яндекс
    assert await call_service.agent_engine_for_call(call_id) == "yandex"
    config = await call_service.build_live_config(call_id)
    assert config.voice == "ermil"
    assert "вызови функцию book_meeting" in config.instructions  # формулировки под движок Яндекса
    assert config.backend_instructions == ""  # у Яндекса нет делегирования бэкенду
