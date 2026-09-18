"""Secret Shopper grading of Esther's QA "shop" calls (Reid's #7).

The QA test personas (qa-line tag) call each store's Esther line on a script.
This reads each shop call's post-call summary and grades Esther's handling 0–100
against a fixed 5-part rubric, via the same raw-httpx + forced-tool-call pattern
as the classifier. Graded once per call (keyed by ghl_message_id) and cached in
esther_qa_scores, so the 5-minute window rebuild never re-charges it.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

from ..config import get_settings

log = logging.getLogger("esther.qa_grader")
settings = get_settings()

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
GRADER_MODEL = "claude-haiku-4-5"

# Reid's Secret Shopper rubric — 5 criteria, 20 points each = 100. Change the
# labels/weights here and the grader follows; re-grading is automatic for new calls.
RUBRIC = [
    ("greeting_id", "Greeting & identification — professional greeting, correct dealership/department."),
    ("intent_capture", "Intent capture — correctly understood what the shopper wanted."),
    ("accuracy", "Accuracy — gave correct info (hours, pricing, availability, policy)."),
    ("task_completion", "Task completion — booked the appointment / transferred correctly / resolved the request."),
    ("professionalism", "Professionalism & tone — natural, polite, no errors, no dead air."),
]

_TOOL = {
    "name": "grade_shop_call",
    "description": "Grade one AI voice-agent QA 'secret shopper' call from its post-call summary.",
    "input_schema": {
        "type": "object",
        "properties": {
            **{k: {"type": "integer", "description": f"0–20. {desc}"} for k, desc in RUBRIC},
            "note": {"type": "string", "description": "One short sentence: the single biggest thing that helped or hurt the score."},
        },
        "required": [k for k, _ in RUBRIC] + ["note"],
    },
}

_SYSTEM = (
    "You are a secret-shopper QA grader for an AI voice agent (Esther) that answers "
    "a car dealership's service/sales calls. You are given Esther's own post-call "
    "summary of a scripted test call. Grade her handling on each rubric criterion "
    "from 0 to 20 (20 = excellent, 0 = failed). Be fair but strict: a smooth, correct "
    "booking scores high; a dropped call, wrong info, or an unresolved request scores "
    "low. If the summary is empty or uninformative, give low scores and say so in the note."
)


def enabled() -> bool:
    return bool(settings.anthropic_api_key)


async def grade_one(client: httpx.AsyncClient, summary: str) -> dict | None:
    """Grade a single shop-call summary. Returns {score, breakdown} or None."""
    body = {
        "model": GRADER_MODEL,
        "max_tokens": 500,
        "system": _SYSTEM,
        "tools": [_TOOL],
        "tool_choice": {"type": "tool", "name": "grade_shop_call"},
        "messages": [{"role": "user", "content": f"Post-call summary:\n{summary.strip()}"}],
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
                        inp = b.get("input") or {}
                        subs = {k: max(0, min(20, int(inp.get(k, 0)))) for k, _ in RUBRIC}
                        total = sum(subs.values())  # 0..100
                        return {"score": total, "breakdown": {**subs, "note": inp.get("note", "")}}
                return None
            if resp.status_code in (429, 500, 502, 503, 529) and backoff:
                await asyncio.sleep(backoff)
                continue
            log.warning("qa_grader: Anthropic %s: %s", resp.status_code, resp.text[:200])
            return None
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
            if backoff:
                await asyncio.sleep(backoff)
                continue
            log.warning("qa_grader: request failed: %s", exc)
    return None


async def grade_ungraded(pg, limit: int = 40) -> int:
    """Grade qa-line calls that have a summary but no score yet. Bounded per run.
    HTTP runs concurrently; DB writes are sequential (single asyncpg connection)."""
    if not enabled():
        return 0
    rows = await pg.fetch(
        """
        select c.ghl_message_id, c.store_id, c.local_date, c.department, c.summary
        from esther_calls c
        left join esther_qa_scores q using (ghl_message_id)
        where q.ghl_message_id is null
          and c.tags @> array['qa-line']
          and c.summary is not null and length(trim(c.summary)) > 0
        order by c.started_at desc
        limit $1
        """,
        limit,
    )
    if not rows:
        return 0

    sem = asyncio.Semaphore(6)

    async def _grade(client, r):
        async with sem:
            res = await grade_one(client, r["summary"])
        return r, res

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_grade(client, r) for r in rows])

    done = 0
    for r, res in results:
        if not res:
            continue
        await pg.execute(
            """
            insert into esther_qa_scores
              (ghl_message_id, store_id, local_date, department, score, breakdown, model, graded_at)
            values ($1,$2,$3,$4,$5,$6,$7, now())
            on conflict (ghl_message_id) do nothing
            """,
            r["ghl_message_id"], r["store_id"], r["local_date"], r["department"],
            res["score"], json.dumps(res["breakdown"]), GRADER_MODEL,
        )
        done += 1
    log.info("qa_grader: graded %d shop calls", done)
    return done
