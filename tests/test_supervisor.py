"""The agent loop: delegation, parallelism, verification, retries, cancellation."""

from __future__ import annotations

import json
import threading
import time

import pytest

from tests.helpers import use_mock_provider
from otto.agentloop.supervisor import Supervisor
from otto.core.state import Status, Task


def plan_json(*steps, rationale="r") -> str:
    return json.dumps({"rationale": rationale, "steps": list(steps)})


def run(services, request: str) -> Task:
    task = Task(request=request)
    Supervisor(services).run(task)
    return task


# -- planning ---------------------------------------------------------------


def test_a_model_plan_is_executed_end_to_end(approving):
    use_mock_provider(
        approving,
        scripted={
            "safari": plan_json(
                {"id": "s1", "agent": "mac", "description": "Open Safari",
                 "tool": "open_app", "args": {"name": "Safari"}}
            )
        },
    )
    approving.config.fast_path = False
    task = run(approving, "please deal with safari somehow")

    assert task.status is Status.COMPLETED
    assert approving.mac.frontmost_app() == "Safari"
    assert task.subtasks[0].status is Status.COMPLETED
    assert any(e.kind == "plan" for e in task.timeline)


def test_an_invalid_plan_is_retried_once_with_the_reason(approving):
    replies = [
        plan_json({"id": "s1", "agent": "wizard", "description": "magic"}),
        plan_json({"id": "s1", "agent": "mac", "description": "Open Safari",
                   "tool": "open_app", "args": {"name": "Safari"}}),
    ]

    class Sequenced:
        name = "seq"
        model = "seq"

        def __init__(self):
            self.prompts = []

        def complete(self, messages, **kw):
            from otto.providers.base import Completion

            self.prompts.append(messages[-1].content)
            return Completion(text=replies[min(len(self.prompts) - 1, 1)])

        def available(self):
            return True, "ok"

        def describe(self):
            return "seq"

    provider = Sequenced()
    approving._provider_cache["strong"] = provider
    approving._provider_cache["fast"] = provider
    approving.config.fast_path = False

    task = run(approving, "do the thing")

    assert task.status is Status.COMPLETED
    assert len(provider.prompts) == 2
    assert "was rejected" in provider.prompts[1]
    assert "wizard" in provider.prompts[1]


def test_two_bad_plans_fail_honestly_rather_than_looping(approving):
    use_mock_provider(
        approving, scripted={}, default_reply='{"steps": [{"agent": "wizard"}]}'
    )
    approving.config.fast_path = False
    task = run(approving, "do the thing")
    assert task.status is Status.FAILED
    assert "could not make a plan" in task.summary


def test_no_model_configured_is_a_friendly_state_not_a_crash(services):
    services.config.fast_path = False
    task = run(services, "write me an essay about otters")
    assert task.status is Status.REQUIRES_HUMAN
    assert "No language model is configured" in task.summary
    assert "setup.sh" in task.summary


# -- delegation and messaging ----------------------------------------------


def test_delegation_is_recorded_as_agent_messages(approving):
    use_mock_provider(
        approving,
        scripted={
            "folder": plan_json(
                {"id": "s1", "agent": "files", "description": "Make a folder",
                 "tool": "make_folder",
                 "args": {"path": str(approving.config.home) + "/Desktop/New"}}
            )
        },
    )
    approving.config.fast_path = False
    task = run(approving, "sort out a folder for me")

    kinds = {(m.sender, m.recipient, m.kind) for m in task.messages}
    assert ("supervisor", "files", "delegation") in kinds
    assert ("files", "supervisor", "result") in kinds


# -- parallelism ------------------------------------------------------------


def test_independent_steps_really_run_concurrently(approving):
    """Not just scheduled in one wave — actually overlapping in time."""
    overlap = threading.Event()
    inside = []
    lock = threading.Lock()

    original = approving.mac.list_apps

    def slow_list_apps():
        with lock:
            inside.append(1)
            if len(inside) >= 2:
                overlap.set()
        time.sleep(0.2)
        with lock:
            inside.pop()
        return original()

    approving.mac.list_apps = slow_list_apps  # type: ignore[method-assign]
    use_mock_provider(
        approving,
        scripted={
            "audit": plan_json(
                {"id": "a", "agent": "mac", "description": "List apps",
                 "tool": "list_apps", "args": {}},
                {"id": "b", "agent": "qa", "description": "List apps again",
                 "tool": "list_apps", "args": {}},
            )
        },
    )
    approving.config.fast_path = False

    started = time.time()
    task = run(approving, "audit the machine")
    elapsed = time.time() - started

    assert task.status is Status.COMPLETED
    assert overlap.is_set(), "the two independent steps never overlapped"
    assert elapsed < 0.4, "they ran serially"


def test_dependent_steps_do_not_run_concurrently(approving):
    order: list[str] = []
    original = approving.mac.open_app

    def tracked(name):
        order.append(f"start:{name}")
        time.sleep(0.05)
        original(name)
        order.append(f"end:{name}")

    approving.mac.open_app = tracked  # type: ignore[method-assign]
    use_mock_provider(
        approving,
        scripted={
            "both": plan_json(
                {"id": "a", "agent": "mac", "description": "Open Safari",
                 "tool": "open_app", "args": {"name": "Safari"}},
                {"id": "b", "agent": "mac", "description": "Open Notes",
                 "tool": "open_app", "args": {"name": "Notes"}, "depends_on": ["a"]},
            )
        },
    )
    approving.config.fast_path = False
    run(approving, "open both of them")

    assert order == ["start:Safari", "end:Safari", "start:Notes", "end:Notes"]


def test_a_step_whose_dependency_failed_does_not_run(approving):
    approving.mac.open_failures["Safari"] = RuntimeError("kaboom")
    use_mock_provider(
        approving,
        scripted={
            "chain": plan_json(
                {"id": "a", "agent": "mac", "description": "Open Safari",
                 "tool": "open_app", "args": {"name": "Safari"}},
                {"id": "b", "agent": "mac", "description": "Open Notes",
                 "tool": "open_app", "args": {"name": "Notes"}, "depends_on": ["a"]},
            )
        },
    )
    approving.config.fast_path = False
    task = run(approving, "chain them")

    assert task.subtasks[0].status is Status.FAILED
    assert task.subtasks[1].status is Status.FAILED
    assert "depended on" in task.subtasks[1].error
    assert "Notes" not in approving.mac.running


# -- retries ----------------------------------------------------------------


def test_a_failure_is_retried_exactly_once(approving):
    attempts: list[str] = []
    original = approving.mac.open_app

    def flaky(name):
        attempts.append(name)
        if len(attempts) == 1:
            raise RuntimeError("first attempt fails")
        original(name)

    approving.mac.open_app = flaky  # type: ignore[method-assign]
    approving.config.fast_path = True
    task = run(approving, "open Safari")

    assert attempts == ["Safari", "Safari"]
    assert task.status is Status.COMPLETED
    assert any(e.kind == "retry" for e in task.timeline)


def test_a_persistent_failure_is_not_retried_forever(approving):
    attempts: list[str] = []

    def always_fails(name):
        attempts.append(name)
        raise RuntimeError("nope")

    approving.mac.open_app = always_fails  # type: ignore[method-assign]
    task = run(approving, "open Safari")

    assert len(attempts) == 2  # one try, one retry, then stop
    assert task.status is Status.FAILED


def test_state_is_inspected_before_retrying(approving, home):
    """If the work actually landed, the retry must not repeat it."""
    calls: list[str] = []
    target = home / "Desktop" / "Once"

    real_mkdir_tool = approving.registry.get("make_folder")

    def handler_that_works_but_reports_late(ctx, path):
        calls.append(path)
        result = real_mkdir_tool.handler(ctx, path)
        return result

    verifications: list[int] = []

    def verifier_that_fails_the_first_time(ctx, args, result):
        verifications.append(1)
        if len(verifications) == 1:
            return False, "not yet visible"
        return True, "exists"

    from otto.tools.registry import ToolSpec

    approving.registry.register(
        ToolSpec(
            name="make_folder",
            description=real_mkdir_tool.description,
            schema=real_mkdir_tool.schema,
            required=real_mkdir_tool.required,
            handler=handler_that_works_but_reports_late,
            verifier=verifier_that_fails_the_first_time,
            permission=real_mkdir_tool.permission,
        )
    )

    task = run(approving, f"create a folder called Once on my Desktop")

    assert task.status is Status.COMPLETED
    assert len(calls) == 1, "the folder was created twice"
    assert any(e.kind == "retry_skipped" for e in task.timeline)
    assert target.is_dir()


# -- cancellation -----------------------------------------------------------


def test_cancelling_mid_run_stops_the_remaining_steps(approving):
    task = Task(request="open both")
    use_mock_provider(
        approving,
        scripted={
            "both": plan_json(
                {"id": "a", "agent": "mac", "description": "Open Safari",
                 "tool": "open_app", "args": {"name": "Safari"}},
                {"id": "b", "agent": "mac", "description": "Open Notes",
                 "tool": "open_app", "args": {"name": "Notes"}, "depends_on": ["a"]},
            )
        },
    )
    approving.config.fast_path = False

    original = approving.mac.open_app

    def cancel_after_first(name):
        original(name)
        if name == "Safari":
            task.cancel()

    approving.mac.open_app = cancel_after_first  # type: ignore[method-assign]
    Supervisor(approving).run(task)

    assert task.status is Status.CANCELLED
    assert "Notes" not in approving.mac.running


def test_cancelling_while_an_approval_is_pending_releases_the_worker(services):
    """The deadlock case: a worker blocked on a modal that never gets answered."""
    services.broker.set_ask(lambda approval: None)  # never decides
    services.config.approval_timeout = 30
    services.broker._timeout = 30
    task = Task(request="create a folder called Slow on my Desktop")

    finished = threading.Event()

    def run_it():
        Supervisor(services).run(task)
        finished.set()

    thread = threading.Thread(target=run_it, daemon=True)
    thread.start()

    for _ in range(100):
        if task.pending_approvals:
            break
        time.sleep(0.02)
    assert task.pending_approvals, "no approval was ever raised"

    task.cancel()
    assert finished.wait(timeout=5), "the worker never came back after cancellation"
    assert task.status is Status.CANCELLED


# -- reporting --------------------------------------------------------------


def test_a_denied_approval_reports_requires_human(denying):
    task = run(denying, "create a folder called Nope on my Desktop")
    assert task.status is Status.REQUIRES_HUMAN
    assert "declined" in task.summary


def test_success_is_summarised_from_verifications_not_from_the_model(approving):
    """Even a model that claims disaster cannot change a verified success."""
    use_mock_provider(
        approving,
        scripted={
            "safari": plan_json(
                {"id": "s1", "agent": "mac", "description": "Open Safari",
                 "tool": "open_app", "args": {"name": "Safari"}}
            )
        },
    )
    approving.config.fast_path = False
    task = run(approving, "deal with safari")
    assert task.status is Status.COMPLETED
    assert "Safari is frontmost" in " ".join(s.result for s in task.subtasks)


def test_failure_summaries_name_what_failed(approving):
    approving.mac.open_failures["Safari"] = RuntimeError("Safari would not launch")
    task = run(approving, "open Safari")
    assert task.status is Status.FAILED
    assert "Safari" in task.summary
    assert task.error


def test_results_are_spoken_when_enabled(approving):
    approving.config.speak_results = True
    run(approving, "open Safari")
    assert approving.mac.spoken


def test_results_are_not_spoken_when_disabled(approving):
    approving.config.speak_results = False
    run(approving, "open Safari")
    assert approving.mac.spoken == []


# -- the bounded agent loop -------------------------------------------------


def test_the_agent_loop_runs_tools_until_it_says_done(approving):
    use_mock_provider(
        approving,
        scripted={
            "look around": plan_json(
                {"id": "s1", "agent": "mac", "description": "Find out what is open"}
            ),
            "find out what is open": json.dumps(
                {"tool": "get_active_window", "args": {}}
            ),
            "get_active_window": json.dumps({"done": True, "result": "Finder is open"}),
        },
    )
    approving.config.fast_path = False
    task = run(approving, "look around")
    assert task.status is Status.COMPLETED
    assert task.subtasks[0].result == "Finder is open"


def test_the_agent_loop_gives_up_after_max_steps(approving):
    use_mock_provider(
        approving,
        scripted={
            "loop forever": plan_json(
                {"id": "s1", "agent": "qa", "description": "Never finish"}
            )
        },
        default_reply=json.dumps({"tool": "get_active_window", "args": {}}),
    )
    approving.config.fast_path = False
    task = run(approving, "loop forever")
    assert task.status is Status.FAILED
    assert "gave up after" in task.subtasks[0].error


def test_non_json_replies_are_corrected_not_parsed_as_prose(approving):
    provider = use_mock_provider(
        approving,
        scripted={
            "vague": plan_json(
                {"id": "s1", "agent": "qa", "description": "Do something"}
            )
        },
        default_reply="I think I'll just open Safari, how's that?",
    )
    approving.config.fast_path = False
    task = run(approving, "vague request")
    assert task.status is Status.FAILED
    corrections = [
        m[-1].content for m in provider.calls if "not JSON" in m[-1].content
    ]
    assert corrections, "the model was never told its reply was unusable"
    assert approving.mac.frontmost_app() == "Finder"  # nothing was inferred from prose


def test_a_provider_outage_mid_step_fails_cleanly(approving):
    from otto.providers.base import ProviderError

    class Dying:
        name = "dying"
        model = "d"

        def __init__(self):
            self.n = 0

        def complete(self, messages, **kw):
            from otto.providers.base import Completion

            self.n += 1
            if self.n == 1:
                return Completion(
                    text=plan_json({"id": "s1", "agent": "qa", "description": "Check"})
                )
            raise ProviderError("connection reset")

        def available(self):
            return True, ""

        def describe(self):
            return "dying"

    provider = Dying()
    approving._provider_cache["strong"] = provider
    approving._provider_cache["fast"] = provider
    approving.config.fast_path = False

    task = run(approving, "check something")
    assert task.status is Status.FAILED
    assert "unreachable" in task.subtasks[0].error
