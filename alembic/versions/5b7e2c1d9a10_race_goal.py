"""race goal

Revision ID: 5b7e2c1d9a10
Revises: 04f1aa67959e
Create Date: 2026-11-13 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5b7e2c1d9a10"
down_revision: Union[str, Sequence[str], None] = "04f1aa67959e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "race_goal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("race_date", sa.Date(), nullable=False),
        sa.Column("distance_km", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("ascent_m", sa.Numeric(precision=8, scale=1), nullable=True),
        sa.Column("target_time_h", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["athlete_id"],
            ["athlete.id"],
            name=op.f("fk_race_goal_athlete_id_athlete"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_race_goal")),
    )
    op.create_index(op.f("ix_race_goal_athlete_id"), "race_goal", ["athlete_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_race_goal_athlete_id"), table_name="race_goal")
    op.drop_table("race_goal")
