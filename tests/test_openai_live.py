"""Проверка клиента GPT-Live против локального WebSocket-сервера, воспроизводящего документированный
протокол.
"""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from app.config import get_settings
from app.live.base import LiveSessionConfig
from app.live.bridge import CallBridge
from app.live.openai_live import OpenAILiveSession


async def _fake_openai_server(ws):
    received = []
    async for raw in ws:
        msg = json.loads(raw)
        received.append(msg)
        t = msg["type"]
        if t == "session.start":
            assert msg["session"]["model"] == "gpt-live-1"
            assert msg["session"]["audio"]["format"] == {"type": "audio/pcmu", "rate": 8000}
            assert msg["session"]["delegation"]["type"] == "responses"
            assert msg["session"]["delegation"]["responses"]["tools"][0]["name"] == "book_meeting"
            await ws.send(json.dumps({"type": "session.started", "session": {"id": "live_test"}}))
            await ws.send(
                json.dumps(
                    {
                        "type": "session.output_transcript.delta",
                        "delta": "Здравствуйте",
                        "start_ms": 0,
                    }
                )
            )
            await ws.send(json.dumps({"type": "session.output_audio.delta", "delta": "AAAA"}))
        elif t == "session.input_audio.append":
            await ws.send(
                json.dumps(
                    {
                        "type": "session.input_transcript.delta",
                        "delta": "Да, давайте",
                        "start_ms": 1000,
                    }
                )
            )
            await ws.send(
                json.dumps(
                    {
                        "type": "response.event",
                        "delegation_id": "item_1",
                        "event": {
                            "type": "response.output_item.done",
                            "item": {
                                "type": "function_call",
                                "call_id": "call_1",
                                "name": "book_meeting",
                                "arguments": json.dumps(
                                    {"datetime": "2026-09-15T14:00:00+03:00", "comment": "офис"}
                                ),
                            },
                        },
                    }
                )
            )
        elif t == "response.item.create":
            assert msg["item"]["type"] == "function_call_output" and msg["item"]["call_id"] in (
                "call_1",
                "call_2",
            )
            assert json.loads(msg["item"]["output"])["ok"] is True
        elif t == "response.create" and not any(
            r["type"] == "response.create" for r in received[:-1]
        ):
            await ws.send(
                json.dumps(
                    {
                        "type": "response.event",
                        "delegation_id": "item_2",
                        "event": {
                            "type": "response.output_item.done",
                            "item": {
                                "type": "function_call",
                                "call_id": "call_2",
                                "name": "end_call",
                                "arguments": "{}",
                            },
                        },
                    }
                )
            )
        elif t == "session.close":
            await ws.send(
                json.dumps(
                    {"type": "session.closed", "reason": "close_requested", "usage": {"seconds": 7}}
                )
            )
            break
    ws.received = received


@pytest.mark.asyncio
async def test_openai_live_client_roundtrip(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    async with serve(_fake_openai_server, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(
            settings, "openai_live_ws_url", f"ws://127.0.0.1:{port}/v1/live/sessions"
        )

        tool_calls = []
        audio_out = []

        async def tool_handler(name, args):
            tool_calls.append((name, args))
            return {"ok": True}

        async def out(delta):
            audio_out.append(delta)

        live = OpenAILiveSession()
        bridge = CallBridge(1, live, tool_handler, audio_out=out, hangup_delay_seconds=0.05)
        from app.prompts import AGENT_TOOLS

        config = LiveSessionConfig(
            instructions="тест", backend_instructions="бэкенд", voice="marin", tools=AGENT_TOOLS
        )
        task = asyncio.create_task(bridge.run(config))
        await asyncio.wait_for(bridge.started.wait(), 5)
        await bridge.feed_audio("//8=")
        await asyncio.wait_for(task, 10)

    assert bridge.live_session_id == "live_test"
    assert audio_out == ["AAAA"]
    assert [c[0] for c in tool_calls] == ["book_meeting", "end_call"]
    assert tool_calls[0][1]["comment"] == "офис"
    assert bridge.hangup_requested
    assert bridge.close_reason == "close_requested"
    assert bridge.live_usage_seconds == 7
    assert [s["role"] for s in bridge.transcript] == ["agent", "user"]
    assert bridge.transcript[1]["text"] == "Да, давайте"
    assert not bridge.error
