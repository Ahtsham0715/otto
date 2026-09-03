"""Permission levels, agent ceilings and the approval broker."""

from __future__ import annotations

import threading
import time

from otto.core.permissions import (
    ApprovalBroker,
    Permission,
    exceeds_ceiling,
)
from otto.core.state import Status, Task


def test_levels_are_ordered():
    assert Permission.SAFE < Permission.CONFIRM < Permission.ALWAYS_CONFIRM
    assert Permission.SAFE.rank == 0


def test_ceiling_maths():
    assert not exceeds_ceiling(Permission.SAFE, Permission.SAFE)
    assert exceeds_ceiling(Permission.CONFIRM, Permission.SAFE)
    assert exceeds_ceiling(Permission.ALWAYS_CONFIRM, Permission.CONFIRM)
    assert not exceeds_ceiling(Permission.CONFIRM, Permission.ALWAYS_CONFIRM)


def test_broker_fails_closed_without_a_ui():
    """No ask hook must mean 'no', never 'yes'."""
    broker = ApprovalBroker()
    task = Task(request="x")
    approval = broker.request(
        task, tool="write_file", args={}, agent_id="files",
        level=Permission.CONFIRM, reason="r",
    )
    assert approval.granted is False
    assert not approval.pending


def test_broker_grants_when_the_human_says_yes():
    broker = ApprovalBroker(ask=lambda a: a.decide(True))
    task = Task(request="x")
    approval = broker.request(
        task, tool="write_file", args={}, agent_id="files",
        level=Permission.CONFIRM, reason="r",
    )
    assert approval.granted is True


def test_broker_survives_a_broken_ui_hook():
    def exploding(_approval):
        raise RuntimeError("the UI fell over")

    broker = ApprovalBroker(ask=exploding)
    task = Task(request="x")
    approval = broker.request(
        task, tool="t", args={}, agent_id="files",
        level=Permission.CONFIRM, reason="r",
    )
    assert approval.granted is False
    assert any(e.kind == "approval_error" for e in task.timeline)


def test_broker_times_out_into_a_denial():
    broker = ApprovalBroker(ask=lambda a: None, timeout=0.05)
    task = Task(request="x")
    started = time.time()
    approval = broker.request(
        task, tool="t", args={}, agent_id="files",
        level=Permission.CONFIRM, reason="r",
    )
    assert approval.granted is False
    assert time.time() - started < 2
    assert any(e.kind == "approval_timeout" for e in task.timeline)


def test_cancelling_mid_prompt_releases_the_broker():
    task = Task(request="x")
    held: list = []
    broker = ApprovalBroker(ask=lambda a: held.append(a), timeout=5)
    result: list = []

    def ask_in_background():
        approval = broker.request(
            task, tool="t", args={}, agent_id="files",
            level=Permission.ALWAYS_CONFIRM, reason="r",
        )
        result.append(approval)

    thread = threading.Thread(target=ask_in_background)
    thread.start()
    time.sleep(0.1)
    task.cancel()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result and result[0].cancelled and result[0].granted is False


def test_auto_mode_is_not_reachable_from_arguments():
    """set_auto is a test/headless hook, not something a tool argument can flip."""
    broker = ApprovalBroker()
    broker.set_auto(True)
    task = Task(request="x")
    approval = broker.request(
        task, tool="t", args={"auto": True, "granted": True}, agent_id="files",
        level=Permission.CONFIRM, reason="r",
    )
    assert approval.granted is True
    broker.set_auto(None)
    approval2 = broker.request(
        task, tool="t", args={"auto": True}, agent_id="files",
        level=Permission.CONFIRM, reason="r",
    )
    assert approval2.granted is False


def test_task_records_the_request(services):
    task = Task(request="x")
    services.broker.set_auto(True)
    services.broker.request(
        task, tool="write_file", args={"path": "/tmp/x"}, agent_id="files",
        level=Permission.CONFIRM, reason="Write /tmp/x?",
    )
    assert task.approvals and task.approvals[0].tool == "write_file"
    assert any(e.kind == "approval_requested" for e in task.timeline)
    assert task.status is Status.PENDING  # requesting does not move the task
