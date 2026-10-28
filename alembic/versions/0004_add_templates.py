"""add templates table (Postgres-backed template store, variables, owner)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "templates",
        sa.Column("name", sa.String(length=64), primary_key=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("match", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("steps", JSONB, nullable=False),
        sa.Column(
            "variables", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("owner_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
    )
    op.create_index("ix_templates_owner_id", "templates", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_templates_owner_id", table_name="templates")
    op.drop_table("templates")
