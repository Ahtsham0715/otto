"""Configuration.

A single JSON file at `~/.otto/config.json`. Everything has a working default, so
Otto starts with no config at all — which is the state the user's machine is in.

Nothing secret lives here. API keys go to the Keychain (`security/secrets.py`); this
file only records *which* provider to use.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CONFIG_DIR_ENV = "OTTO_HOME"


def config_dir() -> Path:
    return Path(os.environ.get(CONFIG_DIR_ENV, Path.home() / ".otto")).expanduser()


@dataclass
class ProviderConfig:
    """One model endpoint. `kind` selects the client; `api_key_name` names a
    Keychain entry, never a key."""

    kind: str = "none"  # none | mock | ollama | openai_compatible | anthropic | gemini
    model: str = ""
    base_url: str = ""
    api_key_name: str = ""
    temperature: float = 0.1
    max_tokens: int = 800
    timeout: float = 90.0

    @property
    def configured(self) -> bool:
        return self.kind not in ("", "none")


@dataclass
class Config:
    # -- machine ---------------------------------------------------------
    home: str = field(default_factory=lambda: str(Path.home()))
    workspace_roots: tuple[str, ...] = ("Desktop", "Documents", "Downloads", "Projects")

    # -- voice -----------------------------------------------------------
    hotkey: str = "<ctrl>+<alt>+space"
    push_to_talk: str = "toggle"  # toggle | hold
    asr_model: str = "base"  # tiny | base | small — see DECISIONS D-03
    asr_compute_type: str = "int8"
    asr_language: str = "en"
    asr_idle_unload_seconds: float = 300.0
    max_recording_seconds: float = 20.0
    sample_rate: int = 16000
    tts: str = "say"  # say | piper | none
    tts_voice: str = ""
    tts_rate: int = 0
    speak_results: bool = True

    # -- models ----------------------------------------------------------
    #: "fast" handles per-step work, "strong" handles planning. Both may be the
    #: same endpoint; the split is what makes the recommended hybrid possible.
    providers: dict[str, ProviderConfig] = field(
        default_factory=lambda: {
            "fast": ProviderConfig(),
            "strong": ProviderConfig(),
        }
    )
    allow_cloud_file_contents: bool = False
    allow_cloud_audio: bool = False

    # -- behaviour -------------------------------------------------------
    fast_path: bool = True
    max_parallel: int = 4
    approval_timeout: float = 180.0
    command_timeout: float = 120.0
    console_port: int = 8787
    audit_path: str = ""

    # -- derived ---------------------------------------------------------

    @property
    def otto_dir(self) -> Path:
        return config_dir()

    @property
    def db_path(self) -> Path:
        return self.otto_dir / "memory.sqlite3"

    @property
    def audit_file(self) -> Path:
        return Path(self.audit_path) if self.audit_path else self.otto_dir / "audit.jsonl"

    def provider(self, tier: str) -> ProviderConfig:
        return self.providers.get(tier) or self.providers.get("fast") or ProviderConfig()

    @property
    def any_model_configured(self) -> bool:
        return any(p.configured for p in self.providers.values())

    def cloud_in_use(self) -> list[str]:
        """Which tiers are pointed at a non-local endpoint. Surfaced in the UI so it
        is always obvious when the machine is talking to someone else's computer."""
        cloud = []
        for tier, provider in self.providers.items():
            if not provider.configured or provider.kind in ("mock", "none"):
                continue
            local = provider.kind == "ollama" or _is_local(provider.base_url)
            if not local:
                cloud.append(tier)
        return cloud

    # -- persistence -----------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or (config_dir() / "config.json")
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        raw = dict(raw)
        providers_raw = raw.pop("providers", {}) or {}
        known = {f for f in cls.__dataclass_fields__}  # ignore unknown keys, don't crash
        clean = {k: v for k, v in raw.items() if k in known}
        if "workspace_roots" in clean:
            clean["workspace_roots"] = tuple(clean["workspace_roots"])
        config = cls(**clean)
        for tier, values in providers_raw.items():
            if isinstance(values, dict):
                fields = {
                    k: v
                    for k, v in values.items()
                    if k in ProviderConfig.__dataclass_fields__
                }
                config.providers[tier] = ProviderConfig(**fields)
        config.providers.setdefault("fast", ProviderConfig())
        config.providers.setdefault("strong", ProviderConfig())
        return config

    def save(self, path: Path | None = None) -> Path:
        path = path or (config_dir() / "config.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data["workspace_roots"] = list(self.workspace_roots)
        data["providers"] = {k: asdict(v) for k, v in self.providers.items()}
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return path


def _is_local(url: str) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return any(
        marker in lowered
        for marker in ("127.0.0.1", "localhost", "0.0.0.0", "::1", ".local")
    )
