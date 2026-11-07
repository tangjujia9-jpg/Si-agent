"""Approve workspace edits and persist isolated command tasks."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_development_tools"
down_revision = "0009_context_summaries"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    op.add_column("projects", sa.Column("permissions", json_type, nullable=False, server_default="{}"))
    op.add_column("tool_operations", sa.Column("preview", json_type, nullable=False, server_default="{}"))
    op.create_table(
        "command_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("tool_operations.id"), unique=True, nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("backend", sa.String(24), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("request", json_type, nullable=False),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column("result", json_type, nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled','unknown')", name="command_status"),
    )
    op.create_index("ix_command_tasks_project_id", "command_tasks", ["project_id"])
    op.create_index("ix_command_tasks_run_id", "command_tasks", ["run_id"])


def downgrade():
    op.drop_table("command_tasks")
    op.drop_column("tool_operations", "preview")
    op.drop_column("projects", "permissions")
