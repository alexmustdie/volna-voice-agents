import enum
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Gender(str, enum.Enum):
    female = "female"
    male = "male"


# Голоса GPT-Live-1 по документации OpenAI (developers.openai.com, раздел Managing sessions);
# marin используется в официальных примерах.
LIVE_VOICES = [
    "marin",
    "quartz",
    "ripple",
    "vesper",
    "willow",
    "stone",
    "gleam",
    "meridian",
    "bossa",
    "tempo",
    "beacon",
    "delta",
    "cinder",
]


class ContactStatus(str, enum.Enum):
    new = "new"
    calling = "calling"
    done = "done"
    failed = "failed"
    do_not_call = "do_not_call"


class CampaignStatus(str, enum.Enum):
    draft = "draft"
    running = "running"
    paused = "paused"
    stopped = "stopped"
    completed = "completed"


class CallDirection(str, enum.Enum):
    outbound = "outbound"
    inbound = "inbound"


class CallStatus(str, enum.Enum):
    queued = "queued"
    ringing = "ringing"
    in_progress = "in_progress"
    completed = "completed"
    no_answer = "no_answer"
    busy = "busy"
    failed = "failed"
    canceled = "canceled"


ACTIVE_CALL_STATUSES = (CallStatus.queued, CallStatus.ringing, CallStatus.in_progress)

# Условия частичных индексов: один текст для PostgreSQL и SQLite, миграция 0006 использует тот же.
ACTIVE_CALL_WHERE = "status IN ('queued', 'ringing', 'in_progress')"
HAS_PROVIDER_CALL_WHERE = "provider_call_id IS NOT NULL"


def _partial_unique(name: str, *columns: str, where: str) -> Index:
    return Index(
        name,
        *columns,
        unique=True,
        postgresql_where=text(where),
        sqlite_where=text(where),
    )


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    gender: Mapped[Gender] = mapped_column(Enum(Gender, name="gender"), default=Gender.female)
    age: Mapped[int] = mapped_column(Integer, default=30)
    voice: Mapped[str] = mapped_column(String(50), default="marin")
    # Голосовой движок агента. Голос живёт внутри движка (у GPT-Live свои голоса, у Яндекса свои),
    # поэтому движок определяется выбранным голосом: app.live.factory.engine_for_voice.
    engine: Mapped[str] = mapped_column(String(20), default="openai")
    niche: Mapped[str] = mapped_column(String(200), default="")
    company_description: Mapped[str] = mapped_column(Text, default="")
    call_goal: Mapped[str] = mapped_column(Text, default="")
    required_questions: Mapped[list] = mapped_column(JSON, default=list)
    extra_instructions: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    campaigns: Mapped[list["Campaign"]] = relationship(back_populates="agent")


class ContactList(Base):
    __tablename__ = "contact_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # контакты удаляет каскад БД (ondelete=CASCADE), ORM их не подгружает ради удаления
    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="contact_list", cascade="all, delete-orphan", passive_deletes=True
    )


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("list_id", "phone", name="uq_contact_list_phone"),
        # выборка кандидатов воркером: list_id = ? AND status = ? AND next_attempt_at <= ?
        Index("ix_contacts_list_status_next", "list_id", "status", "next_attempt_at"),
        CheckConstraint("attempts >= 0", name="ck_contacts_attempts"),
        CheckConstraint("rooms IS NULL OR (rooms >= 0 AND rooms <= 7)", name="ck_contacts_rooms"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(
        ForeignKey("contact_lists.id", ondelete="CASCADE"), index=True
    )
    phone: Mapped[str] = mapped_column(String(20), index=True)  # поиск "не звонить" по номеру
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # то, что известно о лиде до звонка: из CSV. Нужно для подбора УТП через внешний сервис офферов.
    district: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metro: Mapped[str | None] = mapped_column(String(100), nullable=True)
    budget: Mapped[int | None] = mapped_column(Integer, nullable=True)  # рубли
    rooms: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0 студия, 1..7 комнат
    status: Mapped[ContactStatus] = mapped_column(
        Enum(ContactStatus, name="contact_status"), default=ContactStatus.new, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    contact_list: Mapped[ContactList] = relationship(back_populates="contacts")


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(
            "start_hour >= 0 AND end_hour <= 24 AND start_hour < end_hour",
            name="ck_campaigns_hours",
        ),
        CheckConstraint(
            "max_concurrent >= 1 AND max_concurrent <= 20", name="ck_campaigns_max_concurrent"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), index=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("contact_lists.id"), index=True)
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, name="campaign_status"), default=CampaignStatus.draft
    )
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    start_hour: Mapped[int] = mapped_column(Integer, default=10)
    end_hour: Mapped[int] = mapped_column(Integer, default=19)
    days_of_week: Mapped[list] = mapped_column(
        JSON, default=lambda: [0, 1, 2, 3, 4]
    )  # 0 = понедельник
    max_concurrent: Mapped[int] = mapped_column(Integer, default=3)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    agent: Mapped[Agent] = relationship(back_populates="campaigns")
    contact_list: Mapped[ContactList] = relationship()
    calls: Mapped[list["Call"]] = relationship(back_populates="campaign")


class InboundNumber(Base):
    __tablename__ = "inbound_numbers"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    agent: Mapped[Agent] = relationship()


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        # один звонок провайдера = одна запись: повтор вебхука входящего не создаёт дубль
        _partial_unique(
            "uq_calls_provider_call_id",
            "provider",
            "provider_call_id",
            where=HAS_PROVIDER_CALL_WHERE,
        ),
        # один активный звонок на контакт: второй воркер не наберёт того же абонента
        _partial_unique("uq_calls_active_contact", "contact_id", where=ACTIVE_CALL_WHERE),
        Index("ix_calls_campaign_status", "campaign_id", "status"),  # слоты кампании
        Index("ix_calls_from_phone_created", "from_phone", "created_at"),  # квота и пауза номера
        Index("ix_calls_status_created", "status", "created_at"),  # зависшие звонки
        CheckConstraint("duration_seconds >= 0", name="ck_calls_duration"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id"), nullable=True, index=True
    )
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id"), nullable=True, index=True
    )
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), index=True)
    direction: Mapped[CallDirection] = mapped_column(Enum(CallDirection, name="call_direction"))
    phone: Mapped[str] = mapped_column(String(20))
    status: Mapped[CallStatus] = mapped_column(
        Enum(CallStatus, name="call_status"), default=CallStatus.queued, index=True
    )
    provider: Mapped[str] = mapped_column(String(30), default="fake")
    # С какого своего номера звонили. Нужно для суточных лимитов площадки и ротации номеров.
    from_phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    provider_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    live_session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    live_usage_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    transcript: Mapped[list] = mapped_column(JSON, default=list)  # [{role, text, start_ms, end_ms}]
    goal_achieved: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome_summary: Mapped[str] = mapped_column(Text, default="")
    do_not_call_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # УТП из внешнего сервиса офферов, запрошенное до набора номера: шесть полей контракта UtpOffer
    utp_offer: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Прямой контакт, полученный в разговоре. Номер, по которому звонили, может быть временным
    # (подменные номера площадок объявлений), поэтому для связи после звонка нужен отдельный
    # номер и мессенджер.
    direct_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    messenger: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Исход разговора по классификатору из report_outcome (см. OUTCOMES в app/prompts.py)
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    callback_after: Mapped[str | None] = mapped_column(String(100), nullable=True)
    callback_reason: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")

    campaign: Mapped[Campaign | None] = relationship(back_populates="calls")
    contact: Mapped[Contact | None] = relationship()
    agent: Mapped[Agent] = relationship()
    meetings: Mapped[list["Meeting"]] = relationship(
        back_populates="call", cascade="all, delete-orphan"
    )


class Meeting(Base):
    __tablename__ = "meetings"
    # повторный вызов book_meeting с тем же временем не создаёт вторую встречу
    __table_args__ = (
        UniqueConstraint("call_id", "scheduled_at", name="uq_meetings_call_scheduled"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    call: Mapped[Call] = relationship(back_populates="meetings")


class CallEvent(Base):
    """Inbox вебхуков провайдера: каждое событие записывается один раз по ключу провайдера.

    Twilio шлёт события отдельными запросами, повторяет их и не гарантирует порядок; дубль
    (тот же provider_event_id) отбрасывается на INSERT ... ON CONFLICT DO NOTHING, до обработки.
    """

    __tablename__ = "call_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_call_events_provider_event"),
        Index("ix_call_events_call_received", "call_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(20))
    provider_event_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30))  # статус как прислал провайдер
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NumberDailyUsage(Base):
    """Счётчик звонков с нашего номера за сутки рабочей таймзоны.

    Резервируется атомарно (quota.reserve_daily_slot): INSERT ... ON CONFLICT DO UPDATE с условием
    used < лимит, поэтому параллельные воркеры не превысят суточный лимит площадки.
    """

    __tablename__ = "number_daily_usage"

    from_phone: Mapped[str] = mapped_column(String(20), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    used: Mapped[int] = mapped_column(Integer, default=0)
