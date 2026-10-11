"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

gender = sa.Enum("female", "male", name="gender")
contact_status = sa.Enum("new", "calling", "done", "failed", "do_not_call", name="contact_status")
campaign_status = sa.Enum("draft", "running", "paused", "stopped", "completed", name="campaign_status")
call_direction = sa.Enum("outbound", "inbound", name="call_direction")
call_status = sa.Enum(
    "queued", "ringing", "in_progress", "completed", "no_answer", "busy", "failed", "canceled", name="call_status"
)


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("gender", gender, nullable=False),
        sa.Column("age", sa.Integer(), nullable=False),
        sa.Column("voice", sa.String(50), nullable=False),
        sa.Column("niche", sa.String(200), nullable=False),
        sa.Column("company_description", sa.Text(), nullable=False),
        sa.Column("call_goal", sa.Text(), nullable=False),
        sa.Column("required_questions", sa.JSON(), nullable=False),
        sa.Column("extra_instructions", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "contact_lists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("list_id", sa.Integer(), sa.ForeignKey("contact_lists.id", ondelete="CASCADE"), nullable=False),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("status", contact_status, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("list_id", "phone", name="uq_contact_list_phone"),
    )
    op.create_index("ix_contacts_list_id", "contacts", ["list_id"])
    op.create_index("ix_contacts_status", "contacts", ["status"])
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("list_id", sa.Integer(), sa.ForeignKey("contact_lists.id"), nullable=False),
        sa.Column("status", campaign_status, nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("start_hour", sa.Integer(), nullable=False),
        sa.Column("end_hour", sa.Integer(), nullable=False),
        sa.Column("days_of_week", sa.JSON(), nullable=False),
        sa.Column("max_concurrent", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "inbound_numbers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.String(20), nullable=False, unique=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=True),
        sa.Column("contact_id", sa.Integer(), sa.ForeignKey("contacts.id"), nullable=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("direction", call_direction, nullable=False),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("status", call_status, nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("provider_call_id", sa.String(100), nullable=True),
        sa.Column("live_session_id", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("live_usage_seconds", sa.Float(), nullable=False),
        sa.Column("transcript", sa.JSON(), nullable=False),
        sa.Column("goal_achieved", sa.Boolean(), nullable=False),
        sa.Column("outcome_summary", sa.Text(), nullable=False),
        sa.Column("do_not_call_requested", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
    )
    op.create_index("ix_calls_campaign_id", "calls", ["campaign_id"])
    op.create_index("ix_calls_status", "calls", ["status"])
    op.create_index("ix_calls_provider_call_id", "calls", ["provider_call_id"])
    op.create_table(
        "meetings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("call_id", sa.Integer(), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_meetings_call_id", "meetings", ["call_id"])


def downgrade() -> None:
    op.drop_table("meetings")
    op.drop_table("calls")
    op.drop_table("inbound_numbers")
    op.drop_table("campaigns")
    op.drop_table("contacts")
    op.drop_table("contact_lists")
    op.drop_table("agents")
    bind = op.get_bind()
    for enum in (call_status, call_direction, campaign_status, contact_status, gender):
        enum.drop(bind, checkfirst=True)
