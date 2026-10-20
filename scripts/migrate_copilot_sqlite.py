"""Copy a stopped Copilot SQLite database into an empty PostgreSQL database.

The source is opened read-only. The target schema is upgraded with Alembic;
all copied data and verification share one transaction. Credentials come only
from SI_DATABASE_URL and are never printed. Existing target rows are rejected.
"""

import argparse
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, func, select, update

from waku.storage.models import Base


def migrate(source_path, target_url):
    source_path = Path(source_path).resolve(strict=True)
    source = create_engine(f"sqlite:///file:{source_path.as_posix()}?mode=ro&uri=true")
    target = create_engine(target_url, connect_args={"connect_timeout": 10})
    if target.dialect.name != "postgresql":
        raise ValueError("target must be PostgreSQL")
    metadata = MetaData()
    metadata.reflect(source)
    tables = Base.metadata.sorted_tables
    with source.connect() as original, target.begin() as destination:
        original.exec_driver_sql("BEGIN")
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "infra/migrations")
        )
        config.attributes["connection"] = destination
        config.attributes["version_table_schema"] = destination.scalar(
            select(func.current_schema())
        )
        command.upgrade(config, "head")
        if any(destination.scalar(select(func.count()).select_from(t)) for t in tables):
            raise ValueError("target contains data; refusing to overwrite")
        counts, references = {}, []
        for table in tables:
            if table.name not in metadata.tables:
                raise ValueError(f"source table missing: {table.name}")
            rows = list(original.execute(select(metadata.tables[table.name])).mappings())
            for row in rows:
                values = {k: v for k, v in row.items() if k in table.c and k != "search_vector"}
                for key, value in values.items():
                    if isinstance(value, datetime) and value.tzinfo is None:
                        values[key] = value.replace(tzinfo=UTC)
                if table.name == "context_nodes" and values.get("supersedes_id"):
                    references.append((values["id"], values.pop("supersedes_id")))
                destination.execute(table.insert().values(**values))
            counts[table.name] = len(rows)
        nodes = Base.metadata.tables["context_nodes"]
        for node_id, supersedes_id in references:
            destination.execute(
                update(nodes).where(nodes.c.id == node_id).values(supersedes_id=supersedes_id)
            )
        # Rebuild PostgreSQL-native lexical indexes from the original text.
        for name, column in (("context_nodes", "content"), ("context_indexes", "body")):
            table = Base.metadata.tables[name]
            destination.execute(
                update(table).values(search_vector=func.to_tsvector("simple", table.c[column]))
            )
        for table in tables:
            migrated = {r["id"]: r for r in destination.execute(select(table)).mappings()}
            rows = list(original.execute(select(metadata.tables[table.name])).mappings())
            if len(migrated) != counts[table.name]:
                raise ValueError(f"row count mismatch: {table.name}")
            for row in rows:
                copy = migrated[row["id"]]
                for key, value in row.items():
                    actual = copy[key]
                    if key == "search_vector":
                        continue
                    if key == "embedding" and value is not None:
                        equal = len(value) == len(actual) and all(
                            math.isclose(a, b, abs_tol=1e-7, rel_tol=1e-6)
                            for a, b in zip(value, actual, strict=True)
                        )
                    elif isinstance(value, datetime):
                        equal = value.replace(tzinfo=UTC) == actual.astimezone(UTC)
                    else:
                        equal = value == actual
                    if not equal:
                        raise ValueError(f"verification mismatch: {table.name}.{key}")
    source.dispose()
    target.dispose()
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    url = os.environ.get("SI_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        parser.error("set SI_DATABASE_URL to an empty PostgreSQL target")
    try:
        print(json.dumps(migrate(args.source, url), indent=2))
    except Exception as error:
        # Database exception strings can contain a DSN or user content.
        raise SystemExit(
            f"Migration failed ({type(error).__name__}); target transaction rolled back."
        ) from None


if __name__ == "__main__":
    main()
