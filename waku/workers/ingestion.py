"""Transactional document import into the project context namespace.

Imports store source documents and extractive previews, refresh lexical tiers,
and queue vector indexing. Generated summaries and chunks follow in week 4.
"""

from hashlib import sha256
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from waku.memory.indexing import enqueue_index, refresh_indexes
from waku.storage.models import ContextNode, Job, Project


def import_documents(session: Session, job: Job) -> dict:
    project = session.get(Project, job.project_id)
    if project is None:
        raise ValueError("project no longer exists")
    # Serialize writes within a project. This also guards concurrent imports
    # of the same URI arriving with different idempotency keys.
    session.execute(select(Project).where(Project.id == project.id).with_for_update())
    root = f"waku://users/{quote(project.user_id, safe='')}/projects/{project.id}"
    nodes = {
        n.uri: n
        for n in session.scalars(select(ContextNode).where(ContextNode.project_id == project.id))
    }

    def directory(uri: str, parent: str | None, title: str):
        if uri in nodes and nodes[uri].kind != "directory":
            raise ValueError("directory collides with a document")
        if uri not in nodes:
            node = ContextNode(
                project_id=project.id, uri=uri, parent_uri=parent, title=title, kind="directory"
            )
            session.add(node)
            nodes[uri] = node

    directory(root, None, project.name)
    for category in ("resources", "decisions", "tasks", "memories", "sessions"):
        directory(f"{root}/{category}", root, category)

    imported = unchanged = 0
    for doc in job.payload["documents"]:
        parts = doc["path"].split("/")
        parent = f"{root}/resources"
        for part in parts[:-1]:
            uri = f"{parent}/{quote(part, safe='')}"
            directory(uri, parent, part)
            parent = uri
        uri = f"{parent}/{quote(parts[-1], safe='')}"
        content = doc["content"]
        checksum = sha256(content.encode()).hexdigest()
        node = nodes.get(uri)
        if node and node.kind == "directory":
            raise ValueError("document collides with a directory")
        if node and node.checksum == checksum:
            unchanged += 1
            continue
        if node is None:
            node = ContextNode(
                project_id=project.id, uri=uri, parent_uri=parent, title=parts[-1], kind="resource"
            )
            session.add(node)
            nodes[uri] = node
        else:
            node.revision += 1
        node.content = content
        node.abstract = next(
            (line.strip().lstrip("# ") for line in content.splitlines() if line.strip()), ""
        )[:240]
        node.overview = content[:1200]
        node.checksum = checksum
        node.source_event_ids = [job.id]
        node.status = "active"
        # Updating a document invalidates its future semantic index.
        node.embedding = None
        node.search_vector = None
        imported += 1
    session.flush()
    refresh_indexes(session, project.id)
    indexing = enqueue_index(session, project.id)
    return {
        "imported": imported,
        "unchanged": unchanged,
        "root_uri": root,
        "index_job_id": indexing.id,
    }
