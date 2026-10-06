"""Initial Copilot storage. Frozen SQL keeps historical migrations stable."""

from alembic import op


revision = "0001_copilot"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("""
        CREATE TABLE projects (
            user_id VARCHAR(80) NOT NULL,
            name VARCHAR(160) NOT NULL,
            description TEXT NOT NULL,
            id VARCHAR(36) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id)
        )
    """)
    op.execute("""
        CREATE TABLE context_nodes (
            project_id VARCHAR(36) NOT NULL,
            uri TEXT NOT NULL,
            parent_uri TEXT,
            kind VARCHAR(32) NOT NULL,
            title VARCHAR(240) NOT NULL,
            abstract TEXT NOT NULL,
            overview TEXT NOT NULL,
            content TEXT NOT NULL,
            checksum VARCHAR(64),
            revision INTEGER NOT NULL,
            source_event_ids JSON NOT NULL,
            confidence FLOAT,
            valid_from TIMESTAMP WITH TIME ZONE,
            valid_to TIMESTAMP WITH TIME ZONE,
            status VARCHAR(16) NOT NULL,
            supersedes_id VARCHAR(36),
            embedding VECTOR(1536),
            search_vector TSVECTOR,
            id VARCHAR(36) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_node_project_uri UNIQUE (project_id, uri),
            CONSTRAINT node_status CHECK (status IN ('active', 'superseded', 'retracted')),
            CONSTRAINT node_confidence CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            FOREIGN KEY(project_id) REFERENCES projects (id),
            FOREIGN KEY(supersedes_id) REFERENCES context_nodes (id)
        )
    """)
    op.execute("""
        CREATE TABLE jobs (
            project_id VARCHAR(36) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            idempotency_key VARCHAR(128) NOT NULL,
            payload_hash VARCHAR(64) NOT NULL,
            payload JSONB NOT NULL,
            status VARCHAR(16) NOT NULL,
            attempts INTEGER NOT NULL,
            max_attempts INTEGER NOT NULL,
            available_at TIMESTAMP WITH TIME ZONE NOT NULL,
            lease_until TIMESTAMP WITH TIME ZONE,
            lease_token VARCHAR(36),
            error TEXT,
            result JSONB NOT NULL,
            id VARCHAR(36) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_job_project_key UNIQUE (project_id, idempotency_key),
            CONSTRAINT job_status CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
            FOREIGN KEY(project_id) REFERENCES projects (id)
        )
    """)
    op.execute("""
        CREATE TABLE threads (
            project_id VARCHAR(36) NOT NULL,
            title VARCHAR(160) NOT NULL,
            id VARCHAR(36) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (id, project_id),
            FOREIGN KEY(project_id) REFERENCES projects (id)
        )
    """)
    op.execute("""
        CREATE TABLE messages (
            thread_id VARCHAR(36) NOT NULL,
            project_id VARCHAR(36) NOT NULL,
            role VARCHAR(16) NOT NULL,
            content TEXT NOT NULL,
            event_data JSONB NOT NULL,
            id VARCHAR(36) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(thread_id, project_id) REFERENCES threads (id, project_id),
            CONSTRAINT message_role CHECK (role IN ('user', 'assistant', 'tool')),
            FOREIGN KEY(project_id) REFERENCES projects (id)
        )
    """)
    op.execute("""
        CREATE TABLE runs (
            project_id VARCHAR(36) NOT NULL,
            thread_id VARCHAR(36) NOT NULL,
            status VARCHAR(16) NOT NULL,
            model VARCHAR(160) NOT NULL,
            trace_id VARCHAR(36),
            context JSONB NOT NULL,
            id VARCHAR(36) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(thread_id, project_id) REFERENCES threads (id, project_id),
            CONSTRAINT run_status CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
            FOREIGN KEY(project_id) REFERENCES projects (id)
        )
    """)
    op.execute("CREATE INDEX ix_projects_user_id ON projects (user_id)")
    op.execute("CREATE INDEX ix_nodes_project_parent ON context_nodes (project_id, parent_uri)")
    op.execute("CREATE INDEX ix_jobs_claim ON jobs (status, available_at)")
    op.execute("CREATE INDEX ix_jobs_project_id ON jobs (project_id)")
    op.execute("CREATE INDEX ix_threads_project_id ON threads (project_id)")
    op.execute("CREATE INDEX ix_messages_thread_id ON messages (thread_id)")
    op.execute("CREATE INDEX ix_runs_project_id ON runs (project_id)")


def downgrade():
    op.execute("DROP TABLE runs")
    op.execute("DROP TABLE messages")
    op.execute("DROP TABLE threads")
    op.execute("DROP TABLE jobs")
    op.execute("DROP TABLE context_nodes")
    op.execute("DROP TABLE projects")
