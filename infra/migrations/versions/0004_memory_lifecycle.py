"""Chunk spans, immutable revisions, evidence, candidates and forget barriers."""

from uuid import uuid4

from alembic import op
from sqlalchemy import text

revision = "0004_memory_lifecycle"
down_revision = "0003_si_namespace"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE context_indexes DROP CONSTRAINT uq_index_node_tier")
    for name in ("position", "start_char", "end_char"):
        op.execute(f"ALTER TABLE context_indexes ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        op.execute(f"ALTER TABLE context_indexes ALTER COLUMN {name} DROP DEFAULT")
    op.execute("UPDATE context_indexes SET end_char = char_length(body)")
    op.execute(
        "ALTER TABLE context_indexes ADD CONSTRAINT uq_index_node_tier_position UNIQUE(node_id,tier,position)"
    )
    op.execute("""
        CREATE TABLE memory_versions (
            id VARCHAR(36) PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL,
            node_id VARCHAR(36) NOT NULL REFERENCES context_nodes(id),
            revision INTEGER NOT NULL, snapshot JSONB NOT NULL,
            CONSTRAINT uq_version_node_revision UNIQUE(node_id,revision)
        )
    """)
    op.execute("CREATE INDEX ix_memory_versions_node_id ON memory_versions(node_id)")
    op.execute("""
        INSERT INTO memory_versions(id,created_at,node_id,revision,snapshot)
        SELECT id, CURRENT_TIMESTAMP, id, revision,
            jsonb_build_object('uri',uri,'kind',kind,'title',title,'abstract',abstract,
                'overview',overview,'content',content,'checksum',checksum,'status',status,
                'source_event_ids',source_event_ids,'valid_from',valid_from,'valid_to',valid_to,
                'confidence',confidence,'supersedes_id',supersedes_id)
        FROM context_nodes WHERE kind != 'directory'
    """)
    op.execute("""
        CREATE TABLE memory_evidence (
            id VARCHAR(36) PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL,
            node_id VARCHAR(36) NOT NULL REFERENCES context_nodes(id), revision INTEGER NOT NULL,
            start_char INTEGER NOT NULL, end_char INTEGER NOT NULL, quote TEXT NOT NULL,
            checksum VARCHAR(64) NOT NULL, source_event_ids JSON NOT NULL, redacted BOOLEAN NOT NULL,
            CONSTRAINT uq_evidence_span UNIQUE(node_id,revision,start_char,end_char),
            CONSTRAINT evidence_range CHECK(start_char >= 0 AND end_char >= start_char)
        )
    """)
    op.execute("CREATE INDEX ix_memory_evidence_node_id ON memory_evidence(node_id)")
    op.execute("""
        CREATE TABLE forget_tombstones (
            id VARCHAR(36) PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL,
            project_id VARCHAR(36) NOT NULL REFERENCES projects(id), uri TEXT NOT NULL,
            content_hashes JSON NOT NULL, reason VARCHAR(240) NOT NULL,
            CONSTRAINT uq_tombstone_uri UNIQUE(project_id,uri)
        )
    """)
    op.execute("CREATE INDEX ix_forget_tombstones_project_id ON forget_tombstones(project_id)")
    op.execute("""
        CREATE TABLE memory_candidates (
            id VARCHAR(36) PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL,
            project_id VARCHAR(36) NOT NULL REFERENCES projects(id), uri TEXT NOT NULL,
            kind VARCHAR(32) NOT NULL, title VARCHAR(240) NOT NULL, content TEXT NOT NULL,
            action VARCHAR(16) NOT NULL, status VARCHAR(16) NOT NULL,
            expected_revision INTEGER, supersedes_id VARCHAR(36) REFERENCES context_nodes(id),
            evidence_ids JSON NOT NULL, idempotency_key VARCHAR(128) NOT NULL,
            payload_hash VARCHAR(64) NOT NULL, reason VARCHAR(240),
            node_id VARCHAR(36) REFERENCES context_nodes(id), job_id VARCHAR(36) REFERENCES jobs(id),
            CONSTRAINT uq_candidate_key UNIQUE(project_id,idempotency_key),
            CONSTRAINT candidate_action CHECK(action IN ('ADD','UPDATE','SUPERSEDES','RETRACT','SKIP')),
            CONSTRAINT candidate_status CHECK(status IN ('pending','applied','conflict','skipped'))
        )
    """)
    op.execute("CREATE INDEX ix_memory_candidates_project_id ON memory_candidates(project_id)")
    # Existing L2 representations must be rebuilt too; a schema migration alone
    # must not leave older documents with truncated embedding coverage.
    connection = op.get_bind()
    for project_id in connection.execute(text("SELECT id FROM projects")).scalars():
        connection.execute(
            text("""
            INSERT INTO jobs(id,created_at,project_id,kind,idempotency_key,payload_hash,
                payload,status,attempts,max_attempts,available_at,result)
            VALUES(:id,CURRENT_TIMESTAMP,:project,'rebuild','migration:0004','migration:0004',
                '{}'::jsonb,'queued',0,3,CURRENT_TIMESTAMP,'{}'::jsonb)
            ON CONFLICT(project_id,idempotency_key) DO NOTHING
        """),
            {"id": str(uuid4()), "project": project_id},
        )


def downgrade():
    op.execute(
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM forget_tombstones) THEN RAISE EXCEPTION 'Refuse to discard durable forget barriers'; END IF; END $$"
    )
    # Refuse a lossy downgrade of chunk indexes. Rebuild indexes with the older
    # application only after explicitly reviewing and removing extra positions.
    op.execute(
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM context_indexes WHERE position != 0) THEN RAISE EXCEPTION 'Chunk indexes require explicit reindex before downgrade'; END IF; END $$"
    )
    for table in ("memory_candidates", "forget_tombstones", "memory_evidence", "memory_versions"):
        op.drop_table(table)
    op.execute("ALTER TABLE context_indexes DROP CONSTRAINT uq_index_node_tier_position")
    for column in ("position", "start_char", "end_char"):
        op.drop_column("context_indexes", column)
    op.execute("ALTER TABLE context_indexes ADD CONSTRAINT uq_index_node_tier UNIQUE(node_id,tier)")
