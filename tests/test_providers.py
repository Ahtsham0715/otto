"""Provider abstraction, the factory, and the no-model-configured path."""

from __future__ import annotations

import json

import pytest

from otto.config import ProviderConfig
from otto.providers import build_provider
from otto.providers.base import (
    Completion,
    Message,
    MockProvider,
    NoModelConfigured,
    NullProvider,
    ProviderError,
    parse_json_object,
)
from otto.providers.clients import (
    AnthropicProvider,
    GeminiProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
)
from otto.security.secrets import SecretStore


# -- JSON tolerance ---------------------------------------------------------


def test_plain_json_parses():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_fenced_json_parses():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object("```\n{\"a\": 1}\n```") == {"a": 1}


def test_json_with_chatter_around_it_parses():
    assert parse_json_object('Sure! Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}


@pytest.mark.parametrize("text", ["", "no json here", "[1,2,3]", "{oops", "null"])
def test_unparseable_replies_raise(text):
    with pytest.raises(ValueError):
        parse_json_object(text)


def test_completion_helper():
    assert Completion(text='{"x": true}').json_object() == {"x": True}


# -- null / mock ------------------------------------------------------------


def test_null_provider_explains_instead_of_pretending():
    provider = NullProvider()
    ok, detail = provider.available()
    assert ok is False and "no model" in detail
    with pytest.raises(NoModelConfigured, match="setup.sh"):
        provider.complete([Message("user", "hi")])


def test_mock_provider_scripts_by_substring():
    provider = MockProvider(scripted={"safari": '{"steps": [{"id": "s1"}]}'})
    reply = provider.complete([Message("user", "please open Safari")])
    assert "steps" in reply.text
    assert provider.calls


def test_mock_provider_can_fail_a_set_number_of_times():
    provider = MockProvider(fail_times=1, default_reply="ok")
    with pytest.raises(ProviderError):
        provider.complete([Message("user", "x")])
    assert provider.complete([Message("user", "x")]).text == "ok"


# -- the factory ------------------------------------------------------------


def test_no_configuration_yields_the_null_provider():
    assert isinstance(build_provider(ProviderConfig()), NullProvider)


def test_factory_builds_each_kind():
    secrets = SecretStore(use_keychain=False)
    secrets.set_override("groq", "test-key")

    ollama = build_provider(ProviderConfig(kind="ollama", model="qwen2.5:3b"), secrets)
    assert isinstance(ollama, OllamaProvider)
    assert ollama.is_cloud is False

    groq = build_provider(
        ProviderConfig(kind="groq", model="llama-3.3-70b-versatile",
                       api_key_name="groq"),
        secrets,
    )
    assert isinstance(groq, OpenAICompatibleProvider)
    assert groq.api_key == "test-key"
    assert groq.is_cloud is True
    assert "groq.com" in groq.base_url

    assert isinstance(build_provider(ProviderConfig(kind="anthropic"), secrets),
                      AnthropicProvider)
    assert isinstance(build_provider(ProviderConfig(kind="gemini"), secrets),
                      GeminiProvider)


def test_a_local_openai_compatible_endpoint_is_not_cloud():
    provider = build_provider(
        ProviderConfig(kind="openai_compatible", model="m",
                       base_url="http://127.0.0.1:1234/v1")
    )
    assert provider.is_cloud is False


def test_unknown_kind_raises():
    with pytest.raises(ProviderError, match="unknown provider"):
        build_provider(ProviderConfig(kind="telepathy"))


def test_keys_come_from_the_store_not_the_config():
    """The config file names a key; it never holds one."""
    config = ProviderConfig(kind="groq", model="m", api_key_name="groq")
    assert "key" not in str(config.__dict__.get("api_key", ""))
    secrets = SecretStore(use_keychain=False)
    provider = build_provider(config, secrets)
    assert provider.api_key == ""  # nothing stored yet


# -- HTTP shapes (no network: urlopen is replaced) --------------------------


def _fake_http(monkeypatch, payload, capture=None):
    import otto.providers.base as base

    class FakeResponse:
        def read(self, n=None):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        if capture is not None:
            capture.append(
                {
                    "url": request.full_url,
                    "headers": dict(request.headers),
                    "body": json.loads(request.data.decode()),
                }
            )
        return FakeResponse()

    monkeypatch.setattr(base.urllib.request, "urlopen", fake_urlopen)


def test_ollama_request_and_response_shape(monkeypatch):
    capture: list = []
    _fake_http(monkeypatch, {"message": {"content": "hi"}, "eval_count": 7}, capture)
    provider = OllamaProvider(model="qwen2.5:3b")
    reply = provider.complete([Message("user", "hello")])
    assert reply.text == "hi"
    assert reply.completion_tokens == 7
    assert capture[0]["url"].endswith("/api/chat")
    assert capture[0]["body"]["stream"] is False


def test_openai_compatible_sends_a_bearer_token(monkeypatch):
    capture: list = []
    _fake_http(
        monkeypatch,
        {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 3}},
        capture,
    )
    provider = OpenAICompatibleProvider(model="m", base_url="https://api.groq.com/openai/v1",
                                        api_key="k")
    reply = provider.complete([Message("user", "hello")])
    assert reply.text == "hi"
    headers = {k.lower(): v for k, v in capture[0]["headers"].items()}
    assert headers["authorization"] == "Bearer k"


def test_anthropic_lifts_the_system_prompt_out(monkeypatch):
    capture: list = []
    _fake_http(monkeypatch, {"content": [{"type": "text", "text": "hi"}]}, capture)
    provider = AnthropicProvider(api_key="k")
    provider.complete([Message("system", "be brief"), Message("user", "hello")])
    body = capture[0]["body"]
    assert body["system"] == "be brief"
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_gemini_maps_roles(monkeypatch):
    capture: list = []
    _fake_http(
        monkeypatch,
        {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]},
        capture,
    )
    provider = GeminiProvider(api_key="k")
    provider.complete([Message("assistant", "prior"), Message("user", "hello")])
    roles = [c["role"] for c in capture[0]["body"]["contents"]]
    assert roles == ["model", "user"]


def test_a_non_http_endpoint_is_refused():
    provider = OpenAICompatibleProvider(model="m", base_url="file:///etc")
    with pytest.raises(ProviderError, match="http/https only"):
        provider.complete([Message("user", "x")])


def test_an_empty_choices_array_is_an_error(monkeypatch):
    _fake_http(monkeypatch, {"choices": []})
    provider = OpenAICompatibleProvider(model="m", api_key="k")
    with pytest.raises(ProviderError, match="no choices"):
        provider.complete([Message("user", "x")])


def test_available_reports_a_missing_key_without_calling_out():
    provider = OpenAICompatibleProvider(model="m", base_url="https://api.groq.com/openai/v1")
    ok, detail = provider.available()
    assert ok is False and "API key" in detail
