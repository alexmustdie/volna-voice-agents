"""Клиент Yandex Realtime против локального сервера.

Сервер воспроизводит документированный протокол; кодек G.711.
"""

import asyncio
import base64
import json

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError

from app.config import get_settings
from app.live.base import LiveSessionConfig
from app.live.bridge import CallBridge
from app.live.g711 import pcm16_to_ulaw, ulaw_to_pcm16
from app.live.yandex_live import YandexLiveSession, normalize_yandex_event
from app.prompts import AGENT_TOOLS


def test_g711_roundtrip():
    samples = [0, 1, -1, 100, -100, 1000, -1000, 8000, -8000, 32767, -32768]
    pcm = b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples)
    back = ulaw_to_pcm16(pcm16_to_ulaw(pcm))
    decoded = [
        int.from_bytes(back[i : i + 2], "little", signed=True) for i in range(0, len(back), 2)
    ]
    for orig, got in zip(samples, decoded, strict=True):
        assert abs(orig - got) <= max(8, abs(orig) * 0.07), (
            orig,
            got,
        )  # логарифмический кодек, допуск ~7%
    assert pcm16_to_ulaw(b"\x00\x00") == b"\xff"


def test_normalize_yandex_events():
    assert (
        normalize_yandex_event({"type": "session.updated", "session": {"id": "s1"}}).type
        == "started"
    )
    assert (
        normalize_yandex_event({"type": "response.output_audio.delta", "delta": "AAA="}).type
        == "audio"
    )
    ev = normalize_yandex_event(
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Алло"}
    )
    assert ev.type == "input_transcript" and ev.data["delta"] == "Алло "
    done = normalize_yandex_event(
        {"type": "response.output_audio_transcript.done", "transcript": " Здравствуйте, Иван!"}
    )
    assert done.type == "output_transcript" and done.data["delta"] == "Здравствуйте, Иван! "
    assert (
        normalize_yandex_event({"type": "response.output_text.delta", "delta": "Здр"}).type
        == "other"
    )
    fc = normalize_yandex_event(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "c1",
                "name": "end_call",
                "arguments": "{}",
            },
        }
    )
    assert fc.type == "function_call" and fc.data["name"] == "end_call"
    assert (
        normalize_yandex_event({"type": "input_audio_buffer.speech_started"}).type == "interrupted"
    )


async def _fake_yandex_server(ws):
    assert ws.request.path.endswith("?model=gpt://folder-1/speech-realtime-260528")
    assert ws.request.headers["Authorization"] == "Api-Key test-key"
    got_tool_output = False
    async for raw in ws:
        msg = json.loads(raw)
        t = msg["type"]
        if t == "session.update":
            s = msg["session"]
            assert s["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 8000}
            assert s["audio"]["output"]["voice"] == "dasha"
            assert s["tools"][0]["name"] == "book_meeting"
            await ws.send(json.dumps({"type": "session.updated", "session": {"id": "ya_1"}}))
            await ws.send(
                json.dumps(
                    {"type": "response.output_audio_transcript.done", "transcript": "Здравствуйте"}
                )
            )
            pcm = b"".join(
                int(x).to_bytes(2, "little", signed=True) for x in (0, 1000, -1000, 8000)
            )
            await ws.send(
                json.dumps(
                    {"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()}
                )
            )
        elif t == "input_audio_buffer.append":
            pcm = base64.b64decode(msg["audio"])
            assert len(pcm) == 4  # 2 байта mu-law -> 2 сэмпла PCM16
            await ws.send(json.dumps({"type": "input_audio_buffer.speech_started"}))
            await ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "Да, давайте",
                    }
                )
            )
            await ws.send(
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "call_id": "c1",
                            "name": "book_meeting",
                            "arguments": json.dumps(
                                {"datetime": "2026-09-15T14:00:00+03:00", "comment": "офис"}
                            ),
                        },
                    }
                )
            )
        elif t == "conversation.item.create":
            assert msg["item"]["type"] == "function_call_output" and msg["item"]["call_id"] == "c1"
            got_tool_output = True
        elif t == "response.create" and got_tool_output:
            got_tool_output = False
            await ws.send(
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "call_id": "c2",
                            "name": "end_call",
                            "arguments": "{}",
                        },
                    }
                )
            )


@pytest.mark.asyncio
async def test_yandex_client_roundtrip_ulaw(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "voice_engine", "yandex")
    monkeypatch.setattr(settings, "yandex_api_key", "test-key")
    monkeypatch.setattr(settings, "yandex_folder_id", "folder-1")
    async with serve(_fake_yandex_server, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(settings, "yandex_realtime_url", f"ws://127.0.0.1:{port}/v1/realtime")
        tool_calls, audio_out, interrupted = [], [], []

        async def tool_handler(name, args):
            tool_calls.append((name, args))
            return {"ok": True}

        async def out(delta):
            audio_out.append(delta)

        async def listener(ev):
            if ev.type == "interrupted":
                interrupted.append(1)

        live = YandexLiveSession()
        bridge = CallBridge(
            1, live, tool_handler, audio_out=out, hangup_delay_seconds=0.05, event_listener=listener
        )
        config = LiveSessionConfig(
            instructions="тест",
            backend_instructions="бэкенд",
            voice="dasha",
            tools=AGENT_TOOLS,
            audio_format={"type": "audio/pcmu", "rate": 8000},
        )
        task = asyncio.create_task(bridge.run(config))
        await asyncio.wait_for(bridge.started.wait(), 5)
        await bridge.feed_audio(base64.b64encode(b"\xff\x80").decode())
        await asyncio.wait_for(task, 10)

    assert bridge.live_session_id == "ya_1"
    assert (
        len(audio_out) == 1 and len(base64.b64decode(audio_out[0])) == 4
    )  # 4 сэмпла PCM -> 4 байта mu-law
    assert [c[0] for c in tool_calls] == ["book_meeting", "end_call"]
    assert interrupted == [1]
    assert [s["role"] for s in bridge.transcript] == ["agent", "user"]
    assert bridge.hangup_requested and bridge.close_reason == "connection_closed"
    assert not bridge.error


class _StubWs:
    """Транспорт для проверки events() без сети: отдаёт заготовленные кадры, потом либо висит,
    либо рвёт соединение."""

    def __init__(self, frames: list[str], then: str) -> None:
        self._frames = list(frames)
        self._then = then
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        if self._then == "hang":
            await asyncio.sleep(3600)
        if self._then == "drop":
            raise ConnectionClosedError(None, None)
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


async def test_yandex_events_aclose_without_runtime_error():
    """Потребитель бросил итерацию посреди сессии: generator.aclose() не должен падать.

    Раньше closed отдавался из finally, и aclose() давал RuntimeError
    "async generator ignored GeneratorExit".
    """
    live = YandexLiveSession()
    live._ws = ws = _StubWs(
        [json.dumps({"type": "session.created", "session": {"id": "s1"}})], then="hang"
    )
    gen = live.events()
    assert (await gen.__anext__()).type == "started"
    await gen.aclose()
    assert ws.closed and live._ws is None
    await live.terminate()  # идемпотентна


@pytest.mark.parametrize("then", ["end", "drop"])
async def test_yandex_events_emit_closed_on_both_exits(then):
    """closed приходит и при штатном конце цикла, и при обрыве соединения."""
    live = YandexLiveSession()
    live._ws = ws = _StubWs(
        [json.dumps({"type": "session.created", "session": {"id": "s1"}})], then=then
    )
    types = [ev.type async for ev in live.events()]
    assert types == ["started", "closed"]
    assert ws.closed and live._ws is None
