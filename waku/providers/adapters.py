"""Native SDK adapters with one JSON message contract; credentials are injected."""

import json
from dataclasses import asdict

from waku.providers.contracts import (
    ModelCapabilities,
    ModelRef,
    ModelResponse,
    StreamEvent,
    Usage,
)


class OpenAIProvider:
    def __init__(self, client, models=(), catalog=None):
        self.client, self.models = client, tuple(models)
        self.catalog = catalog

    def close(self):
        self.client.close()

    def list_models(self):
        return self.models

    def capabilities(self, model):
        entry = self.catalog.get(model) if self.catalog else None
        return entry.capabilities if entry else ModelCapabilities(tool_calling=True, streaming=True)

    def _arguments(self, model, request):
        messages = [{"role": "system", "content": request.system}] if request.system else []
        for item in request.messages:
            converted = {"role": item["role"], "content": item.get("content", "")}
            if item.get("tool_calls"):
                converted["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                        },
                    }
                    for c in item["tool_calls"]
                ]
            if item["role"] == "tool":
                converted["tool_call_id"] = item["tool_call_id"]
            messages.append(converted)
        args = {
            "model": model.model,
            "messages": messages,
            "max_completion_tokens": request.max_tokens,
        }
        if model.provider == "openai-compatible":
            args["max_tokens"] = args.pop("max_completion_tokens")
        if request.tools:
            args["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in request.tools
            ]
        if request.temperature is not None:
            args["temperature"] = request.temperature
        if "timeout_seconds" in request.metadata:
            args["timeout"] = request.metadata["timeout_seconds"]
        return args

    def complete(self, model, request):
        result = self.client.chat.completions.create(**self._arguments(model, request))
        choice = result.choices[0]
        calls = []
        for call in choice.message.tool_calls or []:
            arguments = json.loads(call.function.arguments)
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be an object")
            calls.append({"id": call.id, "name": call.function.name, "arguments": arguments})
        usage = result.usage
        return ModelResponse(
            choice.message.content or "",
            calls,
            choice.finish_reason or "stop",
            Usage(usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
            if usage
            else Usage(),
        )

    def stream(self, model, request):
        yield StreamEvent("start", {"model": model.model})
        pending, usage, stop = {}, Usage(), "stop"
        with self.client.chat.completions.create(
            **self._arguments(model, request),
            stream=True,
            stream_options={"include_usage": True},
        ) as events:
            for event in events:
                if event.usage:
                    u = event.usage
                    usage = Usage(u.prompt_tokens, u.completion_tokens, u.total_tokens)
                for choice in event.choices:
                    if choice.delta.content:
                        yield StreamEvent("text_delta", {"text": choice.delta.content})
                    for call in choice.delta.tool_calls or []:
                        item = pending.setdefault(
                            call.index, {"id": "", "name": "", "arguments": ""}
                        )
                        item["id"] += call.id or ""
                        if call.function:
                            item["name"] += call.function.name or ""
                            item["arguments"] += call.function.arguments or ""
                    stop = choice.finish_reason or stop
        for item in pending.values():
            item["arguments"] = json.loads(item["arguments"])
            if not isinstance(item["arguments"], dict):
                raise TypeError("tool arguments must be an object")
            yield StreamEvent("tool_call", item)
        yield StreamEvent("usage", asdict(usage))
        yield StreamEvent("done", {"stop_reason": stop})


class AnthropicProvider(OpenAIProvider):
    def _arguments(self, model, request):
        messages = []
        for item in request.messages:
            if item["role"] == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": item["tool_call_id"],
                    "content": item["content"],
                }
                if (
                    messages
                    and messages[-1]["role"] == "user"
                    and isinstance(messages[-1]["content"], list)
                ):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
            elif item.get("tool_calls"):
                blocks = [{"type": "text", "text": item["content"]}] if item.get("content") else []
                blocks += [
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                    for c in item["tool_calls"]
                ]
                messages.append({"role": "assistant", "content": blocks})
            else:
                messages.append({"role": item["role"], "content": item.get("content", "")})
        args = {
            "model": model.model,
            "system": request.system,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            args["tools"] = list(request.tools)
        if request.temperature is not None:
            args["temperature"] = request.temperature
        if "timeout_seconds" in request.metadata:
            args["timeout"] = request.metadata["timeout_seconds"]
        return args

    def _response(self, result):
        text = "".join(b.text for b in result.content if b.type == "text")
        calls = [
            {"id": b.id, "name": b.name, "arguments": b.input}
            for b in result.content
            if b.type == "tool_use"
        ]
        u = result.usage
        return ModelResponse(
            text,
            calls,
            result.stop_reason,
            Usage(u.input_tokens, u.output_tokens, u.input_tokens + u.output_tokens),
        )

    def complete(self, model, request):
        return self._response(self.client.messages.create(**self._arguments(model, request)))

    def stream(self, model, request):
        yield StreamEvent("start", {"model": model.model})
        with self.client.messages.stream(**self._arguments(model, request)) as stream:
            for text in stream.text_stream:
                yield StreamEvent("text_delta", {"text": text})
            response = self._response(stream.get_final_message())
        for call in response.tool_calls:
            yield StreamEvent("tool_call", call)
        yield StreamEvent("usage", asdict(response.usage))
        yield StreamEvent("done", {"stop_reason": response.stop_reason})


class DemoProvider:
    """Explicitly offline: cites supplied evidence without claiming model reasoning."""

    def capabilities(self, model):
        return ModelCapabilities(tool_calling=True, streaming=True, context_window=32768)

    def list_models(self):
        return [ModelRef("demo", "evidence-demo")]

    def complete(self, model, request):
        import re

        cites = re.findall(r"\[si://[^\]]+\]", request.system.partition("\nEvidence:\n")[2])
        answer = "Offline demo. Retrieved project evidence: " + " ".join(dict.fromkeys(cites))
        if not cites:
            answer = "Offline demo. No project evidence was retrieved. Configure a provider for generated answers."
        # No provider call happened; the runtime reservation is the estimate.
        return ModelResponse(answer)

    def stream(self, model, request):
        result = self.complete(model, request)
        yield StreamEvent("start", {"model": model.model})
        yield StreamEvent("text_delta", {"text": result.text})
        yield StreamEvent("usage", asdict(result.usage))
        yield StreamEvent("done", {"stop_reason": "stop"})


def estimated_size(request):
    # Upper bound for ordinary UTF-8 tokenizers, plus message framing reserve.
    return (
        len(
            json.dumps(
                {"messages": request.messages, "system": request.system, "tools": request.tools},
                ensure_ascii=False,
            ).encode()
        )
        + 128
    )


def configured_provider(provider, model, *, timeout=20):
    import os

    from waku.providers.catalog import CredentialResolver, configured_catalog

    ref = ModelRef(provider, model)
    if provider == "demo":
        return DemoProvider()
    if provider not in {"openai", "anthropic", "openai-compatible"}:
        raise ValueError("unsupported provider")
    key, url = CredentialResolver(os.environ).resolve(provider)
    catalog = configured_catalog(os.environ)
    if provider == "anthropic":
        import anthropic

        return AnthropicProvider(
            anthropic.Anthropic(api_key=key, base_url=url, timeout=timeout, max_retries=0),
            [ref],
            catalog,
        )
    if provider in ("openai", "openai-compatible"):
        import openai

        return OpenAIProvider(
            openai.OpenAI(api_key=key or "local", base_url=url, timeout=timeout, max_retries=0),
            [ref],
            catalog,
        )
    raise ValueError("unsupported provider")
