"""The deterministic fast path — the thing that makes Otto useful with no LLM."""

from __future__ import annotations

import pytest

from otto.agentloop import fastpath
from otto.core.state import Status


def match(services, text):
    return fastpath.match(text, services)


# -- normalisation ----------------------------------------------------------


@pytest.mark.parametrize(
    "spoken",
    [
        "open Safari",
        "Open Safari.",
        "hey Otto, open Safari",
        "otto: open Safari",
        "could you open Safari?",
        "please open safari",
        "I'd like you to open Safari",
    ],
)
def test_spoken_phrasings_all_reach_the_same_intent(services, spoken):
    matched = match(services, spoken)
    assert matched is not None, spoken
    assert matched.intent == "open_app"
    assert matched.plan.steps[0].args["name"] == "Safari"


def test_app_aliases_are_resolved(services):
    services.mac.installed.append("Visual Studio Code")
    for phrase in ("open VS Code", "open vscode", "open Visual Studio"):
        matched = match(services, phrase)
        assert matched.plan.steps[0].args["name"] == "Visual Studio Code", phrase


def test_an_app_that_is_not_installed_falls_through_when_a_model_can_help(services):
    from otto.config import ProviderConfig

    services.config.providers["strong"] = ProviderConfig(kind="ollama", model="qwen2.5:3b")
    assert match(services, "open the pod bay doors") is None
    assert match(services, "open Photoshop") is None


def test_an_unknown_app_gets_a_useful_reply_when_there_is_no_model(services):
    """Speech recognition garbles proper nouns constantly. With no model to fall
    through to, guessing at the nearest installed app beats "no model
    configured"."""
    assert not services.config.any_model_configured

    matched = match(services, "open Safaris")
    assert matched is not None and matched.intent == "unknown_app"
    said = matched.plan.steps[0].args["text"]
    assert "Safari" in said and "Did you mean" in said

    matched = match(services, "open the pod bay doors")
    assert matched.intent == "unknown_app"
    assert "Say help" in matched.plan.steps[0].args["text"]


def test_help_is_answerable_without_a_model(services):
    for phrase in ("help", "what can you do", "what can I say", "show me your commands"):
        matched = match(services, phrase)
        assert matched is not None and matched.intent == "help", phrase
    said = match(services, "help").plan.steps[0].args["text"]
    assert "open Safari" in said
    assert "say yes or no" in said.lower()
    assert "no language model" in said.lower()


def test_help_does_not_mention_the_missing_model_once_one_is_set_up(services):
    from otto.config import ProviderConfig

    services.config.providers["fast"] = ProviderConfig(kind="ollama", model="qwen2.5:3b")
    said = match(services, "help").plan.steps[0].args["text"]
    assert "no language model" not in said.lower()


# -- URLs -------------------------------------------------------------------


def test_urls_beat_app_names(services):
    matched = match(services, "open github.com")
    assert matched.intent == "open_url"
    assert matched.plan.steps[0].args["url"] == "https://github.com"


def test_an_explicit_url_is_kept(services):
    matched = match(services, "go to https://example.com/x?y=1")
    assert matched.plan.steps[0].args["url"] == "https://example.com/x?y=1"


# -- folders ----------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase,folder",
    [
        ("create a folder called Test on my Desktop", "Desktop"),
        ("make a folder named Test in Documents", "Documents"),
        ("create a new folder called Test in Downloads", "Downloads"),
        ("create a folder called Test", "Desktop"),  # Desktop is the default
    ],
)
def test_folder_creation_targets_the_right_place(services, home, phrase, folder):
    matched = match(services, phrase)
    assert matched.intent == "make_folder"
    assert matched.plan.steps[0].args["path"] == str(home / folder / "Test")


def test_folder_names_with_spaces_survive(services, home):
    matched = match(services, "create a folder called Trip Photos on my Desktop")
    assert matched.plan.steps[0].args["path"] == str(home / "Desktop" / "Trip Photos")


# -- memory -----------------------------------------------------------------


def test_remember_produces_a_stable_key(services):
    matched = match(services, "remember that my projects live in ~/Projects")
    assert matched.intent == "remember"
    assert matched.plan.steps[0].args["key"] == "projects"
    assert "~/Projects" in matched.plan.steps[0].args["value"]


def test_restating_a_preference_uses_the_same_key(services):
    first = match(services, "remember that my projects live in ~/Projects")
    second = match(services, "remember my projects are in ~/Code now")
    assert first.plan.steps[0].args["key"] == second.plan.steps[0].args["key"]


def test_recall_with_and_without_a_topic(services):
    assert match(services, "what do you remember").plan.steps[0].args["query"] == ""
    assert match(services, "what do you remember about projects").plan.steps[0].args[
        "query"
    ] == "projects"


def test_wiping_memory_is_not_automated(services):
    """Deleting everything should happen where the user can see the rows."""
    matched = match(services, "forget everything")
    assert matched.intent == "forget_all"
    assert matched.plan.steps[0].tool == "speak"


# -- projects and tests -----------------------------------------------------


def test_my_project_resolves_from_memory(services, home):
    services.memory.remember("projects", "my projects live in ~/Projects")
    resolved = fastpath.resolve_folder(services, "my project")
    assert resolved is not None
    assert resolved.name == "Projects"


def test_run_the_tests_picks_the_command_from_the_project(services, home):
    project = home / "Projects" / "app"
    project.mkdir()
    (project / "package.json").write_text("{}")
    matched = match(services, "run the tests in ~/Projects/app")
    assert matched.intent == "run_tests"
    assert matched.plan.steps[0].args["argv"] == ["npm", "test"]

    (project / "package.json").unlink()
    (project / "pyproject.toml").write_text("")
    matched = match(services, "run the tests in ~/Projects/app")
    assert matched.plan.steps[0].args["argv"] == ["pytest", "-q"]


def test_open_my_project_and_run_the_tests_is_two_steps(services, home):
    (home / "Projects" / "app").mkdir()
    services.memory.remember("projects", "my projects live in ~/Projects")
    matched = match(services, "open my project and run the tests")
    assert matched is not None
    tools = sorted(s.tool for s in matched.plan.steps)
    assert tools == ["open_app", "run_command"]


# -- other intents ----------------------------------------------------------


def test_summarise_a_file(services, home):
    doc = home / "Documents" / "notes.md"
    doc.write_text("# Notes\n\nSomething worth reading about the project.\n")
    matched = match(services, f"read {doc} and summarise it")
    assert matched.intent == "summarise"
    assert matched.plan.steps[0].args["path"] == str(doc)


def test_active_window_question(services):
    assert match(services, "what is the active window").intent == "active_window"
    assert match(services, "what am I looking at").intent == "active_window"


def test_unmatched_requests_fall_through(services):
    for text in (
        "write me a haiku about otters",
        "why is the sky blue",
        "refactor my authentication module",
        "",
    ):
        assert match(services, text) is None


# -- the safety property ----------------------------------------------------


def test_fast_path_plans_still_go_through_permissions(denying, home):
    """A shortcut around the model is not a shortcut around the safety."""
    from otto.agentloop.supervisor import Supervisor
    from otto.core.state import Task

    task = Task(request="create a folder called Sneaky on my Desktop")
    Supervisor(denying).run(task)
    assert task.status is Status.REQUIRES_HUMAN
    assert not (home / "Desktop" / "Sneaky").exists()


def test_fast_path_plans_validate_against_the_real_roster(services):
    """Every intent must name a real agent holding the real tool."""
    from otto.agentloop.planner import validate_plan

    phrases = [
        "open Safari",
        "open github.com",
        "create a folder called Test on my Desktop",
        "remember that my projects live in ~/Projects",
        "what do you remember",
        "what is the active window",
        "forget everything",
    ]
    for phrase in phrases:
        matched = match(services, phrase)
        assert matched is not None, phrase
        raw = {"steps": [s.as_dict() for s in matched.plan.steps]}
        validate_plan(raw, services.roster, services.registry)


def test_the_fast_path_makes_no_model_calls(services):
    """The whole point on a 2019 i9: these commands cost zero tokens."""
    from tests.helpers import use_mock_provider

    provider = use_mock_provider(services)
    services.broker.set_auto(True)
    from otto.app import Otto

    otto = Otto(services)
    otto.handle_utterance("open Safari")
    otto.handle_utterance("create a folder called Test on my Desktop")
    assert provider.calls == [], "the fast path called the model"


def test_long_paths_still_match(services, home):
    """Real paths are routinely long; a short length cap silently drops the intent
    and sends the request to a model the user may not have."""
    deep = home / "Projects" / "a-fairly-deeply-nested" / "client-work" / "backend-service"
    deep.mkdir(parents=True)
    (deep / "pyproject.toml").write_text("")
    assert len(str(deep)) > 60

    matched = match(services, f"run the tests in {deep}")
    assert matched is not None and matched.intent == "run_tests"
    assert matched.plan.steps[0].args["cwd"] == str(deep)

    matched = match(services, f"create a folder called Reports in {deep}")
    assert matched is not None and matched.intent == "make_folder"
    assert matched.plan.steps[0].args["path"] == str(deep / "Reports")
