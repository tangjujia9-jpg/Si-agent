"""Scoped spans with an allowlist before persistence or optional export."""

import hashlib
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import uuid4

SAFE_FIELDS = {
    "provider",
    "model",
    "status",
    "iteration",
    "operation_id",
    "tool",
    "error_code",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
    "estimated",
    "count",
    "source",
    "probability",
    "confidence",
    "reason",
    "revision",
    "node_id",
    "job_id",
    "run_id",
    "project_id",
    "user_id",
    "session_id",
    "duration_ms",
    "attempt",
    "stage",
}
ACTIVE_TRACE = ContextVar("si_active_trace", default=None)


def safe_attributes(data):
    output = {}
    for key, value in data.items():
        if key not in SAFE_FIELDS or not isinstance(value, (str, int, float, bool, type(None))):
            continue
        if isinstance(value, str):
            # Metadata strings can still contain an accidentally copied credential.
            value = re.sub(
                r"(?i)(bearer\s+\S+|sk-[\w-]+|(?:api[_-]?key|token|password)\s*[=:]\s*\S+)",
                "[redacted]",
                value,
            )
            value = value[:240]
        output[key] = value
    return output


class Telemetry:
    def __init__(self, sink=lambda event: None, exporter=None, trace_id=None, parent_id=None):
        self.sink, self.exporter = sink, exporter
        self.trace_id = trace_id or uuid4().hex
        self.parent_id = parent_id

    @contextmanager
    def span(self, name, attributes=None):
        sid = uuid4().hex[:16]
        start = datetime.now(UTC)
        clock = time.monotonic()
        data = safe_attributes(attributes or {})
        child = Telemetry(self.sink, self.exporter, self.trace_id, sid)
        token = ACTIVE_TRACE.set(child)
        status = "succeeded"
        try:
            yield child, data
        except BaseException as exc:
            status = "failed"
            data["error_code"] = type(exc).__name__
            raise
        finally:
            ACTIVE_TRACE.reset(token)
            record = {
                "name": name,
                "span_id": sid,
                "parent_id": self.parent_id,
                "trace_id": self.trace_id,
                "started_at": start.isoformat(),
                "ended_at": datetime.now(UTC).isoformat(),
                "duration_ms": round((time.monotonic() - clock) * 1000, 3),
                "status": data.get("status", status),
                "attributes": safe_attributes(data),
            }
            try:
                self.sink(record)
            except Exception:
                pass  # observers cannot turn a successful operation into a failure
            if self.exporter:
                try:
                    self.exporter.emit(record)
                except Exception:
                    pass


@contextmanager
def observed(name, attributes=None):
    trace = ACTIVE_TRACE.get()
    if trace is None:
        yield None, {}
    else:
        with trace.span(name, attributes) as result:
            yield result


def traced_post(client, url, *, kind, model, headers, payload):
    with observed(kind, {"model": model}) as (_, attrs):
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage", {})
        attrs.update(
            input_tokens=usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            output_tokens=usage.get("completion_tokens", usage.get("output_tokens", 0)),
            total_tokens=usage.get("total_tokens", 0),
            estimated=not bool(usage),
        )
        return data


class LangfuseOTLP:
    """Own OTel provider; does not replace the process-wide tracer provider."""

    def __init__(self, endpoint, public_key, secret_key, *, processor=None):
        import base64
        from urllib.parse import urlsplit

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        target = urlsplit(endpoint)
        local = target.hostname in {"localhost", "127.0.0.1", "::1", "langfuse", "langfuse-web"}
        if (
            target.username
            or target.password
            or not target.hostname
            or target.query
            or target.fragment
            or not (target.scheme == "https" or (target.scheme == "http" and local))
        ):
            raise ValueError("Langfuse endpoint requires HTTPS outside local development")
        self.provider = TracerProvider(
            resource=Resource.create({"service.name": "si-agent"}), shutdown_on_exit=False
        )
        credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self.processor = processor or BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=endpoint.rstrip("/") + "/api/public/otel/v1/traces",
                headers={"Authorization": "Basic " + credentials},
                timeout=5,
            ),
            max_queue_size=512,
            max_export_batch_size=64,
            schedule_delay_millis=1000,
        )
        self.provider.add_span_processor(self.processor)
        self.tracer = self.provider.get_tracer("si.runtime", "1")

    def emit(self, record):
        from opentelemetry.trace import SpanContext, TraceFlags

        trace = int(hashlib.sha256(record["trace_id"].encode()).hexdigest()[:32], 16)
        parent = record["parent_id"]
        attrs = record["attributes"]
        mapped = {"si." + k: v for k, v in attrs.items() if v is not None}
        mapped.update(
            {
                "langfuse.observation.type": "generation"
                if record["name"]
                in {"llm", "summarization", "extraction", "retrieval_gate", "retention_gate"}
                else "span",
                "langfuse.trace.name": "si-project-run",
            }
        )
        for source, target in (
            ("user_id", "langfuse.user.id"),
            ("session_id", "langfuse.session.id"),
            ("model", "gen_ai.request.model"),
            ("input_tokens", "gen_ai.usage.input_tokens"),
            ("output_tokens", "gen_ai.usage.output_tokens"),
        ):
            if attrs.get(source) is not None:
                mapped[target] = attrs[source]
        start = int(datetime.fromisoformat(record["started_at"]).timestamp() * 1e9)
        end = int(datetime.fromisoformat(record["ended_at"]).timestamp() * 1e9)
        # Custom ID generator keeps the recorded parent/child IDs consistent.
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.trace import Status, StatusCode

        span = ReadableSpan(
            name=record["name"],
            context=SpanContext(trace, int(record["span_id"], 16), False, TraceFlags(1)),
            parent=SpanContext(trace, int(parent, 16), False, TraceFlags(1)) if parent else None,
            resource=self.provider.resource,
            attributes=mapped,
            start_time=start,
            end_time=end,
            status=Status(StatusCode.ERROR if record["status"] == "failed" else StatusCode.OK),
        )
        self.processor.on_end(span)

    def close(self):
        import threading

        def finish():
            self.provider.force_flush(timeout_millis=5000)
            self.provider.shutdown()

        worker = threading.Thread(target=finish, daemon=True)
        worker.start()
        worker.join(timeout=6)


def configured_exporter():
    import os

    endpoint = os.environ.get("SI_LANGFUSE_HOST", "")
    if not endpoint:
        return None
    public, secret = (
        os.environ.get("SI_LANGFUSE_PUBLIC_KEY"),
        os.environ.get("SI_LANGFUSE_SECRET_KEY"),
    )
    if not public or not secret:
        raise ValueError("configure Langfuse public and secret keys")
    try:
        return LangfuseOTLP(endpoint, public, secret)
    except ImportError as exc:
        raise RuntimeError("install the copilot-observability extra") from exc
