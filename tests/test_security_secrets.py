"""Secret detection, redaction and the secret store."""

from __future__ import annotations

import pytest

from otto.core.audit import AuditLog, redact
from otto.security.secrets import SecretStore, looks_like_secret, shannon_entropy

# Fake credentials, assembled from fragments so that GitHub's push protection does
# not flag these test fixtures as real leaked tokens.
GH_TOKEN = "ghp_" + "1234567890abcdefghijklmnopqrstuvwx"
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.mark.parametrize(
    "value",
    [
        "sk-" + "abcdefghijklmnopqrstuvwxyz012345",
        "sk_live_" + "51H8xKzabcdefghijklmnop",
        "ghp_" + "1234567890abcdefghijklmnopqrstuvwx",
        "github_pat_" + "11ABCDEFG0abcdefghijk",
        # Assembled from fragments so GitHub's push protection does not flag the
        # test fixture itself as a real leaked token.
        "xoxb-" + "123456789012-1234567890123-abcdefghijklmnopqrstuvwx",
        "AKIA" + "IOSFODNN7EXAMPLE",
        "AIza" + "SyD-1234567890abcdefghijklmnopqrst",
        "gsk_" + "abcdefghijklmnopqrstuvwxyz0123456789",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
        "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w",
        "a3f5c8e91b2d4f6a8c0e2b4d6f8a0c2e",  # 32 hex
    ],
)
def test_credentials_are_detected(value):
    assert looks_like_secret(value)


@pytest.mark.parametrize(
    "value",
    [
        "my projects live in ~/Projects",
        "I prefer Visual Studio Code",
        "working hours are 9 to 5",
        "Desktop",
        "call me Sam",
        "the build takes about 3 minutes",
    ],
)
def test_ordinary_preferences_are_not_refused(value):
    assert not looks_like_secret(value)


def test_a_credential_shaped_key_refuses_a_plain_value():
    assert looks_like_secret("hunter2xyz", key="password")
    assert looks_like_secret("abcd1234efgh", key="openai_api_key")
    assert not looks_like_secret("in the morning", key="working hours")


def test_entropy_is_sane():
    assert shannon_entropy("aaaaaaaa") < 1.0
    assert shannon_entropy("a8Xq2LmZ9pRt4WvY") > 3.0
    assert shannon_entropy("") == 0.0


# -- redaction --------------------------------------------------------------


def test_redaction_covers_keys_and_shapes():
    payload = {
        "api_key": "sk-" + "should-not-appear",
        "note": "the token is " + GH_TOKEN + " ok",
        "nested": {"Authorization": "Bearer abc", "fine": "hello"},
        "list": [AWS_KEY, "harmless"],
    }
    cleaned = redact(payload)
    dumped = str(cleaned)
    assert "sk-" + "should-not-appear" not in dumped
    assert "ghp_" + "1234567890abcdefghijklmnopqrstuvwx" not in dumped
    assert AWS_KEY not in dumped
    assert "Bearer abc" not in dumped
    assert cleaned["nested"]["fine"] == "hello"
    assert cleaned["list"][1] == "harmless"


def test_redaction_is_depth_limited():
    deep: dict = {}
    node = deep
    for _ in range(20):
        node["next"] = {}
        node = node["next"]
    assert "too deep" in str(redact(deep))


def test_audit_writes_jsonl_and_redacts(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("tool_attempt", tool="write_file", args={"api_key": "sk-secret-value"})
    text = (tmp_path / "audit.jsonl").read_text()
    assert "sk-secret-value" not in text
    assert '"event": "tool_attempt"' in text
    assert log.count("tool_attempt") == 1


def test_audit_survives_an_unwritable_path(tmp_path):
    log = AuditLog(tmp_path / "nope" / "audit.jsonl")
    (tmp_path / "nope").rmdir()
    log.path.parent.mkdir(exist_ok=True)
    log.record("x", detail="y")
    assert log.count("x") == 1


# -- the store --------------------------------------------------------------


def test_store_prefers_memory_then_environment(monkeypatch):
    store = SecretStore(use_keychain=False)
    assert store.get("groq") is None
    monkeypatch.setenv("OTTO_GROQ", "from-env")
    assert store.get("groq") == "from-env"
    store.set_override("groq", "from-memory")
    assert store.get("groq") == "from-memory"


def test_store_reports_when_it_could_not_reach_the_keychain():
    store = SecretStore(use_keychain=False)
    assert store.set("k", "v") is False  # memory, not Keychain
    assert store.get("k") == "v"
    assert store.has("k")
    store.delete("k")
    assert store.get("k") is None


def test_store_never_uses_a_shell(monkeypatch):
    """Patch subprocess itself, so this checks the real call and not a stand-in."""
    import subprocess

    captured: list = []

    def fake_run(argv, **kw):
        captured.append((argv, kw))

        class R:
            returncode = 1
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    store = SecretStore(use_keychain=True)
    store.get("groq")
    store.set("groq", "value-that-must-not-be-shell-quoted")

    assert captured
    for argv, kw in captured:
        assert isinstance(argv, list), "the Keychain call must be an argv list"
        assert argv[0] == "security"
        assert kw["shell"] is False
