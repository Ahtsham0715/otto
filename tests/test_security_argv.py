"""Command screening.

An allowlist that only checks argv[0] is bypassable. Each of these is a real way to
run arbitrary code through an *allowed* binary, and each gets its own test.
"""

from __future__ import annotations

import os

import pytest

from otto.security.argv import (
    CommandRefused,
    CommandScreen,
    scrubbed_environment,
)


@pytest.fixture
def screen() -> CommandScreen:
    return CommandScreen()


# -- the binary allowlist ---------------------------------------------------


def test_allowed_command_passes(screen):
    assert screen.check(["git", "status"]) == ["git", "status"]


def test_unknown_binary_is_refused(screen):
    with pytest.raises(CommandRefused, match="not on the allowlist"):
        screen.check(["curl", "https://example.com"])


@pytest.mark.parametrize("binary", ["bash", "sh", "zsh", "env", "xargs", "osascript",
                                    "sudo", "perl", "ruby", "nc", "ssh", "scp"])
def test_shells_and_smugglers_are_not_on_the_allowlist(screen, binary):
    with pytest.raises(CommandRefused):
        screen.check([binary, "-c", "id"])


def test_a_path_is_not_a_binary_name(screen):
    with pytest.raises(CommandRefused, match="bare binary name"):
        screen.check(["/bin/sh", "-c", "id"])
    with pytest.raises(CommandRefused, match="bare binary name"):
        screen.check(["./evil", "arg"])


def test_a_string_is_not_a_command(screen):
    with pytest.raises(CommandRefused):
        screen.check("git status")  # type: ignore[arg-type]
    with pytest.raises(CommandRefused):
        screen.check([])


def test_non_string_arguments_are_refused(screen):
    with pytest.raises(CommandRefused, match="not a string"):
        screen.check(["git", 42])  # type: ignore[list-item]


def test_null_bytes_are_refused(screen):
    with pytest.raises(CommandRefused, match="null byte"):
        screen.check(["git", "status\x00id"])


# -- shell metacharacters ---------------------------------------------------


@pytest.mark.parametrize(
    "arg",
    [
        "; id",
        "&& id",
        "| id",
        "$(id)",
        "`id`",
        "> /tmp/pwned",
        "< /etc/passwd",
        "a\nid",
        "x'y",
        'x"y',
        "*",
    ],
)
def test_shell_metacharacters_are_refused_in_any_argument(screen, arg):
    with pytest.raises(CommandRefused, match="metacharacters"):
        screen.check(["git", "status", arg])


# -- argument-level bypasses through ALLOWED binaries -----------------------


def test_find_exec_is_refused(screen):
    with pytest.raises(CommandRefused, match="arbitrary code"):
        screen.check(["find", ".", "-exec", "id", "+"])


def test_find_execdir_and_ok_are_refused(screen):
    for flag in ("-execdir", "-ok", "-okdir"):
        with pytest.raises(CommandRefused):
            screen.check(["find", ".", flag, "id", "+"])


def test_find_delete_is_refused(screen):
    with pytest.raises(CommandRefused):
        screen.check(["find", ".", "-name", "x", "-delete"])


def test_git_dash_c_is_refused(screen):
    """git -c core.sshCommand=… and git -c credential.helper=… both execute."""
    with pytest.raises(CommandRefused, match="arbitrary code"):
        screen.check(["git", "status", "-c", "core.sshCommand=id"])
    with pytest.raises(CommandRefused):
        screen.check(["git", "status", "-c", "credential.helper=!id"])


def test_git_upload_pack_and_receive_pack_are_refused(screen):
    with pytest.raises(CommandRefused):
        screen.check(["git", "fetch", "--upload-pack=id", "origin"])
    with pytest.raises(CommandRefused):
        screen.check(["git", "fetch", "--receive-pack=id", "origin"])


def test_git_exec_path_is_refused(screen):
    with pytest.raises(CommandRefused):
        screen.check(["git", "status", "--exec-path=/tmp/evil"])


def test_ext_transport_urls_are_refused(screen):
    with pytest.raises(CommandRefused, match="remote helper"):
        screen.check(["git", "fetch", "ext::sh -c id"])


def test_git_config_env_is_refused(screen):
    with pytest.raises(CommandRefused):
        screen.check(["git", "status", "--config-env=core.pager=id"])


def test_ripgrep_pre_is_refused(screen):
    """rg --pre runs a preprocessor binary on every file it reads."""
    with pytest.raises(CommandRefused):
        screen.check(["rg", "--pre", "id", "pattern"])
    with pytest.raises(CommandRefused):
        screen.check(["rg", "--pre=id", "pattern"])


def test_make_f_is_refused(screen):
    with pytest.raises(CommandRefused):
        screen.check(["make", "-f", "/tmp/evil.mk"])
    with pytest.raises(CommandRefused):
        screen.check(["make", "--file=/tmp/evil.mk"])
    with pytest.raises(CommandRefused):
        screen.check(["make", "--eval=$(shell id)"])


def test_python_dash_c_is_refused(screen):
    with pytest.raises(CommandRefused):
        screen.check(["python3", "-c", "import os"])
    with pytest.raises(CommandRefused):
        screen.check(["python", "-c", "import os"])


def test_node_eval_is_refused(screen):
    for flag in ("-e", "--eval", "-p"):
        with pytest.raises(CommandRefused):
            screen.check(["node", flag, "process.exit(0)"])


def test_npm_node_options_is_refused(screen):
    with pytest.raises(CommandRefused):
        screen.check(["npm", "test", "--node-options=--require=/tmp/x.js"])


def test_tar_style_program_flags_are_refused_everywhere(screen):
    with pytest.raises(CommandRefused):
        screen.check(["ls", "--use-compress-program=id"])
    with pytest.raises(CommandRefused):
        screen.check(["ls", "--to-command=id"])


def test_subcommand_allowlists_are_enforced(screen):
    with pytest.raises(CommandRefused, match="not permitted"):
        screen.check(["git", "push"])
    with pytest.raises(CommandRefused, match="not permitted"):
        screen.check(["npm", "publish"])
    assert screen.check(["npm", "test"])


def test_a_binary_needing_a_subcommand_refuses_none(screen):
    with pytest.raises(CommandRefused, match="needs a subcommand"):
        screen.check(["git"])


def test_legitimate_commands_still_work(screen):
    for argv in (
        ["pytest", "-q"],
        ["git", "diff", "--stat"],
        ["npm", "run", "build"],
        ["ls", "-la"],
        ["python3", "-m", "pytest"],
        ["cargo", "test"],
        ["go", "test", "./..."],
    ):
        assert screen.check(argv) == argv


# -- environment scrubbing --------------------------------------------------


def test_credentials_are_stripped_from_the_child_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("MY_PASSWORD", "hunter2")
    monkeypatch.setenv("OTTO_GROQ_KEY", "gsk_secret")
    monkeypatch.setenv("HOME", "/Users/apple")

    env = scrubbed_environment()

    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "MY_PASSWORD" not in env
    assert "OTTO_GROQ_KEY" not in env
    assert env["HOME"] == "/Users/apple"
    assert env["PATH"]
    assert "sk-secret" not in str(env.values())


def test_scrubbed_environment_always_has_a_path():
    env = scrubbed_environment({"FOO": "bar"})
    assert env["PATH"]
    assert env["OTTO_CHILD"] == "1"
    assert os.sep in env["PATH"]
