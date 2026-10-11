"""Воркер обзвона: берёт контакты из очереди кампаний, инициирует звонки через провайдера.

Запускается как asyncio-задача в lifespan приложения (WORKER_ENABLED=true) или отдельным
процессом: `python -m app.worker.dialer`.

Контакты забираются (claim) в одной транзакции под блокировкой строки кампании (SELECT ... FOR
UPDATE): подсчёт активных звонков, выбор кандидатов (FOR UPDATE SKIP LOCKED), резерв суточной квоты
номера, перевод контакта в calling и создание звонка коммитятся вместе. Поэтому второй экземпляр
воркера на той же базе не превысит max_concurrent и не наберёт тот же контакт. Сетевой вызов
провайдера идёт уже после коммита: держать блокировку поверх сети нельзя. На SQLite (только тесты)
блокировки строк диалект игнорирует, и claim защищён лишь тем, что писатель там один.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import (
    ACTIVE_CALL_STATUSES,
    Call,
    CallStatus,
    Campaign,
    CampaignStatus,
    Contact,
    ContactStatus,
    utcnow,
)
from app.services import calls as call_service
from app.services.campaigns import campaign_in_window
from app.services.quota import pause_elapsed, reserve_daily_slot
from app.telephony.base import TelephonyProvider, get_provider

log = logging.getLogger(__name__)

Claimed = list[tuple[Contact, Call]]


async def tick() -> int:
    """Один проход воркера. Возвращает число инициированных звонков."""
    provider = get_provider()
    settings = get_settings()
    caller = provider.caller_number
    started = 0
    await _expire_stuck_calls()

    for campaign_id in await _running_campaigns():
        async with get_sessionmaker()() as session:
            # статус перечитывается под блокировкой: кампанию могли остановить после выборки
            campaign = (
                await session.execute(
                    select(Campaign)
                    .where(Campaign.id == campaign_id, Campaign.status == CampaignStatus.running)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if campaign is None:
                continue
            now = utcnow()
            active = (
                await session.execute(
                    select(func.count(Call.id)).where(
                        Call.campaign_id == campaign.id, Call.status.in_(ACTIVE_CALL_STATUSES)
                    )
                )
            ).scalar_one()
            pending = (
                await session.execute(
                    select(func.count(Contact.id)).where(
                        Contact.list_id == campaign.list_id,
                        Contact.status.in_([ContactStatus.new, ContactStatus.calling]),
                    )
                )
            ).scalar_one()
            if active == 0 and pending == 0:
                campaign.status = CampaignStatus.completed
                await session.commit()
                log.info("кампания %s завершена", campaign.id)
                continue
            if not campaign_in_window(campaign, now):
                continue
            slots = campaign.max_concurrent - active
            if slots <= 0:
                continue
            claimed, quota_exhausted = await _claim_contacts(
                session, campaign, slots, provider, now
            )
            await session.commit()

        for contact, call in claimed:
            if await _launch_call(provider, contact, call):
                started += 1
        if quota_exhausted:
            log.info("суточный лимит звонков с номера %s исчерпан", caller)
            return started
        if claimed and settings.min_seconds_between_calls > 0:
            return started  # следующий звонок только после паузы, её проверит следующий тик
    return started


async def _running_campaigns() -> list[int]:
    async with get_sessionmaker()() as session:
        return list(
            (
                await session.execute(
                    select(Campaign.id)
                    .where(Campaign.status == CampaignStatus.running)
                    .order_by(Campaign.id)
                )
            ).scalars()
        )


async def _claim_contacts(
    session: AsyncSession, campaign: Campaign, slots: int, provider: TelephonyProvider, now
) -> tuple[Claimed, bool]:
    """Забирает до slots контактов кампании: new -> calling, attempts + 1, звонок в queued.

    Всё в транзакции вызывающей стороны (коммит там же). Кандидаты берутся с FOR UPDATE SKIP LOCKED,
    чтобы параллельный claim по той же базе номеров не ждал и не взял те же строки. Перед каждым
    контактом в той же транзакции проверяется пауза между звонками и резервируется суточный слот
    номера. Возвращает список (контакт, звонок) и флаг "квота номера исчерпана".
    """
    settings = get_settings()
    caller = provider.caller_number
    candidates = (
        (
            await session.execute(
                select(Contact)
                .where(
                    Contact.list_id == campaign.list_id,
                    Contact.status == ContactStatus.new,
                    or_(Contact.next_attempt_at.is_(None), Contact.next_attempt_at <= now),
                )
                .order_by(Contact.next_attempt_at.nulls_first(), Contact.id)
                .limit(slots)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    claimed: Claimed = []
    for contact in candidates:
        blocked = (
            await session.execute(
                select(func.count(Contact.id)).where(
                    Contact.phone == contact.phone,
                    Contact.status == ContactStatus.do_not_call,
                )
            )
        ).scalar_one()
        if blocked:
            contact.status = ContactStatus.do_not_call
            continue
        if settings.min_seconds_between_calls > 0 and not await pause_elapsed(session, caller, now):
            break
        if not await reserve_daily_slot(session, caller, now):
            return claimed, True
        contact.status = ContactStatus.calling
        contact.attempts += 1
        contact.next_attempt_at = None
        call = await call_service.create_outbound_call(
            session, campaign, contact, provider.name, from_phone=caller
        )
        claimed.append((contact, call))
    return claimed, False


async def _launch_call(provider: TelephonyProvider, contact: Contact, call: Call) -> bool:
    """Запускает уже созданный звонок у провайдера. False: провайдер не смог, звонок failed,
    контакт уходит на повтор через finalize_call."""
    # УТП запрашиваем здесь, до набора номера: в разговоре пауза была бы слышна
    await call_service.prefetch_offer(call.id, contact)
    try:
        provider_call_id = await provider.start_outbound_call(call.id, contact.phone)
    except Exception as exc:
        log.exception("не удалось начать звонок %s", call.id)
        await call_service.finalize_call(call.id, CallStatus.failed, error=f"start: {exc}")
        return False
    await call_service.set_call_status(
        call.id, CallStatus.queued, provider_call_id=provider_call_id
    )
    return True


async def _expire_stuck_calls() -> None:
    """Звонки без ответа провайдера дольше таймаута помечаем терминальными."""
    settings = get_settings()
    now = utcnow()
    async with get_sessionmaker()() as session:
        ringing_deadline = now - timedelta(seconds=settings.ringing_timeout_seconds + 60)
        talk_deadline = now - timedelta(seconds=settings.call_timeout_seconds)
        stuck = (
            await session.execute(
                select(Call.id, Call.status, Call.provider_call_id).where(
                    or_(
                        and_(
                            Call.status.in_([CallStatus.queued, CallStatus.ringing]),
                            Call.created_at < ringing_deadline,
                        ),
                        and_(
                            Call.status == CallStatus.in_progress, Call.created_at < talk_deadline
                        ),
                    )
                )
            )
        ).all()
    if not stuck:
        return
    provider = get_provider()
    for call_id, status, provider_call_id in stuck:
        bridge = call_service.ACTIVE_BRIDGES.get(call_id)
        if bridge is not None:
            await bridge.stop()
        if provider_call_id:
            try:
                await provider.hangup(provider_call_id)
            except Exception:
                log.warning("hangup зависшего звонка %s не удался", provider_call_id, exc_info=True)
        if status == CallStatus.in_progress:
            await call_service.finalize_call(
                call_id, CallStatus.completed, error="таймаут разговора"
            )
        else:
            await call_service.finalize_call(
                call_id, CallStatus.no_answer, error="таймаут ожидания ответа"
            )


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    settings = get_settings()
    log.info("воркер обзвона запущен (provider=%s)", get_provider().name)
    while stop_event is None or not stop_event.is_set():
        try:
            await tick()
        except Exception:
            log.exception("ошибка воркера")
        try:
            if stop_event is None:
                await asyncio.sleep(settings.worker_poll_seconds)
            else:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_poll_seconds)
        except TimeoutError:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())
