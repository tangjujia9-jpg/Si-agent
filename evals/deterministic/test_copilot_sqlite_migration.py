"""Real PostgreSQL import preserves original facts, vectors and supersession."""

import os
from uuid import uuid4

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("pgvector")

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from scripts.migrate_copilot_sqlite import migrate
from waku.storage.models import Base, ContextIndex, ContextNode, Project


def test_sqlite_copy_is_verified_and_refuses_existing_postgres_data(tmp_path):
    url = os.environ.get("SI_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires PostgreSQL")
    source_path = tmp_path / "original.db"
    source = create_engine(f"sqlite:///{source_path}")
    Base.metadata.create_all(source)
    with Session(source) as session:
        project = Project(name="Original", user_id="default")
        session.add(project)
        session.flush()
        first = ContextNode(
            project_id=project.id,
            uri="si://first.md",
            title="First",
            kind="semantic",
            content="Original fact",
            embedding=[0.1, 0.2, 0.3],
            status="superseded",
        )
        session.add(first)
        session.flush()
        second = ContextNode(
            project_id=project.id,
            uri="si://second.md",
            title="Second",
            kind="semantic",
            content="Updated fact",
            supersedes_id=first.id,
        )
        session.add(second)
        session.flush()
        session.add(
            ContextIndex(
                node_id=first.id,
                tier="l2",
                revision=1,
                body="Original fact",
                embedding=[0.1, 0.2, 0.3],
                embedding_model="fixture",
            )
        )
        session.commit()
        node_id, original_id = second.id, first.id
    # Emulate the previous release's project table; the importer supplies new defaults.
    with source.begin() as c:
        c.execute(text("ALTER TABLE projects DROP COLUMN root_path"))
        c.execute(text("ALTER TABLE projects DROP COLUMN folder_name"))
    admin = create_engine(url)
    schema = "test_copy_" + uuid4().hex
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    target_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
    target = create_engine(target_url)
    try:
        counts = migrate(source_path, target_url)
        assert counts["context_nodes"] == 2
        with Session(target) as session:
            assert session.get(ContextNode, node_id).supersedes_id == original_id
            assert session.get(ContextNode, original_id).content == "Original fact"
            assert session.get(ContextNode, original_id).embedding == pytest.approx([0.1, 0.2, 0.3])
            assert session.scalar(select(Project)).folder_name == ""
        with pytest.raises(ValueError, match="refusing to overwrite"):
            migrate(source_path, target_url)
        with source.connect() as c:
            assert c.execute(text("SELECT count(*) FROM context_nodes")).scalar() == 2
    finally:
        target.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
        source.dispose()
