"""Separate source revisions and record per-thread extraction watermarks."""

from alembic import op

revision = "0006_unified_memory"
down_revision = "0005_runtime"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE threads ADD COLUMN event_sequence INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE threads ADD COLUMN memory_cursor INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE messages ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0")
    op.execute("""
        WITH numbered AS (
            SELECT id, row_number() OVER(PARTITION BY thread_id ORDER BY created_at,id) AS seq
            FROM messages
        ) UPDATE messages SET sequence=numbered.seq FROM numbered WHERE messages.id=numbered.id
    """)
    op.execute("""
        UPDATE threads SET event_sequence=COALESCE(
            (SELECT MAX(sequence) FROM messages WHERE messages.thread_id=threads.id),0)
    """)
    op.execute(
        "ALTER TABLE context_nodes ADD COLUMN representation_revision INTEGER NOT NULL DEFAULT 1"
    )
    op.execute("ALTER TABLE context_nodes ADD COLUMN needs_review BOOLEAN NOT NULL DEFAULT false")
    op.execute(
        "ALTER TABLE context_indexes ADD COLUMN representation_revision INTEGER NOT NULL DEFAULT 1"
    )
    op.create_index("ix_messages_thread_sequence", "messages", ["thread_id", "sequence"])
    # Existing derived facts may already reference an obsolete source revision.
    op.execute("""
        WITH RECURSIVE dependencies AS (
            SELECT derived.id AS derived_id, source.id AS source_id,
                   (e.redacted OR source.status != 'active' OR e.revision != source.revision
                    OR derived.project_id != source.project_id) AS invalid
            FROM context_nodes derived
            CROSS JOIN LATERAL json_array_elements_text(derived.source_event_ids) AS ref(id)
            JOIN memory_evidence e ON e.id=ref.id
            JOIN context_nodes source ON source.id=e.node_id
            WHERE derived.kind != 'directory' AND derived.id != source.id
        ), stale(id) AS (
            SELECT derived_id FROM dependencies WHERE invalid
            UNION
            SELECT d.derived_id FROM dependencies d JOIN stale s ON d.source_id=s.id
        )
        UPDATE context_nodes SET needs_review=true WHERE id IN (SELECT id FROM stale)
    """)


def downgrade():
    op.execute("""
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM threads WHERE memory_cursor > 0)
             OR EXISTS(SELECT 1 FROM context_nodes WHERE needs_review)
             OR EXISTS(SELECT 1 FROM jobs WHERE kind='distill')
          THEN RAISE EXCEPTION 'Export unified memory state before downgrade'; END IF;
        END $$
    """)
    op.drop_column("context_indexes", "representation_revision")
    op.drop_column("context_nodes", "needs_review")
    op.drop_column("context_nodes", "representation_revision")
    op.drop_index("ix_messages_thread_sequence", table_name="messages")
    op.drop_column("messages", "sequence")
    op.drop_column("threads", "memory_cursor")
    op.drop_column("threads", "event_sequence")
