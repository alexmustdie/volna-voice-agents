"""Общий жизненный цикл звонка поверх WebSocket: мост, чтение сокета, teardown.

Используется медиа-сокетом Twilio (twilio_routes) и браузерным стендом (browser_call_routes).
Сокет читает вызывающая сторона (протоколы разные), здесь общее: сессия заканчивается, когда
завершилось любое из двух (абонент положил трубку или мост закончил, штатно либо с ошибкой),
после чего один teardown: остановить мост, дождаться его, положить трубку, финализировать звонок по
итогу моста, запустить разбор транскрипта фоновой задачей.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from fastapi import WebSocketDisconnect

from app.live.base import LiveSession
from app.live.bridge import AudioOut, EventListener, Hangup
from app.models import CallStatus
from app.services import calls as call_service

log = logging.getLogger(__name__)

BRIDGE_FINISH_TIMEOUT = 15.0


async def run_call_session(
    call_id: int,
    live: LiveSession,
    receive_loop: Callable[[], Awaitable[None]],
    *,
    audio_format: dict,
    audio_out: AudioOut | None,
    on_hangup: Hangup,
    event_listener: EventListener | None = None,
    contact_name: str | None = None,
    label: str = "call",
) -> CallStatus:
    """Ведёт звонок до конца и возвращает его терминальный статус.

    receive_loop читает сокет и возвращается, когда абонент положил трубку; on_hangup должен быть
    идемпотентен: его зовёт и мост после end_call, и teardown.
    """
    bridge_task = asyncio.create_task(
        call_service.run_bridge(
            call_id,
            live,
            audio_out,
            on_hangup,
            audio_format=audio_format,
            event_listener=event_listener,
            contact_name=contact_name,
        )
    )
    receive_task = asyncio.create_task(receive_loop())
    try:
        done, _ = await asyncio.wait(
            {receive_task, bridge_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if receive_task in done:
            exc = receive_task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                log.exception("%s %s: ошибка чтения сокета", label, call_id, exc_info=exc)
    finally:
        if not receive_task.done():
            receive_task.cancel()
            await asyncio.gather(receive_task, return_exceptions=True)
        status = await _teardown(call_id, bridge_task, on_hangup, label)
    if live.supports_analysis:
        call_service.schedule_post_call_analysis(call_id)
    return status


async def _teardown(
    call_id: int, bridge_task: asyncio.Task, on_hangup: Hangup, label: str
) -> CallStatus:
    bridge = call_service.ACTIVE_BRIDGES.get(call_id)
    if bridge is not None:
        await bridge.stop()
    failure = ""
    try:
        bridge = await asyncio.wait_for(bridge_task, timeout=BRIDGE_FINISH_TIMEOUT)
    except TimeoutError:
        # wait_for уже отменил задачу; итог берём из объекта моста, если он успел появиться
        failure = f"мост не завершился за {BRIDGE_FINISH_TIMEOUT:.0f} с"
        log.error("%s %s: %s", label, call_id, failure)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        log.exception("%s %s: мост упал", label, call_id)
    await on_hangup()
    status, error = call_service.bridge_outcome(bridge, failure)
    await call_service.finalize_call(call_id, status, error=error)
    return status
