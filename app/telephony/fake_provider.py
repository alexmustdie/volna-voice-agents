"""Мок телефонии: имитирует дозвон/недозвон.

Прогоняет разговор через реальный мост с FakeLiveSession.
"""

import asyncio
import logging
import random

from app.config import get_settings
from app.models import CallStatus
from app.telephony.base import TelephonyProvider

log = logging.getLogger(__name__)


class FakeTelephonyProvider(TelephonyProvider):
    name = "fake"

    def __init__(self, seed: int | None = None) -> None:
        self.settings = get_settings()
        self._rng = random.Random(seed)
        self._tasks: set[asyncio.Task] = set()
        self._hung_up: set[str] = set()

    @property
    def caller_number(self) -> str | None:
        return self.settings.twilio_phone_number or "fake"

    async def start_outbound_call(self, call_id: int, to_phone: str) -> str:
        provider_call_id = f"fake_{call_id}_{self._rng.randrange(10**6)}"
        task = asyncio.create_task(self._simulate(call_id, provider_call_id, to_phone))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return provider_call_id

    async def hangup(self, provider_call_id: str) -> None:
        self._hung_up.add(provider_call_id)

    async def wait_all(self) -> None:
        """Для тестов: дождаться завершения всех симуляций."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _simulate(self, call_id: int, provider_call_id: str, to_phone: str) -> None:
        # импорт здесь, чтобы избежать циклов
        from app.live.fake import FakeLiveSession
        from app.services import calls as call_service

        try:
            await call_service.set_call_status(call_id, CallStatus.ringing)
            await asyncio.sleep(min(0.3, self.settings.fake_call_seconds / 10))
            if self._rng.random() >= self.settings.fake_answer_rate:
                status = self._rng.choice(
                    [CallStatus.no_answer, CallStatus.no_answer, CallStatus.busy, CallStatus.failed]
                )
                await call_service.finalize_call(call_id, status)
                return
            await call_service.set_call_status(call_id, CallStatus.in_progress)
            contact_name = await call_service.get_contact_name(call_id)
            live = FakeLiveSession(contact_name=contact_name, seed=self._rng.randrange(10**9))
            await call_service.run_bridge(call_id, live, audio_out=None)
            await call_service.finalize_call(call_id, CallStatus.completed)
        except Exception as exc:
            log.exception("fake call %s failed", call_id)
            await call_service.finalize_call(call_id, CallStatus.failed, error=str(exc))
