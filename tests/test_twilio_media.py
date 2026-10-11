"""Медиа-сокет Twilio: start/media и обратное аудио через мост с FakeLiveSession.

Приложение поднимается uvicorn-ом в том же event loop, чтобы БД-соединения не пересекали циклы.
"""

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import pytest
import uvicorn
from sqlalchemy import select
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.config import get_settings
from app.db import get_sessionmaker
from app.live.fake import FakeLiveSession
from app.main import app
from app.models import Agent, Call, CallDirection, CallStatus, Gender
from app.security import media_token
from app.services import calls as call_service
from app.telephony import base as telephony_base
from app.telephony.fake_provider import FakeTelephonyProvider
from app.web import twilio_routes


@pytest.fixture
def provider():
    p = FakeTelephonyProvider(seed=1)
    telephony_base.set_provider(p)
    yield p
    telephony_base.set_provider(None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _make_call() -> int:
    async with get_sessionmaker()() as s:
        agent = Agent(
            name="Анна", gender=Gender.female, age=30, voice="marin", required_questions=[]
        )
        s.add(agent)
        await s.flush()
        call = Call(
            agent_id=agent.id,
            direction=CallDirection.outbound,
            phone="+79991234567",
            provider="twilio",
        )
        s.add(call)
        await s.commit()
        return call.id


@asynccontextmanager
async def _server():
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, 10)


def _start_message(call_id: int, stream_sid: str, token: str | None = None) -> str:
    """Событие start как его шлёт Twilio: <Parameter> из TwiML приходят в customParameters."""
    return json.dumps(
        {
            "event": "start",
            "start": {
                "streamSid": stream_sid,
                "callSid": "CA1",
                "customParameters": {"token": media_token(call_id) if token is None else token},
            },
        }
    )


async def _get_call(call_id: int) -> Call:
    async with get_sessionmaker()() as s:
        return (await s.execute(select(Call).where(Call.id == call_id))).scalar_one()


async def test_media_stream_roundtrip(provider, monkeypatch):
    call_id = await _make_call()
    monkeypatch.setattr(
        twilio_routes,
        "live_session_factory",
        lambda engine=None: FakeLiveSession(contact_name="Иван", seed=7),
    )

    async with _server() as port:
        async with connect(f"ws://127.0.0.1:{port}/twilio/media/{call_id}") as ws:
            await ws.send(json.dumps({"event": "connected"}))
            await ws.send(_start_message(call_id, "MZ1"))
            got_media = False
            received = []
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    received.append(msg)
                    if msg.get("event") == "media" and not got_media:
                        assert msg["streamSid"] == "MZ1" and msg["media"]["payload"]
                        got_media = True
                        await ws.send(json.dumps({"event": "media", "media": {"payload": "//8="}}))
                    # агент вызывает end_call -> сервер закрывает сокет, цикл завершается
            except ConnectionClosed:  # закрытие сокета сервером
                pass
            assert got_media, received

    call = await _get_call(call_id)
    assert call.status == CallStatus.completed
    assert call.transcript and call.transcript[0]["role"] == "agent"
    assert call.live_session_id


async def test_media_stream_clears_playback_on_barge_in(provider, monkeypatch):
    """Когда клиент заговорил, серверу нужно сбросить очередь Twilio.

    Иначе агент доиграет поверх.
    """
    call_id = await _make_call()
    monkeypatch.setattr(
        twilio_routes,
        "live_session_factory",
        lambda engine=None: FakeLiveSession(contact_name="Иван", seed=3),
    )
    # порог -1: любая реплика клиента считается перебиванием, проверяем саму проводку события
    monkeypatch.setattr(get_settings(), "barge_in_clear_seconds", -1.0)

    events = []
    async with _server() as port:
        async with connect(f"ws://127.0.0.1:{port}/twilio/media/{call_id}") as ws:
            await ws.send(_start_message(call_id, "MZ7"))
            try:
                async for raw in ws:
                    events.append(json.loads(raw))
            except ConnectionClosed:  # сокет закрывает сервер после end_call
                pass

    clears = [e for e in events if e.get("event") == "clear"]
    assert clears, [e.get("event") for e in events]
    assert all(e["streamSid"] == "MZ7" for e in clears)


async def test_media_stream_rejects_bad_token(provider, monkeypatch):
    """Без верного токена из TwiML мост не создаётся, сокет закрывается кодом 1008."""
    call_id = await _make_call()
    created = []
    monkeypatch.setattr(
        twilio_routes,
        "live_session_factory",
        lambda engine=None: created.append(1) or FakeLiveSession(seed=1),
    )

    async with _server() as port:
        async with connect(f"ws://127.0.0.1:{port}/twilio/media/{call_id}") as ws:
            await ws.send(_start_message(call_id, "MZ9", token="wrong"))
            try:
                await asyncio.wait_for(ws.recv(), 5)
                raise AssertionError("сокет с неверным токеном не закрылся")
            except ConnectionClosed as exc:
                assert exc.rcvd is not None and exc.rcvd.code == 1008

    assert not created and call_id not in call_service.ACTIVE_BRIDGES
    call = await _get_call(call_id)
    assert call.status == CallStatus.queued  # чужое соединение статус звонка не трогает


class _BrokenLiveSession(FakeLiveSession):
    async def start(self, config) -> None:
        raise RuntimeError("boom")


async def test_media_stream_ends_when_bridge_fails(provider, monkeypatch):
    """Мост упал до первой реплики: сокет закрывается сам, звонок failed с текстом ошибки."""
    call_id = await _make_call()
    monkeypatch.setattr(
        twilio_routes, "live_session_factory", lambda engine=None: _BrokenLiveSession()
    )

    async with _server() as port:
        async with connect(f"ws://127.0.0.1:{port}/twilio/media/{call_id}") as ws:
            await ws.send(_start_message(call_id, "MZ2"))

            async def until_closed():
                try:
                    async for _ in ws:
                        pass
                except ConnectionClosed:
                    pass

            # Twilio события stop не шлёт: сокет обязан закрыть сервер
            await asyncio.wait_for(until_closed(), 10)

    call = await _get_call(call_id)
    assert call.status == CallStatus.failed
    assert "RuntimeError: boom" in call.error
    assert not call.transcript


async def test_media_stream_bridge_task_exception_gives_failed(provider, monkeypatch):
    """Исключение самой задачи моста (до создания CallBridge) тоже даёт failed с текстом."""
    call_id = await _make_call()
    monkeypatch.setattr(
        twilio_routes, "live_session_factory", lambda engine=None: FakeLiveSession()
    )

    async def broken_run_bridge(*args, **kwargs):
        raise RuntimeError("bridge exploded")

    monkeypatch.setattr(call_service, "run_bridge", broken_run_bridge)

    async with _server() as port:
        async with connect(f"ws://127.0.0.1:{port}/twilio/media/{call_id}") as ws:
            await ws.send(_start_message(call_id, "MZ3"))
            try:
                await asyncio.wait_for(ws.recv(), 10)
            except ConnectionClosed:
                pass

    call = await _get_call(call_id)
    assert call.status == CallStatus.failed and "RuntimeError: bridge exploded" in call.error


async def test_post_call_analysis_runs_as_separate_task(provider, monkeypatch):
    """Разбор звонка запускается отдельной задачей и не отменяется teardown-ом сокета."""
    call_id = await _make_call()
    settings = get_settings()
    monkeypatch.setattr(settings, "post_call_analysis", True)
    monkeypatch.setattr(settings, "openai_api_key", "k")  # analysis_available() -> True

    def factory(engine=None):
        live = FakeLiveSession(contact_name="Иван", seed=7)
        live.supports_analysis = True  # мок, но разбор просим как у реальной сессии
        return live

    monkeypatch.setattr(twilio_routes, "live_session_factory", factory)

    release = asyncio.Event()
    analyzed: list[int] = []

    async def fake_analyze_call(cid: int):
        await release.wait()
        analyzed.append(cid)
        return {}

    monkeypatch.setattr(call_service, "analyze_call", fake_analyze_call)

    async with _server() as port:
        async with connect(f"ws://127.0.0.1:{port}/twilio/media/{call_id}") as ws:
            await ws.send(_start_message(call_id, "MZ4"))
            try:
                async for _ in ws:
                    pass
            except ConnectionClosed:
                pass
        # сокет закрыт, звонок финализирован, а разбор ещё идёт в своей задаче
        for _ in range(100):
            if call_service._POST_CALL_TASKS:
                break
            await asyncio.sleep(0.05)
        tasks = list(call_service._POST_CALL_TASKS)
        assert len(tasks) == 1 and not tasks[0].done()
        assert (await _get_call(call_id)).status == CallStatus.completed
        assert analyzed == []
        release.set()
        await asyncio.wait_for(tasks[0], 5)
        assert analyzed == [call_id] and not call_service._POST_CALL_TASKS
