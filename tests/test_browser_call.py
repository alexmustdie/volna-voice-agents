"""Тестовый звонок из браузера (/test/call).

События транскрипта, инструментов и аудио через мост.
"""

import asyncio
import json
import socket

import uvicorn
from sqlalchemy import select
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.db import get_sessionmaker
from app.main import app
from app.models import Agent, Call, CallStatus, Gender, Meeting
from app.security import media_token
from app.web.browser_call_routes import browser_token_subject


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_browser_call_roundtrip(client=None):
    async with get_sessionmaker()() as s:
        s.add(
            Agent(name="Анна", gender=Gender.female, age=30, voice="marin", required_questions=[])
        )
        await s.commit()

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    events: list[dict] = []
    try:
        token = media_token(browser_token_subject(1))
        async with connect(
            f"ws://127.0.0.1:{port}/test/call?agent_id=1&direction=outbound&name=Иван&token={token}"
        ) as ws:
            await ws.send(json.dumps({"type": "audio", "data": "AAAA"}))
            try:
                async for raw in ws:
                    events.append(json.loads(raw))
            except Exception:  # сервер закрывает сокет после finished
                pass
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, 10)

    types = [e["type"] for e in events]
    assert types[0] == "call" and events[0]["real"] is False
    assert "started" in types and "audio" in types and "finished" in types
    agent_text = "".join(
        e["delta"] for e in events if e["type"] == "transcript" and e["role"] == "agent"
    )
    assert "Иван" in agent_text
    tools = [e["name"] for e in events if e["type"] == "tool"]
    assert "report_outcome" in tools and tools[-1] == "end_call"
    call_id = events[0]["call_id"]
    async with get_sessionmaker()() as s:
        call = await s.get(Call, call_id)
        assert call.status == CallStatus.completed and call.provider == "browser"
        assert call.transcript and call.outcome_summary
        if "book_meeting" in tools:
            assert (
                (await s.execute(select(Meeting).where(Meeting.call_id == call_id)))
                .scalars()
                .first()
            )


async def test_browser_call_rejects_bad_token():
    """Сокет стенда открывается без cookie панели: без токена со страницы его закрывают с 1008."""
    async with get_sessionmaker()() as s:
        s.add(
            Agent(name="Анна", gender=Gender.female, age=30, voice="marin", required_questions=[])
        )
        await s.commit()

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        async with connect(f"ws://127.0.0.1:{port}/test/call?agent_id=1&token=wrong") as ws:
            try:
                await asyncio.wait_for(ws.recv(), 5)
                raise AssertionError("сокет с неверным токеном не закрылся")
            except ConnectionClosed as exc:
                assert exc.rcvd is not None and exc.rcvd.code == 1008
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, 10)

    async with get_sessionmaker()() as s:
        assert (await s.execute(select(Call))).scalars().first() is None  # звонок не создан
