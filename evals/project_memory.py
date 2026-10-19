"""Reproducible retrieval baselines over isolated, synthetic project memories.

Run with python -m evals.project_memory. Network embeddings require --live;
no model calls occur on import or on the default lexical run.
"""

import argparse
import hashlib
import json
import math
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select, text

from evals.embedding_budget import BudgetedEmbedder
from waku.domain.contracts import MemoryNode
from waku.memory.embeddings import configured_embedder
from waku.memory.indexing import enqueue_index, process_index
from waku.memory.port import MemoryQuery, MemoryWrite
from waku.memory.postgres import PostgresMemory, root_uri
from waku.storage.database import build_engine, session_factory
from waku.storage.models import Base, ContextIndex, ContextNode, Job, MemoryEvidence, Project

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/fixtures/project_memory_v1.json"


def load_dataset(path=DATASET):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    docs = {d["id"]: d for d in data["documents"]}
    if len(docs) != len(data["documents"]):
        raise ValueError("duplicate document id")
    ids = [q["id"] for q in data["queries"] + data["retention"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate sample id")
    for d in docs.values():
        if not d["content"] or not d["path"] or d["path"].startswith("/"):
            raise ValueError("invalid fixture document")
    for q in data["queries"]:
        if not q["query"].strip() or q["split"] not in {"calibration", "test"}:
            raise ValueError("invalid query")
        if set(q["expected"]) & set(q["forbidden"]):
            raise ValueError("expected and forbidden references overlap")
        for did in q["expected"] + q["forbidden"]:
            if did not in docs:
                raise ValueError("unknown reference")
        for did in q["expected"]:
            d = docs[did]
            if d["project"] != q["project"] or d.get("forget") or d.get("needs_review"):
                raise ValueError("expected reference is unavailable")
            quote = q["gold_quotes"].get(did, "")
            if not quote or quote not in d["content"]:
                raise ValueError("gold quote must be exact")
            at = moment(q["as_of"])
            if d.get("valid_from") and moment(d["valid_from"]) > at:
                raise ValueError("expected reference not valid yet")
            if d.get("valid_to") and moment(d["valid_to"]) <= at:
                raise ValueError("expected reference expired")
    return data, hashlib.sha256(raw).hexdigest()


def moment(value):
    return datetime.fromisoformat(value) if value else None


def seed(sessions, data):
    """Freeze reference memories independently from extraction quality."""
    repo = PostgresMemory(sessions)
    projects, references = {}, {}
    with sessions.begin() as session:
        for scope in sorted({d["project"] for d in data["documents"]}):
            project = Project(id="baseline-" + scope, name="Synthetic baseline " + scope, user_id="baseline")
            session.add(project)
            projects[scope] = project.id
    for doc in data["documents"]:
        pid = projects[doc["project"]]
        uri = root_uri("baseline", pid) + "/" + doc["path"]
        node = MemoryNode(
            uri=uri, project_id=pid, user_id="baseline", kind=doc["kind"],
            title=doc["title"], content=doc["content"], abstract=doc["content"][:240],
            overview=doc["content"][:1200], valid_from=moment(doc.get("valid_from")),
            valid_to=moment(doc.get("valid_to")),
        )
        receipt = repo.write(MemoryWrite(node, "baseline:" + doc["id"]))
        references[doc["id"]] = receipt.id
        if doc.get("forget"):
            repo.forget(uri, user_id="baseline", reason="synthetic_fixture")
    from waku.memory.indexing import refresh_indexes

    with sessions.begin() as session:
        for doc in data["documents"]:
            if doc.get("needs_review"):
                session.get(ContextNode, references[doc["id"]]).needs_review = True
        for pid in projects.values():
            refresh_indexes(session, pid)
        for row, uri in session.execute(select(ContextIndex, ContextNode.uri).join(ContextNode)):
            row.id = str(uuid5(NAMESPACE_URL, f"{uri}:{row.tier}:{row.position}"))
        # Writes queued index jobs, but this isolated runner executes its own
        # single frozen snapshot. No product worker consumes this database.
        for job in session.scalars(select(Job)):
            job.status = "succeeded"
    return projects, references


def index_corpus(sessions, projects, embedder):
    for pid in projects.values():
        with sessions.begin() as session:
            job = enqueue_index(session, pid)
            job.status, job.lease_token = "running", "baseline"
            job_id = job.id
        if not process_index(sessions, job_id, "baseline", embedder):
            raise RuntimeError("baseline indexing lost its lease")


def ranking_metrics(expected, retrieved, k):
    relevant = set(expected)
    top = list(dict.fromkeys(retrieved))[:k]
    if not relevant:
        return {"recall_at_k": None, "mrr": None, "ndcg_at_k": None}
    ranks = [i for i, value in enumerate(top, 1) if value in relevant]
    dcg = sum(1 / math.log2(rank + 1) for rank in ranks)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(relevant), k) + 1))
    return {"recall_at_k": len(ranks) / len(relevant), "mrr": 1 / ranks[0] if ranks else 0,
            "ndcg_at_k": dcg / ideal}


def evaluate(sessions, data, projects, references, *, mode="lexical", embedder=None, top_k=5, split="all", limit=None):
    repo = PostgresMemory(sessions, embedder if mode == "hybrid" else None)
    inverse = {value: key for key, value in references.items()}
    docs = {d["id"]: d for d in data["documents"]}
    queries = [q for q in data["queries"] if split == "all" or q["split"] == split]
    if limit is not None:
        queries = queries[:limit]
    records = []
    for q in queries:
        start = time.perf_counter()
        result = repo.retrieve(MemoryQuery(q["query"], user_id="baseline", project_id=projects[q["project"]], top_k=top_k, as_of=moment(q["as_of"])))
        ms = (time.perf_counter() - start) * 1000
        if mode == "hybrid" and result.embedding_model is None:
            raise RuntimeError("hybrid baseline refused silent embedding fallback")
        hits = result.context.hits
        returned = [inverse.get(h.id, "unknown") for h in hits]
        evidence_valid = 0
        with sessions() as session:
            for hit in hits:
                spans = [session.get(MemoryEvidence, eid) for eid in hit.evidence_ids]
                node = session.get(ContextNode, hit.id)
                if spans and all(e and not e.redacted and e.node_id == node.id and e.revision == node.revision and e.quote == node.content[e.start_char:e.end_char] for e in spans):
                    evidence_valid += 1
        violations = []
        for did in returned:
            d = docs.get(did)
            if not d or d["project"] != q["project"]:
                violations.append("scope")
            if did in q["forbidden"]:
                violations.append("forbidden")
            if d and (d.get("forget") or d.get("needs_review")):
                violations.append("unavailable")
            at = moment(q["as_of"])
            if d and ((d.get("valid_from") and moment(d["valid_from"]) > at) or (d.get("valid_to") and moment(d["valid_to"]) <= at)):
                violations.append("stale")
        records.append({
            "id": q["id"], "split": q["split"], "category": q["category"], "query": q["query"],
            "expected": q["expected"], "retrieved": returned,
            **ranking_metrics(q["expected"], returned, top_k), "latency_ms": round(ms, 3),
            "violations": violations, "evidence_valid_hits": evidence_valid,
            "hit_count": len(hits), "warnings": list(result.warnings),
            "hits": [asdict(h) for h in hits], "estimated_tokens": result.context.estimated_tokens,
        })
    return {"mode": mode, "top_k": top_k, "records": records, "summary": summarize(records)}


def summarize(records):
    def mean(key):
        values = [r[key] for r in records if r[key] is not None]
        return round(sum(values) / len(values), 6) if values else None

    hits = sum(r["hit_count"] for r in records)
    return {
        "queries": len(records), "positive_queries": sum(bool(r["expected"]) for r in records),
        "recall_at_k": mean("recall_at_k"), "mrr": mean("mrr"), "ndcg_at_k": mean("ndcg_at_k"),
        "evidence_integrity": round(sum(r["evidence_valid_hits"] for r in records) / hits, 6) if hits else None,
        "scope_violation_queries": sum("scope" in r["violations"] for r in records),
        "forbidden_violation_queries": sum("forbidden" in r["violations"] for r in records),
        "stale_violation_queries": sum("stale" in r["violations"] for r in records),
        "unavailable_violation_queries": sum("unavailable" in r["violations"] for r in records),
        "mean_query_ms": mean("latency_ms"),
        "no_answer_queries_with_hits": sum(not r["expected"] and r["hit_count"] > 0 for r in records),
    }


def markdown(report):
    lines = ["# Si-agent 项目记忆基线", "", f"数据集：`{report['dataset_version']}`，SHA256：`{report['dataset_sha256']}`。", "",
             f"执行后端：{report['database_backend']}；向量模型：{report.get('embedding_model') or '未启用'}。", "",
             "数据为 Agent 编写的合成参考样本，尚待独立人工复核；结果不能代表生产质量。", "",
             "| 检索方式 | 查询数 | Recall@k | MRR | nDCG@k | 来源证据完整性 | 违规查询 | 平均查询毫秒 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for run in report["runs"]:
        s = run["summary"]
        violations = sum(bool(r["violations"]) for r in run["records"])
        lines.append(f"| {run['mode']} | {s['queries']} | {s['recall_at_k']} | {s['mrr']} | {s['ndcg_at_k']} | {s['evidence_integrity']} | {violations} | {s['mean_query_ms']} |")
    lines += ["", "只对有正确来源的查询计算召回指标；无答案查询单列，返回邻近资料不等于正确回答。",
              "来源证据完整性只校验引用是否确实对应原文版本和字符区间，不等于答案引用准确率。",
              "本轮固定正确记忆后测检索，未将模型抽取质量、留存筛选或 Agent 回答质量混入召回分数。",
              "检索调用包含向量缓存命中；平均延迟不能代表冷请求或生产 p95。", "",
              "## 向量额度", "", "```json", json.dumps(report.get("embedding_budget"), ensure_ascii=False, indent=2), "```", "",
              "额度按请求尝试、文本数和字符数限制，字符数不等于厂商计费 token。费用未知时不估算成真实账单。", "",
              "## 失败样本", ""]
    for run in report["runs"]:
        for r in run["records"]:
            if r["violations"] or r["recall_at_k"] == 0:
                lines.append(f"- {run['mode']} / {r['id']}：期望 {r['expected']}，实际 {r['retrieved']}，违规 {r['violations']}。")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--mode", choices=("lexical", "hybrid", "both"), default="lexical")
    parser.add_argument("--live", action="store_true", help="allow explicit external embedding requests")
    parser.add_argument("--config", type=Path, help="explicit local env file; never auto-discover secrets")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache", type=Path, default=ROOT / ".artifacts/baseline/embedding-cache.sqlite")
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument("--max-texts", type=int, default=200)
    parser.add_argument("--max-characters", type=int, default=40000)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--split", choices=("all", "calibration", "test"), default="all")
    args = parser.parse_args()
    if not 1 <= args.top_k <= 50 or (args.limit is not None and args.limit < 1):
        parser.error("invalid query limit or top-k")
    if args.config:
        from dotenv import dotenv_values

        os.environ.update({k: v for k, v in dotenv_values(args.config).items() if k.startswith("SI_") and v is not None})
    data, checksum = load_dataset(args.dataset)
    embedder = None
    if args.mode != "lexical":
        if not args.live:
            parser.error("hybrid requires explicit --live")
        delegate = configured_embedder()
        if delegate is None or delegate.model_id.startswith("demo:"):
            parser.error("hybrid quality evaluation requires a real embedding model")
        embedder = BudgetedEmbedder(delegate, args.cache, max_requests=args.max_requests, max_texts=args.max_texts, max_characters=args.max_characters)
    output = args.output or ROOT / ".artifacts/baseline" / ("run-" + uuid4().hex[:10])
    # A new directory prevents silently overwriting a prior baseline or user DB.
    output.mkdir(parents=True, exist_ok=False)
    database_url = os.environ.get("SI_BASELINE_DATABASE_URL")
    schema = None
    if database_url:
        from sqlalchemy import create_engine

        admin = build_engine(database_url)
        if admin.dialect.name != "postgresql":
            parser.error("SI_BASELINE_DATABASE_URL must be PostgreSQL")
        schema = "baseline_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(database_url, connect_args={"options": f"-csearch_path={schema},public"})
        # Use production migrations in the new isolated PostgreSQL schema.
        from alembic import command
        from alembic.config import Config

        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["version_table_schema"] = schema
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        admin.dispose()
    else:
        engine = build_engine("sqlite:///" + (output / "baseline.db").resolve().as_posix())
        Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    report = {
        "created_at": datetime.now(UTC).isoformat(), "dataset_version": data["version"],
        "dataset_sha256": checksum, "annotation": data["annotation"],
        "database_backend": engine.dialect.name, "schema": schema, "split": args.split,
        "embedding_model": embedder.model_id if embedder else None,
        "dimensions": embedder.dimensions if embedder else None,
        "retrieval_strategy": "flat", "context_budget": 3000, "tiers": ["l0", "l1", "l2"],
        "jev": "disabled", "retention_cases": {"count": len(data["retention"]), "status": "labelled_not_scored"},
        "status": "running", "runs": [],
    }
    try:
        projects, refs = seed(sessions, data)
        if embedder:
            index_corpus(sessions, projects, embedder)
            with sessions() as session:
                rows = session.scalars(select(ContextIndex).join(ContextNode).where(ContextNode.status == "active", ContextNode.needs_review.is_(False))).all()
                report["index_coverage"] = {"representations": len(rows), "ready": sum(r.embedding_model == embedder.model_id for r in rows)}
            # Batch the frozen query set to avoid one network request per query.
            # Timed retrieval below is explicitly warm-cache, not cold latency.
            selected = [q for q in data["queries"] if args.split == "all" or q["split"] == args.split]
            embedder.embed([q["query"][:6000] for q in (selected[:args.limit] if args.limit else selected)])
        for mode in (("lexical", "hybrid") if args.mode == "both" else (args.mode,)):
            report["runs"].append(evaluate(sessions, data, projects, refs, mode=mode, embedder=embedder, top_k=args.top_k, split=args.split, limit=args.limit))
        report["status"] = "completed"
    except Exception as error:
        # Reports intentionally keep only the exception type, never server
        # bodies, request headers, credential values or a connection URL.
        report["status"], report["error_type"] = "incomplete", type(error).__name__
    finally:
        if embedder:
            report["embedding_budget"] = {"session": embedder.stats, "cumulative": embedder.totals(), "limits": dict(zip(("requests", "texts", "characters"), embedder.limits, strict=True))}
        engine.dispose()
        (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (output / "report.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(output), "summaries": [r["summary"] for r in report["runs"]]}))
    if report["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
