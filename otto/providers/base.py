"""Provider abstraction.

Otto never hardcodes a vendor. A provider takes messages and returns text; that is
the whole contract. Each agent picks a *tier* (`fast` or `strong`) and the config
maps tiers to endpoints, which is what makes the recommended hybrid — small local
model for simple steps, fast cloud model for planning — a configuration change
rather than a code change.

HTTP is `urllib` from the standard library (DECISIONS D-22): one less wheel to break
on Intel macOS.
"""

from __future__ import annotations

import abc
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class ProviderError(Exception):
    """The model could not be reached, or refused."""


class NoModelConfigured(ProviderError):
    """No provider is set up. This is the user's state on first run, and it is a
    normal, recoverable condition — never a crash."""


@dataclass
class Message:
    role: str  # system | user | assistant
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class Completion:
    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency: float = 0.0
    provider: str = ""

    def json_object(self) -> dict[str, Any]:
        """Parse the reply as a JSON object, tolerating the fences small models add.

        This is *not* parsing orchestration state out of prose — the caller still
        validates every field against the real roster (see agentloop/planner.py).
        It only copes with ````json` wrappers and leading chatter.
        """
        return parse_json_object(self.text)


def parse_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw.strip("`")
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    candidate = raw[start : end + 1]
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"reply was not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("reply was JSON but not an object")
    return value


class Provider(abc.ABC):
    """The whole interface."""

    name: str = "provider"
    model: str = ""

    @abc.abstractmethod
    def complete(self, messages: list[Message], **kw: Any) -> Completion: ...

    @abc.abstractmethod
    def available(self) -> tuple[bool, str]:
        """(reachable, human-readable detail). Never raises."""

    @property
    def is_cloud(self) -> bool:
        return False

    def describe(self) -> str:
        return f"{self.name}:{self.model}" if self.model else self.name


class NullProvider(Provider):
    """What you get when nothing is configured.

    It does not pretend. Every call raises `NoModelConfigured`, which the supervisor
    catches and turns into a plain-language explanation plus the rule-based fast path.
    """

    name = "none"

    def complete(self, messages: list[Message], **kw: Any) -> Completion:
        raise NoModelConfigured(
            "No language model is configured. Otto can still run simple commands "
            "like 'open Safari' or 'create a folder on my Desktop'. Run ./setup.sh "
            "to add a local model with Ollama, or paste a Groq/Cerebras API key in "
            "Settings for a fast cloud model."
        )

    def available(self) -> tuple[bool, str]:
        return False, "no model configured"


@dataclass
class MockProvider(Provider):
    """Deterministic provider for tests.

    `scripted` maps a substring of the last user message to a canned reply, so a
    test can drive a full plan-execute-summarise loop without a network or a model.
    """

    name: str = "mock"
    model: str = "mock-1"
    scripted: dict[str, str] = field(default_factory=dict)
    default_reply: str = '{"steps": []}'
    calls: list[list[Message]] = field(default_factory=list)
    fail_times: int = 0
    _failures: int = field(default=0, init=False)

    def complete(self, messages: list[Message], **kw: Any) -> Completion:
        self.calls.append(list(messages))
        if self._failures < self.fail_times:
            self._failures += 1
            raise ProviderError("mock failure")
        last = next(
            (m.content for m in reversed(messages) if m.role == "user"), ""
        )
        for needle, reply in self.scripted.items():
            if needle.lower() in last.lower():
                return Completion(text=reply, model=self.model, provider=self.name)
        return Completion(text=self.default_reply, model=self.model, provider=self.name)

    def available(self) -> tuple[bool, str]:
        return True, "mock provider"


def http_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    """POST JSON, get JSON. Shared by every HTTP-backed provider."""
    if not url.lower().startswith(("http://", "https://")):
        raise ProviderError(f"refusing endpoint {url!r}: http/https only")
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, data=body, headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read(4_000_000)
    except urllib.error.HTTPError as exc:
        detail = exc.read(4000).decode("utf-8", errors="replace")
        raise ProviderError(f"HTTP {exc.code} from {url}: {detail[:400]}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProviderError(f"{url} timed out after {timeout}s") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError(f"{url} did not return JSON") from exc
