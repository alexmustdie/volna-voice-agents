"""Клиент GPT-Live-1 по WebSocket.

Протокол взят из документации OpenAI (developers.openai.com, guides: voice-websockets?api=live,
live-conversations, live-delegation, voice-sip?api=live) по состоянию на 13.09.2026:

  URL:     wss://api.openai.com/v1/live/sessions   (Authorization: Bearer <key>)
  client:  session.start {session: {model, instructions, audio: {format, output: {voice}},
    delegation}}
           session.input_audio.append {audio: <base64>}
           response.item.create {item: {type: function_call_output, call_id, output}} +
             response.create
           session.close
  server:  session.started {session: {id}}
           session.output_audio.delta {delta}
           session.input_transcript.delta / session.output_transcript.delta {delta, start_ms,
             end_ms}
           response.event {delegation_id, event: {type: response.output_item.done, item: {type:
             function_call,
                           call_id, name, arguments}}}
           session.usage.updated {usage: {seconds}}
           session.closed {reason, usage: {seconds}}
           error {error: {...}}

Форматы аудио: audio/pcm (16000|24000), audio/pcmu 8000, audio/pcma 8000.
Бэкенд-модель задаётся в delegation.responses.model (у OpenAI нет модели по умолчанию).

Проверено на реальном трафике 13.09.2026 (audio/pcm 24000, delegation responses):
  - session.started, session.output_audio.delta {delta} (без start_ms/end_ms),
    session.usage.updated,
    session.closed {reason, usage.seconds}, error {error.message} приходят как описано;
  - session.input_transcript.delta / session.output_transcript.delta приходят автоматически,
    фрагментами
    ~200 мс с start_ms/end_ms, включать ничего не нужно;
  - выходной аудиопоток непрерывный: на тишине модель шлёт тишину, а не молчит по событиям;
  - модель не говорит первой: на исходящем звонке ждёт "Алло" от абонента. Если абонент молчит,
    можно подтолкнуть через session.commentary.append (пока не реализовано).

Что не проверено и не реализовано, см. README, раздел "Известные ограничения".
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import websockets

from app.config import get_settings
from app.live.base import LiveEvent, LiveSession, LiveSessionConfig

log = logging.getLogger(__name__)


def normalize_server_event(raw: dict[str, Any]) -> LiveEvent:
    """Переводит сырое событие GPT-Live в LiveEvent. Чистая функция, покрыта тестами."""
    t = raw.get("type", "")
    if t == "session.started":
        return LiveEvent("started", {"session_id": (raw.get("session") or {}).get("id")})
    if t == "session.output_audio.delta":
        return LiveEvent("audio", {"delta": raw.get("delta", "")})
    if t == "session.input_transcript.delta":
        return LiveEvent(
            "input_transcript",
            {
                "delta": raw.get("delta", ""),
                "start_ms": raw.get("start_ms"),
                "end_ms": raw.get("end_ms"),
            },
        )
    if t == "session.output_transcript.delta":
        return LiveEvent(
            "output_transcript",
            {
                "delta": raw.get("delta", ""),
                "start_ms": raw.get("start_ms"),
                "end_ms": raw.get("end_ms"),
            },
        )
    if t == "session.usage.updated":
        return LiveEvent("usage", {"seconds": (raw.get("usage") or {}).get("seconds", 0)})
    if t == "session.closed":
        return LiveEvent(
            "closed",
            {"reason": raw.get("reason"), "seconds": (raw.get("usage") or {}).get("seconds", 0)},
        )
    if t == "error":
        err = raw.get("error") or {}
        return LiveEvent(
            "error",
            {"message": err.get("message") or json.dumps(err, ensure_ascii=False), "raw": raw},
        )
    if t == "response.event":
        inner = raw.get("event") or {}
        item = inner.get("item") or {}
        if inner.get("type") == "response.output_item.done" and item.get("type") == "function_call":
            args = item.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {"_raw": args}
            return LiveEvent(
                "function_call",
                {
                    "call_id": item.get("call_id"),
                    "name": item.get("name"),
                    "arguments": args,
                    "delegation_id": raw.get("delegation_id"),
                },
            )
    return LiveEvent("other", {"raw": raw})


class OpenAILiveSession(LiveSession):
    def __init__(self) -> None:
        self.settings = get_settings()
        self._ws: Any = None
        self._event_counter = 0

    def _next_event_id(self, prefix: str) -> str:
        self._event_counter += 1
        return f"{prefix}_{self._event_counter}"

    async def start(self, config: LiveSessionConfig) -> None:
        if not self.settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY не задан, а FAKE_CALLS=false")
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "User-Agent": "volna/0.1 (python-websockets)",
        }
        self._ws = await websockets.connect(
            self.settings.openai_live_ws_url, additional_headers=headers, max_size=None
        )
        session = {
            "model": self.settings.live_model,
            "instructions": config.instructions,
            "audio": {
                "format": config.audio_format,
                "output": {"voice": config.voice},
            },
            "delegation": {
                "type": "responses",
                "responses": {
                    "model": self.settings.backend_model,
                    "instructions": config.backend_instructions,
                    "tools": config.tools,
                    "tool_choice": "auto",
                },
            },
        }
        await self._send(
            {"type": "session.start", "event_id": self._next_event_id("start"), "session": session}
        )

    async def _send(self, payload: dict) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def send_audio(self, audio_b64: str) -> None:
        await self._send({"type": "session.input_audio.append", "audio": audio_b64})

    async def send_tool_result(self, call_id: str, output: dict) -> None:
        await self._send(
            {
                "type": "response.item.create",
                "event_id": self._next_event_id("tool_result"),
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output, ensure_ascii=False),
                },
            }
        )
        await self._send({"type": "response.create", "event_id": self._next_event_id("continue")})

    async def close(self) -> None:
        if self._ws is None:
            return
        try:
            await self._send({"type": "session.close"})
        except Exception:
            # соединение могло уже закрыться: прощаться не с кем
            log.debug("GPT-Live: session.close не ушёл", exc_info=True)

    async def terminate(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None

    async def events(self) -> AsyncIterator[LiveEvent]:
        if self._ws is None:
            return
        try:
            async for message in self._ws:
                try:
                    raw = json.loads(message)
                except json.JSONDecodeError:
                    log.warning("GPT-Live: не JSON: %r", message[:200])
                    continue
                event = normalize_server_event(raw)
                yield event
                if event.type == "closed":
                    break
        except websockets.ConnectionClosed as exc:
            yield LiveEvent("closed", {"reason": f"connection_closed:{exc.code}", "seconds": 0})
        finally:
            await asyncio.shield(self.terminate())
