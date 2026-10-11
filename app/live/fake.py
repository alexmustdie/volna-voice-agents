"""Мок GPT-Live-1: сценарный диалог без сети.

Воспроизводит те же LiveEvent, что и реальный клиент.
"""

import asyncio
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.live.base import LiveEvent, LiveSession, LiveSessionConfig


class FakeLiveSession(LiveSession):
    supports_analysis = False  # транскрипт сценарный: разбор после звонка не нужен

    def __init__(self, contact_name: str | None = None, seed: int | None = None) -> None:
        self.settings = get_settings()
        self.contact_name = contact_name or "клиент"
        self._rng = random.Random(seed)
        self._queue: asyncio.Queue[LiveEvent | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._closed = False
        self.tool_results: list[tuple[str, dict]] = []
        self.audio_chunks_received = 0

    async def start(self, config: LiveSessionConfig) -> None:
        self._config = config
        self._task = asyncio.create_task(self._scenario())

    async def send_audio(self, audio_b64: str) -> None:
        self.audio_chunks_received += 1

    async def send_tool_result(self, call_id: str, output: dict) -> None:
        self.tool_results.append((call_id, output))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task and not self._task.done():
            self._task.cancel()
        await self._queue.put(
            LiveEvent("closed", {"reason": "close_requested", "seconds": self._seconds})
        )
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[LiveEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event
            if event.type == "closed":
                return

    # --- сценарий ---
    _seconds = 0.0

    async def _emit(self, event: LiveEvent, pause: float) -> None:
        await self._queue.put(event)
        await asyncio.sleep(pause)
        self._seconds += pause

    async def _say(self, role: str, text: str, pause: float) -> None:
        etype = "output_transcript" if role == "agent" else "input_transcript"
        start_ms = int(self._seconds * 1000)
        words = text.split(" ")
        for i, word in enumerate(words):
            delta = word if i == len(words) - 1 else word + " "
            await self._queue.put(
                LiveEvent(etype, {"delta": delta, "start_ms": start_ms, "end_ms": None})
            )
        if role == "agent":
            await self._queue.put(LiveEvent("audio", {"delta": "AAAA"}))  # заглушка аудио
        await asyncio.sleep(pause)
        self._seconds += pause

    async def _scenario(self) -> None:
        total = max(self.settings.fake_call_seconds, 0.5)
        step = total / 8
        try:
            await self._emit(
                LiveEvent("started", {"session_id": f"fake_live_{self._rng.randrange(10**6)}"}), 0
            )
            meeting = self._rng.random() < self.settings.fake_meeting_rate
            refuse = (not meeting) and self._rng.random() < 0.2

            await self._say(
                "agent",
                f"Здравствуйте, {self.contact_name}! Это симулированный звонок. Удобно говорить?",
                step,
            )
            if refuse:
                await self._say("user", "Нет, не звоните мне больше, пожалуйста.", step)
                await self._say(
                    "agent",
                    "Поняла вас, извините за беспокойство. Больше не побеспокоим. До свидания!",
                    step,
                )
                await self._emit(
                    LiveEvent(
                        "function_call",
                        {
                            "call_id": "fc_dnc",
                            "name": "mark_do_not_call",
                            "arguments": {"reason": "просьба не звонить"},
                        },
                    ),
                    step,
                )
                await self._emit(
                    LiveEvent(
                        "function_call",
                        {
                            "call_id": "fc_out",
                            "name": "report_outcome",
                            "arguments": {
                                "outcome": "refused",
                                "summary": "Клиент просил не звонить.",
                            },
                        },
                    ),
                    step,
                )
            else:
                await self._say("user", "Да, слушаю.", step)
                await self._say(
                    "agent", "Подскажите, после продажи планируете покупать другое жильё?", step
                )
                if meeting:
                    await self._say("user", "Да, ищем двушку взамен.", step)
                    when = (datetime.now(UTC) + timedelta(days=1)).replace(
                        hour=12, minute=0, second=0, microsecond=0
                    )
                    await self._say(
                        "agent",
                        "Специалист может созвониться завтра в пятнадцать или в семнадцать. Как "
                        "удобнее?",
                        step,
                    )
                    await self._say(
                        "user",
                        "Давайте в пятнадцать. Пишите в телеграм, номер 921 555 01 23.",
                        step,
                    )
                    await self._say(
                        "agent",
                        "Девятьсот двадцать один, пятьсот пятьдесят пять, ноль один, двадцать три, "
                        "верно?",
                        step,
                    )
                    await self._say("user", "Да.", step)
                    await self._emit(
                        LiveEvent(
                            "function_call",
                            {
                                "call_id": "fc_meet",
                                "name": "book_meeting",
                                "arguments": {
                                    "datetime": when.isoformat(),
                                    "contact_phone": "9215550123",
                                    "messenger": "telegram",
                                    "comment": "ищет двушку взамен (симуляция)",
                                },
                            },
                        ),
                        step,
                    )
                    await self._emit(
                        LiveEvent(
                            "function_call",
                            {
                                "call_id": "fc_out",
                                "name": "report_outcome",
                                "arguments": {
                                    "outcome": "meeting_booked",
                                    "summary": "Назначена встреча.",
                                },
                            },
                        ),
                        step,
                    )
                else:
                    await self._say("user", "Нет, только продаю, взамен ничего не беру.", step)
                    await self._say("agent", "Понятно, не буду отвлекать. Хорошей продажи.", step)
                    await self._emit(
                        LiveEvent(
                            "function_call",
                            {
                                "call_id": "fc_out",
                                "name": "report_outcome",
                                "arguments": {
                                    "outcome": "not_buying",
                                    "summary": "Продаёт без покупки.",
                                },
                            },
                        ),
                        step,
                    )
            await self._emit(LiveEvent("usage", {"seconds": round(self._seconds, 1)}), 0)
            await self._emit(
                LiveEvent(
                    "function_call", {"call_id": "fc_end", "name": "end_call", "arguments": {}}
                ),
                0,
            )
            # мост после end_call вызовет close(); если нет, закрываемся сами через паузу
            await asyncio.sleep(step)
            await self.close()
        except asyncio.CancelledError:
            pass
