"""Вебхуки Twilio и медиа-сокет Media Streams.

Поток исходящего звонка:
  воркер -> TwilioProvider.start_outbound_call -> Twilio звонит абоненту
  абонент ответил -> Twilio POST /twilio/voice/outbound/{call_id} -> TwiML <Connect><Stream> с
    <Parameter name="token"> (app.security.media_token)
  Twilio открывает WS /twilio/media/{call_id}, в событии start возвращает токен -> проверка ->
    CallBridge <-> голосовая сессия (app.web.call_session)
  статусы -> POST /twilio/status/{call_id}
Входящий: Twilio POST /twilio/voice/inbound (Voice URL арендованного номера) -> та же схема.
"""

import base64
import logging

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from app.config import get_settings
from app.db import get_sessionmaker, insert_ignore
from app.live.factory import create_live_session
from app.models import Call, CallEvent, CallStatus, utcnow
from app.security import media_token, verify_media_token
from app.services import calls as call_service
from app.web.call_session import run_call_session
from app.web.playback import PlaybackQueue, should_clear

log = logging.getLogger(__name__)
router = APIRouter(prefix="/twilio")

# Заголовок, которым Twilio помечает повторную доставку одного и того же вебхука (документация
# "Webhooks: connection overrides", раздел про повторы): по нему повтор отбрасывается до обработки.
IDEMPOTENCY_HEADER = "I-Twilio-Idempotency-Token"

# Twilio Media Streams всегда отдают и принимают G.711 mu-law 8 кГц
TWILIO_AUDIO_FORMAT = {"type": "audio/pcmu", "rate": 8000}


# фабрика голосовой сессии; тесты подменяют её на FakeLiveSession
def live_session_factory(engine: str | None = None):
    return create_live_session(engine, allow_fake=False)


TWILIO_STATUS_MAP = {
    "queued": CallStatus.queued,
    "initiated": CallStatus.queued,
    "ringing": CallStatus.ringing,
    "in-progress": CallStatus.in_progress,
    "answered": CallStatus.in_progress,
    "completed": CallStatus.completed,
    "busy": CallStatus.busy,
    "no-answer": CallStatus.no_answer,
    "failed": CallStatus.failed,
    "canceled": CallStatus.canceled,
}


async def _validated_form(request: Request) -> dict:
    settings = get_settings()
    form = dict(await request.form())
    if settings.twilio_validate_signature and not settings.fake_calls:
        url = settings.public_base_url.rstrip("/") + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        validator = RequestValidator(settings.twilio_auth_token)
        if not validator.validate(url, form, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(403, "Неверная подпись Twilio")
    return form


def _stream_twiml(call_id: int) -> Response:
    """TwiML <Connect><Stream> с токеном медиа-сокета.

    Twilio возвращает <Parameter> в событии start как start.customParameters (документация
    Media Streams, WebSocket messages: "customParameters: an object holding the custom values set
    when the Stream was defined"). VoiceResponse экранирует XML сам.
    """
    ws_url = f"{get_settings().public_ws_base}/twilio/media/{call_id}"
    stream = Stream(url=ws_url)
    stream.parameter(name="token", value=media_token(call_id))
    connect = Connect()
    connect.append(stream)
    response = VoiceResponse()
    response.append(connect)
    return Response(content=str(response), media_type="application/xml")


@router.post("/voice/outbound/{call_id}")
async def voice_outbound(call_id: int, request: Request):
    await _validated_form(request)
    if not await call_service.call_exists(call_id):
        raise HTTPException(404, "Звонок не найден")
    await call_service.set_call_status(call_id, CallStatus.in_progress)
    return _stream_twiml(call_id)


@router.post("/voice/inbound")
async def voice_inbound(request: Request):
    form = await _validated_form(request)
    call = await call_service.create_inbound_call(
        from_phone=str(form.get("From", "")),
        to_phone=str(form.get("To", "")),
        provider_name="twilio",
        provider_call_id=form.get("CallSid"),
    )
    if call is None:
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?><Response>'
            '<Say language="ru-RU">Этот номер пока не настроен. До '
            "свидания.</Say><Hangup/></Response>"
        )
        return Response(content=twiml, media_type="application/xml")
    return _stream_twiml(call.id)


def event_key(headers, form: dict, call_id: int) -> str:
    """Ключ события статус-колбэка для inbox (уникален в пределах провайдера).

    Приоритет: заголовок I-Twilio-Idempotency-Token (повтор той же доставки), иначе
    CallSid + SequenceNumber (параметр статус-колбэка Call resource: порядковый номер события
    начиная с 0; Twilio шлёт события отдельными запросами и порядок не гарантирует), иначе
    CallSid + CallStatus: без StatusCallbackEvent приходит одно событие completed, у него номера
    нет. CallSid в запросе есть всегда, запасной вариант с call_id на случай его отсутствия.
    """
    token = headers.get(IDEMPOTENCY_HEADER)
    if token:
        return token
    sid = str(form.get("CallSid") or f"call{call_id}")
    sequence = form.get("SequenceNumber")
    if sequence not in (None, ""):
        return f"{sid}:{sequence}"
    return f"{sid}:{form.get('CallStatus', '')}"


async def _record_event(call_id: int, key: str, form: dict) -> bool:
    """Пишет событие в inbox. False: такой ключ уже был, событие это повтор.

    Повтор ловится на INSERT ... ON CONFLICT DO NOTHING, без выборки перед вставкой: два повтора,
    пришедшие одновременно, не пройдут оба. Проверка CallSid здесь же: событие чужого звонка
    (другой CallSid при уже записанном provider_call_id) не принимается.
    """
    call_sid = str(form.get("CallSid") or "")
    async with get_sessionmaker()() as session:
        call = await session.get(Call, call_id)
        if call is None:
            raise HTTPException(404, "Звонок не найден")
        if call_sid and call.provider_call_id and call.provider_call_id != call_sid:
            log.warning(
                "status callback для звонка %s с чужим CallSid %s (ожидался %s)",
                call_id,
                call_sid,
                call.provider_call_id,
            )
            raise HTTPException(400, "CallSid не совпадает со звонком")
        inserted = (
            await session.execute(
                insert_ignore(
                    session,
                    CallEvent,
                    {
                        "call_id": call_id,
                        "provider": "twilio",
                        "provider_event_id": key,
                        "status": str(form.get("CallStatus", ""))[:30],
                        "payload": {k: str(v) for k, v in form.items()},
                        "received_at": utcnow(),
                    },
                    conflict_columns=["provider", "provider_event_id"],
                )
            )
        ).rowcount
        await session.commit()
    return inserted == 1


@router.post("/status/{call_id}")
async def status_callback(call_id: int, request: Request):
    form = await _validated_form(request)
    key = event_key(request.headers, form, call_id)
    if not await _record_event(call_id, key, form):
        log.info("повтор события %s для звонка %s пропущен", key, call_id)
        return Response(status_code=204)
    raw_status = str(form.get("CallStatus", ""))
    status = TWILIO_STATUS_MAP.get(raw_status)
    if status is None:
        log.warning("неизвестный статус Twilio %r для звонка %s", raw_status, call_id)
        return Response(status_code=204)
    duration = int(form.get("CallDuration") or 0) or None
    if status in call_service.TERMINAL_STATUSES:
        bridge = call_service.ACTIVE_BRIDGES.get(call_id)
        if bridge is not None:
            await bridge.stop()
        await call_service.finalize_call(call_id, status, duration=duration)
    else:
        await call_service.set_call_status(call_id, status, provider_call_id=form.get("CallSid"))
    return Response(status_code=204)


async def _wait_for_start(ws: WebSocket) -> dict | None:
    """Читает сокет до события start. None, если поток закрылся раньше (stop или обрыв)."""
    while True:
        msg = await ws.receive_json()
        event = msg.get("event")
        if event == "start":
            return msg["start"]
        if event == "stop":
            return None


@router.websocket("/media/{call_id}")
async def media_stream(ws: WebSocket, call_id: int):
    await ws.accept()
    stream_sid: str | None = None
    closed = False
    settings = get_settings()
    playback = PlaybackQueue(TWILIO_AUDIO_FORMAT)

    async def audio_out(payload_b64: str) -> None:
        if stream_sid and not closed:
            await ws.send_json(
                {"event": "media", "streamSid": stream_sid, "media": {"payload": payload_b64}}
            )
            playback.sent(len(base64.b64decode(payload_b64)))

    async def clear_playback(reason: str) -> None:
        """Сбрасывает очередь провайдера: иначе уже отправленное доиграет поверх речи клиента."""
        if not stream_sid or closed:
            return
        await ws.send_json({"event": "clear", "streamSid": stream_sid})
        log.info("media stream %s: сброс очереди воспроизведения (%s)", call_id, reason)
        playback.reset()

    async def on_live_event(event) -> None:
        outstanding = playback.outstanding_seconds
        if should_clear(event.type, outstanding, settings.barge_in_clear_seconds):
            await clear_playback(f"{event.type}, в очереди {outstanding:.1f} с")

    async def on_hangup() -> None:
        # закрытие сокета завершает <Connect><Stream>, Twilio кладёт трубку
        nonlocal closed
        if not closed:
            closed = True
            try:
                await ws.close()
            except Exception:
                log.debug("media stream %s: сокет уже закрыт", call_id, exc_info=True)

    async def receive_loop() -> None:
        while True:
            msg = await ws.receive_json()
            event = msg.get("event")
            if event == "media":
                bridge = call_service.ACTIVE_BRIDGES.get(call_id)
                if bridge is not None:
                    await bridge.feed_audio(msg["media"]["payload"])
            elif event == "stop":
                return

    # до события start моста нет, ждём только сокет
    try:
        start = await _wait_for_start(ws)
    except WebSocketDisconnect:
        start = None
    except Exception:
        log.exception("media stream %s: ошибка до события start", call_id)
        start = None
    if start is None:
        await on_hangup()
        await call_service.finalize_call(
            call_id, CallStatus.failed, error="медиа-поток Twilio закрылся до события start"
        )
        return
    # Twilio отдаёт <Parameter> из TwiML в start.customParameters
    token = (start.get("customParameters") or {}).get("token")
    if not verify_media_token(call_id, token):
        log.warning("media stream %s: неверный токен, соединение отклонено", call_id)
        await ws.close(code=1008)
        return

    stream_sid = start["streamSid"]
    await call_service.set_call_status(call_id, CallStatus.in_progress)
    live = live_session_factory(await call_service.agent_engine_for_call(call_id))
    # финализируем здесь и входящие, и исходящие; для исходящих status callback Twilio приходит
    # позже и, раз статус уже терминальный, только уточняет длительность (finalize_call
    # идемпотентна)
    await run_call_session(
        call_id,
        live,
        receive_loop,
        audio_format=TWILIO_AUDIO_FORMAT,
        audio_out=audio_out,
        on_hangup=on_hangup,
        event_listener=on_live_event,
        label="media stream",
    )
