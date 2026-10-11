"""Результаты разговора, общие для инструментов агента и разбора транскрипта после звонка.

Пишутся дважды: инструментами во время разговора (calls.handle_tool_call) и разбором после него
(analysis.apply_analysis). Живут отдельно от calls.py, чтобы analysis.py не зависел от calls.py.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Call, Meeting

_MESSENGER_ALIASES = {
    "telegram": "telegram",
    "телеграм": "telegram",
    "телеграмм": "telegram",
    "tg": "telegram",
    "тг": "telegram",
    "max": "max",
    "макс": "max",
    "мах": "max",
    "whatsapp": "whatsapp",
    "ватсап": "whatsapp",
    "вотсап": "whatsapp",
    "вацап": "whatsapp",
}


def normalize_messenger(value) -> str | None:
    text = str(value or "").strip().lower()
    return _MESSENGER_ALIASES.get(text)


async def recompute_goal(session: AsyncSession, call: Call) -> None:
    """Цель звонка по фактам, а не по слову модели: назначена встреча и есть куда о ней написать.

    Модель может сказать "договорились", не вызвав функцию, или отчитаться об успехе без номера.
    Поэтому goal_achieved выставляется только здесь, из того, что реально записано в базу.
    """
    has_meeting = (
        await session.execute(select(Meeting.id).where(Meeting.call_id == call.id).limit(1))
    ).first() is not None
    call.goal_achieved = bool(has_meeting and call.direct_phone and call.messenger)
