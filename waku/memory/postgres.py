"""Scoped hierarchical hybrid retrieval and transactional MemoryPort writes.

PostgreSQL owns lexical ranking and cosine distance. SQLite is an explicit
offline fallback; its vectors exercise the contract, not pgvector SQL.
"""

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from urllib.parse import quote, unquote, urlsplit

from sqlalchemy import func, literal_column, or_, select

from waku.domain.contracts import MemoryHit, MemoryNode
from waku.memory.context import CompiledContext, compile_context
from waku.memory.embeddings import Embedder, terms, validate_vectors
from waku.memory.indexing import enqueue_index, refresh_indexes
from waku.memory.port import MemoryQuery, MemoryWrite, MemoryWriteReceipt
from waku.storage.models import ContextIndex, ContextNode, MemoryWriteRecord, Project


def root_uri(user_id: str, project_id: str) -> str:
    return f"waku://users/{quote(user_id, safe='')}/projects/{quote(project_id, safe='')}"


def validate_uri(uri: str, user_id: str, project_id: str) -> str | None:
    root = root_uri(user_id, project_id)
    parsed = urlsplit(uri)
    parts = uri[len(root) + 1 :].split("/") if uri.startswith(root + "/") else []
    if uri == root:
        return None
    if (
        not parts
        or parsed.query
        or parsed.fragment
        or any(
            not p
            or unquote(p) in (".", "..")
            or "/" in unquote(p)
            or "\\" in unquote(p)
            or any(ord(c) < 32 for c in unquote(p))
            or quote(unquote(p), safe="") != p
            for p in parts
        )
    ):
        raise ValueError("URI must be canonical and inside the project's namespace")
    return uri.rsplit("/", 1)[0]


def domain_node(node: ContextNode, user_id: str) -> MemoryNode:
    return MemoryNode(
        uri=node.uri,
        kind=node.kind,
        title=node.title,
        abstract=node.abstract,
        overview=node.overview,
        content=node.content,
        user_id=user_id,
        project_id=node.project_id,
        source_event_ids=tuple(node.source_event_ids),
        confidence=node.confidence,
        created_at=node.created_at,
        valid_from=node.valid_from,
        valid_to=node.valid_to,
        status=node.status,
        supersedes_id=node.supersedes_id,
        metadata={"id": node.id, "revision": node.revision, "parent_uri": node.parent_uri},
    )


@dataclass(frozen=True)
class RetrievalResult:
    context: CompiledContext
    stages: tuple[dict, ...]
    warnings: tuple[str, ...]
    embedding_model: str | None


class PostgresMemory:
    """Caller-supplied sessions and embedder keep credentials outside memory logic."""

    def __init__(self, sessions, embedder: Embedder | None = None):
        self.sessions, self.embedder = sessions, embedder

    def get(self, uri: str, *, user_id: str = "default") -> MemoryNode | None:
        with self.sessions() as session:
            node = session.scalar(
                select(ContextNode)
                .join(Project)
                .where(
                    ContextNode.uri == uri,
                    Project.user_id == user_id,
                )
            )
            return domain_node(node, user_id) if node else None

    def list_children(self, uri: str, *, user_id: str = "default") -> list[MemoryNode]:
        with self.sessions() as session:
            nodes = session.scalars(
                select(ContextNode)
                .join(Project)
                .where(
                    ContextNode.parent_uri == uri,
                    Project.user_id == user_id,
                    ContextNode.status == "active",
                )
                .order_by(ContextNode.uri)
            )
            return [domain_node(n, user_id) for n in nodes]

    def write(self, request: MemoryWrite) -> MemoryWriteReceipt:
        node = request.node
        if not node.project_id or not request.idempotency_key or len(request.idempotency_key) > 128:
            raise ValueError("project and bounded idempotency key are required")
        parent = validate_uri(node.uri, node.user_id, node.project_id)
        if parent is None or node.kind == "directory":
            raise ValueError("write accepts leaf memory nodes only")
        if (
            len(node.title) > 240
            or len(node.content) > 200000
            or node.status not in ("active", "superseded", "retracted")
        ):
            raise ValueError("invalid memory node")
        if any(t is not None and t.tzinfo is None for t in (node.valid_from, node.valid_to)):
            raise ValueError("memory validity timestamps must include a timezone")
        if node.valid_from and node.valid_to and node.valid_to <= node.valid_from:
            raise ValueError("valid_to must follow valid_from")
        digest = hashlib.sha256(
            json.dumps(asdict(request), sort_keys=True, default=str).encode()
        ).hexdigest()
        with self.sessions.begin() as session:
            project = session.scalar(
                select(Project)
                .where(
                    Project.id == node.project_id,
                    Project.user_id == node.user_id,
                )
                .with_for_update()
            )
            if project is None:
                raise ValueError("project not found in user scope")
            previous = session.scalar(
                select(MemoryWriteRecord).where(
                    MemoryWriteRecord.project_id == project.id,
                    MemoryWriteRecord.idempotency_key == request.idempotency_key,
                )
            )
            if previous:
                if previous.payload_hash != digest:
                    raise ValueError("idempotency key conflicts with a different memory write")
                stored = session.get(ContextNode, previous.node_id)
                return MemoryWriteReceipt(stored.id, stored.uri, previous.created)
            # Materialize missing parents without granting cross-project references.
            root = root_uri(node.user_id, project.id)
            ancestors = [root]
            for part in parent[len(root) + 1 :].split("/") if parent != root else []:
                ancestors.append(ancestors[-1] + "/" + part)
            for i, uri in enumerate(ancestors):
                existing = session.scalar(
                    select(ContextNode).where(
                        ContextNode.project_id == project.id, ContextNode.uri == uri
                    )
                )
                if existing and existing.kind != "directory":
                    raise ValueError("directory collides with a memory")
                if not existing:
                    session.add(
                        ContextNode(
                            project_id=project.id,
                            uri=uri,
                            kind="directory",
                            title=project.name if i == 0 else unquote(uri.rsplit("/", 1)[1]),
                            parent_uri=ancestors[i - 1] if i else None,
                        )
                    )
                    session.flush()
            stored = session.scalar(
                select(ContextNode).where(
                    ContextNode.project_id == project.id, ContextNode.uri == node.uri
                )
            )
            created = stored is None
            if stored and stored.kind == "directory":
                raise ValueError("memory collides with a directory")
            if stored is None:
                stored = ContextNode(
                    project_id=project.id,
                    uri=node.uri,
                    parent_uri=parent,
                    title=node.title,
                    kind=node.kind,
                )
                session.add(stored)
            else:
                stored.revision += 1
            if node.supersedes_id:
                old = session.get(ContextNode, node.supersedes_id)
                if old is None or old.project_id != project.id or old.id == stored.id:
                    raise ValueError("superseded node must be a different node in the same project")
                old.status = "superseded"
            for key in (
                "kind",
                "title",
                "abstract",
                "overview",
                "content",
                "confidence",
                "valid_from",
                "valid_to",
                "status",
                "supersedes_id",
            ):
                setattr(stored, key, getattr(node, key))
            stored.source_event_ids = list(
                dict.fromkeys((*node.source_event_ids, *request.evidence_ids))
            )
            stored.abstract = (
                stored.abstract
                or next(
                    (
                        line.strip().lstrip("# ")
                        for line in node.content.splitlines()
                        if line.strip()
                    ),
                    "",
                )[:240]
            )
            stored.overview = stored.overview or node.content[:1200]
            stored.checksum = hashlib.sha256(node.content.encode()).hexdigest()
            stored.embedding, stored.search_vector = None, None
            session.flush()
            session.add(
                MemoryWriteRecord(
                    project_id=project.id,
                    node_id=stored.id,
                    idempotency_key=request.idempotency_key,
                    payload_hash=digest,
                    created=created,
                )
            )
            refresh_indexes(session, project.id)
            enqueue_index(session, project.id)
            return MemoryWriteReceipt(stored.id, stored.uri, created)

    def forget(self, uri: str, *, user_id: str = "default", reason: str = "user_request") -> bool:
        # A status flag alone would allow ingestion/consolidation to resurrect
        # forgotten content. Week 4 owns tombstones and the public forget API.
        raise NotImplementedError("forget requires durable tombstones (week 4)")

    def search(self, query: MemoryQuery) -> list[MemoryHit]:
        return list(self.retrieve(query).context.hits)

    def retrieve(self, query: MemoryQuery) -> RetrievalResult:
        if not query.project_id:
            raise ValueError("retrieval requires an explicit project")
        # Scope validation precedes any paid or externally transmitted query.
        with self.sessions() as session:
            allowed = session.scalar(
                select(Project.id).where(
                    Project.id == query.project_id,
                    Project.user_id == query.user_id,
                )
            )
            if allowed is None:
                raise ValueError("project not found in user scope")
        stages, warnings = [], []
        vector = None
        if self.embedder:
            started = time.perf_counter()
            try:
                vector = validate_vectors(self.embedder.embed([query.text[:6000]]), 1)[0]
            except Exception:
                warnings.append("Embedding unavailable; lexical retrieval was used.")
            stages.append(
                {
                    "stage": "query_embedding",
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
        else:
            warnings.append("Embeddings disabled; lexical retrieval was used.")
        if self.embedder and self.embedder.model_id.startswith("demo:"):
            warnings.append("Hash demo vectors do not provide semantic paraphrase retrieval.")
        at = query.as_of or datetime.now(UTC)
        with self.sessions() as session:
            project = session.scalar(
                select(Project).where(
                    Project.id == query.project_id, Project.user_id == query.user_id
                )
            )
            if project is None:
                raise ValueError("project not found in user scope")
            base = (
                select(ContextIndex, ContextNode)
                .join(ContextNode)
                .where(
                    ContextNode.project_id == project.id,
                    ContextNode.status == "active",
                    ContextIndex.revision == ContextNode.revision,
                    or_(ContextNode.valid_from.is_(None), ContextNode.valid_from <= at),
                    or_(ContextNode.valid_to.is_(None), ContextNode.valid_to > at),
                )
            )
            limit = min(100, max(20, query.top_k * 4))
            if vector is not None:
                ready = session.scalar(
                    select(func.count()).select_from(
                        base.where(
                            ContextIndex.embedding_model == self.embedder.model_id,
                            ContextIndex.embedding.is_not(None),
                        )
                        .with_only_columns(ContextIndex.id)
                        .subquery()
                    )
                )
                stages.append({"stage": "index_coverage", "ready_representations": ready})
                if not ready:
                    warnings.append(
                        "No vectors for this model are ready in this scope; request reindex or wait for the worker."
                    )

            def ranked(tier, *, directories=False, parents=None):
                started = time.perf_counter()
                statement = base.where(ContextIndex.tier == tier)
                statement = statement.where(
                    ContextNode.kind == "directory"
                    if directories
                    else ContextNode.kind != "directory"
                )
                if parents is not None:
                    if not parents:
                        return []
                    statement = statement.where(
                        or_(ContextNode.uri.in_(parents), ContextNode.parent_uri.in_(parents))
                        if directories
                        else ContextNode.parent_uri.in_(parents)
                    )
                tokens = list(dict.fromkeys(terms(query.text)))[:64]
                lexical = []
                if tokens:
                    if session.bind.dialect.name == "postgresql":
                        tsquery = func.to_tsquery(literal_column("'simple'"), " | ".join(tokens))
                        lexical = session.execute(
                            statement.where(ContextIndex.search_vector.op("@@")(tsquery))
                            .order_by(
                                func.ts_rank_cd(ContextIndex.search_vector, tsquery).desc(),
                                ContextIndex.id,
                            )
                            .limit(limit)
                        ).all()
                    else:
                        rows = session.execute(statement).all()
                        scored = [
                            (sum(t in set(terms(f"{n.title} {r.body}")) for t in tokens), r, n)
                            for r, n in rows
                        ]
                        lexical = [
                            (r, n)
                            for score, r, n in sorted(scored, key=lambda x: (-x[0], x[1].id))
                            if score > 0
                        ][:limit]
                semantic = []
                if vector is not None:
                    semantic_statement = statement.where(
                        ContextIndex.embedding_model == self.embedder.model_id,
                        ContextIndex.embedding.is_not(None),
                    )
                    if session.bind.dialect.name == "postgresql":
                        distance = ContextIndex.embedding.cosine_distance(vector)
                        semantic = session.execute(
                            semantic_statement.where(distance < 0.8)
                            .order_by(distance, ContextIndex.id)
                            .limit(limit)
                        ).all()
                    else:
                        rows = session.execute(semantic_statement).all()
                        scored = [(cosine(vector, r.embedding), r, n) for r, n in rows]
                        semantic = [
                            (r, n)
                            for score, r, n in sorted(scored, key=lambda x: (-x[0], x[1].id))
                            if score > 0.2
                        ][:limit]
                fused = {}
                for branch in (lexical, semantic):
                    for rank, (row, node) in enumerate(branch, 1):
                        old = fused.get(row.id, (0.0, row, node))
                        fused[row.id] = (old[0] + 1 / (60 + rank), row, node)
                out = sorted(fused.values(), key=lambda item: (-item[0], item[2].uri))
                stages.append(
                    {
                        "stage": f"{tier}_{'directories' if directories else 'leaves'}",
                        "lexical": len(lexical),
                        "vector": len(semantic),
                        "candidates": len(out),
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                )
                return out

            l0 = ranked("l0", directories=True)[:6]
            l1 = ranked("l1", directories=True, parents={n.uri for _, _, n in l0})[:6]
            if not l1:
                l1 = ranked("l1", directories=True)[:6]
            parents = {n.uri for _, _, n in (*l0, *l1)}
            tiers = [t for t in query.tiers if query.include_details or t != "l2"]
            best = {}
            for tier in tiers:
                # Directory routing prioritizes a branch. Global rescue prevents
                # a truncated overview from hiding relevant detail elsewhere.
                branch = ranked(tier, parents=parents)
                rescue = ranked(tier)
                branch_ids = {r.id for _, r, _ in branch}
                for score, row, node in rescue:
                    score += 0.001 if row.id in branch_ids else 0
                    previous = best.get(node.id)
                    # Prefer deeper evidence when scores tie, never duplicate a URI.
                    if previous is None or (score, row.tier) > (previous[0], previous[1].tier):
                        best[node.id] = (score, row, node)
            hits = []
            for score, row, node in sorted(best.values(), key=lambda x: (-x[0], x[2].uri))[
                : query.top_k
            ]:
                hits.append(
                    MemoryHit(
                        id=node.id,
                        uri=node.uri,
                        tier=row.tier,
                        snippet=snippet(row.body, query.text),
                        score=round(score, 8),
                        source=node.kind,
                        project_scope=project.id,
                        evidence_ids=tuple(node.source_event_ids),
                        valid_from=node.valid_from,
                        valid_to=node.valid_to,
                    )
                )
        context = compile_context(hits, query.max_tokens)
        stages.append(
            {
                "stage": "context_compile",
                "hits": len(context.hits),
                "estimated_tokens": context.estimated_tokens,
                "truncated": context.truncated,
            }
        )
        return RetrievalResult(
            context,
            tuple(stages),
            tuple(warnings),
            self.embedder.model_id if vector is not None else None,
        )


def cosine(a, b) -> float:
    denominator = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / denominator if denominator else 0


def snippet(body: str, query: str, limit: int = 4000) -> str:
    positions = [body.lower().find(t) for t in terms(query)]
    start = max(0, min((p for p in positions if p >= 0), default=0) - 200)
    return body[start : start + limit]
