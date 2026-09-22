"""Appraisal conversations, rebuilt from GHL into esther_equity_messages.

Reid, 22 Sep: "It's hard to find the conversations and if we had 100 come in
today how many got the message".

The equity flow sends its texts from INSIDE GoHighLevel — our endpoint only
rules on eligibility and hands back the wording — so nothing on our side has
ever recorded that a text went out. esther_equity_appraisals holds only the
people who said yes, which is the numerator; Reid is asking for the denominator.

So the record is reconstructed from the conversation itself. The opener is fixed
copy ("...professionally appraised lately?"), which makes every appraisal thread
findable among ordinary SMS.

Outcome is read from OUR OWN follow-ups rather than from the customer's words.
The connector sends a different scripted message down each branch, so the
presence of the confirm line in the thread IS the yes. That keeps this in step
with equity.py instead of re-deciding intent here and drifting away from it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger("esther.equity_messages")

GHL_BASE = "https://services.leadconnectorhq.com"

# The opener, as equity.py writes it. Matching on the distinctive middle of the
# sentence rather than the whole thing, so a tweak to the greeting or the STOP
# notice does not silently stop finding threads.
_OPENER = re.compile(r"professionally appraised", re.I)

# The store's scripted branches — these identify the outcome. Kept as fragments
# for the same reason: resilient to punctuation edits, specific enough not to
# collide with each other.
_SEE_OPTIONS = re.compile(r"actively looking for vehicles like yours", re.I)
_CONFIRM = re.compile(r"come find you in the lounge", re.I)
_DECLINE = re.compile(r"we'll see you at your service appointment", re.I)
_VALUE_ONLY = re.compile(r"have that number ready for you", re.I)
_STOP = re.compile(r"^\s*(stop|stopall|unsubscribe|end|quit)\s*$", re.I)


def _dt(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def classify_thread(msgs: list[dict]) -> dict | None:
    """One appraisal conversation -> the row the dashboard reads.

    Returns None when the thread contains no appraisal opener at all, which is
    how ordinary service SMS are filtered out."""
    sms = sorted(
        [m for m in msgs if m.get("messageType") == "TYPE_SMS" and (m.get("body") or "").strip()],
        key=lambda m: str(m.get("dateAdded") or ""),
    )
    opener = next((m for m in sms if _OPENER.search(m.get("body") or "")), None)
    if opener is None:
        return None

    sent_at = _dt(opener.get("dateAdded"))
    # Only what happened from the opener onwards: an earlier service exchange in
    # the same thread is not part of this conversation.
    after = [m for m in sms if str(m.get("dateAdded") or "") >= str(opener.get("dateAdded") or "")]

    inbound = [m for m in after if (m.get("direction") or "").lower() == "inbound"]
    outbound_text = " ".join((m.get("body") or "") for m in after
                             if (m.get("direction") or "").lower() != "inbound")

    first_reply = inbound[0] if inbound else None
    reply_text = (first_reply.get("body") or "").strip() if first_reply else None

    if any(_STOP.match((m.get("body") or "")) for m in inbound):
        outcome = "opted_out"
    elif _CONFIRM.search(outbound_text):
        outcome = "yes"                 # agreed to someone coming to them
    elif _VALUE_ONLY.search(outbound_text):
        outcome = "value_only"          # wants the number, not the conversation
    elif _DECLINE.search(outbound_text):
        outcome = "declined"
    elif inbound:
        outcome = "engaged"             # replied, but the branch never resolved
    else:
        outcome = "no_reply"

    return {
        "sent_at": sent_at,
        "replied_at": _dt(first_reply.get("dateAdded")) if first_reply else None,
        "reply_text": reply_text,
        "outcome": outcome,
        "msg_count": len(after),
        "thread": [
            {
                "at": str(m.get("dateAdded") or ""),
                "direction": "inbound" if (m.get("direction") or "").lower() == "inbound" else "outbound",
                "body": (m.get("body") or "").strip(),
            }
            for m in after
        ],
    }


async def _messages(client: httpx.AsyncClient, conv_id: str, token: str) -> list[dict]:
    try:
        r = await client.get(
            f"{GHL_BASE}/conversations/{conv_id}/messages",
            headers={"Authorization": f"Bearer {token}", "Version": "2021-07-28",
                     "Accept": "application/json"},
            timeout=20.0,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError):
        return []
    if r.status_code != 200:
        return []
    body = r.json().get("messages")
    arr = body.get("messages") if isinstance(body, dict) else body
    return arr or []


async def sync_equity_messages(pg, store: dict, conversations: list[dict], token: str,
                               tz) -> int:
    """Scan this store's recent conversations for appraisal threads and upsert
    them. Returns how many threads were found.

    Upsert, never delete: a customer who replies later must update in place, and
    the row must not vanish from Reid's view between syncs.
    """
    sem = asyncio.Semaphore(10)

    async def one(client, conv):
        async with sem:
            msgs = await _messages(client, conv["id"], token)
        row = classify_thread(msgs)
        return (conv, row) if row else None

    async with httpx.AsyncClient() as client:
        found = [r for r in await asyncio.gather(*(one(client, c) for c in conversations)) if r]

    for conv, row in found:
        sent = row["sent_at"]
        if sent is None:
            continue
        await pg.execute(
            """
            insert into esther_equity_messages
              (ghl_contact_id, store_id, local_date, customer_name, phone,
               sent_at, replied_at, reply_text, outcome, msg_count, thread, synced_at)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11, now())
            on conflict (ghl_contact_id, local_date) do update set
              replied_at = excluded.replied_at,
              reply_text = excluded.reply_text,
              outcome    = excluded.outcome,
              msg_count  = excluded.msg_count,
              thread     = excluded.thread,
              synced_at  = now()
            """,
            conv.get("contactId"), store["id"], sent.astimezone(tz).date(),
            conv.get("fullName"), conv.get("phone"),
            sent, row["replied_at"], row["reply_text"], row["outcome"],
            row["msg_count"], json.dumps(row["thread"]),
        )
    log.info("equity_messages: %s found %d appraisal threads", store["name"], len(found))
    return len(found)
