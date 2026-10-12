"""Backend-owned model configuration and credential lookup, without network discovery."""

from dataclasses import asdict, dataclass, field

from waku.providers.contracts import ModelCapabilities, ModelRef


@dataclass(frozen=True)
class ModelEntry:
    ref: ModelRef
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None


class ModelCatalog:
    def __init__(self, entries=()):
        self.entries = {(e.ref.provider, e.ref.model): e for e in entries}

    def get(self, ref):
        return self.entries.get((ref.provider, ref.model))

    def public(self):
        return [asdict(entry) for entry in self.entries.values()]


class CredentialResolver:
    def __init__(self, values):
        # The composition root passes environment values. No filesystem or keyring scan.
        self.values = values

    def resolve(self, provider):
        prefix = {
            "openai": "SI_OPENAI",
            "anthropic": "SI_ANTHROPIC",
            "openai-compatible": "SI_COMPATIBLE",
        }[provider]
        key = self.values.get(prefix + "_API_KEY", "")
        base = self.values.get(prefix + "_BASE_URL")
        if provider != "openai-compatible" and not key:
            raise ValueError(prefix + "_API_KEY is required")
        if provider == "openai-compatible" and not base:
            raise ValueError("SI_COMPATIBLE_BASE_URL is required")
        return key or "local", base


def configured_catalog(values):
    entries = [
        ModelEntry(
            ModelRef("demo", "evidence-demo"),
            ModelCapabilities(tool_calling=True, streaming=True, context_window=32768),
        )
    ]
    for provider, prefix in (
        ("openai", "SI_OPENAI"),
        ("anthropic", "SI_ANTHROPIC"),
        ("openai-compatible", "SI_COMPATIBLE"),
    ):
        model = values.get(prefix + "_MODEL")
        if model:
            entries.append(
                ModelEntry(
                    ModelRef(provider, model),
                    ModelCapabilities(
                        tool_calling=values.get(prefix + "_TOOL_CALLING", "true") == "true",
                        streaming=values.get(prefix + "_STREAMING", "true") == "true",
                        context_window=int(values.get(prefix + "_CONTEXT_WINDOW", "8192")),
                    ),
                )
            )
    return ModelCatalog(entries)
