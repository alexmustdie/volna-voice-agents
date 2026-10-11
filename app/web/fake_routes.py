"""Эндпоинты мок-режима: симуляция входящего звонка без Twilio."""

import asyncio
import logging

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.live.fake import FakeLiveSession
from app.models import CallStatus
from app.phone import normalize_phone
from app.services import calls as call_service

log = logging.getLogger(__name__)
router = APIRouter(prefix="/fake")
_tasks: set[asyncio.Task] = set()


async def simulate_inbound(from_phone: str, to_phone: str) -> int | None:
    call = await call_service.create_inbound_call(
        from_phone, to_phone, provider_name="fake", provider_call_id=None
    )
    if call is None:
        return None

    async def _run() -> None:
        try:
            await call_service.run_bridge(
                call.id, FakeLiveSession(contact_name=None), audio_out=None
            )
            await call_service.finalize_call(call.id, CallStatus.completed)
        except Exception as exc:
            log.exception("fake inbound %s failed", call.id)
            await call_service.finalize_call(call.id, CallStatus.failed, error=str(exc))

    task = asyncio.create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return call.id


async def wait_all() -> None:
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


@router.post("/inbound")
async def fake_inbound(from_phone: str = Form(...), to_phone: str = Form(...)):
    if not get_settings().fake_calls:
        raise HTTPException(400, "Доступно только при FAKE_CALLS=true")
    frm = normalize_phone(from_phone) or from_phone
    to = normalize_phone(to_phone) or to_phone
    call_id = await simulate_inbound(frm, to)
    if call_id is None:
        raise HTTPException(404, f"Номер {to} не привязан к агенту")
    return RedirectResponse(f"/calls/{call_id}", status_code=303)
