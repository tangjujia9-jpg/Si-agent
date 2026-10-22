"""Validate and load derived context under the caller's project/thread locks."""

from sqlalchemy import select

from waku.runtime.compaction import POLICY, fingerprint
from waku.storage.models import ContextNode, ContextSummary, MemoryEvidence, Message


def dependencies_valid(session, project_id, dependencies):
    for ref in dependencies.get("source_nodes", []):
        node = session.get(ContextNode, ref["id"])
        if (not node or node.project_id != project_id or node.status != "active"
                or node.needs_review or node.revision != ref["revision"]):
            return False
    for eid in dependencies.get("evidence_ids", []):
        evidence = session.get(MemoryEvidence, eid)
        node = session.get(ContextNode, evidence.node_id) if evidence else None
        if (not node or node.project_id != project_id or node.status != "active"
                or node.needs_review or evidence.redacted or node.revision != evidence.revision):
            return False
    for ref in dependencies.get("message_refs", []):
        message = session.get(Message, ref["id"])
        if (not message or message.project_id != project_id or message.event_data.get("redacted")
                or fingerprint(message.content) != ref["hash"]):
            return False
    return True


def latest_summary(session, thread):
    rows = session.scalars(select(ContextSummary).where(
        ContextSummary.thread_id == thread.id, ContextSummary.project_id == thread.project_id,
        ContextSummary.status == "valid", ContextSummary.policy_version == POLICY,
    ).order_by(ContextSummary.covered_sequence.desc(), ContextSummary.created_at.desc()))
    for row in rows:
        if dependencies_valid(session, thread.project_id, row.dependencies):
            return row
        row.status = "invalid"
    return None


def summary_state(row):
    return {"id": row.id, "run_id": row.run_id, "body": row.body, "covered_sequence": row.covered_sequence,
            "dependencies": row.dependencies, "policy_version": row.policy_version}
