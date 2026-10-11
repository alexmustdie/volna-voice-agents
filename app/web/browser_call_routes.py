"""Тестовый звонок из браузера: микрофон -> наш мост -> GPT-Live-1, без телефонии.

Проверяет промпт агента, голос, инструменты и запись результатов в БД на реальной модели.
Аудио: PCM16 24 кГц в обе стороны (браузер <-> сервер: JSON с base64; сервер <-> OpenAI: audio/pcm
24000).
Если для движка агента нет ключей, используется FakeLiveSession, чтобы страница работала и в
мок-режиме.

Сокет /test/call открывается без cookie панели (у панели нет аутентификации, см. README), поэтому
страница стенда выдаёт токен app.security.media_token("browser:{agent_id}"), а сокет требует его
в query и закрывается с кодом 1008 без него.
"""

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session, get_sessionmaker
from app.live.base import LiveEvent
from app.live.factory import create_live_session, engine_has_credentials
from app.models import Agent, Call, CallDirection, CallStatus, utcnow
from app.security import media_token, verify_media_token
from app.services import calls as call_service
from app.web.call_session import run_call_session
from app.web.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/test")
DB = Annotated[AsyncSession, Depends(get_session)]

BROWSER_AUDIO_FORMAT = {"type": "audio/pcm", "rate": 24000}


def make_live_session(engine: str | None, contact_name: str | None):
    return create_live_session(engine, contact_name, allow_fake=True)


def browser_token_subject(agent_id: int) -> str:
    return f"browser:{agent_id}"


@router.get("", response_class=HTMLResponse)
async def test_page(request: Request, db: DB):
    agents = (await db.execute(select(Agent).order_by(Agent.name))).scalars().all()
    real = {a.id: engine_has_credentials(a.engine) for a in agents}
    tokens = {a.id: media_token(browser_token_subject(a.id)) for a in agents}
    return templates.TemplateResponse(
        request, "test_call.html", {"agents": agents, "real": real, "tokens": tokens}
    )


VOICE_SAMPLES = [
    (
        "marin",
        "Здравствуйте, Иван! Меня Аня зовут, я из агентства недвижимости Дом. Звоню вам по поводу "
        "подбора квартиры в новостройке. Удобно сейчас буквально пару минут поговорить?",
    ),
    (
        "quartz",
        "Иван, добрый день! Меня Анна зовут, агентство недвижимости Дом. Я вам звоню по поводу "
        "подбора квартиры в новостройке. Удобно сейчас пару минут поговорить?",
    ),
    (
        "ripple",
        "Здравствуйте, меня зовут Анна, агентство недвижимости Дом. Звоню по поводу подбора "
        "квартиры в новостройке. Удобно сейчас поговорить пару минут?",
    ),
    (
        "vesper",
        "Здравствуйте, Иван! Меня зовут Анна, я из агентства недвижимости Дом. Звоню вам по поводу "
        "подбора квартиры в новостройке. Вам сейчас удобно уделить пару минут?",
    ),
    (
        "willow",
        "Здравствуйте, Иван. Меня зовут Анна, звоню из агентства Дом по поводу подбора квартиры в "
        "новостройке. Удобно сейчас пару минут поговорить?",
    ),
    (
        "stone",
        "Здравствуйте! Иван, это Анна, агентство недвижимости Дом. Я звоню по поводу подбора "
        "квартиры в новостройке. Удобно сейчас говорить пару минут?",
    ),
    (
        "gleam",
        "Здравствуйте, Иван! Меня Анна зовут, я из агентства недвижимости Дом. Я вам звоню по "
        "поводу подбора квартиры в новостройке. Удобно пару минут поговорить?",
    ),
    (
        "meridian",
        "Здравствуйте, Иван. Меня зовут Анна, я из агентства недвижимости Дом. Звоню по поводу "
        "подбора квартиры в новостройке, удобно сейчас говорить пару минут?",
    ),
    (
        "bossa",
        "Здравствуйте, Иван. Меня зовут Анна, агентство недвижимости Дом. Звоню по поводу подбора "
        "квартиры в новостройке. Удобно сейчас поговорить пару минут?",
    ),
    (
        "tempo",
        "Здравствуйте, меня зовут Анна, я из агентства недвижимости Дом. Звоню по поводу подбора "
        "квартиры в новостройке. Вам удобно сейчас пару минут поговорить?",
    ),
    (
        "beacon",
        "Здравствуйте, Иван! Меня зовут Анна, агентство недвижимости Дом. Звоню по поводу подбора "
        "квартиры в новостройке. Вам удобно сейчас пару минут поговорить?",
    ),
    (
        "delta",
        "Здравствуйте, Иван! Меня зовут Анна, я из агентства недвижимости Дом. Звоню по поводу "
        "подбора квартиры в новостройке. Вам сейчас удобно пару минут поговорить?",
    ),
    (
        "cinder",
        "Здравствуйте, Иван! Меня зовут Анна, я из агентства недвижимости Дом. Звоню по поводу "
        "подбора квартиры в новостройке. Вам удобно сейчас пару минут поговорить?",
    ),
]


VOICES_DIR = Path(__file__).parent / "static" / "voices"


@router.get("/voices", response_class=HTMLResponse)
async def voices_page(request: Request):
    texts = dict(VOICE_SAMPLES)
    samples = []
    for name, _ in VOICE_SAMPLES:
        samples.append(
            {
                "name": name,
                "text": texts[name],
                "base": (VOICES_DIR / f"{name}.wav").exists(),
                "native": (VOICES_DIR / f"{name}_native.wav").exists(),
            }
        )
    bossa_variants = [
        ("bossa_native", "носитель языка: описание произношения и интонации"),
        ("bossa_native_fast", "носитель языка, чуть быстрее и разговорнее ('здрасьте', 'щас')"),
        (
            "bossa_native_phonetic",
            "носитель языка плюс фонетические подсказки (аканье, ударения, мягкость)",
        ),
    ]
    bossa_variants = [(f, d) for f, d in bossa_variants if (VOICES_DIR / f"{f}.wav").exists()]
    from app.live.yandex_live import YANDEX_VOICES

    yandex = [v for v in YANDEX_VOICES if (VOICES_DIR / f"ya_{v}.wav").exists()]
    exists = lambda f: (VOICES_DIR / f"{f}.wav").exists()  # noqa: E731
    style = [
        (f, d)
        for f, d in [
            ("ya_alena", "alena, обычный промпт"),
            (
                "ya_alena_colloq",
                "alena, разговорный промпт (короткие фразы, 'слушайте', без канцелярита)",
            ),
            ("ya_alena_good", "alena, разговорный промпт + амплуа good (радостный)"),
            ("ya_alena_speed", "alena, разговорный промпт + скорость 1.15"),
            ("ya_dasha_friendly", "dasha, разговорный промпт + амплуа friendly"),
            ("ya_masha_friendly", "masha, разговорный промпт + амплуа friendly"),
            ("ya_marina_friendly", "marina, разговорный промпт + амплуа friendly"),
            ("ya_lera_friendly", "lera, разговорный промпт + амплуа friendly"),
            ("ya_jane_good", "jane, разговорный промпт + амплуа good"),
        ]
        if exists(f)
    ]
    phone = [
        (f, d)
        for f, d in [
            ("phone_bossa", "GPT-Live-1 bossa"),
            ("phone_bossa_native", "GPT-Live-1 bossa, промпт 'носитель языка'"),
            ("phone_ya_alena", "Яндекс alena"),
            ("phone_ya_alena_good", "Яндекс alena, амплуа good"),
            ("phone_ya_dasha_friendly", "Яндекс dasha, амплуа friendly"),
            ("phone_ya_masha_friendly", "Яндекс masha, амплуа friendly"),
            ("phone_ya_marina_friendly", "Яндекс marina, амплуа friendly"),
        ]
        if exists(f)
    ]
    return templates.TemplateResponse(
        request,
        "test_voices.html",
        {
            "samples": samples,
            "bossa_variants": bossa_variants,
            "yandex": yandex,
            "style": style,
            "phone": phone,
            "engine": get_settings().voice_engine,
        },
    )


@router.websocket("/call")
async def test_call(
    ws: WebSocket, agent_id: int, direction: str = "outbound", name: str = "", token: str = ""
):
    await ws.accept()
    if not verify_media_token(browser_token_subject(agent_id), token):
        log.warning("test call: неверный токен для агента %s, соединение отклонено", agent_id)
        await ws.close(code=1008)
        return
    async with get_sessionmaker()() as session:
        agent = await session.get(Agent, agent_id)
        if agent is None:
            await ws.send_json({"type": "error", "message": "агент не найден"})
            await ws.close()
            return
        call = Call(
            agent_id=agent.id,
            direction=CallDirection.inbound if direction == "inbound" else CallDirection.outbound,
            phone="browser",
            status=CallStatus.in_progress,
            provider="browser",
            started_at=utcnow(),
        )
        session.add(call)
        await session.commit()
        call_id = call.id

    contact_name = name.strip() or None
    live = make_live_session(agent.engine, contact_name)
    closed = False
    hangup_sent = False

    async def send(payload: dict) -> None:
        if not closed:
            try:
                await ws.send_json(payload)
            except Exception:
                log.debug("test call %s: сообщение %s не ушло", call_id, payload.get("type"))

    async def audio_out(b64: str) -> None:
        await send({"type": "audio", "data": b64})

    async def listener(event: LiveEvent) -> None:
        if event.type in ("input_transcript", "output_transcript"):
            await send(
                {
                    "type": "transcript",
                    "role": "user" if event.type == "input_transcript" else "agent",
                    "delta": event.data.get("delta", ""),
                }
            )
        elif event.type == "tool_result":
            await send(
                {
                    "type": "tool",
                    "name": event.data["name"],
                    "arguments": event.data["arguments"],
                    "output": event.data["output"],
                }
            )
        elif event.type in ("started", "closed", "error", "usage", "interrupted"):
            await send({"type": event.type, **{k: v for k, v in event.data.items() if k != "raw"}})

    async def on_hangup() -> None:
        # зовут и мост после end_call, и teardown: сообщение одно
        nonlocal hangup_sent
        if not hangup_sent:
            hangup_sent = True
            await send({"type": "hangup"})

    async def receive_loop() -> None:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")
            if t == "audio":
                bridge = call_service.ACTIVE_BRIDGES.get(call_id)
                if bridge is not None:
                    await bridge.feed_audio(msg.get("data", ""))
            elif t == "stop":
                return

    await send(
        {
            "type": "call",
            "call_id": call_id,
            "real": engine_has_credentials(agent.engine),
            "engine": agent.engine,
        }
    )
    try:
        await run_call_session(
            call_id,
            live,
            receive_loop,
            audio_format=BROWSER_AUDIO_FORMAT,
            audio_out=audio_out,
            on_hangup=on_hangup,
            event_listener=listener,
            contact_name=contact_name,
            label="test call",
        )
    finally:
        await send({"type": "finished", "call_id": call_id})
        closed = True
        try:
            await ws.close()
        except Exception:
            log.debug("test call %s: сокет уже закрыт", call_id, exc_info=True)
