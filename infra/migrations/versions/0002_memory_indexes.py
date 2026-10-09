"""Tier indexes and idempotent memory writes. Existing documents stay intact."""

from alembic import op

revision = "0002_memory_indexes"
down_revision = "0001_copilot"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE context_indexes (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            node_id VARCHAR(36) NOT NULL REFERENCES context_nodes(id) ON DELETE CASCADE,
            tier VARCHAR(2) NOT NULL,
            revision INTEGER NOT NULL,
            body TEXT NOT NULL,
            embedding_model VARCHAR(240),
            embedding VECTOR(1536),
            search_vector TSVECTOR,
            CONSTRAINT uq_index_node_tier UNIQUE (node_id, tier),
            CONSTRAINT index_tier CHECK (tier IN ('l0', 'l1', 'l2'))
        )
    """)
    op.execute("CREATE INDEX ix_context_indexes_search ON context_indexes USING gin(search_vector)")
    # Exact cosine search is intentional at this corpus size. ANN can prune
    # scoped candidates; add it only with measured recall and filtering tests.
    op.execute("""
        CREATE TABLE memory_writes (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
            node_id VARCHAR(36) NOT NULL REFERENCES context_nodes(id),
            idempotency_key VARCHAR(128) NOT NULL,
            payload_hash VARCHAR(64) NOT NULL,
            created BOOLEAN NOT NULL,
            CONSTRAINT uq_memory_write_key UNIQUE (project_id, idempotency_key)
        )
    """)


def downgrade():
    op.drop_table("memory_writes")
    op.drop_table("context_indexes")
