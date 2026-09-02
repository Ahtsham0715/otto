"""Command screening.

There is no shell in Otto. Commands are argv lists executed with `shell=False`, and
before anything runs it passes two checks:

1. **Binary allowlist** — `argv[0]` must be a bare name in the allowlist. No paths,
   no metacharacters, no `env`, no interpreter smuggling.
2. **Argument screening** — because an allowlist that only checks `argv[0]` is
   trivially bypassable. All of these execute arbitrary code *through an allowed
   program*:

   * `find … -exec` / `-execdir` / `-ok` / `-okdir`, and `-delete` for destruction
   * `git --upload-pack=…` / `--receive-pack=…`, `git -c core.sshCommand=…`,
     `git -c credential.helper=…`, `git --exec-path=…`, and `ext::` remote URLs
   * `rg --pre=…` (runs a preprocessor per file), `--hostname-bin`
   * `make -f …` / `--eval=…`
   * `python -c …`, `python -X …`, `tar --use-compress-program=…` /
     `--to-command=…`, `npm --node-options=…`

   Each of these has its own test in `tests/test_security_argv.py`.

The child's environment is scrubbed of credentials, output is capped and every run
is time-limited.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

#: Characters that only mean anything to a shell. We never invoke one, but an
#: argument containing them is either an attempt to reach one or a sign the caller
#: built a string where it should have built a list. Both are refused.
SHELL_METACHARACTERS = frozenset(";|&$`\n\r<>()\\\"'*?{}!#")


class CommandRefused(Exception):
    """A command was refused before execution."""


@dataclass(frozen=True)
class BinaryPolicy:
    """What a single allowed binary may and may not be asked to do."""

    name: str
    #: Whole arguments that are never allowed (exact match, case-sensitive).
    denied_exact: frozenset[str] = frozenset()
    #: Argument prefixes that are never allowed (e.g. "--upload-pack=").
    denied_prefixes: tuple[str, ...] = ()
    #: If set, argv[1] must be one of these subcommands.
    subcommands: frozenset[str] | None = None
    #: Extra note surfaced when the binary itself is refused.
    note: str = ""


def _p(name: str, **kw) -> BinaryPolicy:
    return BinaryPolicy(name=name, **kw)


#: Flags that smuggle execution through *many* tools; screened for every binary.
UNIVERSAL_DENIED_PREFIXES: tuple[str, ...] = (
    "--exec",
    "--pre=",
    "--pre-glob",
    "--use-compress-program",
    "--to-command",
    "--node-options",
    "--config-env",
    "--upload-pack",
    "--receive-pack",
    "--sshcommand",
    "--hostname-bin",
)

UNIVERSAL_DENIED_EXACT: frozenset[str] = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "--pre", "--eval", "-delete", "--delete"}
)

GIT_DENIED_PREFIXES = (
    "--upload-pack",
    "--receive-pack",
    "--exec-path",
    "--config-env",
    "--namespace=ext",
)

DEFAULT_POLICIES: dict[str, BinaryPolicy] = {
    p.name: p
    for p in (
        _p(
            "git",
            # `-c` sets arbitrary config for one invocation, including
            # core.sshCommand and credential.helper, both of which execute.
            denied_exact=frozenset({"-c", "--config-env", "-u", "--upload-pack"}),
            denied_prefixes=GIT_DENIED_PREFIXES,
            subcommands=frozenset(
                {
                    "status",
                    "diff",
                    "log",
                    "branch",
                    "show",
                    "rev-parse",
                    "remote",
                    "stash",
                    "fetch",
                    "pull",
                    "add",
                    "commit",
                }
            ),
        ),
        _p(
            "python3",
            denied_exact=frozenset({"-c", "-X", "-i", "-E", "-S"}),
            denied_prefixes=("-c", "-X"),
        ),
        _p(
            "python",
            denied_exact=frozenset({"-c", "-X", "-i", "-E", "-S"}),
            denied_prefixes=("-c", "-X"),
        ),
        _p("pytest"),
        _p("node", denied_exact=frozenset({"-e", "--eval", "-p", "--print"})),
        _p(
            "npm",
            subcommands=frozenset({"run", "test", "ci", "install", "ls", "outdated"}),
            denied_prefixes=("--node-options",),
        ),
        _p("pnpm", subcommands=frozenset({"run", "test", "install", "ls"})),
        _p("yarn", subcommands=frozenset({"run", "test", "install", "list"})),
        _p("ls"),
        _p("cat"),
        _p("head"),
        _p("tail"),
        _p("wc"),
        _p("file"),
        _p("du"),
        _p("df"),
        _p("which"),
        _p("sw_vers"),
        _p("uname"),
        _p("sysctl"),
        _p(
            "find",
            denied_exact=frozenset(
                {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls", "-fprint",
                 "-fprintf", "-fprint0"}
            ),
        ),
        _p("grep"),
        _p("rg", denied_prefixes=("--pre", "--hostname-bin", "--search-zip=")),
        _p("cargo", subcommands=frozenset({"test", "build", "check"})),
        _p("go", subcommands=frozenset({"test", "build", "vet"})),
        _p("make", denied_exact=frozenset({"-f", "--file", "--makefile", "--eval"}),
           denied_prefixes=("-f", "--file=", "--makefile=", "--eval")),
        _p("swift", subcommands=frozenset({"test", "build"})),
        _p("ruff"),
        _p("black"),
        _p("mypy"),
        _p("tsc"),
        _p("jest"),
        _p("vitest"),
    )
}

#: Environment variables never passed to a child process.
ENV_DENY_SUBSTRINGS = (
    "API_KEY",
    "APIKEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "SESSION",
    "PRIVATE_KEY",
    "OPENAI",
    "ANTHROPIC",
    "GROQ",
    "CEREBRAS",
    "GEMINI",
    "GOOGLE_APPLICATION",
    "AWS_",
    "GH_",
    "GITHUB_",
    "NPM_",
    "OTTO_",
)

_BARE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")


@dataclass
class CommandScreen:
    """Screens argv lists against the allowlist and the argument rules."""

    policies: dict[str, BinaryPolicy] = field(
        default_factory=lambda: dict(DEFAULT_POLICIES)
    )

    def allowed_binaries(self) -> list[str]:
        return sorted(self.policies)

    def check(self, argv: list[str] | tuple[str, ...]) -> list[str]:
        """Return the screened argv, or raise `CommandRefused`.

        The returned list is a copy — the caller cannot mutate what was screened.
        """
        if not isinstance(argv, (list, tuple)) or not argv:
            raise CommandRefused("a command must be a non-empty argv list")
        if isinstance(argv, str):  # pragma: no cover - defensive, str is not a list
            raise CommandRefused("a command must be a list, never a shell string")

        parts = list(argv)
        for arg in parts:
            if not isinstance(arg, str):
                raise CommandRefused(f"argument {arg!r} is not a string")
            if "\x00" in arg:
                raise CommandRefused("argument contains a null byte")

        binary = parts[0]
        if os.sep in binary or binary.startswith("."):
            raise CommandRefused(
                f"refusing {binary!r}: give a bare binary name, not a path"
            )
        if not _BARE_NAME.match(binary):
            raise CommandRefused(f"refusing {binary!r}: not a plain binary name")

        policy = self.policies.get(binary)
        if policy is None:
            raise CommandRefused(
                f"{binary!r} is not on the allowlist "
                f"({', '.join(self.allowed_binaries()[:12])}…)"
            )

        for arg in parts[1:]:
            bad = SHELL_METACHARACTERS.intersection(arg)
            if bad:
                raise CommandRefused(
                    f"argument {arg!r} contains shell metacharacters "
                    f"({''.join(sorted(bad))})"
                )

        self._screen_arguments(policy, parts)
        return parts

    # -- internals ---------------------------------------------------------

    def _screen_arguments(self, policy: BinaryPolicy, parts: list[str]) -> None:
        args = parts[1:]

        if policy.subcommands is not None:
            positional = next((a for a in args if not a.startswith("-")), None)
            if positional is None:
                raise CommandRefused(
                    f"{policy.name} needs a subcommand "
                    f"(one of {', '.join(sorted(policy.subcommands))})"
                )
            if positional not in policy.subcommands:
                raise CommandRefused(
                    f"{policy.name} {positional!r} is not permitted; allowed: "
                    f"{', '.join(sorted(policy.subcommands))}"
                )

        denied_exact = set(policy.denied_exact) | set(UNIVERSAL_DENIED_EXACT)
        denied_prefixes = tuple(policy.denied_prefixes) + UNIVERSAL_DENIED_PREFIXES

        for arg in args:
            lowered = arg.lower()
            if arg in denied_exact or lowered in {d.lower() for d in denied_exact}:
                raise CommandRefused(
                    f"{policy.name} {arg!r} can execute arbitrary code and is refused"
                )
            for prefix in denied_prefixes:
                if lowered.startswith(prefix.lower()):
                    raise CommandRefused(
                        f"{policy.name} {arg!r} can execute arbitrary code and is "
                        "refused"
                    )
            # `ext::` and other transport-smuggling remote URLs.
            if lowered.startswith("ext::") or "::" in lowered and lowered.startswith(
                ("ext", "transport")
            ):
                raise CommandRefused(
                    f"refusing remote helper URL {arg!r}: it runs a command"
                )
            if lowered.startswith("--upload-pack") or lowered.startswith(
                "--receive-pack"
            ):
                raise CommandRefused(f"refusing {arg!r}")


def scrubbed_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal environment for a child process, with every credential removed.

    Starts from the real environment so that PATH, HOME and locale still work, then
    drops anything whose *name* suggests a credential. We drop by name rather than
    by value because a key we fail to recognise is still a key.
    """
    source = dict(os.environ if base is None else base)
    keep: dict[str, str] = {}
    for name, value in source.items():
        upper = name.upper()
        if any(marker in upper for marker in ENV_DENY_SUBSTRINGS):
            continue
        keep[name] = value
    keep.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin")
    keep["OTTO_CHILD"] = "1"
    return keep
