"""HTML-панель управления: агенты, базы номеров, кампании, звонки, дашборд."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import get_session
from app.live.factory import engine_for_voice, engine_voices
from app.models import (
    Agent,
    Call,
    CallDirection,
    Campaign,
    CampaignStatus,
    Contact,
    ContactList,
    Gender,
    InboundNumber,
    Meeting,
)
from app.phone import normalize_phone
from app.prompts import build_backend_instructions, build_voice_instructions
from app.services import calls as call_service
from app.services.campaigns import clamp_concurrency
from app.services.contacts import import_contacts
from app.services.stats import call_stats, contact_status_counts, contact_status_counts_by_list
from app.web.templating import templates

router = APIRouter()
DB = Annotated[AsyncSession, Depends(get_session)]


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------- дашборд ----------
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: DB):
    stats = await call_stats(db)
    campaigns = (
        (
            await db.execute(
                select(Campaign).options(selectinload(Campaign.agent)).order_by(Campaign.id.desc())
            )
        )
        .scalars()
        .all()
    )
    recent_calls = (
        (
            await db.execute(
                select(Call)
                .options(selectinload(Call.agent), selectinload(Call.contact))
                .order_by(Call.id.desc())
                .limit(15)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"stats": stats, "campaigns": campaigns, "recent_calls": recent_calls},
    )


# ---------- агенты ----------
@router.get("/agents", response_class=HTMLResponse)
async def agents_list(request: Request, db: DB):
    agents = (await db.execute(select(Agent).order_by(Agent.id))).scalars().all()
    return templates.TemplateResponse(request, "agents.html", {"agents": agents})


@router.get("/agents/new", response_class=HTMLResponse)
async def agent_new(request: Request):
    return templates.TemplateResponse(request, "agent_form.html", {"agent": None})


@router.get("/agents/{agent_id}", response_class=HTMLResponse)
async def agent_edit(request: Request, agent_id: int, db: DB):
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "agent_form.html", {"agent": agent})


@router.get("/agents/{agent_id}/prompt", response_class=PlainTextResponse)
async def agent_prompt(agent_id: int, db: DB):
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404)
    # предпросмотр собирается так же, как build_live_config: под движок агента
    engine = agent.engine or get_settings().voice_engine
    text = (
        f"Движок: {engine}, голос: {agent.voice}\n\n"
        "=== Голосовой слой (исходящий) ===\n"
        + build_voice_instructions(agent, CallDirection.outbound, "Иван", engine=engine)
        + "\n\n=== Голосовой слой (входящий) ===\n"
        + build_voice_instructions(agent, CallDirection.inbound, engine=engine)
    )
    if engine != "yandex":  # у Яндекса функции вызывает сама голосовая модель, бэкенда нет
        text += "\n\n=== Бэкенд-модель ===\n" + build_backend_instructions(agent)
    return text


def _apply_agent_form(
    agent: Agent,
    name: str,
    gender: str,
    age: int,
    voice: str,
    niche: str,
    company_description: str,
    call_goal: str,
    required_questions: str,
    extra_instructions: str,
) -> None:
    agent.name = name.strip()
    agent.gender = Gender(gender)
    agent.age = max(18, min(90, age))
    engine = engine_for_voice(voice)
    if engine is None:  # неизвестный голос: голос по умолчанию движка по умолчанию
        engine = get_settings().voice_engine
        voice = engine_voices(engine)[0]
    agent.voice, agent.engine = voice, engine
    agent.niche = niche.strip()
    agent.company_description = company_description.strip()
    agent.call_goal = call_goal.strip()
    agent.required_questions = [q.strip() for q in required_questions.splitlines() if q.strip()]
    agent.extra_instructions = extra_instructions.strip()


@router.post("/agents")
async def agent_create(
    db: DB,
    name: str = Form(...),
    gender: str = Form("female"),
    age: int = Form(30),
    voice: str = Form("marin"),
    niche: str = Form(""),
    company_description: str = Form(""),
    call_goal: str = Form(""),
    required_questions: str = Form(""),
    extra_instructions: str = Form(""),
):
    agent = Agent()
    _apply_agent_form(
        agent,
        name,
        gender,
        age,
        voice,
        niche,
        company_description,
        call_goal,
        required_questions,
        extra_instructions,
    )
    db.add(agent)
    await db.commit()
    return redirect("/agents")


@router.post("/agents/{agent_id}")
async def agent_update(
    agent_id: int,
    db: DB,
    name: str = Form(...),
    gender: str = Form("female"),
    age: int = Form(30),
    voice: str = Form("marin"),
    niche: str = Form(""),
    company_description: str = Form(""),
    call_goal: str = Form(""),
    required_questions: str = Form(""),
    extra_instructions: str = Form(""),
):
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404)
    _apply_agent_form(
        agent,
        name,
        gender,
        age,
        voice,
        niche,
        company_description,
        call_goal,
        required_questions,
        extra_instructions,
    )
    await db.commit()
    return redirect("/agents")


@router.post("/agents/{agent_id}/delete")
async def agent_delete(agent_id: int, db: DB):
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404)
    # на агента ссылаются кампании, звонки и входящие номера: удаление сломало бы внешние ключи
    references = (
        (Campaign.id, Campaign.agent_id, "Агент используется в кампании, удалить нельзя"),
        (Call.id, Call.agent_id, "У агента есть звонки, удалить нельзя"),
        (
            InboundNumber.id,
            InboundNumber.agent_id,
            "К агенту привязан входящий номер, удалить нельзя",
        ),
    )
    for pk, fk, message in references:
        if (await db.execute(select(pk).where(fk == agent_id).limit(1))).first():
            raise HTTPException(400, message)
    await db.delete(agent)
    await db.commit()
    return redirect("/agents")


# ---------- базы номеров ----------
@router.get("/lists", response_class=HTMLResponse)
async def lists_index(request: Request, db: DB):
    lists = (await db.execute(select(ContactList).order_by(ContactList.id.desc()))).scalars().all()
    counts = await contact_status_counts_by_list(db, [cl.id for cl in lists])
    return templates.TemplateResponse(request, "lists.html", {"lists": lists, "counts": counts})


@router.post("/lists")
async def list_create(
    request: Request, db: DB, name: str = Form(...), file: UploadFile | None = None
):
    contact_list = ContactList(name=name.strip() or "Без названия")
    db.add(contact_list)
    await db.commit()
    if file is not None and file.filename:
        result = await import_contacts(db, contact_list, await file.read())
        request.session["flash"] = _import_message(result)
    return redirect(f"/lists/{contact_list.id}")


@router.get("/lists/{list_id}", response_class=HTMLResponse)
async def list_detail(request: Request, list_id: int, db: DB):
    contact_list = await db.get(ContactList, list_id)
    if contact_list is None:
        raise HTTPException(404)
    contacts = (
        (await db.execute(select(Contact).where(Contact.list_id == list_id).order_by(Contact.id)))
        .scalars()
        .all()
    )
    counts = await contact_status_counts(db, list_id)
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        request,
        "list_detail.html",
        {"list": contact_list, "contacts": contacts, "counts": counts, "flash": flash},
    )


@router.post("/lists/{list_id}/upload")
async def list_upload(request: Request, list_id: int, db: DB, file: UploadFile):
    contact_list = await db.get(ContactList, list_id)
    if contact_list is None:
        raise HTTPException(404)
    result = await import_contacts(db, contact_list, await file.read())
    request.session["flash"] = _import_message(result)
    return redirect(f"/lists/{list_id}")


@router.post("/lists/{list_id}/delete")
async def list_delete(list_id: int, db: DB):
    contact_list = await db.get(ContactList, list_id)
    if contact_list is None:
        raise HTTPException(404)
    used = (
        await db.execute(select(Campaign.id).where(Campaign.list_id == list_id).limit(1))
    ).first()
    if used:
        raise HTTPException(400, "База используется в кампании, удалить нельзя")
    await db.delete(contact_list)
    await db.commit()
    return redirect("/lists")


def _import_message(result) -> str:
    msg = (
        f"Добавлено: {result.added}, дубликатов: {result.duplicates}, "
        f"невалидных: {len(result.invalid)}"
    )
    if result.added:
        msg += f", с профилем лида (район, метро, бюджет, комнаты): {result.with_profile}"
    if result.invalid:
        msg += (
            " ("
            + ", ".join(result.invalid[:10])
            + (", ..." if len(result.invalid) > 10 else "")
            + ")"
        )
    return msg


# ---------- кампании ----------
@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_index(request: Request, db: DB):
    campaigns = (
        (
            await db.execute(
                select(Campaign)
                .options(selectinload(Campaign.agent), selectinload(Campaign.contact_list))
                .order_by(Campaign.id.desc())
            )
        )
        .scalars()
        .all()
    )
    by_list = await contact_status_counts_by_list(db, list({c.list_id for c in campaigns}))
    progress = {c.id: by_list[c.list_id] for c in campaigns}
    return templates.TemplateResponse(
        request, "campaigns.html", {"campaigns": campaigns, "progress": progress}
    )


@router.get("/campaigns/new", response_class=HTMLResponse)
async def campaign_new(request: Request, db: DB):
    agents = (await db.execute(select(Agent).order_by(Agent.name))).scalars().all()
    lists = (await db.execute(select(ContactList).order_by(ContactList.name))).scalars().all()
    return templates.TemplateResponse(
        request, "campaign_form.html", {"agents": agents, "lists": lists}
    )


@router.post("/campaigns")
async def campaign_create(
    db: DB,
    name: str = Form(...),
    agent_id: int = Form(...),
    list_id: int = Form(...),
    timezone: str = Form("Europe/Moscow"),
    start_hour: int = Form(10),
    end_hour: int = Form(19),
    days_of_week: list[int] = Form([]),  # noqa: B008  # так FastAPI объявляет поля формы
    max_concurrent: int = Form(3),
):
    if not (0 <= start_hour < end_hour <= 24):
        raise HTTPException(400, "Окно часов задано неверно")
    if await db.get(Agent, agent_id) is None:
        raise HTTPException(400, "Агент не найден")
    if await db.get(ContactList, list_id) is None:
        raise HTTPException(400, "База номеров не найдена")
    campaign = Campaign(
        name=name.strip(),
        agent_id=agent_id,
        list_id=list_id,
        timezone=timezone.strip() or "Europe/Moscow",
        start_hour=start_hour,
        end_hour=end_hour,
        days_of_week=sorted({int(d) for d in days_of_week if 0 <= int(d) <= 6}),
        max_concurrent=clamp_concurrency(max_concurrent),
    )
    db.add(campaign)
    await db.commit()
    return redirect(f"/campaigns/{campaign.id}")


@router.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(request: Request, campaign_id: int, db: DB):
    campaign = (
        await db.execute(
            select(Campaign)
            .options(selectinload(Campaign.agent), selectinload(Campaign.contact_list))
            .where(Campaign.id == campaign_id)
        )
    ).scalar_one_or_none()
    if campaign is None:
        raise HTTPException(404)
    stats = await call_stats(db, campaign_id)
    counts = await contact_status_counts(db, campaign.list_id)
    calls = (
        (
            await db.execute(
                select(Call)
                .options(selectinload(Call.contact))
                .where(Call.campaign_id == campaign_id)
                .order_by(Call.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    meetings = (
        (
            await db.execute(
                select(Meeting)
                .join(Call)
                .options(selectinload(Meeting.call))
                .where(Call.campaign_id == campaign_id)
                .order_by(Meeting.scheduled_at)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "campaign_detail.html",
        {
            "campaign": campaign,
            "stats": stats,
            "counts": counts,
            "calls": calls,
            "meetings": meetings,
        },
    )


@router.post("/campaigns/{campaign_id}/{action}")
async def campaign_action(campaign_id: int, action: str, db: DB):
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404)
    if action == "start":
        if campaign.status in (
            CampaignStatus.draft,
            CampaignStatus.paused,
            CampaignStatus.completed,
        ):
            campaign.status = CampaignStatus.running
    elif action == "pause":
        if campaign.status == CampaignStatus.running:
            campaign.status = CampaignStatus.paused
    elif action == "stop":
        campaign.status = CampaignStatus.stopped
        await db.commit()
        await call_service.hangup_active_calls(campaign_id)
        return redirect(f"/campaigns/{campaign_id}")
    else:
        raise HTTPException(404)
    await db.commit()
    return redirect(f"/campaigns/{campaign_id}")


# ---------- звонки ----------
@router.get("/calls/{call_id}", response_class=HTMLResponse)
async def call_detail(request: Request, call_id: int, db: DB):
    call = (
        await db.execute(
            select(Call)
            .options(
                selectinload(Call.agent),
                selectinload(Call.contact),
                selectinload(Call.meetings),
                selectinload(Call.campaign),
            )
            .where(Call.id == call_id)
        )
    ).scalar_one_or_none()
    if call is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "call_detail.html", {"call": call})


# ---------- входящие номера ----------
@router.get("/inbound", response_class=HTMLResponse)
async def inbound_index(request: Request, db: DB):
    numbers = (
        (
            await db.execute(
                select(InboundNumber)
                .options(selectinload(InboundNumber.agent))
                .order_by(InboundNumber.id)
            )
        )
        .scalars()
        .all()
    )
    agents = (await db.execute(select(Agent).order_by(Agent.name))).scalars().all()
    inbound_calls = (
        (
            await db.execute(
                select(Call)
                .options(selectinload(Call.agent), selectinload(Call.contact))
                .where(Call.direction == CallDirection.inbound)
                .order_by(Call.id.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "inbound.html",
        {"numbers": numbers, "agents": agents, "inbound_calls": inbound_calls},
    )


@router.post("/inbound")
async def inbound_create(db: DB, phone: str = Form(...), agent_id: int = Form(...)):
    normalized = normalize_phone(phone)
    if normalized is None:
        raise HTTPException(400, "Номер невалиден")
    existing = (
        await db.execute(select(InboundNumber).where(InboundNumber.phone == normalized))
    ).scalar_one_or_none()
    if existing:
        existing.agent_id = agent_id
    else:
        db.add(InboundNumber(phone=normalized, agent_id=agent_id))
    await db.commit()
    return redirect("/inbound")


@router.post("/inbound/{number_id}/delete")
async def inbound_delete(number_id: int, db: DB):
    number = await db.get(InboundNumber, number_id)
    if number:
        await db.delete(number)
        await db.commit()
    return redirect("/inbound")
