"""caller number on calls

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-25

"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("from_phone", sa.String(20), nullable=True))
    op.create_index("ix_calls_from_phone", "calls", ["from_phone"])


def downgrade() -> None:
    op.drop_index("ix_calls_from_phone", table_name="calls")
    op.drop_column("calls", "from_phone")
