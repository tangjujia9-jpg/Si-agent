"""Rename context URIs without replacing source content, node IDs or vectors."""

from alembic import op
from sqlalchemy import JSON, Column, MetaData, String, Table, select, text, update
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_si_namespace"
down_revision = "0002_memory_indexes"
branch_labels = None
depends_on = None


def rename_namespace(old: str, new: str):
    connection = op.get_bind()
    # Unique collisions abort the entire migration; never merge or overwrite
    # two source nodes just because their new names would be equal.
    for column in ("uri", "parent_uri"):
        connection.execute(
            text(
                f"UPDATE context_nodes SET {column} = :new || substr({column}, :offset) "
                f"WHERE substr({column}, 1, :length) = :old"
            ),
            {"old": old, "new": new, "offset": len(old) + 1, "length": len(old)},
        )
    jobs = Table(
        "jobs",
        MetaData(),
        Column("id", String(36)),
        Column("result", JSON().with_variant(JSONB(), "postgresql")),
    )
    for job_id, result in connection.execute(select(jobs.c.id, jobs.c.result)):
        root = result.get("root_uri") if isinstance(result, dict) else None
        if isinstance(root, str) and root.startswith(old):
            result = {**result, "root_uri": new + root[len(old) :]}
            connection.execute(update(jobs).where(jobs.c.id == job_id).values(result=result))


def upgrade():
    rename_namespace("waku://", "si://")


def downgrade():
    rename_namespace("si://", "waku://")
