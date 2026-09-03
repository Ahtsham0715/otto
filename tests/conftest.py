"""Shared fixtures.

Everything here runs on Linux with no Mac, no microphone and no model: a `FakeMac`,
an in-memory SQLite, an in-memory audit log and a `MockProvider`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from otto.app import Otto  # noqa: E402
from otto.config import Config  # noqa: E402
from otto.core.state import Subtask, Task  # noqa: E402
from otto.services import Services  # noqa: E402
from otto.tools.registry import ToolContext  # noqa: E402


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A fake macOS home with the sandbox roots present."""
    for name in ("Desktop", "Documents", "Downloads", "Projects"):
        (tmp_path / name).mkdir()
    return tmp_path


@pytest.fixture
def config(home: Path) -> Config:
    # A short approval timeout: an unanswered approval should fail a test in
    # seconds, not sit out the 180 s a real user is given to answer.
    return Config(home=str(home), approval_timeout=2.0)


@pytest.fixture
def services(config: Config) -> Services:
    services = Services.for_tests(config.home, config=config)
    yield services
    services.close()


@pytest.fixture
def approving(services: Services) -> Services:
    """Services whose human always says yes."""
    services.broker.set_auto(True)
    return services


@pytest.fixture
def denying(services: Services) -> Services:
    services.broker.set_auto(False)
    return services


@pytest.fixture
def otto(approving: Services) -> Otto:
    app = Otto(approving)
    yield app


@pytest.fixture
def task() -> Task:
    task = Task(request="test request")
    task.subtasks.append(Subtask(description="step", agent_id="mac"))
    return task


@pytest.fixture
def ctx_for(services: Services, task: Task):
    """Build a ToolContext for a given agent id."""

    def build(agent_id: str, *, the_task: Task | None = None) -> ToolContext:
        target = the_task or task
        if not target.subtasks:
            target.subtasks.append(Subtask(description="step", agent_id=agent_id))
        return ToolContext(
            task=target,
            agent=services.roster.require(agent_id),
            services=services,
            subtask_id=target.subtasks[0].id,
        )

    return build
