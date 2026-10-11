"""Абстракция голосовой сессии GPT-Live-1.

Реальная реализация: app.live.openai_live.OpenAILiveSession (WebSocket wss://api.openai.com/v1/live/sessions).
Мок: app.live.fake.FakeLiveSession (сценарный диалог без сети).
Мост app.live.bridge.CallBridge работает только с нормализованными LiveEvent, поэтому одинаково
обслуживает оба режима.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LiveSessionConfig:
    instructions: str
    backend_instructions: str
    voice: str
    tools: list[dict]
    # Формат аудио на входе и выходе сессии. Для Twilio Media Streams: G.711 mu-law 8 кГц.
    audio_format: dict = field(default_factory=lambda: {"type": "audio/pcmu", "rate": 8000})


@dataclass
class LiveEvent:
    """Нормализованное событие сессии.

    type:
      started            data: {session_id}
      audio              data: {delta}  base64-аудио в формате сессии
      input_transcript   data: {delta, start_ms, end_ms}   речь клиента (ASR)
      output_transcript  data: {delta, start_ms, end_ms}   речь агента
      function_call      data: {call_id, name, arguments: dict}
      usage              data: {seconds}
      closed             data: {reason, seconds}
      error              data: {message, raw}
      other              data: {raw}
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)


class LiveSession(ABC):
    # Нужен ли разбор транскрипта после звонка. У мока транскрипт сценарный, разбирать нечего.
    supports_analysis: bool = True

    @abstractmethod
    async def start(self, config: LiveSessionConfig) -> None: ...

    @abstractmethod
    async def send_audio(self, audio_b64: str) -> None: ...

    @abstractmethod
    async def send_tool_result(self, call_id: str, output: dict) -> None: ...

    @abstractmethod
    async def close(self) -> None:
        """Штатное закрытие: попрощаться с сервером, events() отдаст closed."""

    async def terminate(self) -> None:
        """Закрыть транспорт без прощания. Идемпотентна: мост зовёт её в finally при любом исходе.

        По умолчанию то же, что close(); реальные клиенты рвут WebSocket.
        """
        await self.close()

    @abstractmethod
    def events(self) -> AsyncIterator[LiveEvent]: ...
