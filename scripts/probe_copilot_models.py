"""Opt-in synthetic probes for a configured compatible provider and embedder.

Only requested probes make external calls. Output never includes credentials,
endpoint response bodies on errors, or data from existing user projects.
"""

import argparse
import base64
import json
import os
import struct
import time
import zlib
from dataclasses import asdict
from pathlib import Path


def blue_png():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress((b"\0" + bytes([0, 0, 255]) * 32) * 32)) + chunk(b"IEND", b"")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chat", action="store_true")
    parser.add_argument("--tools", action="store_true")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--vision", action="store_true")
    parser.add_argument("--embedding", action="store_true")
    parser.add_argument("--enrichment", action="store_true")
    args = parser.parse_args()
    if not any((args.chat, args.tools, args.stream, args.vision, args.embedding, args.enrichment)):
        parser.error("select at least one explicit probe")
    if not args.config.is_file():
        parser.error("configuration not found")
    if args.output.exists():
        parser.error("output exists; use a new path to preserve previous evidence")
    from dotenv import dotenv_values

    os.environ.update({k: v for k, v in dotenv_values(args.config).items() if k.startswith("SI_") and v is not None})
    from waku.providers.adapters import configured_provider
    from waku.providers.contracts import ModelRef, ModelRequest

    model = os.environ.get("SI_COMPATIBLE_MODEL")
    if not model:
        parser.error("configure SI_COMPATIBLE_MODEL")
    ref = ModelRef("openai-compatible", model)
    provider = configured_provider(ref.provider, model, timeout=45)
    report = {"model": model, "probes": {}, "source": "synthetic_inputs_only"}

    def run(name, call):
        start = time.perf_counter()
        try:
            value = call()
            report["probes"][name] = {**value, "latency_ms": round((time.perf_counter() - start) * 1000, 2)}
        except Exception as error:
            report["probes"][name] = {"status": "failed", "error_type": type(error).__name__, "http_status": getattr(error, "status_code", None)}

    def complete(messages, tools=()):
        return provider.complete(ref, ModelRequest(messages=messages, tools=tools, max_tokens=1024, temperature=0, metadata={"timeout_seconds": 45}))

    def chat():
        result = complete([{"role": "user", "content": "Reply with the English word ready."}])
        return {"status": "passed" if "ready" in result.text.lower() else "unverified", "text": result.text, "usage": asdict(result.usage)}

    def tools():
        tool = {"name": "record_probe", "description": "Record a harmless test value.", "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False}}
        messages = [{"role": "user", "content": "Call record_probe with value 7. After receiving the tool result, reply with the returned value."}]
        result = complete(messages, [tool])
        valid = next((c for c in result.tool_calls if c["name"] == "record_probe" and c["arguments"] == {"value": 7}), None)
        if not valid:
            return {"status": "unverified", "stop_reason": result.stop_reason, "tool_calls": list(result.tool_calls)}
        messages += [{"role": "assistant", "content": result.text, "tool_calls": list(result.tool_calls)}]
        messages += [{"role": "tool", "tool_call_id": c["id"], "content": json.dumps({"value": 7})} for c in result.tool_calls]
        final = complete(messages, [tool])
        return {"status": "passed" if "7" in final.text else "unverified", "tool_calls": list(result.tool_calls), "final": final.text, "usage": [asdict(result.usage), asdict(final.usage)]}

    def stream():
        events = list(provider.stream(ref, ModelRequest(messages=[{"role": "user", "content": "Reply with the English word ready."}], max_tokens=256, temperature=0)))
        return {"status": "passed" if any(e.kind == "text_delta" for e in events) and events[-1].kind == "done" else "unverified", "events": [asdict(e) for e in events]}

    def vision():
        message = [{"type": "text", "text": "What is the dominant color? Answer with one English color word."}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(blue_png()).decode()}}]
        result = complete([{"role": "user", "content": message}])
        return {"status": "passed" if "blue" in result.text.lower() else "unverified", "text": result.text, "usage": asdict(result.usage)}

    def embedding():
        from waku.memory.embeddings import configured_embedder, validate_vectors

        embedder = configured_embedder()
        if not embedder or embedder.model_id.startswith("demo:"):
            raise ValueError("real embedding configuration required")
        vectors = validate_vectors(embedder.embed(["Si-agent uses PostgreSQL and pgvector."]), 1, embedder.dimensions)
        return {"status": "passed", "dimensions": len(vectors[0]), "model_id": embedder.model_id, "inputs": 1}

    def enrichment():
        from waku.memory.extraction import configured_extractor
        from waku.memory.summaries import CompatibleSummarizer

        source = "Si-agent的主数据库确定使用PostgreSQL和pgvector，每个项目的资料与记忆必须隔离。"
        extractor = configured_extractor()
        if extractor is None:
            raise ValueError("real extraction model required")
        proposals = extractor.extract(source, lambda: None)
        summary = CompatibleSummarizer(os.environ["SI_COMPATIBLE_BASE_URL"], model, os.environ.get("SI_COMPATIBLE_API_KEY", "")).summarize(source, lambda: None)
        return {"status": "passed" if proposals else "unverified", "candidates": proposals, "summary": summary}

    try:
        for name, enabled, call in (("chat", args.chat, chat), ("tools", args.tools, tools), ("stream", args.stream, stream), ("vision", args.vision, vision), ("embedding", args.embedding, embedding), ("enrichment", args.enrichment, enrichment)):
            if enabled:
                run(name, call)
                print(json.dumps({"probe": name, "status": report["probes"][name]["status"]}))
    finally:
        provider.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(p["status"] != "passed" for p in report["probes"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
