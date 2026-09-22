"""Tests for the Esther call auditor.

Two layers, deliberately separate:

  * the plain unit tests below run everywhere and cover the parts that must
    never be wrong — speaker labelling, turn merging, and the severity rules
    that decide whether Reid's phone rings;

  * the GOLDEN_CALLS suite at the bottom is scored against the live model and
    is skipped unless ANTHROPIC_API_KEY is set and RUN_AUDIT_EVAL=1. Every case
    is a real failure taken from a McGrath call, so a regression in the rubric
    shows up as a case that stops being caught.

Run the eval:  RUN_AUDIT_EVAL=1 pytest tests/test_call_auditor.py -k eval -s
"""
from __future__ import annotations

import os

import httpx
import pytest

from app.services.esther_call_auditor import (
    CRITICAL,
    WARN,
    audit_one,
    drop_misattributed,
    identify_speakers,
    severity_of,
    should_alert,
    to_script,
)


# ── speaker labelling and turn merging ────────────────────

def _row(ch, text, start):
    return {"mediaChannel": ch, "transcript": text, "startTime": start, "endTime": start + 1}


GREET = "Thanks for calling McGrath Honda. I'm Esther, your AI service coordinator."


def test_esther_is_found_by_her_greeting_not_by_channel_number():
    """mediaChannel is a per-call diarization index, so the greeting is the only
    reliable anchor. Here Esther is on ch1; a fixed ch2 map would invert it."""
    rows = [_row(1, GREET, 0), _row(2, "I need an oil change", 5)]
    esther, _ = identify_speakers(rows)
    assert esther == 1
    script = to_script(rows)
    assert "ESTHER: Thanks for calling" in script
    assert "CALLER: I need an oil change" in script


def test_the_same_layout_on_another_channel_still_resolves():
    """Same call shape, Esther on ch2 instead. Both must work."""
    rows = [_row(2, GREET, 0), _row(1, "I need an oil change", 5)]
    esther, _ = identify_speakers(rows)
    assert esther == 2
    assert "ESTHER: Thanks for calling" in to_script(rows)


def test_channel_joining_after_the_transfer_is_the_human():
    rows = [_row(1, GREET, 0),
            _row(2, "Let me talk to a person", 5),
            _row(1, "One moment while I transfer your call.", 8),
            _row(3, "Auto Group, this is Mitch.", 14)]
    esther, humans = identify_speakers(rows)
    assert esther == 1 and humans == {3}
    script = to_script(rows)
    assert "HUMAN: Auto Group, this is Mitch." in script
    assert "CALLER: Let me talk to a person" in script


def test_a_channel_heard_before_the_transfer_is_never_the_human():
    """The caller keeps talking after the hand-off; they are not the advisor."""
    rows = [_row(1, GREET, 0),
            _row(2, "Person please", 5),
            _row(1, "Connecting you now.", 8),
            _row(2, "Hello?", 12)]
    esther, humans = identify_speakers(rows)
    assert esther == 1 and humans == set()
    assert "HUMAN" not in to_script(rows)


def test_call_without_the_greeting_is_unlabelled_rather_than_guessed():
    """No anchor means no audit. Guessing here produced confident, wrong alerts."""
    rows = [_row(1, "Hello?", 0), _row(2, "Yeah hi", 2)]
    assert identify_speakers(rows) == (None, set())
    assert to_script(rows) is None


def test_consecutive_sentences_merge_into_one_turn():
    rows = [_row(1, GREET, 0), _row(1, "I can help with that.", 1),
            _row(2, "Great.", 2)]
    lines = to_script(rows).splitlines()
    assert len(lines) == 2
    assert lines[0] == f"[00:00] ESTHER: {GREET} I can help with that."


def test_turns_are_ordered_and_timestamped_mmss():
    rows = [_row(1, GREET, 125), _row(2, "earlier", 5)]
    lines = to_script(rows).splitlines()
    assert lines[0].startswith("[00:05] CALLER:")
    assert lines[1].startswith("[02:05] ESTHER:")


def test_blank_sentences_are_dropped():
    lines = to_script([_row(1, GREET, 0), _row(2, "   ", 1),
                       _row(2, "Real line", 2)]).splitlines()
    assert len(lines) == 2
    assert lines[1] == "[00:02] CALLER: Real line"


def test_empty_transcript_is_unlabelled():
    assert to_script([]) is None


# ── misattribution guard ──────────────────────────────────

SCRIPT = """[00:00] ESTHER: Thanks for calling McGrath Honda. What can I get scheduled?
[00:08] CALLER: Perfect, an oil change.
[00:12] ESTHER: I have Thursday at 9AM.
[00:20] HUMAN: It usually takes about an hour."""


def test_a_quote_esther_really_said_is_kept():
    v = {"failures": [{"code": "quoted_duration", "quote": "I have Thursday at 9AM."}]}
    assert len(drop_misattributed(v, SCRIPT)["failures"]) == 1


def test_the_callers_own_words_are_not_an_esther_failure():
    """The model quoted the caller's "Perfect." as closed_mid_call on a real call."""
    v = {"failures": [{"code": "closed_mid_call", "quote": "Perfect."}]}
    out = drop_misattributed(v, SCRIPT)
    assert out["failures"] == [] and out["call_ok"] is True


def test_the_human_advisors_words_are_not_an_esther_failure():
    v = {"failures": [{"code": "quoted_duration", "quote": "It usually takes about an hour."}]}
    assert drop_misattributed(v, SCRIPT)["failures"] == []


def test_an_invented_quote_is_dropped():
    v = {"failures": [{"code": "invented_service", "quote": "I'll book you a transmission flush."}]}
    assert drop_misattributed(v, SCRIPT)["failures"] == []


def test_punctuation_and_case_differences_still_match():
    v = {"failures": [{"code": "slot_loop", "quote": "i have thursday at 9am"}]}
    assert len(drop_misattributed(v, SCRIPT)["failures"]) == 1


def test_a_clean_verdict_passes_through_untouched():
    v = {"failures": [], "call_ok": True}
    assert drop_misattributed(v, SCRIPT) == v


# ── severity: the alerting contract ───────────────────────

def test_clean_call_is_ok():
    assert severity_of([]) == "ok"


def test_soft_problem_is_only_a_warning():
    assert severity_of([{"code": "greeting_repeated"}]) == "warn"


def test_customer_losing_failure_is_critical():
    assert severity_of([{"code": "invented_service"}]) == "critical"


def test_critical_wins_when_mixed_with_warnings():
    assert severity_of([{"code": "greeting_repeated"},
                        {"code": "quoted_duration"}]) == "critical"


def test_taxonomies_do_not_overlap():
    """A code in both buckets would make severity ambiguous."""
    assert not (set(CRITICAL) & set(WARN))


@pytest.mark.parametrize("verdict,expected", [
    ({"failures": [{"code": "invented_service"}], "confidence": 0.9}, True),
    ({"failures": [{"code": "invented_service"}], "confidence": 0.4}, False),
    ({"failures": [{"code": "greeting_repeated"}], "confidence": 0.99}, False),
    ({"failures": [], "confidence": 1.0}, False),
    ({}, False),
    (None, False),
])
def test_only_confident_critical_failures_alert(verdict, expected):
    """Reid gets a text only for a real, customer-losing failure the model is
    sure about. A shaky reading stays on the dashboard."""
    assert should_alert(verdict) is expected


def test_missing_confidence_does_not_alert():
    assert should_alert({"failures": [{"code": "closed_mid_call"}]}) is False


# ── golden calls: real failures, scored against the model ──
# Each transcript is trimmed from an actual McGrath call. `expect` is the code
# that MUST be reported; `forbid` guards the false positives we have already hit.

GOLDEN_CALLS = [
    pytest.param(
        "closed_mid_call",
        """[00:00] ESTHER: Thanks for calling McGrath Honda of Saint Charles. I'm Esther, your AI service coordinator. What can I get scheduled for you today?
[00:14] CALLER: I need an oil change.
[00:16] ESTHER: Have a great day.""",
        None, id="says goodbye instead of booking",
    ),
    pytest.param(
        "greeting_repeated",
        """[00:00] ESTHER: Thanks for calling McGrath Honda of Elgin. I'm Esther, your AI service coordinator. What can I get scheduled for you today?
[00:12] CALLER: My car is making a noise in the back.
[00:18] ESTHER: Thanks for calling McGrath Honda of Elgin. I'm Esther, your AI service coordinator. What can I get scheduled for you today?
[00:29] CALLER: I just told you that.""",
        None, id="restarts the greeting mid-call",
    ),
    pytest.param(
        "quoted_duration",
        """[00:00] ESTHER: Thanks for calling McGrath Acura of Libertyville. What can I get scheduled for you today?
[00:08] CALLER: I want to get the car detailed. How long does that take?
[00:14] ESTHER: I think it's about an hour. You're welcome to wait here for it.""",
        None, id="invents a time estimate",
    ),
    pytest.param(
        "wait_on_dropoff_only",
        """[00:00] ESTHER: Thanks for calling McGrath Honda of Saint Charles. What can I get scheduled for you today?
[00:09] CALLER: I'd like a full detail on Thursday. Can I wait for it?
[00:16] ESTHER: Absolutely, you can wait for it. I'll put you down as a waiter for Thursday at ten.""",
        None, id="accepts a waiter for drop-off-only detail",
    ),
    pytest.param(
        "wrong_concern_recorded",
        """[00:00] ESTHER: What can I get scheduled for you today?
[00:05] CALLER: There's a wind noise coming from the driver's door at highway speed.
[00:13] ESTHER: Got it, a brake noise. I'll book a diagnostic for the brake noise on Tuesday at nine.
[00:22] CALLER: No, wind noise.""",
        None, id="substitutes the caller's concern",
    ),
    pytest.param(
        "invented_service",
        """[00:00] ESTHER: What can I get scheduled for you today?
[00:04] CALLER: Something's wrong with the rear suspension, it clunks over bumps.
[00:12] ESTHER: Perfect, I'll get you booked for an oil change on Wednesday at eleven.""",
        None, id="books a service never mentioned",
    ),
    pytest.param(
        "wrong_vehicle_confirmed",
        """[00:00] ESTHER: Which vehicle is this for?
[00:03] CALLER: It's my 2023 Pilot.
[00:07] ESTHER: Great, so that's the 2020 Pilot. I'll get that scheduled.""",
        None, id="confirms the wrong model year",
    ),
    pytest.param(
        "transfer_never_landed",
        """[00:00] ESTHER: What can I get scheduled for you today?
[00:06] CALLER: I want to talk to a real person.
[00:10] ESTHER: Of course. Let me connect you with someone who can help. One moment while I transfer your call.
[00:18] ESTHER: Connecting you now.
[00:41] CALLER: Hello? Is anyone there?
[00:52] CALLER: Hello?""",
        None, id="transfer announced but nobody answers",
    ),
    pytest.param(
        "slot_loop",
        """[00:00] ESTHER: I can do 8AM, 8:30AM, or 9AM on Thursday. Which works best?
[00:09] CALLER: Do you have anything around five in the afternoon?
[00:15] ESTHER: I can do 8AM, 8:30AM, or 9AM on Thursday. Which works best?
[00:24] CALLER: I already said those don't work.
[00:28] ESTHER: I can do 8AM, 8:30AM, or 9AM on Thursday. Which works best?""",
        None, id="re-offers rejected times",
    ),
    pytest.param(
        "reasoning_spoken_aloud",
        """[00:00] ESTHER: What can I get scheduled for you today?
[00:05] CALLER: A noise in the car.
[00:09] ESTHER: Okay. Let me think about how to move this along smoothly. Let me line up what we need to keep moving.
[00:19] CALLER: Sorry, what?""",
        None, id="narrates its own reasoning",
    ),
    # ── false-positive guards ──
    # Each of these fired wrongly on real McGrath calls before the rubric was
    # tightened. They are the noise floor: 17 of 32 live calls would have paged
    # Reid, mostly from these.
    pytest.param(
        None,
        """[00:00] ESTHER: Thanks for calling McGrath Kia of St. Charles. I'm Esther, your AI service coordinator. What can I get scheduled for you today?
[00:14] CALLER: Just an oil change please.
[00:18] ESTHER: Happy to help. Would you like to wait for it or drop the car off?
[00:24] CALLER: I'd like to wait if that's alright.
[00:28] ESTHER: Of course. You can wait in our customer lounge. I'll note your appointment as a waiting visit.
[00:36] CALLER: Perfect.""",
        "any", id="GUARD waiting for an oil change is allowed",
    ),
    pytest.param(
        None,
        """[00:00] ESTHER: Thanks for calling McGrath Honda of Elgin. I'm Esther, your AI service coordinator. What can I get scheduled for you today?
[00:13] CALLER: How long does an oil change usually take?
[00:18] ESTHER: It depends on the day and how busy we are, but it's usually pretty quick.
[00:25] CALLER: Okay, let's book one for Friday.
[00:29] ESTHER: You're all set for Friday at 9AM for an oil change.""",
        "any", id="GUARD vague reassurance is not a quoted duration",
    ),
    pytest.param(
        None,
        """[00:00] ESTHER: Thanks for calling McGrath Honda of Saint Charles. I'm Esther, your AI service coordinator. What can I get scheduled for you today?
[00:15] CALLER: I need to talk to someone about my bill.
[00:20] ESTHER: Of course. Let me connect you with someone who can help. One moment while I transfer your call.""",
        "any", id="GUARD call ending at the transfer is not a failed transfer",
    ),
    pytest.param(
        None,
        """[00:00] ESTHER: Thanks for calling McGrath Kia of St. Charles. I'm Esther, your AI service coordinator. What can I get scheduled for you today?
[00:14] CALLER: My check engine light is on.
[00:19] ESTHER: Let me check what we have available. One moment. Is the light flashing, or on solid?
[00:28] CALLER: Solid.
[00:31] ESTHER: Thanks. I have Thursday at 9AM. Shall I book that?""",
        "any", id="GUARD a question after 'one moment' is not stacked",
    ),
    pytest.param(
        None,
        """[00:00] ESTHER: Thanks for calling McGrath Honda of Saint Charles. I'm Esther, your AI service coordinator. Anytime you'd like to speak with a person, just ask. What can I get scheduled for you today?
[00:15] CALLER: I need an oil change on my 2021 Civic.
[00:21] ESTHER: Happy to help. Which day works best for you?
[00:26] CALLER: Thursday morning if you have it.
[00:30] ESTHER: I have 8AM, 8:30AM or 9AM on Thursday. Which would you like?
[00:38] CALLER: Nine is good.
[00:40] ESTHER: Can I get your name?
[00:42] CALLER: Dave Wilson.
[00:45] ESTHER: Thanks Dave. You're all set for Thursday, September 24th at 9AM for an oil change on the 2021 Civic. Anything else I can help with?
[00:58] CALLER: No that's it, thanks.
[01:00] ESTHER: Have a great day.""",
        "any", id="GUARD clean booking must pass",
    ),
    pytest.param(
        None,
        """[00:00] ESTHER: Thanks for calling McGrath Honda of Saint Charles. I'm Esther, your AI service coordinator. Anytime you'd like to speak with a person, just ask. What can I get scheduled for you today?
[00:16] CALLER: I'd rather just talk to somebody about a warranty question.
[00:22] ESTHER: Of course. Let me connect you with someone who can help. One moment while I transfer your call.
[00:31] HUMAN: Auto Group, this is Mitch, how can I help you?
[00:36] CALLER: My car is making a noise and I think it's under warranty.
[00:42] HUMAN: I should be able to get a couple of those resolved for you. It usually takes about an hour.
[00:51] CALLER: Great, thank you.""",
        "any", id="GUARD human advisor's words are not Esther's failure",
    ),
]


@pytest.mark.skipif(
    not (os.getenv("ANTHROPIC_API_KEY") and os.getenv("RUN_AUDIT_EVAL")),
    reason="live-model eval; set ANTHROPIC_API_KEY and RUN_AUDIT_EVAL=1",
)
@pytest.mark.parametrize("expect,script,forbid", GOLDEN_CALLS)
async def test_eval_auditor_catches_known_failures(expect, script, forbid):
    async with httpx.AsyncClient() as client:
        verdict = await audit_one(client, script)

    assert verdict is not None, "auditor returned nothing"
    codes = {f.get("code") for f in verdict.get("failures") or []}

    if expect:
        assert expect in codes, (
            f"missed {expect!r}; auditor reported {codes or 'nothing'} "
            f"— headline: {verdict.get('headline')!r}"
        )
    if forbid == "any":
        assert not codes, (
            f"false positive on a good call: {codes} "
            f"— headline: {verdict.get('headline')!r}"
        )
        assert verdict.get("call_ok") is True
