from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ACTIVE_CALL_STATUSES, Call, CallStatus, Contact, ContactStatus, Meeting

UNREACHED_CALL_STATUSES = (
    CallStatus.no_answer,
    CallStatus.busy,
    CallStatus.failed,
    CallStatus.canceled,
)


@dataclass
class CallStats:
    total: int = 0
    answered: int = 0  # completed
    goal_achieved: int = 0
    meetings: int = 0
    avg_duration_seconds: float = 0.0
    total_minutes: float = 0.0
    live_minutes: float = 0.0
    active: int = 0
    unreached: int = 0

    @property
    def conversion_pct(self) -> float:
        return round(100 * self.goal_achieved / self.answered, 1) if self.answered else 0.0


async def call_stats(session: AsyncSession, campaign_id: int | None = None) -> CallStats:
    """Сводка по звонкам одним запросом с агрегатами: объекты Call в память не грузятся."""
    completed = Call.status == CallStatus.completed
    q = select(
        func.count(Call.id),
        func.count(Call.id).filter(completed),
        func.count(Call.id).filter(completed, Call.goal_achieved.is_(True)),
        func.count(Call.id).filter(Call.status.in_(ACTIVE_CALL_STATUSES)),
        func.count(Call.id).filter(Call.status.in_(UNREACHED_CALL_STATUSES)),
        func.coalesce(func.sum(Call.duration_seconds).filter(completed), 0),
        func.coalesce(func.sum(Call.duration_seconds), 0),
        func.coalesce(func.sum(Call.live_usage_seconds), 0.0),
    )
    mq = select(func.count(Meeting.id)).join(Call, Meeting.call_id == Call.id)
    if campaign_id is not None:
        q = q.where(Call.campaign_id == campaign_id)
        mq = mq.where(Call.campaign_id == campaign_id)
    total, answered, goal, active, unreached, completed_sec, total_sec, live_sec = (
        await session.execute(q)
    ).one()
    return CallStats(
        total=total,
        answered=answered,
        goal_achieved=goal,
        meetings=(await session.execute(mq)).scalar_one(),
        avg_duration_seconds=round(completed_sec / answered, 1) if answered else 0.0,
        total_minutes=round(total_sec / 60, 1),
        live_minutes=round(live_sec / 60, 1),
        active=active,
        unreached=unreached,
    )


def _empty_counts() -> dict[str, int]:
    return {s.value: 0 for s in ContactStatus}


def _with_totals(counts: dict[str, int]) -> dict[str, int]:
    counts["total"] = sum(counts[s.value] for s in ContactStatus)
    finished = counts["done"] + counts["failed"] + counts["do_not_call"]
    counts["progress_pct"] = round(100 * finished / counts["total"], 1) if counts["total"] else 0.0
    return counts


async def contact_status_counts_by_list(
    session: AsyncSession, list_ids: list[int]
) -> dict[int, dict[str, int]]:
    """Счётчики статусов для нескольких баз одним запросом (списки баз и кампаний)."""
    result = {list_id: _empty_counts() for list_id in list_ids}
    if not list_ids:
        return result
    rows = (
        await session.execute(
            select(Contact.list_id, Contact.status, func.count(Contact.id))
            .where(Contact.list_id.in_(list_ids))
            .group_by(Contact.list_id, Contact.status)
        )
    ).all()
    for list_id, status, n in rows:
        result[list_id][status.value] = n
    return {list_id: _with_totals(counts) for list_id, counts in result.items()}


async def contact_status_counts(session: AsyncSession, list_id: int) -> dict[str, int]:
    return (await contact_status_counts_by_list(session, [list_id]))[list_id]
