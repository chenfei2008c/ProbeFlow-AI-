"""Keep submitted originals even when their media cannot be decoded."""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("audio_assets") as batch:
        batch.alter_column("duration_seconds", existing_type=sa.Float(), nullable=True)


def downgrade():
    # Unknown media duration cannot be truthfully converted to a measured zero.
    raise RuntimeError("Unvalidated retained media requires the V1.1 archive schema")
