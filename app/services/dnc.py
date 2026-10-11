"""Пометка "не звонить".

Вынесена из calls.py, потому что нужна и инструментам агента во время разговора, и разбору
транскрипта после него (analysis.py), а calls.py запускает этот разбор: общий модуль разрывает
цикл импортов.
"""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Call, Contact, ContactStatus


async def mark_do_not_call(session: AsyncSession, call: Call) -> None:
    """Все контакты с этим номером переводятся в do_not_call; коммит на вызывающей стороне."""
    call.do_not_call_requested = True
    await session.execute(
        update(Contact).where(Contact.phone == call.phone).values(status=ContactStatus.do_not_call)
    )
