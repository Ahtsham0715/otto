"""Filesystem tools through the real dispatch path."""

from __future__ import annotations

from pathlib import Path

from otto.core.state import Status
from otto.tools.files import summarise_text


def run(services, ctx_for, tool, args, agent="files"):
    return services.registry.dispatch(ctx_for(agent), tool, args)


def test_read_file(approving, ctx_for, home):
    (home / "Desktop" / "notes.txt").write_text("hello world")
    call = run(approving, ctx_for, "read_file", {"path": str(home / "Desktop" / "notes.txt")})
    assert call.status is Status.COMPLETED
    assert call.result["content"] == "hello world"
    assert call.verified is True


def test_read_file_outside_the_sandbox_is_refused(approving, ctx_for):
    call = run(approving, ctx_for, "read_file", {"path": "/etc/passwd"})
    assert call.status is Status.FAILED
    assert "outside the allowed folders" in call.error


def test_read_file_refuses_credentials(approving, ctx_for, home):
    ssh = home / "Documents" / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("PRIVATE")
    call = run(approving, ctx_for, "read_file", {"path": str(ssh / "id_rsa")})
    assert call.status is Status.FAILED
    assert "credential pattern" in call.error


def test_write_file_is_verified_by_reading_it_back(approving, ctx_for, home):
    target = home / "Desktop" / "out.txt"
    call = run(approving, ctx_for, "write_file",
               {"path": str(target), "content": "written"})
    assert call.status is Status.COMPLETED
    assert target.read_text() == "written"
    assert "bytes" in call.verification_detail


def test_write_file_needs_confirmation(denying, ctx_for, home):
    target = home / "Desktop" / "denied.txt"
    call = run(denying, ctx_for, "write_file", {"path": str(target), "content": "x"})
    assert call.status is Status.REQUIRES_HUMAN
    assert not target.exists()


def test_append_mode(approving, ctx_for, home):
    target = home / "Desktop" / "log.txt"
    target.write_text("one\n")
    call = run(approving, ctx_for, "write_file",
               {"path": str(target), "content": "two\n", "append": True})
    assert call.status is Status.COMPLETED
    assert target.read_text() == "one\ntwo\n"


def test_make_folder_and_list_dir(approving, ctx_for, home):
    target = home / "Desktop" / "Test"
    call = run(approving, ctx_for, "make_folder", {"path": str(target)})
    assert call.status is Status.COMPLETED
    assert target.is_dir()

    (target / "a.txt").write_text("a")
    listed = run(approving, ctx_for, "list_dir", {"path": str(target)})
    assert listed.status is Status.COMPLETED
    assert [e["name"] for e in listed.result["entries"]] == ["a.txt"]


def test_make_folder_is_idempotent(approving, ctx_for, home):
    target = home / "Desktop" / "Twice"
    first = run(approving, ctx_for, "make_folder", {"path": str(target)})
    second = run(approving, ctx_for, "make_folder", {"path": str(target)})
    assert first.result["created"] is True
    assert second.result["created"] is False
    assert second.status is Status.COMPLETED


def test_move_to_trash_never_unlinks(approving, ctx_for, home):
    target = home / "Desktop" / "old.txt"
    target.write_text("bye")
    call = run(approving, ctx_for, "move_to_trash", {"path": str(target)})
    assert call.status is Status.COMPLETED
    assert str(target) in approving.mac.trashed
    # The fake records rather than moves, so the file is still there — which is
    # exactly the behaviour STATUS.md flags as needing a real Mac to confirm.
    assert target.exists()


def test_move_to_trash_is_always_confirm(services, ctx_for, home):
    levels = []
    services.broker.set_ask(lambda a: (levels.append(a.level), a.decide(False)))
    target = home / "Desktop" / "keep.txt"
    target.write_text("x")
    call = services.registry.dispatch(
        ctx_for("files"), "move_to_trash", {"path": str(target)}
    )
    assert levels == ["ALWAYS_CONFIRM"]
    assert call.status is Status.REQUIRES_HUMAN
    assert target.exists()


def test_trashing_something_that_does_not_exist_fails(approving, ctx_for, home):
    call = run(approving, ctx_for, "move_to_trash",
               {"path": str(home / "Desktop" / "ghost.txt")})
    assert call.status is Status.FAILED
    assert "does not exist" in call.error


def test_summarise_file(approving, ctx_for, home):
    doc = home / "Documents" / "report.md"
    doc.write_text(
        "# Quarterly report\n\n"
        "Revenue grew by twelve percent across the three main regions this quarter. "
        "The team shipped four features and closed sixty support tickets.\n\n"
        "## Risks\n\nHiring remains the main constraint on the roadmap for next year.\n"
        + "\nFurther detail that a summary should leave out. " * 60
    )
    call = run(approving, ctx_for, "summarise_file", {"path": str(doc)}, agent="research")
    assert call.status is Status.COMPLETED
    summary = call.result["summary"]
    assert "Quarterly report" in summary
    assert "Revenue grew" in summary
    assert len(summary) < len(doc.read_text())


def test_summarise_handles_an_empty_file():
    assert "empty" in summarise_text("", "blank.txt")


def test_summarise_handles_a_file_with_no_prose():
    text = "```\ncode()\n```\n| a | b |\n"
    assert summarise_text(text, "x.md")


def test_a_read_produces_an_artifact(approving, ctx_for, home, task):
    path = home / "Desktop" / "art.txt"
    path.write_text("content")
    run(approving, ctx_for, "read_file", {"path": str(path)})
    assert any(a.kind == "file" for a in task.artifacts)


def test_reading_a_directory_fails_cleanly(approving, ctx_for, home):
    call = run(approving, ctx_for, "read_file", {"path": str(home / "Desktop")})
    assert call.status is Status.FAILED
    assert "folder" in call.error


def test_large_files_are_truncated_not_loaded_whole(approving, ctx_for, home):
    big = home / "Documents" / "big.txt"
    big.write_text("x" * 500_000)
    call = run(approving, ctx_for, "read_file", {"path": str(big)})
    assert call.status is Status.COMPLETED
    assert "truncated" in call.result["content"]
    assert len(call.result["content"]) < 500_000
