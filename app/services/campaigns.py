from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import Campaign

MAX_CONCURRENT_LIMIT = 20


def campaign_in_window(campaign: Campaign, now: datetime | None = None) -> bool:
    """Попадает ли текущий момент в окно звонков кампании (часы и дни недели в её таймзоне)."""
    now = now or datetime.now(UTC)
    try:
        tz = ZoneInfo(campaign.timezone or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)
    days = campaign.days_of_week or []
    if local.weekday() not in days:
        return False
    return campaign.start_hour <= local.hour < campaign.end_hour


def clamp_concurrency(value: int) -> int:
    return max(1, min(MAX_CONCURRENT_LIMIT, int(value)))
