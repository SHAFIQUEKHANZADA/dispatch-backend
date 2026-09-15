"""Claude-powered classification of Esther's calls.

Reads each call's post-call summary (Esther's own TYPE_INTERNAL_COMMENT) and
extracts the finer-grained signals Reid asked for that GHL's coarse tags don't
carry: detailed intent, transfer reason, whether the customer asked for a human,
and a rough open/close sentiment.

Classification is keyed by the call's stable ghl_message_id and stored in
esther_call_classifications, so a call is sent to Claude exactly ONCE — the
5-minute sync's window rebuild never re-charges it. Uses Haiku (cheap, fast) via
the same raw-httpx + forced-tool-call pattern as the warranty auditor.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import get_settings

log = logging.getLogger("esther.classifier")
settings = get_settings()

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
CLASSIFIER_MODEL = "claude-haiku-4-5"

# Reid's intent taxonomy (superset of the topic-* tags GHL already emits).
INTENT_CATEGORIES = [
    "Scheduling", "Reschedule/Cancel", "Parts", "Warranty", "Pricing", "Recall",
    "RO Status", "Loaner", "Advisor Request", "Complaint", "Human Requested",
    "Sales", "Trade-In", "Other", "Unknown",
]
# Reid's transfer reason codes.
TRANSFER_REASONS = [
    "customer requested human", "correct department routing", "vehicle status",
    "employee requested", "policy limitation", "AI knowledge failure",
    "technical/speech failure", "frustrated-customer escalation", "unknown",
]

_TOOL = {
    "name": "classify_call",
    "description": "Return the structured classification of one AI phone call from its post-call summary.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent_detail": {"type": "string", "enum": INTENT_CATEGORIES,
                              "description": "The primary reason the customer called."},
            "transfer_reason": {"type": "string", "enum": TRANSFER_REASONS,
                                "description": "If the call was transferred to a human, why. Use 'unknown' if unclear; ignored when the call was not transferred."},
            "human_requested": {"type": "boolean",
                                "description": "True only if the customer explicitly asked to speak with a human/person."},
            "sentiment_open": {"type": "integer",
                               "description": "Customer sentiment at the START of the call, -100 (very negative) to 100 (very positive), 0 neutral. Estimate from the summary."},
            "sentiment_close": {"type": "integer",
                                "description": "Customer sentiment at the END of the call, -100 to 100."},
        },
        "required": ["intent_detail", "human_requested", "sentiment_open", "sentiment_close"],
    },
}

_SYSTEM = (
    "You classify summaries of phone calls handled by an AI voice agent (Esther) "
    "for a car dealership's service/sales departments. You are given the AI's own "
    "post-call summary. Return ONE classification via the classify_call tool. "
    "Pick the single best intent. Sentiment is a rough estimate from the summary's "
    "tone (a smooth booking is positive; a complaint or unresolved issue is negative; "
    "a plain info request is near neutral). If the summary is empty or uninformative, "
    "use intent_detail 'Unknown' and neutral sentiment (0)."
)


def enabled() -> bool:
    return bool(settings.anthropic_api_key)


async def classify_one(client: httpx.AsyncClient, summary: str, transferred: bool) -> dict | None:
    """Classify a single call summary. Returns the tool input dict, or None on failure."""
    user = f"Call was transferred to a human: {transferred}\n\nPost-call summary:\n{summary.strip()}"
    body = {
        "model": CLASSIFIER_MODEL,
        "max_tokens": 400,
        "system": _SYSTEM,
        "tools": [_TOOL],
        "tool_choice": {"type": "tool", "name": "classify_call"},
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    for backoff in (1.0, 2.5, 0):
        try:
            resp = await client.post(ANTHROPIC_URL, json=body, headers=headers, timeout=30.0)
            if resp.status_code == 200:
                for b in resp.json().get("content", []):
                    if b.get("type") == "tool_use":
                        return b.get("input") or None
                return None
            if resp.status_code in (429, 500, 502, 503, 529) and backoff:
                await asyncio.sleep(backoff)
                continue
            log.warning("classifier: Anthropic %s: %s", resp.status_code, resp.text[:200])
            return None
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
            if backoff:
                await asyncio.sleep(backoff)
                continue
            log.warning("classifier: request failed: %s", exc)
    return None


async def classify_unclassified(pg, limit: int = 80) -> int:
    """Classify recent calls that have a summary but no classification yet.
    Bounded per run so one sync can't fan out over the whole backlog. Returns
    how many were newly classified."""
    if not enabled():
        return 0
    rows = await pg.fetch(
        """
        select c.ghl_message_id, c.summary, c.transferred
        from esther_calls c
        left join esther_call_classifications x using (ghl_message_id)
        where x.ghl_message_id is null
          and c.summary is not null and length(trim(c.summary)) > 0
        order by c.started_at desc
        limit $1
        """,
        limit,
    )
    if not rows:
        return 0

    # Claude calls run concurrently (HTTP is safe to parallelize), but the DB
    # writes go one at a time — a single asyncpg connection can't run concurrent
    # operations.
    sem = asyncio.Semaphore(6)

    async def _classify(client, r):
        async with sem:
            res = await classify_one(client, r["summary"], bool(r["transferred"]))
        return r, res

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_classify(client, r) for r in rows])

    done = 0
    for r, res in results:
        if not res:
            continue
        await pg.execute(
            """
            insert into esther_call_classifications
              (ghl_message_id, intent_detail, transfer_reason, human_requested,
               sentiment_open, sentiment_close, model, classified_at)
            values ($1,$2,$3,$4,$5,$6,$7, now())
            on conflict (ghl_message_id) do nothing
            """,
            r["ghl_message_id"], res.get("intent_detail"),
            res.get("transfer_reason") if r["transferred"] else None,
            res.get("human_requested"),
            res.get("sentiment_open"), res.get("sentiment_close"),
            CLASSIFIER_MODEL,
        )
        done += 1
    log.info("classifier: classified %d calls", done)
    return done
