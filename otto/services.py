"""Service container.

One object holding the things tools and agents need, wired once at start-up. It
exists so that nothing reaches for a global, and so a test can build an entire Otto
with a fake Mac, an in-memory database and a mock provider in three lines.

Import cost matters here (DECISIONS D-26): this module imports only stdlib and Otto's
own stdlib-only modules. Nothing that touches a wheel is imported until it is used.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .config import Config
from .core.agents import AgentRoster
from .core.audit import AuditLog
from .core.permissions import ApprovalBroker
from .memory.store import MemoryStore
from .platform.mac import FakeMac, MacBridge, build_mac_bridge
from .security.argv import CommandScreen
from .security.paths import PathSandbox
from .security.secrets import SecretStore
from .tools.registry import ToolRegistry


class Services:
    """Everything Otto needs, assembled."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        mac: MacBridge | None = None,
        memory: MemoryStore | None = None,
        audit: AuditLog | None = None,
        sandbox: PathSandbox | None = None,
        roster: AgentRoster | None = None,
        secrets: SecretStore | None = None,
        broker: ApprovalBroker | None = None,
    ):
        self.config = config or Config()
        self.mac = mac if mac is not None else build_mac_bridge()
        self.memory = memory if memory is not None else MemoryStore(self.config.db_path)
        self.audit = audit if audit is not None else AuditLog(self.config.audit_file)
        self.sandbox = sandbox or PathSandbox.for_home(
            self.config.home, tuple(self.config.workspace_roots)
        )
        self.screen = CommandScreen()
        self.roster = roster or AgentRoster.default()
        self.secrets = secrets or SecretStore()
        self.broker = broker or ApprovalBroker(timeout=self.config.approval_timeout)
        self.registry = ToolRegistry(audit=self.audit, broker=self.broker)
        self._speech_lock = threading.Lock()
        #: The last thing Otto said, so "say that again" can repeat it.
        self.last_spoken: str = ""
        self._provider_cache: dict[str, Any] = {}
        register_default_tools(self.registry)

    # -- speech ------------------------------------------------------------

    def speak(self, text: str) -> None:
        """Say something. macOS `say` by default; a no-op when TTS is off.

        Kept here rather than in a tool so that the UI can speak status ("no model
        configured") without going through the agent loop.
        """
        if not text:
            return
        # Recorded even when TTS is off: "say that again" should still be able to
        # repeat the last answer, and the text box shows it.
        self.last_spoken = text
        if self.config.tts == "none":
            return
        with self._speech_lock:
            if self.config.tts == "piper":
                try:
                    from .voice.piper import speak_with_piper

                    speak_with_piper(text, self.config)
                    return
                except Exception:  # fall back rather than going silent
                    pass
            try:
                self.mac.speak(
                    text,
                    voice=self.config.tts_voice or None,
                    rate=self.config.tts_rate or None,
                )
            except Exception:
                pass

    def stop_speaking(self) -> None:
        try:
            self.mac.stop_speaking()
        except Exception:
            pass

    # -- providers ---------------------------------------------------------

    def provider_for(self, tier: str):
        """Build (and cache) the LLM client for a tier. Imported lazily."""
        if tier in self._provider_cache:
            return self._provider_cache[tier]
        from .providers import build_provider

        provider = build_provider(self.config.provider(tier), self.secrets)
        self._provider_cache[tier] = provider
        return provider

    def reset_providers(self) -> None:
        self._provider_cache.clear()

    # -- convenience -------------------------------------------------------

    @property
    def workspace(self) -> str:
        """The scope key for workspace memories: the project folder in use."""
        return getattr(self, "_workspace", "")

    @workspace.setter
    def workspace(self, value: str) -> None:
        self._workspace = value

    def close(self) -> None:
        self.memory.close()

    @classmethod
    def for_tests(
        cls, tmp: str | Path | None = None, *, config: Config | None = None
    ) -> Services:
        """A fully working Otto with nothing real behind it."""
        base = Path(tmp) if tmp else Path.cwd() / ".otto-test"
        cfg = config or Config(home=str(base))
        for name in cfg.workspace_roots:
            (base / name).mkdir(parents=True, exist_ok=True)
        return cls(
            cfg,
            mac=FakeMac(),
            memory=MemoryStore(":memory:"),
            audit=AuditLog(None),
            sandbox=PathSandbox.for_home(base, tuple(cfg.workspace_roots)),
        )


def register_default_tools(registry: ToolRegistry) -> ToolRegistry:
    """Register every shipped tool. Adding a tool is adding it to one of these."""
    from .tools.files import FILE_TOOLS
    from .tools.mac import MAC_TOOLS
    from .tools.memory import MEMORY_TOOLS
    from .tools.proc import PROC_TOOLS

    for spec in (*MAC_TOOLS, *FILE_TOOLS, *PROC_TOOLS, *MEMORY_TOOLS):
        registry.register(spec)
    return registry
