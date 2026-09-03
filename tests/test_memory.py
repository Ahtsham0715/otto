"""Memory scopes, isolation, retrieval, secret refusal and implicit learning."""

from __future__ import annotations

import pytest

from otto.core.state import Status
from otto.memory.store import MemoryRefused, MemoryStore


@pytest.fixture
def store() -> MemoryStore:
    store = MemoryStore(":memory:")
    yield store
    store.close()


def test_remember_and_read_back(store):
    stored = store.remember("projects", "my projects live in ~/Projects")
    assert stored.scope == "global"
    assert store.get("projects").value == "my projects live in ~/Projects"


def test_restating_a_preference_updates_rather_than_duplicates(store):
    store.remember("projects", "~/Projects")
    store.remember("projects", "~/Code")
    assert store.get("projects").value == "~/Code"
    assert len(store.all()) == 1


def test_scopes_are_isolated(store):
    store.remember("editor", "vim", scope="global")
    store.remember("editor", "vscode", scope="workspace", scope_key="app")
    store.remember("editor", "emacs", scope="workspace", scope_key="other")
    store.remember("editor", "nano", scope="agent", scope_key="coder")

    assert store.get("editor", scope="global").value == "vim"
    assert store.get("editor", scope="workspace", scope_key="app").value == "vscode"
    assert store.get("editor", scope="workspace", scope_key="other").value == "emacs"
    assert store.get("editor", scope="agent", scope_key="coder").value == "nano"
    assert store.get("editor", scope="workspace", scope_key="missing") is None


def test_context_only_sees_the_current_workspace(store):
    store.remember("build", "make all", scope="workspace", scope_key="alpha")
    store.remember("build", "npm run build", scope="workspace", scope_key="beta")
    store.remember("style", "two spaces", scope="global")

    context = store.context_for("what is the build command", workspace="alpha")
    values = [m.value for m in context]
    assert "make all" in values
    assert "npm run build" not in values


def test_search_matches_key_or_value(store):
    store.remember("projects", "my projects live in ~/Projects")
    store.remember("browser", "I prefer Safari for everything")
    assert store.search("projects")
    assert store.search("Safari")[0].key == "browser"
    assert store.search("nothing relevant at all") == []


def test_search_ignores_stopwords_and_short_words(store):
    store.remember("projects", "my projects live in ~/Projects")
    # "the", "a", "is" alone must not match everything.
    assert store.search("the a is") == []


def test_an_empty_query_returns_everything(store):
    store.remember("a", "one")
    store.remember("b", "two")
    assert len(store.search("")) == 2


def test_hits_are_counted_and_order_results(store):
    store.remember("rare", "rarely used note")
    store.remember("common", "commonly used note")
    for _ in range(3):
        store.search("commonly")
    results = store.search("used note")
    assert results[0].key == "common"


def test_unknown_scope_is_refused(store):
    with pytest.raises(MemoryRefused, match="unknown scope"):
        store.remember("k", "v", scope="galactic")


def test_empty_keys_and_values_are_refused(store):
    with pytest.raises(MemoryRefused):
        store.remember("", "v")
    with pytest.raises(MemoryRefused):
        store.remember("k", "   ")


@pytest.mark.parametrize(
    "value",
    [
        "sk-" + "abcdefghijklmnopqrstuvwxyz0123",
        "ghp_" + "1234567890abcdefghijklmnopqrstuvwx",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
    ],
)
def test_secrets_are_never_stored(store, value):
    with pytest.raises(MemoryRefused, match="credential"):
        store.remember("my api key", value)
    assert store.all() == []


def test_a_credential_named_key_is_refused(store):
    with pytest.raises(MemoryRefused):
        store.remember("openai_api_key", "abcd1234efgh5678")


def test_oversized_values_are_refused(store):
    with pytest.raises(MemoryRefused, match="too long"):
        store.remember("k", "x" * 5000)


def test_everything_is_deletable(store):
    first = store.remember("a", "one")
    store.remember("b", "two", scope="workspace", scope_key="w")
    assert store.forget(first.id) is True
    assert store.forget(first.id) is False
    assert store.forget_key("b", scope="workspace", scope_key="w") is True
    assert store.all() == []


def test_clear_by_scope(store):
    store.remember("a", "one")
    store.remember("b", "two", scope="task", scope_key="t1")
    assert store.clear(scope="task", scope_key="t1") == 1
    assert len(store.all()) == 1
    store.clear()
    assert store.all() == []


def test_usage_counting_learns_preferred_apps(store):
    for _ in range(3):
        store.note_usage("app", "Visual Studio Code")
    store.note_usage("app", "Safari")
    top = store.top_usage("app")
    assert top[0] == ("Visual Studio Code", 3)
    assert ("Safari", 1) in top


def test_stats(store):
    store.remember("a", "one")
    store.remember("b", "two", scope="workspace", scope_key="w")
    stats = store.stats()
    assert stats["total"] == 2
    assert stats["global"] == 1


def test_persistence_across_reopen(tmp_path):
    path = tmp_path / "mem.sqlite3"
    first = MemoryStore(path)
    first.remember("projects", "~/Projects")
    first.close()
    second = MemoryStore(path)
    assert second.get("projects").value == "~/Projects"
    second.close()


# -- through the tools ------------------------------------------------------


def test_remember_tool_verifies_by_reading_sqlite_back(approving, ctx_for):
    call = approving.registry.dispatch(
        ctx_for("memory"), "remember",
        {"key": "projects", "value": "my projects live in ~/Projects"},
    )
    assert call.status is Status.COMPLETED
    assert approving.memory.get("projects") is not None


def test_remember_tool_refuses_a_secret(approving, ctx_for):
    call = approving.registry.dispatch(
        ctx_for("memory"), "remember",
        {"key": "my key", "value": "sk-" + "abcdefghijklmnopqrstuvwxyz0123"},
    )
    assert call.status is Status.FAILED
    assert "credential" in call.error
    assert approving.memory.all() == []


def test_forget_tool_is_always_confirm(services, ctx_for):
    memory = services.memory.remember("a", "one")
    seen = []
    services.broker.set_ask(lambda a: (seen.append(a.level), a.decide(False)))
    call = services.registry.dispatch(
        ctx_for("memory"), "forget_memory", {"memory_id": memory.id}
    )
    assert seen == ["ALWAYS_CONFIRM"]
    assert call.status is Status.REQUIRES_HUMAN
    assert services.memory.get("a") is not None


def test_forget_tool_deletes_when_approved(approving, ctx_for):
    memory = approving.memory.remember("a", "one")
    call = approving.registry.dispatch(
        ctx_for("memory"), "forget_memory", {"memory_id": memory.id}
    )
    assert call.status is Status.COMPLETED
    assert approving.memory.get("a") is None
