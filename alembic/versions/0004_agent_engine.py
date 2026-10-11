"""voice engine per agent

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-21

"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Голоса Яндекса на момент миграции: агентам с такими голосами ставим движок yandex.
YANDEX_VOICES = (
    "alena", "dasha", "jane", "julia", "lera", "masha", "marina", "oksana", "omazh",
    "filipp", "ermil", "zahar", "alexander", "anton", "kirill", "madi_ru",
)


def upgrade() -> None:
    op.add_column("agents", sa.Column("engine", sa.String(20), nullable=False, server_default="openai"))
    agents = sa.table("agents", sa.column("voice", sa.String), sa.column("engine", sa.String))
    op.execute(agents.update().where(agents.c.voice.in_(YANDEX_VOICES)).values(engine="yandex"))


def downgrade() -> None:
    op.drop_column("agents", "engine")
