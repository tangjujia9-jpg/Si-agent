"""Durable runtime events, operation approvals and transactional outbox."""

from alembic import op


revision = "0005_runtime"
down_revision = "0004_memory_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("run_status", "runs", type_="check")
    op.create_check_constraint(
        "run_status",
        "runs",
        "status IN ('pending','running','waiting_approval','waiting_operation','succeeded','failed','cancelled','unknown')",
    )
    op.execute("""
        CREATE TABLE run_events (
            id VARCHAR(36) PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL,
            run_id VARCHAR(36) NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
            kind VARCHAR(40) NOT NULL, data JSONB NOT NULL,
            CONSTRAINT uq_run_event_sequence UNIQUE(run_id,sequence)
        )
    """)
    op.execute("CREATE INDEX ix_run_events_run_id ON run_events(run_id)")
    op.execute("""
        CREATE TABLE tool_operations (
            id VARCHAR(36) PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL,
            run_id VARCHAR(36) NOT NULL REFERENCES runs(id),
            project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
            call_id VARCHAR(128) NOT NULL, tool VARCHAR(80) NOT NULL,
            arguments JSONB NOT NULL, status VARCHAR(20) NOT NULL,
            result JSONB NOT NULL, job_id VARCHAR(36) REFERENCES jobs(id),
            CONSTRAINT uq_operation_call UNIQUE(run_id,call_id),
            CONSTRAINT operation_status CHECK(status IN ('pending','approved','running','succeeded','failed','partial','unknown','rejected'))
        )
    """)
    op.execute("CREATE INDEX ix_tool_operations_run_id ON tool_operations(run_id)")
    op.execute("""
        CREATE TABLE outbox_events (
            id VARCHAR(36) PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL,
            operation_id VARCHAR(36) NOT NULL REFERENCES tool_operations(id),
            project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
            job_id VARCHAR(36) NOT NULL REFERENCES jobs(id), status VARCHAR(20) NOT NULL,
            CONSTRAINT uq_outbox_operation UNIQUE(operation_id)
        )
    """)


def downgrade():
    op.execute(
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM runs WHERE status IN ('waiting_approval','waiting_operation','unknown')) OR EXISTS(SELECT 1 FROM tool_operations) THEN RAISE EXCEPTION 'Resolve runtime operations before downgrade'; END IF; END $$"
    )
    for name in ("outbox_events", "tool_operations", "run_events"):
        op.drop_table(name)
    op.drop_constraint("run_status", "runs", type_="check")
    op.create_check_constraint(
        "run_status", "runs", "status IN ('pending','running','succeeded','failed','cancelled')"
    )
