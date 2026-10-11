"""Интерфейс провайдера телефонии.

Приложение знает о провайдере только через этот интерфейс: воркер просит начать исходящий звонок,
мост просит положить трубку. Медиа и статусы приходят обратно через вебхуки провайдера
(для Twilio: app.web.twilio_routes), которые вызывают общие сервисы app.services.calls.
"""

from abc import ABC, abstractmethod


class TelephonyProvider(ABC):
    name: str = "base"

    @property
    def caller_number(self) -> str | None:
        """Наш номер, с которого идут звонки: по нему считаются суточные лимиты площадки."""
        return None

    @abstractmethod
    async def start_outbound_call(self, call_id: int, to_phone: str) -> str:
        """Инициирует звонок, возвращает идентификатор звонка у провайдера."""

    @abstractmethod
    async def hangup(self, provider_call_id: str) -> None: ...


_provider: TelephonyProvider | None = None


def get_provider() -> TelephonyProvider:
    global _provider
    if _provider is None:
        from app.config import get_settings

        if get_settings().fake_calls:
            from app.telephony.fake_provider import FakeTelephonyProvider

            _provider = FakeTelephonyProvider()
        else:
            from app.telephony.twilio_provider import TwilioProvider

            _provider = TwilioProvider()
    return _provider


def set_provider(provider: TelephonyProvider | None) -> None:
    global _provider
    _provider = provider
