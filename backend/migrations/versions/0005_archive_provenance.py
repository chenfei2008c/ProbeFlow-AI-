"""Persist output provenance; do not infer missing historical mode from current settings."""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("transcript_revisions", "audio_assets", "reports"):
        op.add_column(table, sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    raise RuntimeError("Archive provenance must remain available for audit")
