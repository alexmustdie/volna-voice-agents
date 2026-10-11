"""direct contact, messenger and outcome on calls

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-21

"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("direct_phone", sa.String(20), nullable=True))
    op.add_column("calls", sa.Column("messenger", sa.String(20), nullable=True))
    op.add_column("calls", sa.Column("outcome", sa.String(30), nullable=True))
    op.add_column("calls", sa.Column("callback_after", sa.String(100), nullable=True))
    op.add_column("calls", sa.Column("callback_reason", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    for column in ("callback_reason", "callback_after", "outcome", "messenger", "direct_phone"):
        op.drop_column("calls", column)
