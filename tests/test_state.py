"""Task, subtask and approval lifecycle."""

from __future__ import annotations

import threading
import time

import pytest

from otto.core.state import (
    AgentMessage,
    Approval,
    Artifact,
    IllegalTransition,
    Status,
    Subtask,
    Task,
    can_transition,
)


def test_terminal_states_are_terminal():
    for status in (Status.COMPLETED, Status.FAILED, Status.CANCELLED):
        assert status.terminal
        for other in Status:
            if status is Status.CANCELLED and other is Status.CANCELLED:
                continue
            assert not can_transition(status, other), f"{status} -> {other}"


def test_task_follows_the_transition_table():
    task = Task(request="x")
    task.set_status(Status.RUNNING)
    task.set_status(Status.COMPLETED)
    with pytest.raises(IllegalTransition):
        task.set_status(Status.RUNNING)


def test_subtask_records_timings():
    subtask = Subtask(description="s", agent_id="mac")
    subtask.set_status(Status.RUNNING)
    assert subtask.started_at is not None
    subtask.set_status(Status.COMPLETED)
    assert subtask.finished_at is not None


def test_cancel_releases_every_waiting_approval():
    """The critical one: a worker blocked on an approval must never leak."""
    task = Task(request="x")
    approvals = [
        task.add_approval(Approval(tool="t", args={}, agent_id="files",
                                   level="CONFIRM", reason="r"))
        for _ in range(3)
    ]
    released: list[bool] = []

    def waiter(approval):
        released.append(approval.wait(timeout=5))

    threads = [threading.Thread(target=waiter, args=(a,)) for a in approvals]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    assert all(a.pending for a in approvals)

    task.cancel()

    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert released == [False, False, False]
    assert all(a.cancelled and not a.pending for a in approvals)


def test_a_cancelled_task_never_resurrects():
    task = Task(request="x")
    task.set_status(Status.RUNNING)
    task.cancel()
    assert task.status is Status.CANCELLED
    with pytest.raises(IllegalTransition):
        task.set_status(Status.RUNNING)
    task.cancel()  # idempotent
    assert task.status is Status.CANCELLED


def test_cancel_marks_unfinished_subtasks_only():
    task = Task(request="x")
    done = Subtask(description="done", agent_id="mac")
    done.set_status(Status.RUNNING)
    done.set_status(Status.COMPLETED)
    running = Subtask(description="running", agent_id="mac")
    running.set_status(Status.RUNNING)
    task.subtasks += [done, running]

    task.cancel()

    assert done.status is Status.COMPLETED
    assert running.status is Status.CANCELLED


def test_approval_created_after_cancellation_is_pre_cancelled():
    task = Task(request="x")
    task.cancel()
    approval = task.add_approval(
        Approval(tool="t", args={}, agent_id="files", level="CONFIRM", reason="r")
    )
    assert not approval.pending
    assert approval.wait(timeout=0.1) is False


def test_timeline_messages_and_artifacts_are_recorded():
    task = Task(request="x")
    task.send(AgentMessage(sender="supervisor", recipient="mac", content="go"))
    task.add_artifact(Artifact(kind="text", name="n", value="v"))
    kinds = [e.kind for e in task.timeline]
    assert "message" in kinds and "artifact" in kinds
    assert task.messages[0].task_id == task.id


def test_as_dict_is_json_shaped():
    task = Task(request="x")
    task.subtasks.append(Subtask(description="s", agent_id="mac"))
    data = task.as_dict()
    assert data["status"] == "PENDING"
    assert data["subtasks"][0]["agent_id"] == "mac"
    assert isinstance(data["timeline"], list)


def test_concurrent_logging_is_safe():
    task = Task(request="x")

    def spam():
        for i in range(200):
            task.log("tick", str(i))

    threads = [threading.Thread(target=spam) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(task.timeline) == 800
