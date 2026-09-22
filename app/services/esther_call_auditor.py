"""Claude-powered drift audit of Esther's calls.

Reads the FULL call transcript (not the post-call summary the classifier uses —
a summary cannot show a greeting fired twice, a goodbye said mid-call, or the
agent narrating its own reasoning) and judges it against the behaviour the store
prompt requires. Anything that would lose a customer is flagged CRITICAL and
alerted on immediately; softer problems go into the daily digest.

Transcripts come from GHL, already structured one sentence per row:
    GET /conversations/locations/{locationId}/messages/{messageId}/transcription
Each row carries transcript / startTime / endTime / mediaChannel.

mediaChannel is a per-call DIARIZATION INDEX, not a stable speaker identity.
Measured over 40 live calls, Esther's greeting landed on ch1 in 34 of them and
ch2 in 6, so a fixed channel->speaker map is wrong most of the time. An earlier
build assumed ch2 was always Esther and the audit blamed her for sentences a
human advisor said after the transfer.

So the speaker is anchored on CONTENT instead: Esther always opens with the
scripted greeting, so whichever channel speaks it is Esther for that call. A
channel first heard after Esther announces a transfer is the human who picked
up. When the greeting is nowhere to be found the call is left UNLABELLED rather
than guessed at — a mislabelled transcript produces confident, wrong alerts,
which is the one failure mode that would cost us Reid's trust.

Audits are keyed by the call's stable ghl_message_id and stored in
esther_call_audits, so a call is sent to Claude exactly ONCE — the 5-minute
sync's window rebuild never re-charges it. Same raw-httpx + forced-tool-call
pattern as esther_classifier.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx

from ..config import get_settings

log = logging.getLogger("esther.auditor")
settings = get_settings()

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
AUDITOR_MODEL = "claude-haiku-4-5"
GHL_BASE = "https://services.leadconnectorhq.com"
# Below this a call is a hang-up or a wrong number: nothing to judge.
MIN_DURATION_SEC = 45

# Esther's scripted opener — the anchor that identifies her channel per call.
_GREETING = re.compile(
    r"i'?m esther|ai service coordinator|thanks for calling mcgrath|"
    r"speak with a person, just ask", re.I)
# Esther announcing the hand-off; the next NEW channel after this is the human.
_TRANSFER = re.compile(
    r"transfer your call|connecting you now|connect you with someone|"
    r"one moment while i transfer", re.I)

# ── the failure taxonomy ──────────────────────────────────
# Severity is the alerting contract, not a comment: CRITICAL pages Reid the
# moment it lands, WARN waits for the morning digest. The split is "would this
# lose the customer, or send them somewhere wrong" vs "this was clumsy".
CRITICAL = {
    "invented_service":        "Booked or named a service the caller never asked for.",
    "wrong_concern_recorded":  "Wrote down a different concern than the caller described.",
    "wrong_vehicle_confirmed": "The caller stated one vehicle and Esther read back a "
                               "DIFFERENT one. Both must appear in the transcript. Never "
                               "using the vehicle at all is not this code.",
    "closed_mid_call":         "Said goodbye while the caller still needed something.",
    "wait_on_dropoff_only":    "Let the caller wait for DETAIL work or a DIAGNOSTIC "
                               "(a noise, a warning light, an intermittent fault). Routine "
                               "maintenance — oil change, tire rotation, inspection, recall "
                               "— is waiter-friendly and must NEVER be flagged.",
    "quoted_duration":         "Gave the caller a specific length of time for a service "
                               "('about an hour', 'two hours', 'ninety minutes'). Vague "
                               "reassurance with no number ('pretty quick') is NOT this.",
    # No fake_slot_offered code: whether a time was real cannot be settled from
    # the transcript alone — it needs the get_slots response for that call. The
    # model used it to mean "booked without enough steps", which is not the same
    # thing and fired on clean calls. Revisit if slot responses get logged.
    "slot_loop":               "Offered the SAME times again after the caller turned them "
                               "down. The repeated times must both be in the transcript.",
    "booking_not_confirmed":   "Esther said the caller was booked and then CONTRADICTED it, "
                               "or the caller asks 'am I booked?' and Esther answers "
                               "something else. NOT this: 'You're all set for Friday at 9AM' "
                               "with no date, no name and no vehicle — that is a confirmed "
                               "booking with a short call around it, which is fine.",
}
WARN = {
    # Deliberately NOT critical. Whether a human answered depends on reading the
    # diarization, and diarization is the weakest signal we have — an advisor
    # whose audio lands on Esther's channel is indistinguishable from Esther. On
    # 32 live calls this was 6 of 9 criticals, which would have made it most of
    # what Reid saw. GHL already returns lastCallStatus (voicemail / no-answer /
    # failed) per call; promote this to critical once the audit corroborates
    # against that instead of against the transcript alone.
    "transfer_never_landed":   "Esther announced a transfer and the transcript POSITIVELY "
                               "SHOWS it failed — a voicemail greeting answered, or the "
                               "caller is left saying 'hello?' to silence. If a HUMAN speaks, "
                               "or the call simply ends after the announcement, do NOT use "
                               "this code: a transfer that leaves the recording looks "
                               "identical to one that worked.",
    "greeting_repeated":       "Restarted the greeting in the middle of the call.",
    "reasoning_spoken_aloud":  "Spoke its own STRATEGY or system internals aloud — 'let me "
                               "think how to move this along', 'to keep things moving', or "
                               "reading out field names like 'transport, drop off, "
                               "reschedule appointment'. NOT this: ordinary service phrases "
                               "such as 'let me check what we have' or 'one moment'.",
    "admitted_limits":         "Told the caller it lacked information instead of helping.",
    "contact_asked_twice":     "Asked again for a name or number already given.",
    "stacked_questions":       "Asked two questions in ONE turn that each need their own "
                               "answer. A question after 'one moment', or a question plus "
                               "a confirmation of the same thing, is NOT this.",
    "coached_with_examples":   "Fed the caller example answers instead of listening.",
    "mispronounced_brand":     "Mispronounced the dealership name (e.g. 'McGrass').",
    "over_talking":            "Dominated the call; long turns the caller had to sit through.",
    "ignored_caller":          "Carried on without acknowledging what the caller just said.",
}
ALL_FAILURES = {**CRITICAL, **WARN}

# ── what Esther is REQUIRED to do ─────────────────────────
# This is the rubric. It mirrors the deployed store prompts; when those change,
# change this with them or the audit drifts from the script it is auditing.
_EXPECTED_FLOW = """\
WHAT ESTHER DOES
She answers the service line, finds out what the caller needs, and books it.
A normal call runs: greeting -> what's wrong -> which vehicle -> a time ->
confirm it back. Calls vary enormously and many end early because the caller
changes their mind, gets what they needed, or hangs up. That is normal.

This is background for reading the call. It is NOT a checklist, and a call that
covers less of it is not a worse call. Never mark a call down for a step that
did not happen.

HARD RULES — these are the failures. All of them are things Esther SAYS.
- Never state how long a service takes. Not in hours, not "about an hour".
- Never name or book a service the caller did not ask for.
- Never alter the caller's stated concern ("wind noise" is not "brake noise").
- Detail work and diagnostics are DROP-OFF ONLY; never agree the caller can wait.
  Routine maintenance (oil change, tire rotation, inspection, recall) IS
  waiter-friendly — letting someone wait for those is correct, not a failure.
- Never say internal reasoning out loud ("let me think", "let me line this up",
  "to keep things moving"). The caller should hear only the result.
- One question per turn.
- When a person is requested, transfer promptly and make sure a human answers.
- Never greet twice. Never close while the caller still needs something.
- Never re-offer times the caller has already turned down.
"""

def _codebook() -> str:
    """The failure codes with their meanings. Without this the model sees only
    bare code names in the enum and has to guess what they mean — which is how
    'phone number never asked' came back as contact_asked_twice."""
    lines = ["FAILURE CODES — use a code ONLY for what it literally says."]
    for bucket, label in ((CRITICAL, "CRITICAL"), (WARN, "WARNING")):
        lines.append(f"\n{label}:")
        lines += [f"  {code} — {desc}" for code, desc in sorted(bucket.items())]
    return "\n".join(lines)


_SYSTEM = (
    "You audit phone calls handled by Esther, an AI voice agent for a car "
    "dealership service department. You are given the full transcript, labelled "
    "by speaker and timestamped.\n\n"
    "Judge ONLY Esther's turns (labelled ESTHER). Lines labelled CALLER or HUMAN "
    "are other people — a clumsy sentence from a human advisor after a transfer "
    "is NOT an Esther failure. This distinction matters: do not attribute a "
    "human's words to Esther.\n\n"
    + _EXPECTED_FLOW + "\n" + _codebook() +
    "\n\nHOW TO JUDGE\n"
    "Report a failure only for something the transcript SHOWS Esther do or say, "
    "and quote that exact sentence.\n"
    "NEVER report a failure because something is MISSING. A call that skips a "
    "step, never takes a name, never names the vehicle, or ends sooner than you "
    "expected is NOT a failure. Callers interrupt, hang up and change their "
    "minds, and the store already holds most of this on file. Every failure you "
    "report must point at words that were actually spoken — if your reason "
    "contains 'never', 'without', 'failed to' or 'did not', you are reporting an "
    "absence: drop it.\n"
    "The EXPECTED CALL FLOW is context for reading the call, not a checklist to "
    "mark off. Do not score the call against it step by step.\n"
    "The caller's phone number arrives with the call, so Esther does not need to "
    "ask for it. Not asking is never a failure.\n"
    "transfer_never_landed applies ONLY when no HUMAN line appears anywhere in "
    "the transcript. If a HUMAN speaks, the transfer landed — do not report it.\n"
    "Before reporting a code, check your own reason against that code's "
    "definition. If the reason does not match the definition, report nothing — "
    "do not reach for the closest available code. If no code fits what you saw, "
    "report nothing.\n"
    "If your own reason names the HUMAN advisor as the one who did it, that is "
    "not an Esther failure — drop it. Only a line labelled ESTHER can be quoted "
    "as evidence.\n"
    "A call that simply went well has no failures. Say so plainly rather than "
    "inventing a criticism — a false alarm costs more than a missed nitpick.\n"
    "Speech-to-text is imperfect: ignore transcription noise, garbled words and "
    "dropped syllables unless they changed what the caller received.\n\n"
    "Return ONE verdict via the audit_call tool."
)

_TOOL = {
    "name": "audit_call",
    "description": "Return the structured audit of one AI-handled phone call.",
    "input_schema": {
        "type": "object",
        "properties": {
            "call_ok": {
                "type": "boolean",
                "description": "True if the call met the expected flow with no failures worth flagging.",
            },
            "failures": {
                "type": "array",
                "description": "Every failure the transcript shows. Empty when the call went fine.",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "enum": sorted(ALL_FAILURES),
                                 "description": "The failure code."},
                        "quote": {"type": "string",
                                  "description": "The exact sentence from the transcript that shows it."},
                        "at_sec": {"type": "number",
                                   "description": "Seconds into the call where it occurred."},
                        "why": {"type": "string",
                                "description": "One sentence: what the caller experienced as a result."},
                    },
                    "required": ["code", "quote", "why"],
                },
            },
            "booked": {"type": "boolean",
                       "description": "True if an appointment was confirmed back to the caller."},
            "transfer_requested": {"type": "boolean",
                                   "description": "True if the caller asked for a human at any point."},
            "transfer_connected": {"type": "boolean",
                                   "description": "True only if a human actually spoke on the call."},
            "caller_left_satisfied": {"type": "boolean",
                                      "description": "Did the caller get what they called for?"},
            "headline": {"type": "string",
                         "description": "One short line describing the call, for the alert and the dashboard."},
            "confidence": {"type": "number",
                           "description": "0.0-1.0 confidence in this verdict. Below 0.6 suppresses alerting."},
        },
        "required": ["call_ok", "failures", "booked", "transfer_requested",
                     "transfer_connected", "headline", "confidence"],
    },
}


def enabled() -> bool:
    return bool(settings.anthropic_api_key)


def identify_speakers(rows: list[dict]) -> tuple[int | None, set[int]]:
    """Work out which channel is Esther, and which are the humans who joined
    after a transfer. Returns (esther_channel, human_channels).

    esther_channel is None when the greeting never appears — the caller hung up
    during it, or diarization collapsed everyone onto one channel. Callers must
    treat None as "cannot audit", never as a default."""
    ordered = sorted(rows, key=lambda x: x.get("startTime") or 0)

    esther_ch = next(
        (r.get("mediaChannel") for r in ordered if _GREETING.search(r.get("transcript") or "")),
        None,
    )
    if esther_ch is None:
        return None, set()

    # Channels heard for the first time after Esther announces the hand-off are
    # whoever picked up. Before that point, anyone who isn't Esther is the caller.
    transfer_at = next(
        (float(r.get("startTime") or 0) for r in ordered
         if r.get("mediaChannel") == esther_ch and _TRANSFER.search(r.get("transcript") or "")),
        None,
    )
    humans: set[int] = set()
    if transfer_at is not None:
        seen_before = {r.get("mediaChannel") for r in ordered
                       if float(r.get("startTime") or 0) <= transfer_at}
        humans = {r.get("mediaChannel") for r in ordered
                  if float(r.get("startTime") or 0) > transfer_at} - seen_before
    return esther_ch, humans


def to_script(rows: list[dict]) -> str | None:
    """GHL's per-sentence transcription -> a labelled, timestamped script.

    Returns None when the speakers cannot be identified, so the call is skipped
    rather than audited against guessed labels.

    Consecutive sentences from the same speaker are merged into one turn, which
    is how a human reads a call and what keeps stacked-question and over-talking
    judgements honest."""
    esther_ch, humans = identify_speakers(rows)
    if esther_ch is None:
        return None

    def who(ch) -> str:
        if ch == esther_ch:
            return "ESTHER"
        return "HUMAN" if ch in humans else "CALLER"

    turns: list[list] = []
    for r in sorted(rows, key=lambda x: x.get("startTime") or 0):
        text = (r.get("transcript") or "").strip()
        if not text:
            continue
        label, start = who(r.get("mediaChannel")), float(r.get("startTime") or 0)
        if turns and turns[-1][0] == label:
            turns[-1][2].append(text)
        else:
            turns.append([label, start, [text]])
    return "\n".join(
        f"[{int(s) // 60:02d}:{int(s) % 60:02d}] {lab}: {' '.join(parts)}"
        for lab, s, parts in turns
    )


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower())


def drop_misattributed(verdict: dict, script: str) -> dict:
    """Remove failures whose quote is not something ESTHER actually said.

    Asking the model not to blame Esther for other people's words is not enough
    on its own — it has quoted the caller's "Perfect." and a human advisor's
    duration estimate. This checks the quote against the ESTHER turns and drops
    anything that does not appear there, which also catches invented quotes."""
    failures = verdict.get("failures") or []
    if not failures:
        return verdict

    esther = _norm(" ".join(
        line.split(": ", 1)[1] for line in script.splitlines()
        if "] ESTHER: " in line and ": " in line
    ))
    kept = []
    for f in failures:
        q = _norm(f.get("quote", ""))
        # An ellipsis means the model stitched two turns together; check the
        # longest fragment rather than the whole thing.
        frag = max(q.split("   "), key=len) if "   " in q else q
        if frag and frag[:80] in esther:
            kept.append(f)
        else:
            log.info("auditor: dropped %s — quote not in an ESTHER turn: %.60s",
                     f.get("code"), f.get("quote", ""))
    out = dict(verdict)
    out["failures"] = kept
    if not kept:
        out["call_ok"] = True
    return out


def severity_of(failures: list[dict]) -> str:
    """ok | warn | critical — the alerting decision, derived from the codes."""
    codes = {f.get("code") for f in failures or []}
    if codes & set(CRITICAL):
        return "critical"
    return "warn" if codes else "ok"


def should_alert(verdict: dict, min_confidence: float = 0.6) -> bool:
    """A critical failure the model is actually sure about. Confidence gating is
    what keeps a shaky reading off Reid's phone."""
    if not verdict:
        return False
    if float(verdict.get("confidence") or 0) < min_confidence:
        return False
    return severity_of(verdict.get("failures") or []) == "critical"


async def fetch_transcript(
    client: httpx.AsyncClient, location_id: str, message_id: str, token: str
) -> list[dict] | None:
    """The per-sentence transcription for one call. None when GHL has none yet —
    it lands a little after the call ends, so a fresh call is retried next run."""
    try:
        r = await client.get(
            f"{GHL_BASE}/conversations/locations/{location_id}/messages/{message_id}/transcription",
            headers={"Authorization": f"Bearer {token}", "Version": "2021-07-28",
                     "Accept": "application/json"},
            timeout=30.0,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
        log.warning("auditor: transcript fetch failed for %s: %s", message_id, exc)
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    return data if isinstance(data, list) and data else None


async def audit_unaudited(pg, tokens: dict[str, str], limit: int = 40) -> int:
    """Audit recent calls that have no audit yet. Bounded per run so one sync
    can't fan out over the whole backlog. Returns how many were newly audited.

    Only real conversations are audited — a call shorter than MIN_DURATION_SEC is
    a hang-up or a wrong number and has nothing to judge.

    NOTE: nothing is alerted from here. Rows land with alerted_at NULL and the
    dashboard reads them; wiring the notifier is a separate, deliberate step.
    """
    if not enabled():
        return 0
    rows = await pg.fetch(
        """
        select c.ghl_message_id, c.store_id, c.local_date, c.duration_sec,
               s.key as store_key, s.ghl_location_id
        from esther_calls c
        join esther_stores s on s.id = c.store_id
        left join esther_call_audits a using (ghl_message_id)
        where a.ghl_message_id is null
          and s.ghl_location_id is not null
          -- duration_sec was only added to the ingest recently, so the backlog
          -- has NULLs; those are let through and filtered on transcript length.
          and (c.duration_sec is null or c.duration_sec >= $1)
        order by c.started_at desc
        limit $2
        """,
        MIN_DURATION_SEC, limit,
    )
    rows = [r for r in rows if r["store_key"] in tokens]
    if not rows:
        return 0

    # Claude calls and transcript fetches run concurrently (HTTP parallelises
    # fine), but the DB writes go one at a time — a single asyncpg connection
    # cannot run concurrent operations.
    sem = asyncio.Semaphore(5)

    async def _audit(client, r):
        async with sem:
            rowsj = await fetch_transcript(
                client, r["ghl_location_id"], r["ghl_message_id"], tokens[r["store_key"]])
            if not rowsj:
                return r, None, None          # no transcript yet — retry next run
            # The transcript is the authority on length when duration is unknown.
            secs = int(max((s.get("endTime") or 0) for s in rowsj))
            if secs < MIN_DURATION_SEC:
                return r, None, "too_short"   # hang-up / wrong number — nothing to judge
            script = to_script(rowsj)
            if script is None:
                return r, None, "unlabelled"  # speakers unidentifiable — record, don't retry
            return r, await audit_one(client, script, r["duration_sec"] or secs), None

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_audit(client, r) for r in rows])

    done = 0
    for r, res, marker in results:
        if marker:
            # Recorded, not audited: keeps it out of the retry set and keeps the
            # share of unauditable calls visible instead of silently shrinking
            # the denominator.
            await pg.execute(
                """
                insert into esther_call_audits
                  (ghl_message_id, store_id, local_date, severity, model, audited_at)
                values ($1,$2,$3,$4,$5, now())
                on conflict (ghl_message_id) do nothing
                """,
                r["ghl_message_id"], r["store_id"], r["local_date"], marker, AUDITOR_MODEL,
            )
            continue
        if not res:
            continue
        failures = res.get("failures") or []
        await pg.execute(
            """
            insert into esther_call_audits
              (ghl_message_id, store_id, local_date, call_ok, severity, failures,
               detail, booked, transfer_requested, transfer_connected, headline,
               confidence, model, audited_at)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13, now())
            on conflict (ghl_message_id) do nothing
            """,
            r["ghl_message_id"], r["store_id"], r["local_date"],
            bool(res.get("call_ok")), severity_of(failures),
            [f.get("code") for f in failures if f.get("code")],
            json.dumps(failures),
            res.get("booked"), res.get("transfer_requested"),
            res.get("transfer_connected"), res.get("headline"),
            float(res.get("confidence") or 0), AUDITOR_MODEL,
        )
        done += 1
    log.info("auditor: audited %d calls", done)
    return done


async def audit_one(
    client: httpx.AsyncClient, script: str, duration_sec: int | None = None
) -> dict | None:
    """Audit a single call transcript. Returns the tool input dict, or None on failure."""
    head = f"Call duration: {duration_sec}s\n\n" if duration_sec else ""
    body = {
        "model": AUDITOR_MODEL,
        "max_tokens": 1500,
        # Deterministic: the same call must not pass one run and alert the next.
        # Caught by the golden eval, where a drop-off-only miss reproduced only
        # intermittently until the temperature was pinned.
        "temperature": 0,
        "system": _SYSTEM,
        "tools": [_TOOL],
        "tool_choice": {"type": "tool", "name": "audit_call"},
        "messages": [{"role": "user", "content": f"{head}Transcript:\n{script.strip()}"}],
    }
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    for backoff in (1.0, 2.5, 0):
        try:
            resp = await client.post(ANTHROPIC_URL, json=body, headers=headers, timeout=60.0)
            if resp.status_code == 200:
                for b in resp.json().get("content", []):
                    if b.get("type") == "tool_use":
                        v = b.get("input") or None
                        return drop_misattributed(v, script) if v else None
                return None
            if resp.status_code in (429, 500, 502, 503, 529) and backoff:
                await asyncio.sleep(backoff)
                continue
            log.warning("auditor: Anthropic %s: %s", resp.status_code, resp.text[:200])
            return None
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
            if backoff:
                await asyncio.sleep(backoff)
                continue
            log.warning("auditor: %s", exc)
            return None
    return None
