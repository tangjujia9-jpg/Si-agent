"""Preserve existing vectors while accepting model-specific dimensions."""

from alembic import op

revision = "0007_embedding_dimensions"
down_revision = "0006_unified_memory"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE context_nodes ALTER COLUMN embedding TYPE vector")
    op.execute("ALTER TABLE context_indexes ALTER COLUMN embedding TYPE vector")


def downgrade():
    # A non-1536 vector makes this cast fail transactionally. Never discard
    # vectors to make a downgrade appear successful.
    op.execute("ALTER TABLE context_nodes ALTER COLUMN embedding TYPE vector(1536)")
    op.execute("ALTER TABLE context_indexes ALTER COLUMN embedding TYPE vector(1536)")
