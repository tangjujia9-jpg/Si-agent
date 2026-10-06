"""Explicit migrations for Copilot storage; never touch .waku/state.db."""

import os

from alembic import context
from sqlalchemy import create_engine, pool

from waku.storage.models import Base

url = os.environ.get("SI_DATABASE_URL")
supplied_connection = context.config.attributes.get("connection")
if not url and supplied_connection is None:
    raise RuntimeError("Set SI_DATABASE_URL before running migrations")

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif supplied_connection is not None:
    context.configure(
        connection=supplied_connection,
        target_metadata=Base.metadata,
        compare_type=True,
        version_table_schema=context.config.attributes.get("version_table_schema"),
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
