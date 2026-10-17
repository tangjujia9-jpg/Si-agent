"""Product memory entry point; repositories do not decide acceptance policy."""

from sqlalchemy import select

from waku.memory.candidates import marker_proposals, queue_candidate, submit_candidate
from waku.memory.postgres import PostgresMemory
from waku.runtime.decisions import safe_decision
from waku.runtime.telemetry import observed
from waku.storage.models import ContextNode, MemoryEvidence, Project


def prepare_proposals(content, extractor, decisions, heartbeat, context=None):
    """Generate grounded proposals and classify retention outside transactions."""
    if extractor and context and hasattr(extractor, "extract_context"):
        proposals = extractor.extract_context(content, heartbeat, context)[:100]
    else:
        proposals = (
            extractor.extract(content, heartbeat) if extractor else marker_proposals(content)
        )[:100]
    for proposal in proposals:
        if (
            proposal.get("kind") not in {"semantic", "episodic", "procedural"}
            or not proposal.get("quote")
            or proposal["quote"] not in content
            or not proposal.get("content", "").strip()
        ):
            raise ValueError("proposal requires source-grounded content")
        if decisions:
            heartbeat()
            proposal["retention_decision"] = retention_decision(
                decisions, proposal["content"], proposal["quote"]
            )
    return proposals


def retention_decision(decisions, content, quote):
    with observed("retention_gate") as (_, attrs):
        decision = safe_decision(decisions, "retain", {"text": content, "source": quote})
        attrs.update(
            source=decision.source,
            probability=decision.probability,
            confidence=decision.confidence,
            input_tokens=decision.usage.input_tokens,
            output_tokens=decision.usage.output_tokens,
            total_tokens=decision.usage.total_tokens,
        )
        if getattr(decisions, "model", None):
            attrs["model"] = decisions.model
        return decision


class MemoryService:
    """API, worker and tools share scoped reads and explicit proposal semantics."""

    def __init__(self, sessions, embedder=None):
        self.sessions = sessions
        self.repository = PostgresMemory(sessions, embedder)
        self.embedder = embedder

    def search(self, query):
        return self.repository.search(query)

    def retrieve(self, query):
        return self.repository.retrieve(query)

    def get(self, uri, *, user_id="default"):
        return self.repository.get(uri, user_id=user_id)

    def list_children(self, uri, *, user_id="default"):
        return self.repository.list_children(uri, user_id=user_id)

    def forget(self, uri, *, user_id="default", reason="user_request", expected_revision=None):
        return self.repository.forget(
            uri, user_id=user_id, reason=reason, expected_revision=expected_revision
        )

    @staticmethod
    def submit_in_session(session, project, payload, *, accepted=False):
        # Explicit API mutations already carry the caller's requested action.
        # Automatically extracted and tool-proposed facts remain pending.
        return submit_candidate(session, project, payload, queue=accepted)

    @staticmethod
    def accept_in_session(session, candidate):
        queue_candidate(session, candidate)
        return candidate

    def propose(self, payload, *, user_id, project_id, decisions=None):
        """Tool proposals use the same retention policy as document extraction."""
        with self.sessions() as session:
            project = session.get(Project, project_id)
            if not project or project.user_id != user_id:
                raise ValueError("invalid scope")
            quotes = []
            for eid in payload["evidence_ids"]:
                evidence = session.get(MemoryEvidence, eid)
                source = session.get(ContextNode, evidence.node_id) if evidence else None
                if (
                    not source
                    or source.project_id != project_id
                    or evidence.redacted
                    or source.status != "active"
                    or source.needs_review
                    or evidence.revision != source.revision
                ):
                    raise ValueError("invalid evidence")
                quotes.append(evidence.quote)
        decision = None
        if decisions:
            decision = retention_decision(decisions, payload["content"], "\n".join(quotes))
        with self.sessions.begin() as session:
            project = session.scalar(
                select(Project)
                .where(Project.id == project_id, Project.user_id == user_id)
                .with_for_update()
            )
            if not project:
                raise ValueError("invalid scope")
            candidate = self.submit_in_session(session, project, payload)
            if decision and candidate.status == "pending":
                candidate.reason = f"retention:{decision.source};{decision.reason}"[:240]
                if not decision.value and decision.confidence >= 0.8:
                    candidate.status = "skipped"
            return {
                "candidate_id": candidate.id,
                "status": candidate.status,
                "message": "Proposal recorded; accepted memory requires review",
            }
