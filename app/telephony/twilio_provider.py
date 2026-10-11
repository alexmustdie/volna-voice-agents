"""Twilio Programmable Voice + Media Streams.

Исходящий звонок: REST calls.create(to, from, url=<TwiML endpoint>, status_callback=...).
Twilio по ответу абонента запрашивает TwiML, мы отдаём <Connect><Stream url="wss://.../twilio/media/{call_id}"/>,
после чего Twilio открывает к нам WebSocket с аудио G.711 mu-law 8 кГц (base64), а мы мостим его в
GPT-Live-1.
Хендлеры вебхуков и медиа-сокета: app.web.twilio_routes.
"""

import asyncio

from twilio.rest import Client

from app.config import get_settings
from app.telephony.base import TelephonyProvider


class TwilioProvider(TelephonyProvider):
    name = "twilio"

    def __init__(self) -> None:
        self.settings = get_settings()
        if not (
            self.settings.twilio_account_sid
            and self.settings.twilio_auth_token
            and self.settings.twilio_phone_number
        ):
            raise RuntimeError(
                "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_PHONE_NUMBER не заданы, а "
                "FAKE_CALLS=false"
            )
        self.client = Client(self.settings.twilio_account_sid, self.settings.twilio_auth_token)

    @property
    def caller_number(self) -> str | None:
        return self.settings.twilio_phone_number or None

    def _url(self, path: str) -> str:
        return self.settings.public_base_url.rstrip("/") + path

    async def start_outbound_call(self, call_id: int, to_phone: str) -> str:
        def _create():
            return self.client.calls.create(
                to=to_phone,
                from_=self.settings.twilio_phone_number,
                url=self._url(f"/twilio/voice/outbound/{call_id}"),
                method="POST",
                status_callback=self._url(f"/twilio/status/{call_id}"),
                status_callback_method="POST",
                status_callback_event=["initiated", "ringing", "answered", "completed"],
                timeout=min(self.settings.ringing_timeout_seconds, 600),
            )

        call = await asyncio.to_thread(_create)
        return call.sid

    async def hangup(self, provider_call_id: str) -> None:
        def _update():
            self.client.calls(provider_call_id).update(status="completed")

        await asyncio.to_thread(_update)
