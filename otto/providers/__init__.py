"""Provider factory.

`build_provider` turns a `ProviderConfig` into a client, fetching the API key from
the secret store by *name* — the key itself never appears in the config file, in the
repo, or in a log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import (
    Completion,
    Message,
    MockProvider,
    NoModelConfigured,
    NullProvider,
    Provider,
    ProviderError,
    parse_json_object,
)

if TYPE_CHECKING:  # keeps import cost off the start-up path
    from ..config import ProviderConfig
    from ..security.secrets import SecretStore

#: Well-known OpenAI-compatible endpoints, so SETUP.md can name them and the user
#: only has to paste a key.
KNOWN_ENDPOINTS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "together": "https://api.together.xyz/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
    "llamacpp": "http://127.0.0.1:8080/v1",
}


def build_provider(config: "ProviderConfig", secrets: "SecretStore | None" = None) -> Provider:
    kind = (config.kind or "none").lower()
    if kind in ("", "none"):
        return NullProvider()
    if kind == "mock":
        return MockProvider(model=config.model or "mock-1")

    api_key = ""
    if config.api_key_name and secrets is not None:
        api_key = secrets.get(config.api_key_name) or ""

    from .clients import (
        AnthropicProvider,
        GeminiProvider,
        OllamaProvider,
        OpenAICompatibleProvider,
    )

    if kind == "ollama":
        return OllamaProvider(
            model=config.model or "qwen2.5:3b",
            base_url=config.base_url or "http://127.0.0.1:11434",
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout,
        )
    if kind == "anthropic":
        return AnthropicProvider(
            model=config.model or "claude-sonnet-4-5",
            base_url=config.base_url or "https://api.anthropic.com/v1",
            api_key=api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout,
        )
    if kind == "gemini":
        return GeminiProvider(
            model=config.model or "gemini-2.0-flash",
            base_url=config.base_url
            or "https://generativelanguage.googleapis.com/v1beta",
            api_key=api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout,
        )
    if kind in ("openai_compatible", "openai", "groq", "cerebras", "together",
                "openrouter", "lmstudio", "llamacpp"):
        base = config.base_url or KNOWN_ENDPOINTS.get(kind, KNOWN_ENDPOINTS["openai"])
        return OpenAICompatibleProvider(
            model=config.model or "llama-3.3-70b-versatile",
            base_url=base,
            api_key=api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout,
        )
    raise ProviderError(f"unknown provider kind {config.kind!r}")


__all__ = [
    "Completion",
    "KNOWN_ENDPOINTS",
    "Message",
    "MockProvider",
    "NoModelConfigured",
    "NullProvider",
    "Provider",
    "ProviderError",
    "build_provider",
    "parse_json_object",
]
