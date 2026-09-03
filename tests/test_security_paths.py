"""Filesystem scope: containment, traversal, symlink escape, credential denylist."""

from __future__ import annotations

from pathlib import Path

import pytest

from otto.security.paths import PathRefused, PathSandbox


@pytest.fixture
def sandbox(home: Path) -> PathSandbox:
    return PathSandbox.for_home(home)


def test_paths_inside_a_root_are_allowed(sandbox, home):
    assert sandbox.resolve(home / "Desktop" / "notes.txt") == home / "Desktop" / "notes.txt"
    assert sandbox.resolve(str(home / "Projects" / "app" / "src")).name == "src"


def test_a_path_need_not_exist_yet(sandbox, home):
    resolved = sandbox.resolve(home / "Desktop" / "new" / "deeper" / "file.txt")
    assert not resolved.exists()
    assert sandbox.contains(resolved)


def test_home_itself_is_not_a_root(sandbox, home):
    """An allowlist of $HOME is not an allowlist."""
    with pytest.raises(PathRefused, match="outside the allowed folders"):
        sandbox.resolve(home / "secret.txt")


def test_absolute_escape_is_refused(sandbox):
    with pytest.raises(PathRefused, match="outside"):
        sandbox.resolve("/etc/passwd")


def test_traversal_is_refused(sandbox, home):
    with pytest.raises(PathRefused, match="outside"):
        sandbox.resolve(home / "Desktop" / ".." / ".." / ".." / "etc" / "passwd")


def test_relative_traversal_is_refused(sandbox):
    with pytest.raises(PathRefused):
        sandbox.resolve("../../../../etc/passwd")


def test_symlink_escape_is_refused(sandbox, home):
    """The classic: a symlink inside an allowed folder pointing outside it."""
    target = home / "outside"
    target.mkdir()
    (target / "loot.txt").write_text("secrets")
    link = home / "Desktop" / "escape"
    link.symlink_to(target)

    with pytest.raises(PathRefused, match="outside"):
        sandbox.resolve(link / "loot.txt")


def test_symlinked_file_escape_is_refused(sandbox, home):
    outside = home / "outside.txt"
    outside.write_text("secrets")
    link = home / "Documents" / "innocent.txt"
    link.symlink_to(outside)
    with pytest.raises(PathRefused):
        sandbox.resolve(link)


def test_empty_and_null_paths_are_refused(sandbox):
    with pytest.raises(PathRefused, match="empty path"):
        sandbox.resolve("")
    with pytest.raises(PathRefused, match="null byte"):
        sandbox.resolve("Desktop/x\x00.txt")


def test_relative_paths_anchor_to_a_root_not_the_process_cwd(sandbox, home):
    resolved = sandbox.resolve("notes.txt")
    assert resolved == home / "Desktop" / "notes.txt"


@pytest.mark.parametrize(
    "relative",
    [
        ".ssh/id_rsa",
        ".ssh/config",
        ".aws/credentials",
        ".gnupg/secring.gpg",
        "work/.env",
        "work/.env.production",
        "certs/server.pem",
        "certs/server.key",
        "keys/id_rsa.pub",
        "keys/id_ed25519",
        ".npmrc",
        ".netrc",
        "app/credentials",
        "app/credentials.json",
        "login.keychain-db",
        ".git-credentials",
        "secrets.yaml",
        "release.keystore",
    ],
)
def test_credential_shaped_paths_are_refused_even_inside_a_root(sandbox, home, relative):
    path = home / "Documents" / relative
    with pytest.raises(PathRefused, match="credential pattern"):
        sandbox.resolve(path)


def test_ordinary_files_with_similar_names_are_allowed(sandbox, home):
    for name in ("environment.md", "keynote.txt", "credentials-policy.md", "readme.pem.md"):
        assert sandbox.resolve(home / "Documents" / name)


def test_is_allowed_never_raises(sandbox, home):
    assert sandbox.is_allowed(home / "Desktop" / "ok.txt")
    assert not sandbox.is_allowed("/etc/passwd")
    assert not sandbox.is_allowed(home / "Documents" / ".ssh" / "id_rsa")


def test_describe_lists_the_roots(sandbox, home):
    described = sandbox.describe()
    assert "Desktop" in described and "Projects" in described
    assert str(home) in described
