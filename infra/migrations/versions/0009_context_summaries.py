"""Persist versioned, source-dependent working summaries."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_context_summaries"
down_revision = "0008_project_workspace"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "context_summaries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("covered_count", sa.Integer(), nullable=False),
        sa.Column("covered_sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("body", json_type, nullable=False),
        sa.Column("dependencies", json_type, nullable=False),
        sa.Column("metrics", json_type, nullable=False),
        sa.ForeignKeyConstraint(["thread_id", "project_id"], ["threads.id", "threads.project_id"]),
        sa.UniqueConstraint("run_id", "input_hash", name="uq_summary_run_input"),
        sa.CheckConstraint("status IN ('valid','invalid','redacted')", name="summary_status"),
    )
    op.create_index("ix_summary_thread_boundary", "context_summaries", ["thread_id", "covered_sequence"])


def downgrade():
    op.drop_table("context_summaries")
