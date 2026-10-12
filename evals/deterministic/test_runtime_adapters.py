"""Provider wire formats, real optional SDK schemas and span export mapping."""

import json
from types import SimpleNamespace as NS

import httpx
import pytest

from waku.providers.adapters import AnthropicProvider, OpenAIProvider
from waku.providers.contracts import ModelRef, ModelRequest
from waku.runtime.decisions import JevDecisions, safe_decision
from waku.runtime.telemetry import LangfuseOTLP, Telemetry


def conversation():
    return [
        {"role": "user", "content": "Read source"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call1", "name": "read_memory", "arguments": {"uri": "si://source"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call1", "content": "evidence"},
    ]


def test_openai_and_anthropic_handoff_preserves_tool_identity():
    import anthropic
    import openai

    captured = []

    def respond(request):
        payload = json.loads(request.content)
        captured.append(payload)
        if "anthropic" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude",
                    "content": [{"type": "text", "text": "done"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 4, "output_tokens": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "response",
                "object": "chat.completion",
                "created": 1,
                "model": "model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "done"},
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            },
        )

    transport = httpx.MockTransport(respond)
    request = ModelRequest(messages=conversation(), system="system", max_tokens=50)
    with openai.OpenAI(api_key="fake", http_client=httpx.Client(transport=transport)) as client:
        result = OpenAIProvider(client).complete(ModelRef("openai", "model"), request)
    with anthropic.Anthropic(
        api_key="fake", http_client=httpx.Client(transport=transport)
    ) as client:
        other = AnthropicProvider(client).complete(ModelRef("anthropic", "claude"), request)
    assert result.text == other.text == "done"
    assert result.usage.total_tokens == other.usage.total_tokens == 6
    assert captured[0]["messages"][-1]["tool_call_id"] == "call1"
    assert captured[1]["messages"][-1]["content"][0]["tool_use_id"] == "call1"
    assert captured[1]["messages"][-2]["content"][0]["input"] == {"uri": "si://source"}
    assert json.dumps(conversation())  # Canonical context remains serializable across providers.


def test_openai_compatible_fragmented_stream_and_usage():
    import openai

    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "content": "Reading ",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call1",
                                "type": "function",
                                "function": {"name": "read_memory", "arguments": '{"uri":'},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '"si://source"}'}}]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}},
    ]

    def respond(request):
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 20 and "max_completion_tokens" not in payload
        stream = (
            "".join(
                "data: "
                + json.dumps(
                    {
                        "id": "stream",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "local",
                        **c,
                    }
                )
                + "\n\n"
                for c in chunks
            )
            + "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=stream, headers={"Content-Type": "text/event-stream"})

    with openai.OpenAI(
        api_key="fake", http_client=httpx.Client(transport=httpx.MockTransport(respond))
    ) as client:
        events = list(
            OpenAIProvider(client).stream(
                ModelRef("openai-compatible", "local"),
                ModelRequest(messages=conversation(), max_tokens=20),
            )
        )
    call = next(e.data for e in events if e.kind == "tool_call")
    assert call == {"id": "call1", "name": "read_memory", "arguments": {"uri": "si://source"}}
    assert next(e.data for e in events if e.kind == "usage")["total_tokens"] == 8


def test_jev_real_sdk_choice_schema_and_conservative_uncertainty():
    sdk = pytest.importorskip("typesafe_sdk")
    import httpx2

    captured = []

    def response(request):
        captured.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "model": "jev",
                "usage": {"input_tokens": 24, "output_tokens": 0},
                "answers": {
                    "decision": {
                        "type": "choice",
                        "choice": "no",
                        "confidence": 0.95,
                        "probabilities": {"yes": 0.1, "no": 0.9},
                    }
                },
            },
        )

    with sdk.TypeSafeClient(
        api_key="test",
        transport=httpx2.MockTransport(response),
        retry=sdk.RetryPolicy(max_retries=0),
    ) as client:
        result = JevDecisions(client).decide("retrieve", {"text": "2+2"})
    assert not result.value and result.usage.input_tokens == 24
    assert captured[0]["questions"]["decision"]["type"] == "choice"
    uncertain = NS(
        system_one=lambda **k: NS(
            choices={"decision": NS(probabilities={"yes": 0.1}, confidence=0.2)},
            usage=NS(input_tokens=10, output_tokens=0),
        )
    )
    assert JevDecisions(uncertain).decide("retain", {"text": "a guess"}).value
    failure = NS(decide=lambda *a: (_ for _ in ()).throw(TimeoutError("secret")))
    decision = safe_decision(failure, "retrieve", {})
    assert decision.value and decision.reason == "TimeoutError"


def test_nested_spans_are_redacted_before_both_sinks_and_fail_open():
    records, exported = [], []
    trace = Telemetry(records.append, NS(emit=exported.append))
    with (
        trace.span("run", {"run_id": "r", "prompt": "private", "model": "sk-PRIVATE"}) as (
            child,
            _,
        ),
        pytest.raises(ValueError),
        child.span("tool", {"password": "secret"}),
    ):
        raise ValueError("sensitive payload")
    assert records == exported
    assert "private" not in json.dumps(records) and "PRIVATE" not in json.dumps(records)
    assert records[0]["parent_id"] == records[1]["span_id"]
    assert records[0]["status"] == "failed"
    bad = Telemetry(
        lambda e: (_ for _ in ()).throw(OSError()),
        NS(emit=lambda e: (_ for _ in ()).throw(OSError())),
    )
    with bad.span("run"):
        pass


def test_langfuse_otlp_parentage_generation_usage_and_duration():
    pytest.importorskip("opentelemetry.sdk")

    class Processor:
        def __init__(self):
            self.spans = []

        def on_end(self, span):
            self.spans.append(span)

        def force_flush(self, timeout_millis=0):
            return True

        def shutdown(self):
            pass

    processor = Processor()
    exporter = LangfuseOTLP("http://localhost:3000", "public", "secret", processor=processor)
    trace = Telemetry(exporter=exporter)
    with (
        trace.span("run", {"user_id": "me", "session_id": "thread"}) as (child, _),
        child.span("llm", {"model": "model", "input_tokens": 12, "output_tokens": 4}),
    ):
        pass
    exporter.close()
    generation, root = processor.spans
    assert generation.parent.span_id == root.context.span_id
    assert generation.context.trace_id == root.context.trace_id
    assert generation.attributes["langfuse.observation.type"] == "generation"
    assert generation.attributes["gen_ai.usage.input_tokens"] == 12
    assert generation.start_time <= generation.end_time


def test_optional_langgraph_interrupt_resume_does_not_repeat_advance():
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from waku.workflows.langgraph import build_workflow

    calls, approvals = [], []

    def advance(state):
        calls.append(state["run_id"])
        return {"status": "succeeded" if approvals else "waiting_approval", "operation_id": "op"}

    graph = build_workflow(
        advance, lambda op, approved: approvals.append((op, approved)), InMemorySaver()
    )
    cfg = {"configurable": {"thread_id": "test-thread"}}
    paused = graph.invoke({"run_id": "run"}, cfg)
    assert paused["__interrupt__"] and calls == ["run"]
    done = graph.invoke(Command(resume={"approved": True}), cfg)
    assert done["status"] == "succeeded"
    assert approvals == [("op", True)] and len(calls) == 2


def test_model_catalog_is_serializable_and_never_exposes_credentials():
    from waku.providers.catalog import CredentialResolver, configured_catalog

    values = {
        "SI_OPENAI_MODEL": "configured-model",
        "SI_OPENAI_API_KEY": "sk-private",
        "SI_OPENAI_CONTEXT_WINDOW": "16000",
        "SI_OPENAI_TOOL_CALLING": "false",
    }
    catalog = configured_catalog(values)
    entry = catalog.get(ModelRef("openai", "configured-model"))
    assert entry.capabilities.context_window == 16000 and not entry.capabilities.tool_calling
    assert "private" not in json.dumps(catalog.public())
    assert CredentialResolver(values).resolve("openai") == ("sk-private", None)


@pytest.mark.parametrize(
    "host",
    [
        "http://localhost.evil",
        "http://langfuse.evil",
        "http://remote:3000",
        "https://user:secret@host",
    ],
)
def test_langfuse_rejects_insecure_remote_or_embedded_credentials(host):
    pytest.importorskip("opentelemetry.sdk")
    with pytest.raises(ValueError):
        LangfuseOTLP(host, "public", "secret")


def test_actual_otlp_http_encoding_auth_and_parent_ids():
    pytest.importorskip("opentelemetry.sdk")
    import base64
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    received = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            parsed = ExportTraceServiceRequest.FromString(body)
            received.append((self.path, self.headers["Authorization"], parsed))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        exporter = LangfuseOTLP(f"http://127.0.0.1:{server.server_port}", "public", "secret")
        trace = Telemetry(exporter=exporter)
        with (
            trace.span("run", {"session_id": "thread"}) as (child, _),
            child.span("llm", {"model": "model", "prompt": "private", "input_tokens": 12}),
        ):
            pass
        exporter.close()
        assert received
        path, auth, payload = received[0]
        assert path == "/api/public/otel/v1/traces"
        assert auth == "Basic " + base64.b64encode(b"public:secret").decode()
        spans = [s for r in payload.resource_spans for scope in r.scope_spans for s in scope.spans]
        generation = next(s for s in spans if s.name == "llm")
        root = next(s for s in spans if s.name == "run")
        assert generation.parent_span_id == root.span_id and generation.trace_id == root.trace_id
        assert generation.end_time_unix_nano >= generation.start_time_unix_nano
        assert "private" not in str(payload)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
