"""Compile cited evidence using a conservative UTF-8 byte budget."""

from dataclasses import dataclass

from waku.domain.contracts import MemoryHit


def estimated_tokens(text: str) -> int:
    # A deliberately conservative model-independent estimate. The future
    # Provider adapter must also check its own tokenizer/context window.
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class CompiledContext:
    hits: tuple[MemoryHit, ...]
    text: str
    estimated_tokens: int
    truncated: bool


def compile_context(hits: list[MemoryHit], max_tokens: int) -> CompiledContext:
    from dataclasses import replace

    if max_tokens < 1:
        raise ValueError("context budget must be positive")
    blocks, kept = [], []
    truncated = False
    for hit in hits:
        revision = f", revision: {hit.revision}" if hit.revision is not None else ""
        header = (
            f"{hit.citation()} ({hit.tier}{revision}, evidence: {', '.join(hit.evidence_ids)})\n"
        )
        prefix = "\n\n" if blocks else ""
        remaining = max_tokens - estimated_tokens("\n\n".join(blocks))
        available = remaining - estimated_tokens(prefix + header)
        if available <= 0:
            truncated = True
            continue
        raw = hit.snippet.encode("utf-8")
        snippet = raw[:available].decode("utf-8", errors="ignore")
        if not snippet.strip():
            truncated = True
            continue
        truncated |= len(raw) > available
        kept.append(
            replace(
                hit,
                snippet=snippet,
                end_char=hit.start_char + len(snippet)
                if hit.start_char is not None
                else hit.end_char,
            )
        )
        blocks.append(header + snippet)
    text = "\n\n".join(blocks)
    return CompiledContext(tuple(kept), text, estimated_tokens(text), truncated)
