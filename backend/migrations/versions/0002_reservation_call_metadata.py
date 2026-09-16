"""Keep non-content billing metadata after deletion of in-flight jobs."""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "budget_reservations", sa.Column("call_meta", sa.JSON(), nullable=False, server_default="{}")
    )


def downgrade():
    op.drop_column("budget_reservations", "call_meta")
