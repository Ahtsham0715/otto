"""Answering Otto's own questions by voice.

The safety property under test is one-directional: a stray word must never
approve something. "Yes" approves; "yes but not that one" does not, and neither
does anything Otto is not certain about — it falls through to the ordinary
command path instead.
"""

from __future__ import annotations

import threading
import time

import pytest

from otto.app import EXECUTING, WAITING
from otto.core.state import Status
from otto.voice.replies import (
    CANCEL,
    NO,
    REPEAT,
    YES,
    classify_reply,
    normalise_reply,
    spoken_question,
)


# -- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    ["yes", "Yes.", "yeah", "yep", "sure", "go ahead", "do it", "ok", "okay",
     "alright", "please do", "confirm", "approve", "allow it", "that's fine",
     "OK, Otto, yes", "um, yes", "Yes!"],
)
def test_affirmatives(said):
    assert classify_reply(said) == YES


@pytest.mark.parametrize(
    "said",
    ["no", "No.", "nope", "nah", "no thanks", "don't", "do not", "deny",
     "decline", "skip it", "otto, no"],
)
def test_negatives(said):
    assert classify_reply(said) == NO


@pytest.mark.parametrize(
    "said", ["stop", "cancel", "cancel that", "never mind", "forget it", "hold on"]
)
def test_stop_words(said):
    assert classify_reply(said) == CANCEL


@pytest.mark.parametrize(
    "said", ["say that again", "repeat that", "what did you say", "pardon"]
)
def test_repeat_words(said):
    assert classify_reply(said) == REPEAT


@pytest.mark.parametrize(
    "said",
    [
        "yes but not that one",
        "yes and then open Safari",
        "no I meant the other folder",
        "open Safari",
        "create a folder called yes on my Desktop",
        "remember that I said yes to the contract",
        "stop the tests from running",
        "",
        "   ",
    ],
)
def test_anything_that_is_not_purely_an_answer_falls_through(said):
    """The load-bearing assertion: a sentence that merely contains "yes" is a
    command, never consent."""
    assert classify_reply(said) is None


def test_normalisation_strips_filler_and_punctuation():
    assert normalise_reply("  OK, Otto: yes!  ") == "yes"
    assert normalise_reply("Um, uh, no.") == "no"


def test_the_spoken_question_asks_for_an_answer():
    asked = spoken_question("Create the folder /x?")
    assert asked.startswith("Create the folder /x?")
    assert "yes or no" in asked
    assert spoken_question("Move it to the Trash").count("?") == 1


# -- through Otto -----------------------------------------------------------


def _start(otto, request):
    """Run a request on a worker thread and wait until it asks something."""
    done: list = []
    thread = threading.Thread(
        target=lambda: done.append(otto.handle_utterance(request)), daemon=True
    )
    thread.start()
    for _ in range(300):
        if otto.pending_approvals():
            break
        time.sleep(0.01)
    return thread, done


@pytest.fixture
def asking(otto):
    """An Otto that raises real approvals and can be answered by voice."""
    otto.services.broker.set_auto(None)
    otto.voice_answers = True
    otto.set_approval_hook(None)
    return otto


def test_yes_approves_the_pending_action(asking, home):
    thread, done = _start(asking, "create a folder called Yes on my Desktop")
    assert asking.pending_approvals()

    asking.handle_utterance("yes", source="voice")
    thread.join(timeout=10)

    assert done[0].status is Status.COMPLETED
    assert (home / "Desktop" / "Yes").is_dir()


def test_no_declines_it(asking, home):
    thread, done = _start(asking, "create a folder called No on my Desktop")
    asking.handle_utterance("no", source="voice")
    thread.join(timeout=10)

    assert done[0].status is Status.REQUIRES_HUMAN
    assert not (home / "Desktop" / "No").exists()


def test_an_answer_does_not_become_a_task_of_its_own(asking, home):
    thread, done = _start(asking, "create a folder called Once on my Desktop")
    before = len(asking.tasks)
    asking.handle_utterance("yes", source="voice")
    thread.join(timeout=10)

    assert len(asking.tasks) == before, "the answer was recorded as a separate task"


def test_the_state_shows_waiting_then_moves_on(asking, home):
    seen: list[str] = []
    asking.on_state_change(lambda state, app: seen.append(state))

    thread, done = _start(asking, "create a folder called Waiting on my Desktop")
    assert asking.state == WAITING

    asking.handle_utterance("yes", source="voice")
    assert asking.state in (EXECUTING, "idle")
    thread.join(timeout=10)

    assert WAITING in seen
    assert EXECUTING in seen


def test_the_question_is_spoken(asking, home):
    thread, done = _start(asking, "create a folder called Spoken on my Desktop")
    spoken = " ".join(asking.services.mac.spoken)
    assert "Spoken" in spoken and "Say yes or no" in spoken
    asking.decide_approval(False)
    thread.join(timeout=10)


def test_a_sentence_that_starts_with_yes_is_not_consent(asking, home):
    """It is not an answer, so the question stands and nothing is approved."""
    thread, done = _start(asking, "create a folder called Careful on my Desktop")

    reply = asking.handle_utterance("yes and also open Safari", source="voice")

    assert asking.pending_approvals(), "a full sentence approved something"
    assert "still waiting" in reply.summary
    asking.decide_approval(False)
    thread.join(timeout=10)
    assert not (home / "Desktop" / "Careful").exists()


def test_an_unrelated_command_does_not_orphan_a_pending_question(asking, home):
    """The nastier version of the same bug: starting a new task while Otto is
    waiting would replace `current`, leaving the outstanding approval
    unreachable from every part of the UI until the broker timed it out."""
    thread, done = _start(asking, "create a folder called First on my Desktop")
    assert asking.pending_approvals()

    reply = asking.handle_utterance("open Safari", source="voice")

    assert asking.pending_approvals(), "the pending question was orphaned"
    assert "still waiting" in reply.summary
    assert "First" in reply.summary
    assert asking.services.mac.frontmost_app() == "Finder", "the new command ran"
    assert asking.services.audit.count("command_deferred") == 1

    # And saying stop is the documented way out.
    asking.handle_utterance("stop", source="voice")
    thread.join(timeout=10)
    assert done[0].status is Status.CANCELLED
    assert not (home / "Desktop" / "First").exists()


def test_stop_cancels_the_running_task(asking, home):
    thread, done = _start(asking, "create a folder called Stopped on my Desktop")
    asking.handle_utterance("stop", source="voice")
    thread.join(timeout=10)

    assert done[0].status is Status.CANCELLED
    assert not (home / "Desktop" / "Stopped").exists()
    assert "Stopped." in asking.services.mac.spoken


def test_yes_with_nothing_pending_says_so(otto):
    task = otto.handle_utterance("yes", source="voice")
    assert task.status is Status.COMPLETED
    assert "nothing waiting" in task.summary
    assert otto.tasks == [], "a stray yes must not enter the history"


def test_stop_with_nothing_running_says_so(otto):
    task = otto.handle_utterance("stop", source="voice")
    assert "nothing running" in task.summary


def test_repeat_says_the_last_thing_again(otto):
    otto.handle_utterance("open Safari")
    otto.services.mac.spoken.clear()

    task = otto.handle_utterance("say that again", source="voice")

    assert "Opening Safari." in otto.services.mac.spoken
    assert task.summary == "Opening Safari."


def test_repeat_before_anything_was_said(otto):
    otto.services.last_spoken = ""
    task = otto.handle_utterance("repeat that", source="voice")
    assert "haven't said anything" in task.summary


def test_an_approval_nobody_can_answer_is_denied_at_once(otto, home):
    """No UI hook and no microphone: fail closed immediately rather than making a
    scripted run sit out the broker's timeout."""
    otto.services.broker.set_auto(None)
    otto.voice_answers = False
    otto.set_approval_hook(None)

    started = time.time()
    task = otto.handle_utterance("create a folder called Nobody on my Desktop")

    assert task.status is Status.REQUIRES_HUMAN
    assert time.time() - started < 1.5, "it waited for the timeout"
    assert not (home / "Desktop" / "Nobody").exists()


def test_the_answer_reaches_the_right_approval_when_several_queue_up(asking, home):
    """Answers apply to the oldest pending question, in order."""
    from otto.core.state import Approval, Task

    task = Task(request="two things")
    asking.current = task
    first = task.add_approval(
        Approval(tool="a", args={}, agent_id="files", level="CONFIRM", reason="First?")
    )
    second = task.add_approval(
        Approval(tool="b", args={}, agent_id="files", level="CONFIRM", reason="Second?")
    )

    asking.handle_utterance("yes", source="voice")
    assert first.granted is True and second.pending

    asking.handle_utterance("no", source="voice")
    assert second.granted is False


# -- Otto must not say the same thing twice ---------------------------------


def test_a_spoken_answer_is_not_followed_by_a_summary_of_itself(otto):
    """"What can you do" used to read the whole list and then add "Done —
    spoken.", which is the kind of thing that makes an assistant tiring."""
    task = otto.handle_utterance("what can you do")

    assert "open Safari" in task.summary, "the summary should be the answer itself"
    assert "Done" not in task.summary
    assert otto.services.mac.spoken.count(task.summary) == 1


def test_an_unknown_app_answer_is_spoken_once(otto):
    task = otto.handle_utterance("open Safaris")
    assert "Did you mean" in task.summary
    assert otto.services.mac.spoken.count(task.summary) == 1


def test_an_ordinary_command_still_speaks_its_result(otto):
    task = otto.handle_utterance("open Safari")
    assert task.summary == "Opening Safari."
    assert "Opening Safari." in otto.services.mac.spoken
