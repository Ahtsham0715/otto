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
from otto.config import Config, ProviderConfig  # noqa: E402
from otto.core.state import Subtask, Task  # noqa: E402
from otto.providers.base import MockProvider  # noqa: E402
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
    return Config(home=str(home))


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


def use_mock_provider(services: Services, *, scripted=None, tier="both", **kw) -> MockProvider:
    """Point one or both tiers at a MockProvider and return it."""
    provider = MockProvider(scripted=dict(scripted or {}), **kw)
    tiers = ("fast", "strong") if tier == "both" else (tier,)
    for name in tiers:
        services.config.providers[name] = ProviderConfig(kind="mock", model="mock-1")
        services._provider_cache[name] = provider
    return provider
