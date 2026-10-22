"""Bounded model projections; the operation table retains the complete result."""

import json


def project_result(result):
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text.encode()) <= 6000:
        return result
    data = result.get("data", {})
    if data.get("result_page"):
        # Dependency metadata is consumed by observe_result, not repeated in the model's page.
        return {**result, "context_projected": True, "data": {k: data[k] for k in (
            "resource_id", "content", "offset", "next_offset", "total_characters"
        )}}
    ids = set(data.get("evidence_ids", []))
    for hit in data.get("hits", []):
        ids.update(hit.get("evidence_ids", []))
    return {**result, "context_projected": True, "data": {
        "message": "Full output stored; use read_operation_result with this operation_id",
        "resource_id": result.get("operation_id"),
        "preview": json.dumps(data, ensure_ascii=False, default=str)[:800],
        "evidence_ids": sorted(ids), "source_nodes": data.get("source_nodes", []),
    }}
