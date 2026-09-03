"""Otto — the application object.

`handle_utterance` is the single entry point. The text box calls it; the voice
pipeline transcribes audio and then calls exactly the same method with exactly the
same arguments (DECISIONS D-24). There is no second pipeline to keep in sync.

Nothing heavy is imported here: `import otto.app` must stay in the tens of
milliseconds so the menu bar appears inside the 3 s cold-start budget.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .agentloop.supervisor import Supervisor
from .config import Config
from .core.state import Approval, Status, Task
from .services import Services

#: UI states, in the order the user sees them.
IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
EXECUTING = "executing"
WAITING = "waiting"
SPEAKING = "speaking"
ERROR = "error"

StateHook = Callable[[str, "Otto"], None]
ApprovalHook = Callable[[Approval], None]


class Otto:
    """Owns the task history, the current state and the worker thread."""

    def __init__(self, services: Services | None = None, config: Config | None = None):
        self.services = services or Services(config or Config.load())
        self.supervisor = Supervisor(self.services)
        self.tasks: list[Task] = []
        self.current: Task | None = None
        self.state: str = IDLE
        self.last_error: str = ""
        self._state_hooks: list[StateHook] = []
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None

    # -- state -------------------------------------------------------------

    def on_state_change(self, hook: StateHook) -> None:
        self._state_hooks.append(hook)

    def set_state(self, state: str) -> None:
        with self._lock:
            if self.state == state:
                return
            self.state = state
        for hook in list(self._state_hooks):
            try:
                hook(state, self)
            except Exception:
                pass  # a broken UI hook must never break the run

    def set_approval_hook(self, hook: ApprovalHook | None) -> None:
        """Where approval prompts go. Without one the broker denies (fails closed)."""
        self.services.broker.set_ask(hook)

    # -- the one entry point ----------------------------------------------

    def handle_utterance(self, text: str, *, source: str = "text") -> Task:
        """Run one request to completion. Blocking; use `submit` for the UI."""
        task = Task(request=(text or "").strip(), source=source)
        with self._lock:
            self.tasks.append(task)
            self.current = task
            if len(self.tasks) > 50:
                del self.tasks[:-50]

        if not task.request:
            task.summary = "I didn't catch that."
            task.set_status(Status.FAILED)
            self.set_state(IDLE)
            return task

        self.set_state(THINKING)
        try:
            self.supervisor.run(task)
        except Exception as exc:  # nothing gets to crash the menu bar
            task.error = f"{type(exc).__name__}: {exc}"
            task.summary = f"Something went wrong: {exc}"
            if not task.status.terminal:
                task.set_status(Status.FAILED)
            self.last_error = task.error
        finally:
            self.set_state(ERROR if task.status is Status.FAILED else IDLE)
        return task

    def submit(self, text: str, *, source: str = "text",
               done: Callable[[Task], None] | None = None) -> threading.Thread:
        """Run a request on a worker thread so the UI thread never blocks."""

        def _run() -> None:
            task = self.handle_utterance(text, source=source)
            if done is not None:
                try:
                    done(task)
                except Exception:
                    pass

        thread = threading.Thread(target=_run, name="otto-task", daemon=True)
        with self._lock:
            self._worker = thread
        thread.start()
        return thread

    # -- control -----------------------------------------------------------

    def cancel(self) -> bool:
        """Cancel the running task. Releases every waiting approval."""
        with self._lock:
            task = self.current
        if task is None or task.status.terminal:
            return False
        task.cancel()
        self.services.stop_speaking()
        self.set_state(IDLE)
        return True

    def pending_approvals(self) -> list[Approval]:
        with self._lock:
            task = self.current
        return task.pending_approvals if task else []

    # -- status for the UI and the console --------------------------------

    def model_status(self) -> dict[str, Any]:
        """What the menu should say about models. Never lies about being ready."""
        config = self.services.config
        tiers: dict[str, Any] = {}
        for tier in ("fast", "strong"):
            provider_config = config.provider(tier)
            tiers[tier] = {
                "kind": provider_config.kind,
                "model": provider_config.model,
                "configured": provider_config.configured,
            }
        return {
            "any_configured": config.any_model_configured,
            "cloud_tiers": config.cloud_in_use(),
            "tiers": tiers,
            "fast_path": config.fast_path,
        }

    def snapshot(self) -> dict[str, Any]:
        """A read-only view for the UI and the developer console."""
        with self._lock:
            current = self.current
            recent = list(self.tasks[-10:])
        return {
            "state": self.state,
            "current": current.as_dict() if current else None,
            "recent": [
                {
                    "id": t.id,
                    "request": t.request,
                    "status": t.status.value,
                    "summary": t.summary,
                    "source": t.source,
                    "created_at": t.created_at,
                }
                for t in reversed(recent)
            ],
            "agents": [a.as_dict() for a in self.services.roster],
            "tools": self.services.registry.names(),
            "models": self.model_status(),
            "memory": self.services.memory.stats(),
            "audit": self.services.audit.recent(40),
            "sandbox": self.services.sandbox.describe(),
            "mac_bridge": type(self.services.mac).__name__,
        }

    # -- lifecycle ---------------------------------------------------------

    def greeting(self) -> str:
        """What Otto says on first launch — including the honest model status."""
        status = self.model_status()
        if not status["any_configured"]:
            return (
                "Otto is ready. No language model is configured yet, so I'll handle "
                "simple commands directly — try 'open Safari' or 'create a folder "
                "called Test on my Desktop'."
            )
        cloud = status["cloud_tiers"]
        if cloud:
            return f"Otto is ready, using a cloud model for {', '.join(cloud)}."
        return "Otto is ready, running entirely on this Mac."

    def close(self) -> None:
        self.services.close()
