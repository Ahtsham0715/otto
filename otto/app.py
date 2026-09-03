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
        self._ui_approval_hook: ApprovalHook | None = None
        #: True once something can deliver a spoken answer (the menu-bar app sets
        #: it when it builds a voice pipeline). Without it an unanswerable
        #: approval is denied at once instead of waiting for the timeout.
        self.voice_answers: bool = False
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
        """Where approval prompts go.

        Otto wraps the UI's hook rather than handing the broker straight to it,
        so that *every* approval — however it is displayed — also moves Otto into
        the WAITING state and is asked out loud. Voice is the primary interface;
        a question the user cannot hear is a question they cannot answer.
        Without a UI hook the broker still fails closed.
        """
        self._ui_approval_hook = hook
        self.services.broker.set_ask(self._on_approval)

    def _announce(self, approval: Approval) -> None:
        self.set_state(WAITING)
        if self.services.config.speak_results:
            from .voice.replies import spoken_question

            self.services.speak(spoken_question(approval.reason))

    def _on_approval(self, approval: Approval) -> None:
        """Every approval, whatever displays it, is announced out loud first."""
        self._announce(approval)

        hook = self._ui_approval_hook
        if hook is not None:
            hook(approval)
            return
        if not self.voice_answers:
            # No UI and no microphone: there is nobody who could say yes, so fail
            # closed immediately rather than making a scripted run sit out the
            # broker's timeout.
            approval.decide(False)

    def decide_approval(self, granted: bool, approval: Approval | None = None) -> bool:
        """Answer the oldest pending approval. Used by voice and by the menu."""
        if approval is None:
            pending = self.pending_approvals()
            if not pending:
                return False
            approval = pending[0]
        if not approval.pending:
            return False
        approval.decide(granted)
        # A task is still running; the next tool call will move it on from here.
        if self.state == WAITING:
            self.set_state(EXECUTING)
        return True

    # -- the one entry point ----------------------------------------------

    def handle_utterance(self, text: str, *, source: str = "text") -> Task:
        """Run one request to completion. Blocking; use `submit` for the UI.

        Answers to Otto's own questions are routed before anything else: with an
        approval waiting, "yes" means *approve that*, not "start a task called
        yes". See `voice/replies.py` for why the matching is strict.
        """
        spoken = (text or "").strip()
        answered = self._handle_reply(spoken)
        if answered is not None:
            return answered

        pending = self.pending_approvals()
        if pending:
            # Otto is mid-question. Starting a new task here would replace
            # `current` and orphan the outstanding approval: nothing in the UI
            # could reach it any more, and only the broker's timeout would ever
            # resolve it. Re-ask instead — "stop" is how you back out.
            return self._reask(spoken, pending[0])

        task = Task(request=spoken, source=source)
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

    def _handle_reply(self, text: str) -> Task | None:
        """Deal with "yes" / "no" / "stop" / "say that again".

        Returns the task the answer belongs to, or None when this is an ordinary
        command. An answer never creates a task of its own: "yes" belongs to the
        request already in flight, and cluttering the history with it would make
        the recent-tasks list unreadable.
        """
        from .voice.replies import CANCEL, REPEAT, YES, classify_reply

        verdict = classify_reply(text)
        if verdict is None:
            return None

        if verdict == CANCEL:
            message = "Stopped." if self.cancel() else "There's nothing running."
            self.services.speak(message)
            return self._answer_task(text, message)

        if verdict == REPEAT:
            last = self.services.last_spoken or "I haven't said anything yet."
            self.services.speak(last)
            return self._answer_task(text, last)

        pending = self.pending_approvals()
        if not pending:
            # A bare yes or no with nothing waiting is not a command either —
            # running it through the planner would just confuse everyone.
            message = "There's nothing waiting for an answer."
            self.services.speak(message)
            return self._answer_task(text, message)

        granted = verdict == YES
        approval = pending[0]
        self.decide_approval(granted, approval)
        self.services.speak("OK." if granted else "OK, I won't.")
        with self._lock:
            current = self.current
        return current if current is not None else self._answer_task(
            text, "OK." if granted else "OK, I won't."
        )

    def _reask(self, text: str, approval: Approval) -> Task:
        from .voice.replies import spoken_question

        message = (
            f"I'm still waiting on this. {spoken_question(approval.reason)} "
            "Or say stop to cancel."
        )
        self.services.speak(message)
        self.services.audit.record(
            "command_deferred",
            reason="an approval was still pending",
            pending=approval.reason,
            attempted=text[:200],
        )
        return self._answer_task(text, message)

    def _answer_task(self, text: str, summary: str) -> Task:
        """A completed, unrecorded Task so callers always get one back."""
        task = Task(request=text, source="voice")
        task.summary = summary
        task.set_status(Status.RUNNING)
        task.set_status(Status.COMPLETED)
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
