"""Жизненный цикл звонка: создание, статусы, мост, инструменты, финализация.

Функции открывают собственную сессию БД, потому что вызываются из фоновых задач
(воркер, симулятор, медиа-сокет Twilio), а не только из HTTP-хендлеров.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_sessionmaker
from app.live.base import LiveSession, LiveSessionConfig
from app.live.bridge import CallBridge
from app.models import (
    ACTIVE_CALL_STATUSES,
    Agent,
    Call,
    CallDirection,
    CallStatus,
    Campaign,
    Contact,
    ContactStatus,
    InboundNumber,
    Meeting,
    utcnow,
)
from app.phone import normalize_phone
from app.prompts import (
    AGENT_TOOLS,
    MESSENGERS,
    OUTCOMES,
    build_backend_instructions,
    build_voice_instructions,
)
from app.services import offers_client
from app.services.analysis import analysis_available, analyze_call
from app.services.call_results import normalize_messenger, recompute_goal
from app.services.dnc import mark_do_not_call

log = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    CallStatus.completed,
    CallStatus.no_answer,
    CallStatus.busy,
    CallStatus.failed,
    CallStatus.canceled,
}
_STATUS_ORDER = {CallStatus.queued: 0, CallStatus.ringing: 1, CallStatus.in_progress: 2}

# активные мосты по id звонка: нужны медиа-сокету Twilio и остановке кампании
ACTIVE_BRIDGES: dict[int, CallBridge] = {}


async def create_outbound_call(
    session: AsyncSession,
    campaign: Campaign,
    contact: Contact,
    provider_name: str,
    from_phone: str | None = None,
) -> Call:
    call = Call(
        campaign_id=campaign.id,
        contact_id=contact.id,
        agent_id=campaign.agent_id,
        direction=CallDirection.outbound,
        phone=contact.phone,
        status=CallStatus.queued,
        provider=provider_name,
        from_phone=from_phone,
    )
    session.add(call)
    await session.flush()
    return call


async def _call_by_provider_id(
    session: AsyncSession, provider_name: str, provider_call_id: str
) -> Call | None:
    return (
        await session.execute(
            select(Call).where(
                Call.provider == provider_name, Call.provider_call_id == provider_call_id
            )
        )
    ).scalar_one_or_none()


async def create_inbound_call(
    from_phone: str, to_phone: str, provider_name: str, provider_call_id: str | None
) -> Call | None:
    """Входящий звонок на арендованный номер. None, если номер не привязан к агенту.

    Get-or-create по (provider, provider_call_id): повтор вебхука провайдера возвращает тот же
    звонок. Гонку двух повторов разрешает уникальный индекс uq_calls_provider_call_id: проигравший
    INSERT читает звонок победителя.
    """
    async with get_sessionmaker()() as session:
        number = (
            await session.execute(select(InboundNumber).where(InboundNumber.phone == to_phone))
        ).scalar_one_or_none()
        if number is None:
            return None
        if provider_call_id:
            existing = await _call_by_provider_id(session, provider_name, provider_call_id)
            if existing is not None:
                return existing
        call = Call(
            agent_id=number.agent_id,
            direction=CallDirection.inbound,
            phone=from_phone,
            status=CallStatus.in_progress,
            provider=provider_name,
            provider_call_id=provider_call_id,
            started_at=utcnow(),
        )
        session.add(call)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = (
                await _call_by_provider_id(session, provider_name, provider_call_id)
                if provider_call_id
                else None
            )
            if existing is None:
                raise
            return existing
        await session.refresh(call)
        return call


async def call_exists(call_id: int) -> bool:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(Call.id).where(Call.id == call_id))
        ).scalar_one_or_none() is not None


async def set_call_status(
    call_id: int, status: CallStatus, provider_call_id: str | None = None
) -> None:
    """Продвигает статус активного звонка вперёд; терминальные статусы ставит finalize_call.

    Один UPDATE с условием "текущий статус не старше нового" (compare-and-set): параллельные и
    запоздавшие вызовы (события Twilio приходят не по порядку) не откатывают статус и не трогают
    терминальные. Идентификатор звонка у провайдера записывается и тогда, когда статус уже дальше.
    """
    order = _STATUS_ORDER.get(status, -1)
    allowed = [current for current, rank in _STATUS_ORDER.items() if rank <= order]
    values: dict = {"status": status}
    if status == CallStatus.in_progress:
        values["started_at"] = func.coalesce(Call.started_at, utcnow())
    if provider_call_id:
        values["provider_call_id"] = provider_call_id
    async with get_sessionmaker()() as session:
        moved = 0
        if allowed:
            moved = (
                await session.execute(
                    update(Call)
                    .where(Call.id == call_id, Call.status.in_(allowed))
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
            ).rowcount
        if not moved and provider_call_id:
            await session.execute(
                update(Call)
                .where(Call.id == call_id, Call.status.not_in(TERMINAL_STATUSES))
                .values(provider_call_id=provider_call_id)
                .execution_options(synchronize_session=False)
            )
        await session.commit()


async def agent_engine_for_call(call_id: int) -> str:
    """Движок агента, который ведёт звонок: голос агента существует только внутри своего движка."""
    async with get_sessionmaker()() as session:
        call = await session.get(Call, call_id)
        agent = await session.get(Agent, call.agent_id) if call is not None else None
        return (agent.engine if agent is not None else None) or get_settings().voice_engine


async def get_contact_name(call_id: int) -> str | None:
    async with get_sessionmaker()() as session:
        call = await session.get(Call, call_id)
        if call is None or call.contact_id is None:
            return None
        contact = await session.get(Contact, call.contact_id)
        return contact.name if contact else None


async def build_live_config(
    call_id: int, audio_format: dict | None = None, contact_name: str | None = None
) -> LiveSessionConfig:
    async with get_sessionmaker()() as session:
        call = await session.get(Call, call_id)
        if call is None:
            raise ValueError(f"call {call_id} not found")
        agent = await session.get(Agent, call.agent_id)
        if call.contact_id and contact_name is None:
            contact = await session.get(Contact, call.contact_id)
            contact_name = contact.name if contact else None
        from app.live.factory import engine_voices

        engine = agent.engine or get_settings().voice_engine
        voices = engine_voices(engine)
        offer = offers_client.UtpOffer(**call.utp_offer) if call.utp_offer else None
        config = LiveSessionConfig(
            instructions=build_voice_instructions(
                agent,
                call.direction,
                contact_name,
                engine=engine,
                offer=offer,
                with_offer_block=bool(get_settings().offers_url),
            ),
            backend_instructions="" if engine == "yandex" else build_backend_instructions(agent),
            voice=agent.voice if agent.voice in voices else voices[0],
            tools=AGENT_TOOLS,
        )
        if audio_format:
            config.audio_format = audio_format
        return config


async def run_bridge(
    call_id: int,
    live: LiveSession,
    audio_out=None,
    on_hangup=None,
    audio_format: dict | None = None,
    event_listener=None,
    contact_name: str | None = None,
) -> CallBridge:
    """Запускает мост до конца сессии и сохраняет транскрипт, usage и ошибки в звонок."""
    settings = get_settings()
    config = await build_live_config(call_id, audio_format, contact_name)

    async def tool_handler(name: str, args: dict) -> dict:
        return await handle_tool_call(call_id, name, args)

    bridge = CallBridge(
        call_id,
        live,
        tool_handler,
        audio_out=audio_out,
        on_hangup=on_hangup,
        hangup_delay_seconds=0.2 if settings.fake_calls else 2.0,
        event_listener=event_listener,
    )
    ACTIVE_BRIDGES[call_id] = bridge
    try:
        await bridge.run(config)
    finally:
        ACTIVE_BRIDGES.pop(call_id, None)
        await _save_bridge_result(call_id, bridge)
    return bridge


def bridge_outcome(bridge: CallBridge | None, failure: str = "") -> tuple[CallStatus, str]:
    """Терминальный статус звонка по итогу моста.

    Ошибка без единой реплики это failed: разговора не было. Ошибка после реплик это completed:
    разговор был, ошибка остаётся в call.error. failure это сбой самого запуска моста (исключение
    задачи, таймаут), он ещё не записан в звонок и возвращается вторым элементом для finalize_call;
    bridge.error к этому моменту уже сохранён (_save_bridge_result) и повторно не пишется.
    """
    transcript_empty = bridge is None or not bridge.transcript
    had_error = bool(failure) or (bridge is not None and bool(bridge.error))
    status = CallStatus.failed if had_error and transcript_empty else CallStatus.completed
    return status, failure


# Фоновые разборы звонков: ссылки держим, чтобы задачи не собрал GC и чтобы дождаться их при
# остановке приложения (app.main.lifespan)
_POST_CALL_TASKS: set[asyncio.Task] = set()


def schedule_post_call_analysis(call_id: int) -> asyncio.Task | None:
    """Разбор транскрипта отдельной задачей: teardown сокета его не ждёт и не отменяет."""
    if not analysis_available():
        return None
    task = asyncio.create_task(analyze_call(call_id), name=f"post-call-analysis-{call_id}")
    _POST_CALL_TASKS.add(task)
    task.add_done_callback(_POST_CALL_TASKS.discard)
    task.add_done_callback(_log_post_call_failure)
    return task


def _log_post_call_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("%s: разбор упал", task.get_name(), exc_info=task.exception())


async def wait_post_call_tasks(timeout: float) -> None:
    """При остановке приложения: дать разборам дописать результат, остальные отменить."""
    if not _POST_CALL_TASKS:
        return
    _, pending = await asyncio.wait(set(_POST_CALL_TASKS), timeout=timeout)
    for task in pending:
        task.cancel()


async def _save_bridge_result(call_id: int, bridge: CallBridge) -> None:
    async with get_sessionmaker()() as session:
        call = await session.get(Call, call_id)
        if call is None:
            return
        call.transcript = bridge.transcript
        call.live_usage_seconds = bridge.live_usage_seconds
        if bridge.live_session_id:
            call.live_session_id = bridge.live_session_id
        if bridge.error:
            call.error = (call.error + "\n" if call.error else "") + bridge.error
        await session.commit()


async def handle_tool_call(call_id: int, name: str, args: dict) -> dict:
    """Function calling агента. Результат уходит обратно бэкенд-модели."""
    async with get_sessionmaker()() as session:
        call = await session.get(Call, call_id)
        if call is None:
            return {"ok": False, "error": "call not found"}

        if name == "book_meeting":
            when = _parse_datetime(args.get("datetime"))
            if when is None:
                return {
                    "ok": False,
                    "error": "datetime должен быть в ISO 8601, например 2026-09-15T14:00:00+03:00",
                }
            phone = normalize_phone(_as_str(args.get("contact_phone")))
            if phone is None:
                return {
                    "ok": False,
                    "error": "номер для мессенджера не распознан: переспроси его и повтори цифрами",
                }
            messenger = normalize_messenger(args.get("messenger"))
            if messenger is None:
                return {"ok": False, "error": "уточни мессенджер: " + ", ".join(MESSENGERS)}
            meeting = await _get_or_create_meeting(
                session, call.id, when, str(args.get("comment") or "")
            )
            call = await session.get(Call, call_id)  # после rollback в get-or-create
            call.direct_phone, call.messenger = phone, messenger
            if not call.outcome:
                call.outcome = "meeting_booked"
            await session.flush()
            await recompute_goal(session, call)
            await session.commit()
            return {
                "ok": True,
                "meeting_id": meeting.id,
                "scheduled_at": when.isoformat(),
                "contact_phone": phone,
                "messenger": messenger,
            }

        if name == "mark_do_not_call":
            await mark_do_not_call(session, call)
            await session.commit()
            return {"ok": True}

        if name == "report_outcome":
            outcome = str(args.get("outcome") or "").strip()
            call.outcome = outcome if outcome in OUTCOMES else (call.outcome or "other")
            call.outcome_summary = str(args.get("summary") or "")
            phone = normalize_phone(_as_str(args.get("contact_phone")))
            messenger = normalize_messenger(args.get("messenger"))
            if phone and not call.direct_phone:
                call.direct_phone = phone
            if messenger and not call.messenger:
                call.messenger = messenger
            call.callback_after = _as_str(args.get("callback_after")) or call.callback_after
            call.callback_reason = (
                _as_str(args.get("callback_reason")) or call.callback_reason or ""
            )
            if call.outcome == "aggressive":
                await mark_do_not_call(session, call)  # грубость это сигнал больше не звонить
            await recompute_goal(session, call)
            await session.commit()
            return {"ok": True, "outcome": call.outcome}

        if name == "find_offer":
            # во время разговора пауза слышна: короткий таймаут, при срыве идём без УТП
            offer = await offers_client.fetch_offer(
                budget=_as_int(args.get("budget")),
                rooms=_as_int(args.get("rooms")),
                district=_as_str(args.get("district")),
                metro=_as_str(args.get("metro")),
                motive=_as_str(args.get("motive")),
            )
            call.utp_offer = offer.to_dict() if offer else None
            await session.commit()
            if offer is None:
                return {
                    "ok": True,
                    "offer": None,
                    "message": (
                        "подходящего предложения нет: говори общими словами, ничего не выдумывай"
                    ),
                }
            # в базе оффер целиком (коллеге нужно название), модели без названия ЖК
            return {
                "ok": True,
                "offer": offer.spoken_dict(),
                "message": "произносить можно только эти поля; название жилого комплекса "
                "и застройщика не называй, его сообщит коллега",
            }

        if name == "end_call":
            return {"ok": True, "message": "звонок будет завершён"}

        return {"ok": False, "error": f"неизвестный инструмент {name}"}


async def _get_or_create_meeting(
    session: AsyncSession, call_id: int, when: datetime, comment: str
) -> Meeting:
    """Встреча звонка на это время: повторный вызов book_meeting (модель переспросила, повтор
    инструмента после обрыва) возвращает уже записанную. Гонку закрывает UNIQUE(call_id,
    scheduled_at): проигравший INSERT читает встречу победителя."""
    existing = await _meeting_at(session, call_id, when)
    if existing is not None:
        return existing
    meeting = Meeting(call_id=call_id, scheduled_at=when, comment=comment)
    session.add(meeting)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await _meeting_at(session, call_id, when)
        if existing is None:
            raise
        return existing
    return meeting


async def _meeting_at(session: AsyncSession, call_id: int, when: datetime) -> Meeting | None:
    return (
        await session.execute(
            select(Meeting).where(Meeting.call_id == call_id, Meeting.scheduled_at == when)
        )
    ).scalar_one_or_none()


def _as_int(value) -> int | None:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_str(value) -> str | None:
    text = str(value).strip() if value not in (None, "") else ""
    return text or None


async def prefetch_offer(
    call_id: int, contact: Contact | None, motive: str | None = None
) -> dict | None:
    """УТП до набора номера: в разговоре запроса уже не будет, пауза не появится.

    Гео берём из базы номеров. Если его нет, подбор идёт по бюджету и комнатности, а правило
    сервиса офферов про пустой ответ по гео не срабатывает.
    """
    if contact is None or not get_settings().offers_url:
        return None
    offer = await offers_client.fetch_offer(
        budget=contact.budget,
        rooms=contact.rooms,
        district=contact.district,
        metro=contact.metro,
        motive=motive,
        timeout=get_settings().offers_prefetch_timeout_seconds,
    )
    data = offer.to_dict() if offer else None
    async with get_sessionmaker()() as session:
        call = await session.get(Call, call_id)
        if call is not None:
            call.utp_offer = data
            await session.commit()
    return data


def _parse_datetime(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))  # по умолчанию московское время
    return dt


async def finalize_call(
    call_id: int, status: CallStatus, duration: int | None = None, error: str = ""
) -> None:
    """Терминальный статус звонка + статус контакта с ретраями. Идемпотентна.

    Переход делается одним UPDATE с условием "статус ещё не терминальный" (compare-and-set) и
    RETURNING: из параллельных вызовов (teardown сокета и status callback Twilio, два колбэка
    подряд) ровно один переводит звонок и в той же транзакции меняет контакт; остальные видят
    терминальный статус и только уточняют длительность: у провайдера она точнее нашей.
    """
    settings = get_settings()
    now = utcnow()
    values: dict = {"status": status, "ended_at": now}
    if error:
        values["error"] = case((Call.error == "", error), else_=Call.error + "\n" + error)
    if duration is not None:
        values["duration_seconds"] = duration
    async with get_sessionmaker()() as session:
        won = (
            await session.execute(
                update(Call)
                .where(Call.id == call_id, Call.status.not_in(TERMINAL_STATUSES))
                .values(**values)
                .returning(Call.contact_id, Call.do_not_call_requested, Call.started_at)
                .execution_options(synchronize_session=False)
            )
        ).one_or_none()
        if won is None:
            # звонка нет или он уже терминальный: повторный колбэк провайдера уточняет длительность
            if duration:
                await session.execute(
                    update(Call)
                    .where(Call.id == call_id, Call.status.in_(TERMINAL_STATUSES))
                    .values(duration_seconds=duration)
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
            return

        contact_id, do_not_call_requested, started_at = won
        if duration is None and started_at is not None:
            started = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
            await session.execute(
                update(Call)
                .where(Call.id == call_id)
                .values(duration_seconds=max(0, int((now - started).total_seconds())))
                .execution_options(synchronize_session=False)
            )
        if contact_id is not None:
            contact = (
                await session.execute(
                    select(Contact).where(Contact.id == contact_id).with_for_update()
                )
            ).scalar_one_or_none()
            if contact is not None:
                if do_not_call_requested or contact.status == ContactStatus.do_not_call:
                    contact.status = ContactStatus.do_not_call
                elif status == CallStatus.completed:
                    contact.status = ContactStatus.done
                elif contact.attempts <= settings.max_retries:
                    contact.status = ContactStatus.new
                    contact.next_attempt_at = now + timedelta(
                        seconds=settings.retry_interval_seconds
                    )
                else:
                    contact.status = ContactStatus.failed
        await session.commit()


async def hangup_active_calls(campaign_id: int) -> int:
    """Останавливает активные звонки кампании (при остановке кампании)."""
    from app.telephony.base import get_provider

    provider = get_provider()
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(Call).where(
                        Call.campaign_id == campaign_id, Call.status.in_(ACTIVE_CALL_STATUSES)
                    )
                )
            )
            .scalars()
            .all()
        )
        ids = [(c.id, c.provider_call_id) for c in rows]
    for call_id, provider_call_id in ids:
        bridge = ACTIVE_BRIDGES.get(call_id)
        if bridge is not None:
            await bridge.stop()
        if provider_call_id:
            try:
                await provider.hangup(provider_call_id)
            except Exception:
                log.exception("hangup %s failed", provider_call_id)
        await finalize_call(call_id, CallStatus.canceled, error="остановлено оператором")
    return len(ids)
