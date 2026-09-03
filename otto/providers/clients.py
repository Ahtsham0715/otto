"""Concrete providers: Ollama, any OpenAI-compatible endpoint, Anthropic, Gemini.

They differ only in URL shape, auth header and where the text sits in the response,
so each is a few dozen lines. Adding a vendor means adding one class and one entry
in `providers/__init__.py`.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any

from .base import Completion, Message, Provider, ProviderError, http_json


class OllamaProvider(Provider):
    """Local Ollama. Honest about latency: on a 2019 i9 expect single-digit to
    low-double-digit tokens/sec for a 3B model (see docs/RESEARCH.md §6)."""

    name = "ollama"

    def __init__(
        self,
        model: str = "qwen2.5:3b",
        base_url: str = "http://127.0.0.1:11434",
        *,
        temperature: float = 0.1,
        max_tokens: int = 800,
        timeout: float = 180.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    @property
    def is_cloud(self) -> bool:
        return False

    def complete(self, messages: list[Message], **kw: Any) -> Completion:
        started = time.time()
        data = http_json(
            f"{self.base_url}/api/chat",
            {
                "model": kw.get("model", self.model),
                "messages": [m.as_dict() for m in messages],
                "stream": False,
                "options": {
                    "temperature": kw.get("temperature", self.temperature),
                    "num_predict": kw.get("max_tokens", self.max_tokens),
                },
            },
            {},
            kw.get("timeout", self.timeout),
        )
        text = (data.get("message") or {}).get("content", "")
        return Completion(
            text=text,
            model=data.get("model", self.model),
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            latency=time.time() - started,
            provider=self.name,
        )

    def available(self) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(  # noqa: S310 - local, scheme fixed
                f"{self.base_url}/api/tags", timeout=5
            ) as response:
                body = response.read(200_000).decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return False, f"Ollama is not running at {self.base_url} ({exc})"
        if self.model.split(":")[0] not in body:
            return False, (
                f"Ollama is running but {self.model!r} is not pulled — "
                f"run: ollama pull {self.model}"
            )
        return True, f"Ollama has {self.model}"


class OpenAICompatibleProvider(Provider):
    """Anything speaking `/v1/chat/completions`: OpenAI, Groq, Cerebras, together,
    LM Studio, llama.cpp's server, vLLM."""

    name = "openai_compatible"

    def __init__(
        self,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        *,
        temperature: float = 0.1,
        max_tokens: int = 800,
        timeout: float = 90.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    @property
    def is_cloud(self) -> bool:
        lowered = self.base_url.lower()
        return not any(
            m in lowered for m in ("127.0.0.1", "localhost", "0.0.0.0", "::1")
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def complete(self, messages: list[Message], **kw: Any) -> Completion:
        started = time.time()
        data = http_json(
            f"{self.base_url}/chat/completions",
            {
                "model": kw.get("model", self.model),
                "messages": [m.as_dict() for m in messages],
                "temperature": kw.get("temperature", self.temperature),
                "max_tokens": kw.get("max_tokens", self.max_tokens),
            },
            self._headers(),
            kw.get("timeout", self.timeout),
        )
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.base_url} returned no choices")
        text = (choices[0].get("message") or {}).get("content", "")
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            model=data.get("model", self.model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency=time.time() - started,
            provider=self.name,
        )

    def available(self) -> tuple[bool, str]:
        if self.is_cloud and not self.api_key:
            return False, "no API key stored for this endpoint"
        try:
            self.complete([Message("user", "ping")], max_tokens=1, timeout=15)
        except ProviderError as exc:
            return False, str(exc)[:200]
        return True, f"{self.base_url} answered"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        base_url: str = "https://api.anthropic.com/v1",
        api_key: str = "",
        *,
        temperature: float = 0.1,
        max_tokens: int = 800,
        timeout: float = 90.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    @property
    def is_cloud(self) -> bool:
        return True

    def complete(self, messages: list[Message], **kw: Any) -> Completion:
        started = time.time()
        system = " ".join(m.content for m in messages if m.role == "system")
        turns = [m.as_dict() for m in messages if m.role != "system"]
        payload: dict[str, Any] = {
            "model": kw.get("model", self.model),
            "messages": turns,
            "max_tokens": kw.get("max_tokens", self.max_tokens),
            "temperature": kw.get("temperature", self.temperature),
        }
        if system:
            payload["system"] = system
        data = http_json(
            f"{self.base_url}/messages",
            payload,
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            kw.get("timeout", self.timeout),
        )
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            model=data.get("model", self.model),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            latency=time.time() - started,
            provider=self.name,
        )

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "no API key stored"
        try:
            self.complete([Message("user", "ping")], max_tokens=1, timeout=15)
        except ProviderError as exc:
            return False, str(exc)[:200]
        return True, "Anthropic answered"


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(
        self,
        model: str = "gemini-2.0-flash",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        api_key: str = "",
        *,
        temperature: float = 0.1,
        max_tokens: int = 800,
        timeout: float = 90.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    @property
    def is_cloud(self) -> bool:
        return True

    def complete(self, messages: list[Message], **kw: Any) -> Completion:
        started = time.time()
        system = " ".join(m.content for m in messages if m.role == "system")
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in messages
            if m.role != "system"
        ]
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": kw.get("temperature", self.temperature),
                "maxOutputTokens": kw.get("max_tokens", self.max_tokens),
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = http_json(
            f"{self.base_url}/models/{self.model}:generateContent",
            payload,
            {"x-goog-api-key": self.api_key},
            kw.get("timeout", self.timeout),
        )
        candidates = data.get("candidates") or []
        if not candidates:
            raise ProviderError("Gemini returned no candidates")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        usage = data.get("usageMetadata") or {}
        return Completion(
            text=text,
            model=self.model,
            prompt_tokens=usage.get("promptTokenCount", 0),
            completion_tokens=usage.get("candidatesTokenCount", 0),
            latency=time.time() - started,
            provider=self.name,
        )

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "no API key stored"
        try:
            self.complete([Message("user", "ping")], max_tokens=1, timeout=15)
        except ProviderError as exc:
            return False, str(exc)[:200]
        return True, "Gemini answered"
