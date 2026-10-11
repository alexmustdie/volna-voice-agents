"""Мост между телефонным аудио и сессией GPT-Live-1.

Не зависит от провайдера телефонии и от БД: получает LiveSession, колбэк для отправки аудио
абоненту, обработчик инструментов и колбэк для завершения звонка.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.live.base import LiveEvent, LiveSession, LiveSessionConfig

log = logging.getLogger(__name__)

AudioOut = Callable[[str], Awaitable[None]]
ToolHandler = Callable[[str, dict], Awaitable[dict]]
Hangup = Callable[[], Awaitable[None]]
EventListener = Callable[[LiveEvent], Awaitable[None]]


class CallBridge:
    def __init__(
        self,
        call_id: int,
        live: LiveSession,
        tool_handler: ToolHandler,
        audio_out: AudioOut | None = None,
        on_hangup: Hangup | None = None,
        hangup_delay_seconds: float = 2.0,
        event_listener: EventListener | None = None,
    ) -> None:
        self.call_id = call_id
        self.live = live
        self.tool_handler = tool_handler
        self.audio_out = audio_out
        self.on_hangup = on_hangup
        self.hangup_delay_seconds = hangup_delay_seconds
        self.event_listener = event_listener

        self.transcript: list[dict] = []
        self.live_session_id: str | None = None
        self.live_usage_seconds: float = 0.0
        self.close_reason: str | None = None
        self.error: str = ""
        self.hangup_requested = False
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self._hangup_task: asyncio.Task | None = None

    async def run(self, config: LiveSessionConfig) -> None:
        try:
            await self.live.start(config)
            async for event in self.live.events():
                await self._handle(event)
        except Exception as exc:
            log.exception("bridge %s: ошибка", self.call_id)
            self.error = self.error or f"{type(exc).__name__}: {exc}"
            await self._notify(LiveEvent("error", {"message": self.error}))
        finally:
            # задача отбоя закрывает телефонный сокет: её нужно дождаться, а не отменять
            if self._hangup_task is not None and not self._hangup_task.done():
                try:
                    await asyncio.wait_for(self._hangup_task, timeout=self.hangup_delay_seconds + 5)
                except Exception:
                    log.warning("bridge %s: отбой не завершился штатно", self.call_id)
            # start() мог упасть уже после подключения: транспорт закрываем при любом исходе
            try:
                await self.live.terminate()
            except Exception:
                log.warning("bridge %s: транспорт сессии не закрылся", self.call_id, exc_info=True)
            self.finished.set()

    async def feed_audio(self, audio_b64: str) -> None:
        if self.started.is_set() and not self.finished.is_set():
            await self.live.send_audio(audio_b64)

    async def stop(self) -> None:
        """Внешнее завершение (абонент положил трубку, остановка кампании)."""
        if not self.finished.is_set():
            await self.live.close()

    async def _notify(self, event: LiveEvent) -> None:
        if self.event_listener is not None:
            try:
                await self.event_listener(event)
            except Exception:
                log.debug("bridge %s: слушатель событий упал", self.call_id, exc_info=True)

    async def _handle(self, event: LiveEvent) -> None:
        if event.type != "audio":
            await self._notify(event)
        t = event.type
        if t == "started":
            self.live_session_id = event.data.get("session_id")
            self.started.set()
        elif t == "audio":
            if self.audio_out is not None:
                await self.audio_out(event.data.get("delta", ""))
        elif t == "input_transcript":
            self._append_transcript("user", event.data)
        elif t == "output_transcript":
            self._append_transcript("agent", event.data)
        elif t == "function_call":
            await self._handle_function_call(event.data)
        elif t == "usage":
            self.live_usage_seconds = max(
                self.live_usage_seconds, float(event.data.get("seconds") or 0)
            )
        elif t == "closed":
            self.close_reason = event.data.get("reason")
            self.live_usage_seconds = max(
                self.live_usage_seconds, float(event.data.get("seconds") or 0)
            )
        elif t == "interrupted":
            # слушатель событий (например, тестовая страница) сбрасывает очередь воспроизведения
            pass
        elif t == "error":
            msg = str(event.data.get("message"))
            log.warning("bridge %s: GPT-Live error: %s", self.call_id, msg)
            self.error = (self.error + "\n" if self.error else "") + msg
        else:
            log.debug("bridge %s: событие %s", self.call_id, event.data.get("raw", {}).get("type"))

    def _append_transcript(self, role: str, data: dict) -> None:
        delta = data.get("delta") or ""
        if not delta:
            return
        if self.transcript and self.transcript[-1]["role"] == role:
            seg = self.transcript[-1]
            seg["text"] += delta
            if data.get("end_ms") is not None:
                seg["end_ms"] = data["end_ms"]
        else:
            self.transcript.append(
                {
                    "role": role,
                    "text": delta,
                    "start_ms": data.get("start_ms"),
                    "end_ms": data.get("end_ms"),
                }
            )

    async def _handle_function_call(self, data: dict) -> None:
        name = data.get("name") or ""
        args = data.get("arguments") or {}
        call_id = data.get("call_id") or ""
        try:
            output = await self.tool_handler(name, args)
        except Exception as exc:
            log.exception("bridge %s: инструмент %s упал", self.call_id, name)
            output = {"ok": False, "error": str(exc)}
        try:
            await self.live.send_tool_result(call_id, output)
        except Exception:
            log.warning("bridge %s: не удалось отправить результат инструмента", self.call_id)
        await self._notify(
            LiveEvent("tool_result", {"name": name, "arguments": args, "output": output})
        )
        if name == "end_call" and not self.hangup_requested:
            self.hangup_requested = True
            self._hangup_task = asyncio.create_task(self._delayed_hangup())

    async def _delayed_hangup(self) -> None:
        # даём модели договорить прощание: сигнала окончания речи у GPT-Live нет (README, "Известные
        # ограничения"), поэтому ждём фиксированную паузу
        await asyncio.sleep(self.hangup_delay_seconds)
        try:
            await self.live.close()
        finally:
            if self.on_hangup is not None:
                await self.on_hangup()
