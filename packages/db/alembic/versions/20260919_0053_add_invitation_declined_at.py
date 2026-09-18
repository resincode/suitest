"""Add declined_at to invitations (M1e-9: in-app approve/decline).

Revision ID: 0053_add_invitation_declined_at
Revises: 0052_add_run_status_interrupted
Create Date: 2026-09-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0053_add_invitation_declined_at"
down_revision: str | None = "0052_add_run_status_interrupted"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "invitations",
        sa.Column("declined_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invitations", "declined_at")
