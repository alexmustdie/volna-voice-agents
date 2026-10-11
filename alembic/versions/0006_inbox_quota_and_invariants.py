"""inbox for provider webhooks, daily number usage, schema invariants and indexes

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-11

"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# те же условия, что в app/models.py (ACTIVE_CALL_WHERE, HAS_PROVIDER_CALL_WHERE)
ACTIVE_CALL_WHERE = "status IN ('queued', 'ringing', 'in_progress')"
HAS_PROVIDER_CALL_WHERE = "provider_call_id IS NOT NULL"

PLAIN_INDEXES = (
    ("ix_contacts_phone", "contacts", ["phone"]),
    ("ix_contacts_list_status_next", "contacts", ["list_id", "status", "next_attempt_at"]),
    ("ix_calls_contact_id", "calls", ["contact_id"]),
    ("ix_calls_agent_id", "calls", ["agent_id"]),
    ("ix_calls_campaign_status", "calls", ["campaign_id", "status"]),
    ("ix_calls_from_phone_created", "calls", ["from_phone", "created_at"]),
    ("ix_calls_status_created", "calls", ["status", "created_at"]),
    ("ix_campaigns_agent_id", "campaigns", ["agent_id"]),
    ("ix_campaigns_list_id", "campaigns", ["list_id"]),
    ("ix_inbound_numbers_agent_id", "inbound_numbers", ["agent_id"]),
)


def upgrade() -> None:
    op.create_table(
        "call_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "call_id", sa.Integer(), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_event_id", sa.String(100), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "provider_event_id", name="uq_call_events_provider_event"),
    )
    op.create_index("ix_call_events_call_received", "call_events", ["call_id", "received_at"])
    op.create_table(
        "number_daily_usage",
        sa.Column("from_phone", sa.String(20), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("used", sa.Integer(), nullable=False),
    )

    # Инварианты. batch_alter_table нужен SQLite (там ALTER ADD CONSTRAINT нет, таблица
    # пересоздаётся); на PostgreSQL это обычный ALTER TABLE. Идут до частичных индексов:
    # пересоздание таблицы в SQLite не обязано сохранить условие WHERE.
    with op.batch_alter_table("campaigns") as batch:
        batch.create_check_constraint(
            "ck_campaigns_hours", "start_hour >= 0 AND end_hour <= 24 AND start_hour < end_hour"
        )
        batch.create_check_constraint(
            "ck_campaigns_max_concurrent", "max_concurrent >= 1 AND max_concurrent <= 20"
        )
    with op.batch_alter_table("contacts") as batch:
        batch.create_check_constraint("ck_contacts_attempts", "attempts >= 0")
        batch.create_check_constraint(
            "ck_contacts_rooms", "rooms IS NULL OR (rooms >= 0 AND rooms <= 7)"
        )
    with op.batch_alter_table("calls") as batch:
        batch.create_check_constraint("ck_calls_duration", "duration_seconds >= 0")
    with op.batch_alter_table("meetings") as batch:
        batch.create_unique_constraint("uq_meetings_call_scheduled", ["call_id", "scheduled_at"])

    for name, table, columns in PLAIN_INDEXES:
        op.create_index(name, table, columns)
    # Частичные уникальные индексы: один звонок провайдера на запись и один активный звонок на
    # контакт. NULL в provider_call_id не конфликтует в обоих диалектах, условие WHERE оставляет
    # индекс компактным.
    op.create_index(
        "uq_calls_provider_call_id",
        "calls",
        ["provider", "provider_call_id"],
        unique=True,
        postgresql_where=sa.text(HAS_PROVIDER_CALL_WHERE),
        sqlite_where=sa.text(HAS_PROVIDER_CALL_WHERE),
    )
    op.create_index(
        "uq_calls_active_contact",
        "calls",
        ["contact_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_CALL_WHERE),
        sqlite_where=sa.text(ACTIVE_CALL_WHERE),
    )


def downgrade() -> None:
    op.drop_index("uq_calls_active_contact", table_name="calls")
    op.drop_index("uq_calls_provider_call_id", table_name="calls")
    for name, table, _ in reversed(PLAIN_INDEXES):
        op.drop_index(name, table_name=table)
    with op.batch_alter_table("meetings") as batch:
        batch.drop_constraint("uq_meetings_call_scheduled", type_="unique")
    with op.batch_alter_table("calls") as batch:
        batch.drop_constraint("ck_calls_duration", type_="check")
    with op.batch_alter_table("contacts") as batch:
        batch.drop_constraint("ck_contacts_rooms", type_="check")
        batch.drop_constraint("ck_contacts_attempts", type_="check")
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_constraint("ck_campaigns_max_concurrent", type_="check")
        batch.drop_constraint("ck_campaigns_hours", type_="check")
    op.drop_table("number_daily_usage")
    op.drop_index("ix_call_events_call_received", table_name="call_events")
    op.drop_table("call_events")
