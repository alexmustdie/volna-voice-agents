"""Клиент Realtime API Yandex AI Studio (голосовые модели speech-realtime, родной русский).

Протокол по документации aistudio.yandex.ru (operations/agents/create-voice-agent,
concepts/agents/realtime),
это аналог OpenAI Realtime API:

  URL:     wss://ai.api.cloud.yandex.net/v1/realtime?model=gpt://{FOLDER_ID}/{MODEL}
  header:  Authorization: Api-Key {API_KEY}
  client:  session.update {session: {instructions, output_modalities: ["audio"], audio: {input:
    {format, turn_detection},
                           output: {format, voice, role}}, tools}}
           input_audio_buffer.append {audio: <base64 pcm16>}
           conversation.item.create {item: {type: function_call_output, call_id, output}} +
             response.create
  server:  session.updated / session.created
           response.output_audio.delta {delta}
           conversation.item.input_audio_transcription.completed {transcript}   речь клиента
           response.output_audio_transcript.delta / response.output_text.delta {delta}   речь агента
           response.output_item.done {item: {type: function_call, call_id, name, arguments}}
           input_audio_buffer.speech_started   клиент начал говорить (перебивание)
           error {error: {message}}

Аудио: только audio/pcm (PCM16). Для телефонии с mu-law 8 кГц перекодируем через app.live.g711.
Цены (docs/ai-studio/pricing, 13.09.2026): входящее аудио 0,0264 ₽/с, исходящее 0,0203 ₽/с, токены
0,1–0,8 ₽/1000.

Проверено на живом трафике 13.09.2026 (speech-realtime-260528, audio/pcm 24000, голос dasha):
  - session.created, затем session.updated дважды; распознавание клиента приходит итогом
    conversation.item.input_audio_transcription.completed на каждую фразу;
  - текст агента: дельты response.output_text.delta плюс итог response.output_audio_transcript.done;
    при перебивании (input_audio_buffer.speech_started) текущий ответ обрывается, приходит новый;
  - аудио агента приходит крупными кусками (несколько секунд в одном delta), поэтому при перебивании
    обязательно сбрасывать очередь воспроизведения на стороне клиента.

  - function calling работает; модель может вызвать несколько функций в одном ответе;
  - распознавание клиента накапливается, пока ответы агента прерываются ("алло да", затем
    "алло да слушаю"): в транскрипт пишем только прирост;
  - изредка таймаут хендшейка и обрыв 1006 посреди сессии, поэтому подключение с повторами.

Что не проверено и не реализовано, см. README, раздел "Известные ограничения".
"""

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import websockets

from app.config import get_settings
from app.live.base import LiveEvent, LiveSession, LiveSessionConfig
from app.live.g711 import pcm16_to_ulaw, ulaw_to_pcm16

log = logging.getLogger(__name__)

# Голоса SpeechKit по документации (docs/speechkit/tts/voices); проверить доступность в Realtime
# по факту.
YANDEX_VOICES = [
    "alena",
    "dasha",
    "jane",
    "julia",
    "lera",
    "masha",
    "marina",
    "oksana",
    "omazh",
    "filipp",
    "ermil",
    "zahar",
    "alexander",
    "anton",
    "kirill",
    "madi_ru",
]


async def connect_with_retry(
    url: str, headers: dict, attempts: int = 3, open_timeout: float = 15.0
):
    """Повторяем подключение.

    Вживую TCP-соединение с ai.api.cloud.yandex.net изредка не устанавливается за 10 с.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await websockets.connect(
                url, additional_headers=headers, max_size=None, open_timeout=open_timeout
            )
        except (TimeoutError, OSError) as exc:
            last_exc = exc
            log.warning(
                "yandex realtime: попытка подключения %s/%s не удалась: %s", attempt, attempts, exc
            )
            await asyncio.sleep(0.5 * attempt)
    raise RuntimeError(
        f"не удалось подключиться к Yandex Realtime за {attempts} попытки: {last_exc}"
    )


def normalize_yandex_event(raw: dict[str, Any]) -> LiveEvent:
    t = raw.get("type", "")
    if t in ("session.created", "session.updated"):
        session = raw.get("session") or {}
        return LiveEvent("started", {"session_id": session.get("id") or raw.get("event_id")})
    if t == "response.output_audio.delta":
        return LiveEvent("audio", {"delta": raw.get("delta", "")})
    if t == "conversation.item.input_audio_transcription.completed":
        text = (raw.get("transcript") or "").strip()
        return LiveEvent(
            "input_transcript",
            {"delta": text + " " if text else "", "start_ms": None, "end_ms": None},
        )
    if t == "response.output_audio_transcript.done":
        # вживую текст агента приходит и дельтами output_text.delta, и итогом по каждому ответу;
        # берём только итог, иначе прерванный клиентом ответ дублируется в транскрипте
        text = (raw.get("transcript") or "").strip()
        return LiveEvent(
            "output_transcript",
            {"delta": text + " " if text else "", "start_ms": None, "end_ms": None},
        )
    if t == "response.output_item.done":
        item = raw.get("item") or {}
        if item.get("type") == "function_call":
            args = item.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {"_raw": args}
            return LiveEvent(
                "function_call",
                {"call_id": item.get("call_id"), "name": item.get("name"), "arguments": args},
            )
    if t == "input_audio_buffer.speech_started":
        return LiveEvent("interrupted", {})
    if t == "error":
        err = raw.get("error") or {}
        return LiveEvent(
            "error",
            {"message": err.get("message") or json.dumps(err, ensure_ascii=False), "raw": raw},
        )
    return LiveEvent("other", {"raw": raw})


class YandexLiveSession(LiveSession):
    def __init__(self) -> None:
        self.settings = get_settings()
        self._ws: Any = None
        self._ulaw = False
        self._started_at: float | None = None
        self._started_emitted = False
        self._continue_task: asyncio.Task | None = None
        self._last_user_text = ""

    async def start(self, config: LiveSessionConfig) -> None:
        s = self.settings
        if not (s.yandex_api_key and s.yandex_folder_id):
            raise RuntimeError("YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы, а VOICE_ENGINE=yandex")
        fmt = config.audio_format or {}
        self._ulaw = fmt.get("type") == "audio/pcmu"
        rate = int(fmt.get("rate") or 24000)
        url = f"{s.yandex_realtime_url}?model=gpt://{s.yandex_folder_id}/{s.yandex_realtime_model}"
        self._ws = await connect_with_retry(url, {"Authorization": f"Api-Key {s.yandex_api_key}"})
        instructions = config.instructions
        if config.backend_instructions:
            instructions += "\n\nПравила вызова функций:\n" + config.backend_instructions
        session = {
            "instructions": instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": rate},
                    "languages": ["ru-RU"],
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "silence_duration_ms": 800,
                    },
                },
                "output": {"format": {"type": "audio/pcm", "rate": rate}, "voice": config.voice},
            },
            "tools": config.tools,
        }
        await self._send({"type": "session.update", "session": session})
        self._started_at = time.monotonic()

    async def _send(self, payload: dict) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def send_audio(self, audio_b64: str) -> None:
        if self._ulaw:
            audio_b64 = base64.b64encode(ulaw_to_pcm16(base64.b64decode(audio_b64))).decode()
        await self._send({"type": "input_audio_buffer.append", "audio": audio_b64})

    async def send_tool_result(self, call_id: str, output: dict) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output, ensure_ascii=False),
                },
            }
        )
        # модель может вызвать несколько функций в одном ответе; response.create после каждой даёт
        # "illegal command ... in state WAITING_FOR_TOOL_RESULT", поэтому продолжаем один
        # раз после пачки
        if self._continue_task is not None and not self._continue_task.done():
            self._continue_task.cancel()
        self._continue_task = asyncio.create_task(self._continue_after_tools())

    async def _continue_after_tools(self) -> None:
        await asyncio.sleep(0.2)
        try:
            await self._send({"type": "response.create"})
        except Exception:
            # сессия могла закрыться после end_call: продолжать нечего
            log.debug("yandex realtime: response.create после инструментов не ушёл", exc_info=True)

    async def close(self) -> None:
        # у Realtime API Яндекса нет session.close: закрываем транспорт, events() отдаст closed
        await self.terminate()

    async def terminate(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None

    def _seconds(self) -> float:
        return round(time.monotonic() - self._started_at, 1) if self._started_at else 0.0

    def _closed_event(self) -> LiveEvent:
        return LiveEvent("closed", {"reason": "connection_closed", "seconds": self._seconds()})

    async def events(self) -> AsyncIterator[LiveEvent]:
        # closed отдаём из except и после штатного конца цикла, но не из finally: yield в finally
        # ломает aclose() генератора (RuntimeError "async generator ignored GeneratorExit")
        if self._ws is None:
            return
        try:
            async for message in self._ws:
                try:
                    raw = json.loads(message)
                except json.JSONDecodeError:
                    continue
                event = normalize_yandex_event(raw)
                if event.type == "started":
                    if self._started_emitted:
                        continue
                    self._started_emitted = True
                elif event.type == "input_transcript":
                    # вживую распознавание накапливается, пока ответы агента
                    # прерываются: новая фраза
                    # приходит вместе с предыдущими; оставляем только прирост
                    text = event.data["delta"].strip()
                    prev = self._last_user_text
                    suffix = text[len(prev) :].strip() if prev and text.startswith(prev) else text
                    self._last_user_text = text
                    if not suffix:
                        continue
                    event.data["delta"] = suffix + " "
                elif event.type == "audio" and self._ulaw:
                    event.data["delta"] = base64.b64encode(
                        pcm16_to_ulaw(base64.b64decode(event.data["delta"]))
                    ).decode()
                yield event
        except websockets.ConnectionClosed as exc:
            log.debug(
                "yandex realtime: соединение закрыто (%s)", exc.rcvd.code if exc.rcvd else "-"
            )
            yield self._closed_event()
        else:
            yield self._closed_event()
        finally:
            await asyncio.shield(self.terminate())
