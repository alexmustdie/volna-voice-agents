"""lead profile fields on contacts and cached utp offer on calls

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-19

"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("district", sa.String(100), nullable=True))
    op.add_column("contacts", sa.Column("metro", sa.String(100), nullable=True))
    op.add_column("contacts", sa.Column("budget", sa.Integer(), nullable=True))
    op.add_column("contacts", sa.Column("rooms", sa.Integer(), nullable=True))
    op.add_column("calls", sa.Column("utp_offer", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "utp_offer")
    for column in ("rooms", "budget", "metro", "district"):
        op.drop_column("contacts", column)
