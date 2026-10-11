"""Разбор звонка после разговора: итог, встреча, просьба не звонить по транскрипту.

Голосовая модель в разговоре может сказать "зафиксировала встречу", не вызвав функцию, или ошибиться
с относительной датой. Поэтому результат звонка не зависит только от function calling: после
завершения текстовая модель читает транскрипт и дополняет то, что не записалось во время разговора.
Уже записанное инструментами не перезаписывается.

Модель: YandexGPT при VOICE_ENGINE=yandex (тот же API-ключ), иначе BACKEND_MODEL OpenAI.
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Agent, Call, Meeting
from app.phone import normalize_phone
from app.prompts import OUTCOMES, current_time_line
from app.services.call_results import normalize_messenger, recompute_goal
from app.services.dnc import mark_do_not_call

log = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))

SYSTEM_PROMPT = (
    "Ты анализируешь транскрипт телефонного разговора агента с клиентом и возвращаешь только JSON "
    "без пояснений.\n"
    'Схема: {"summary": str, "outcome": str, "meeting": {"datetime": str, "comment": str} | null, '
    '"contact_phone": str | null, "messenger": str | null, "callback_after": str | null, '
    '"callback_reason": str | null, "do_not_call": bool}.\n'
    "Правила:\n"
    "1. meeting заполняй, только если клиент явно согласился на конкретные дату и время. datetime "
    "в ISO 8601 "
    "с таймзоной +03:00. Относительные даты ('завтра', 'послезавтра', 'в пятницу') считай от "
    "момента звонка, "
    "а не от слов агента: агент мог ошибиться с днём недели.\n"
    "2. contact_phone: номер, который клиент продиктовал для связи в мессенджере, только цифрами; "
    "null, если клиент номер не называл. Не подставляй номер, по которому шёл звонок.\n"
    "3. messenger: telegram, max или whatsapp, если клиент его назвал, иначе null.\n"
    "4. outcome строго одно из: "
    + ", ".join(OUTCOMES)
    + ". Смысл: "
    + "; ".join(f"{k} - {v}" for k, v in OUTCOMES.items())
    + ".\n"
    "5. callback_after и callback_reason заполняй только для callback_later: когда клиент "
    "согласился "
    "на повторный звонок и с чего его начать.\n"
    "6. do_not_call = true, если клиент просил больше не звонить или грубил.\n"
    "7. summary: 1-3 предложения по-русски, включи ответы клиента на обязательные вопросы."
)


def build_user_prompt(agent: Agent, call: Call) -> str:
    started = call.started_at or call.created_at
    if started is not None and started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    lines = [
        "Момент звонка: " + current_time_line(started),
        f"Цель звонка: {agent.call_goal or 'не указана'}",
    ]
    questions = [q for q in (agent.required_questions or []) if str(q).strip()]
    if questions:
        lines.append("Обязательные вопросы: " + "; ".join(questions))
    lines.append("Транскрипт:")
    for seg in call.transcript or []:
        who = "Агент" if seg.get("role") == "agent" else "Клиент"
        lines.append(f"{who}: {seg.get('text', '').strip()}")
    return "\n".join(lines)


def parse_analysis(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError(f"в ответе модели нет JSON: {text[:200]!r}")
    data = json.loads(match.group(0))
    meeting = data.get("meeting")
    if meeting is not None and not (isinstance(meeting, dict) and meeting.get("datetime")):
        meeting = None
    outcome = str(data.get("outcome") or "").strip()

    def text(key: str) -> str | None:
        value = data.get(key)
        return str(value).strip() or None if value not in (None, "") else None

    return {
        "summary": str(data.get("summary") or "").strip(),
        "outcome": outcome if outcome in OUTCOMES else None,
        "meeting": meeting,
        "contact_phone": text("contact_phone"),
        "messenger": text("messenger"),
        "callback_after": text("callback_after"),
        "callback_reason": text("callback_reason"),
        "do_not_call": bool(data.get("do_not_call")),
    }


async def _complete_yandex(system: str, user: str) -> str:
    s = get_settings()
    body = {
        "modelUri": f"gpt://{s.yandex_folder_id}/{s.analysis_yandex_model}",
        "completionOptions": {"stream": False, "temperature": 0.0, "maxTokens": "800"},
        "messages": [{"role": "system", "text": system}, {"role": "user", "text": user}],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            headers={
                "Authorization": f"Api-Key {s.yandex_api_key}",
                "x-folder-id": s.yandex_folder_id,
            },
            json=body,
        )
    r.raise_for_status()
    return r.json()["result"]["alternatives"][0]["message"]["text"]


async def _complete_openai(system: str, user: str) -> str:
    s = get_settings()
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {s.openai_api_key}"},
            json={"model": s.backend_model, "instructions": system, "input": user},
        )
    r.raise_for_status()
    data = r.json()
    if data.get("output_text"):
        return data["output_text"]
    parts = [
        c.get("text", "") for item in data.get("output", []) for c in item.get("content", []) or []
    ]
    return "".join(parts)


async def complete(system: str, user: str) -> str:
    s = get_settings()
    if s.voice_engine == "yandex" and s.yandex_api_key and s.yandex_folder_id:
        return await _complete_yandex(system, user)
    if s.openai_api_key:
        return await _complete_openai(system, user)
    raise RuntimeError("нет ключей для разбора звонка")


def analysis_available() -> bool:
    s = get_settings()
    if not s.post_call_analysis:
        return False
    return bool((s.yandex_api_key and s.yandex_folder_id) or s.openai_api_key)


async def analyze_call(call_id: int) -> dict | None:
    """Разбирает звонок и дополняет результаты.

    Возвращает разбор или None, если разбирать нечего.
    """
    async with get_sessionmaker()() as session:
        call = await session.get(Call, call_id)
        if call is None or not call.transcript:
            return None
        agent = await session.get(Agent, call.agent_id)
        user_prompt = build_user_prompt(agent, call)

    try:
        result = parse_analysis(await complete(SYSTEM_PROMPT, user_prompt))
    except Exception as exc:
        log.warning("разбор звонка %s не удался: %s", call_id, exc)
        async with get_sessionmaker()() as session:
            call = await session.get(Call, call_id)
            if call is not None:
                call.error = (call.error + "\n" if call.error else "") + f"разбор звонка: {exc}"
                await session.commit()
        return None

    await apply_analysis(call_id, result)
    return result


async def apply_analysis(call_id: int, result: dict) -> None:
    """Дополняет звонок разбором.

    Записанное инструментами во время разговора не перезаписывается.
    """
    async with get_sessionmaker()() as session:
        call = await session.get(Call, call_id)
        if call is None:
            return
        if result.get("summary") and not call.outcome_summary:
            call.outcome_summary = result["summary"]
        if result.get("outcome") and not call.outcome:
            call.outcome = result["outcome"]
        phone = normalize_phone(result.get("contact_phone"))
        if phone and not call.direct_phone:
            call.direct_phone = phone
        messenger = normalize_messenger(result.get("messenger"))
        if messenger and not call.messenger:
            call.messenger = messenger
        if result.get("callback_after") and not call.callback_after:
            call.callback_after = result["callback_after"]
        if result.get("callback_reason") and not call.callback_reason:
            call.callback_reason = result["callback_reason"]
        meeting = result.get("meeting")
        if meeting:
            has_meeting = (
                await session.execute(select(Meeting.id).where(Meeting.call_id == call_id).limit(1))
            ).first()
            if not has_meeting:
                when = _parse_iso(meeting.get("datetime"))
                if when is not None:
                    session.add(
                        Meeting(
                            call_id=call_id,
                            scheduled_at=when,
                            comment=str(meeting.get("comment") or "") + " (из разбора звонка)",
                        )
                    )
        if (
            result.get("do_not_call") or call.outcome == "aggressive"
        ) and not call.do_not_call_requested:
            await mark_do_not_call(session, call)
        await session.flush()
        await recompute_goal(session, call)
        if call.goal_achieved and not call.outcome:
            call.outcome = "meeting_booked"
        await session.commit()


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=MSK)
