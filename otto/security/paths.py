"""Filesystem scope.

Two independent gates, both applied to the *resolved* path:

1. **Allowlist.** The path must live under one of a small set of roots — Desktop,
   Documents, Downloads, ~/Projects. Deliberately **not** `$HOME`: an allowlist of
   the home directory is not an allowlist.
2. **Credential denylist.** Even inside an allowed root, anything shaped like a
   credential is refused.

Resolution happens first and with symlinks followed, so `../../..`, an absolute
escape, and a symlink pointing at `/etc` are all the same bug and are all caught by
the same containment check.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT_NAMES = ("Desktop", "Documents", "Downloads", "Projects")

#: Matched case-insensitively against every component of the resolved path.
CREDENTIAL_PATTERNS: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    "*.keychain",
    "*.keychain-db",
    "keychains",
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "id_dsa*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "_netrc",
    "credentials",
    "credentials.*",
    ".git-credentials",
    ".docker/config.json",
    "secrets",
    "secrets.*",
    ".htpasswd",
    "*.jks",
    "*.keystore",
)


class PathRefused(Exception):
    """A path was outside the sandbox, or looked like a credential."""


@dataclass
class PathSandbox:
    """Resolves and screens every agent-supplied path.

    `roots` are the only writable/readable areas. They are resolved once at
    construction; a root that does not exist is kept (the user may create it later)
    but resolved as far as it goes.
    """

    roots: tuple[Path, ...]
    denylist: tuple[str, ...] = CREDENTIAL_PATTERNS

    @classmethod
    def for_home(
        cls, home: str | os.PathLike[str], names: tuple[str, ...] = DEFAULT_ROOT_NAMES
    ) -> PathSandbox:
        base = Path(home).expanduser()
        return cls(tuple(_resolve(base / name) for name in names))

    @classmethod
    def default(cls) -> PathSandbox:
        return cls.for_home(Path.home())

    # -- checks ------------------------------------------------------------

    def resolve(self, raw: str | os.PathLike[str]) -> Path:
        """Resolve, contain, and screen. Returns the safe absolute path or raises.

        This is the *only* way a path reaches the filesystem tools.
        """
        if raw is None or str(raw).strip() == "":
            raise PathRefused("empty path")
        text = str(raw)
        if "\x00" in text:
            raise PathRefused("path contains a null byte")

        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            # A relative path is only meaningful against a root, and picking one
            # for the model would be guessing. Anchor it to the first root instead
            # of silently using the process cwd, which is not in the sandbox.
            candidate = self.roots[0] / candidate

        resolved = _resolve(candidate)

        if not self.contains(resolved):
            raise PathRefused(
                f"{text!r} resolves to {resolved}, which is outside the allowed "
                f"folders ({', '.join(str(r) for r in self.roots)})"
            )
        self.screen_credentials(resolved)
        return resolved

    def contains(self, resolved: Path) -> bool:
        for root in self.roots:
            if resolved == root or resolved.is_relative_to(root):
                return True
        return False

    def screen_credentials(self, resolved: Path) -> None:
        parts = resolved.parts
        lowered = [p.lower() for p in parts]
        for pattern in self.denylist:
            if "/" in pattern:
                joined = "/".join(lowered)
                if fnmatch.fnmatch(joined, f"*{pattern.lower()}*"):
                    raise PathRefused(
                        f"refusing {resolved}: matches credential pattern {pattern!r}"
                    )
                continue
            for component in lowered:
                if fnmatch.fnmatch(component, pattern.lower()):
                    raise PathRefused(
                        f"refusing {resolved}: matches credential pattern {pattern!r}"
                    )

    def is_allowed(self, raw: str | os.PathLike[str]) -> bool:
        try:
            self.resolve(raw)
        except PathRefused:
            return False
        return True

    def describe(self) -> str:
        return ", ".join(str(r) for r in self.roots)


def _resolve(path: Path) -> Path:
    """Resolve symlinks and `..` without requiring the path to exist.

    `strict=False` resolves as much of the path as exists, which is what we need for
    "create a folder that isn't there yet" while still catching a symlinked parent.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):  # loops, or a path we cannot stat
        return Path(os.path.normpath(str(path)))
