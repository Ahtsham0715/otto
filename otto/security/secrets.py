"""Secret detection and storage.

Two jobs:

* `looks_like_secret` — used by the memory layer to *refuse* to remember an API key
  the user read out loud, and by the audit log's callers.
* `SecretStore` — where provider API keys live. macOS Keychain via `/usr/bin/security`
  (argv list, never a shell string), an `OTTO_*` environment fallback, and an
  in-memory store so the test suite runs on Linux.

No key is ever hardcoded, logged, or written to the repo.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
from collections import Counter

#: Prefixes that are unambiguously credentials.
SECRET_PREFIXES = (
    "sk-",
    "sk_live_",
    "sk_test_",
    "rk_live_",
    "pk_live_",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "xoxa-",
    "xoxs-",
    "AKIA",
    "ASIA",
    "AIza",
    "ya29.",
    "gsk_",
    "csk-",
    "hf_",
    "glpat-",
    "dop_v1_",
    "npm_",
    "-----BEGIN",
)

_JWT = re.compile(r"^eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}$")
_HEXY = re.compile(r"^[A-Fa-f0-9]{32,}$")
_KEYISH_KEY = re.compile(
    r"(api[_\- ]?key|secret|token|password|passwd|credential|private[_\- ]?key"
    r"|access[_\- ]?key|auth)",
    re.I,
)


def shannon_entropy(text: str) -> float:
    """Bits per character. High entropy plus length is the classic key signature."""
    if not text:
        return 0.0
    counts = Counter(text)
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def looks_like_secret(value: str, key: str | None = None) -> bool:
    """True when a value should never be stored in memory or echoed.

    Errs towards refusing. A false positive costs the user one remembered
    preference; a false negative writes their API key into a SQLite file.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False

    if any(text.startswith(prefix) for prefix in SECRET_PREFIXES):
        return True
    if _JWT.match(text):
        return True
    if "PRIVATE KEY-----" in text:
        return True

    # A key *named* like a credential holding anything non-trivial is refused
    # regardless of the value's shape.
    if key and _KEYISH_KEY.search(key) and len(text) >= 8 and " " not in text:
        return True

    compact = text.replace("-", "").replace("_", "")
    if len(compact) >= 32 and _HEXY.match(compact):
        return True

    # Long, unbroken, high-entropy token: no spaces, mixed classes, >= 24 chars.
    if len(text) >= 24 and " " not in text:
        has_digit = any(c.isdigit() for c in text)
        has_alpha = any(c.isalpha() for c in text)
        if has_digit and has_alpha and shannon_entropy(text) >= 3.6:
            return True
    return False


class SecretStore:
    """Reads and writes provider credentials.

    Lookup order: explicit in-memory override, then the environment
    (`OTTO_<NAME>`), then the macOS Keychain. Writes go to the Keychain on macOS
    and to memory elsewhere; they are never written to disk by Otto.
    """

    SERVICE = "otto-assistant"

    def __init__(self, *, use_keychain: bool | None = None, service: str | None = None):
        if use_keychain is None:
            use_keychain = sys.platform == "darwin"
        self.use_keychain = use_keychain
        self.service = service or self.SERVICE
        self._memory: dict[str, str] = {}

    # -- api ---------------------------------------------------------------

    def get(self, name: str) -> str | None:
        if name in self._memory:
            return self._memory[name]
        env_name = f"OTTO_{name.upper().replace('-', '_')}"
        value = os.environ.get(env_name)
        if value:
            return value
        if self.use_keychain:
            return self._keychain_get(name)
        return None

    def set(self, name: str, value: str) -> bool:
        """Store a credential. Returns True when it reached the Keychain."""
        if self.use_keychain and self._keychain_set(name, value):
            return True
        self._memory[name] = value
        return False

    def delete(self, name: str) -> None:
        self._memory.pop(name, None)
        if self.use_keychain:
            self._run(
                ["security", "delete-generic-password", "-s", self.service, "-a", name]
            )

    def has(self, name: str) -> bool:
        return bool(self.get(name))

    def set_override(self, name: str, value: str) -> None:
        """In-memory only. Used by tests and by the "try a key without saving it" flow."""
        self._memory[name] = value

    # -- keychain ----------------------------------------------------------

    def _keychain_get(self, name: str) -> str | None:
        result = self._run(
            [
                "security",
                "find-generic-password",
                "-s",
                self.service,
                "-a",
                name,
                "-w",
            ]
        )
        if result is None or result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None

    def _keychain_set(self, name: str, value: str) -> bool:
        result = self._run(
            [
                "security",
                "add-generic-password",
                "-U",  # update if it exists
                "-s",
                self.service,
                "-a",
                name,
                "-w",
                value,
            ]
        )
        return bool(result and result.returncode == 0)

    @staticmethod
    def _run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        """Always an argv list, never a shell string, and never logged."""
        try:
            return subprocess.run(  # noqa: S603 - argv list, shell=False
                argv,
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
