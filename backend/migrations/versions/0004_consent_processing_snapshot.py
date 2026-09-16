"""Bind each consent to the recipients and runtime mode accepted by the participant."""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("consent_snapshot", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("consents", sa.Column("snapshot", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    raise RuntimeError("Consent processing snapshots must remain available for audit")
