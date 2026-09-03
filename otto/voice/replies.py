"""Recognising a spoken answer.

Otto is voice-first, so the answers to Otto's own questions have to arrive by
voice too. When an approval is waiting, "yes" must mean *approve this*, not
"start a new task called yes".

The classification is deliberately strict: the whole utterance must be an answer.
"Yes" is an answer; "yes, and also open Safari" is not, and falls through to the
normal command path rather than silently approving something on the strength of
its first word. Getting that wrong in the permissive direction would mean a
stray word approving an irreversible action.
"""

from __future__ import annotations

import re

YES = "yes"
NO = "no"
CANCEL = "cancel"
REPEAT = "repeat"

#: Whole utterances that mean "go ahead". ASR output is unpunctuated and often
#: carries filler, so the phrasings people actually use are enumerated rather
#: than pattern-matched — a pattern like "starts with yes" would approve
#: "yes but not that one".
AFFIRMATIVE = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "yes please",
        "yes do it",
        "yes go ahead",
        "yes that's right",
        "yes thats right",
        "sure",
        "go ahead",
        "do it",
        "do that",
        "ok",
        "okay",
        "okay do it",
        "alright",
        "all right",
        "affirmative",
        "confirm",
        "confirmed",
        "approve",
        "approved",
        "allow",
        "allow it",
        "accept",
        "please do",
        "please do it",
        "that's fine",
        "thats fine",
        "fine",
        "go for it",
        "carry on",
        "continue",
        "proceed",
        "sounds good",
        "correct",
    }
)

NEGATIVE = frozenset(
    {
        "no",
        "nope",
        "nah",
        "no thanks",
        "no thank you",
        "no don't",
        "no dont",
        "don't",
        "dont",
        "do not",
        "negative",
        "deny",
        "denied",
        "refuse",
        "reject",
        "decline",
        "skip",
        "skip it",
        "leave it",
    }
)

STOP = frozenset(
    {
        "stop",
        "stop it",
        "stop that",
        "cancel",
        "cancel it",
        "cancel that",
        "never mind",
        "nevermind",
        "forget it",
        "abort",
        "wait",
        "wait no",
        "hold on",
    }
)

REPEAT_THAT = frozenset(
    {
        "repeat",
        "repeat that",
        "say that again",
        "say again",
        "what did you say",
        "what was that",
        "come again",
        "pardon",
    }
)

_STRIP = re.compile(r"^(otto[\s,:]+|ok |okay |um |uh |er |well )+", re.I)
_PUNCTUATION = re.compile(r"[^\w\s']+")
_SPACES = re.compile(r"\s+")


def normalise_reply(text: str) -> str:
    """Lowercase, depunctuate, and drop the leading filler ASR loves to add."""
    cleaned = (text or "").strip().lower()
    cleaned = _PUNCTUATION.sub(" ", cleaned)
    cleaned = _SPACES.sub(" ", cleaned).strip()
    # Strip filler repeatedly: "ok otto, um, yes" → "yes".
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _STRIP.sub("", cleaned).strip()
    return cleaned


def classify_reply(text: str) -> str | None:
    """`YES`, `NO`, `CANCEL`, `REPEAT`, or None when this is a normal command.

    Returns None for anything that is not *entirely* an answer, so a sentence
    that merely starts with "yes" is treated as a command and never as consent.
    """
    cleaned = normalise_reply(text)
    if not cleaned or len(cleaned.split()) > 4:
        return None
    if cleaned in STOP:
        return CANCEL
    if cleaned in AFFIRMATIVE:
        return YES
    if cleaned in NEGATIVE:
        return NO
    if cleaned in REPEAT_THAT:
        return REPEAT
    return None


def spoken_question(reason: str) -> str:
    """How an approval request sounds when Otto asks it out loud."""
    question = (reason or "Is that OK?").strip()
    if not question.endswith("?"):
        question += "?"
    return f"{question} Say yes or no."
