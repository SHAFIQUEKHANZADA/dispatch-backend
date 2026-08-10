"""FR-6 — the Scoreboard."""

from __future__ import annotations

import copy
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from .. import audit
from ..deps import CurrentUserDep, SessionDep, get_dealer_settings
from ..models import DEFAULT_SCOREBOARD_CONFIG, AdvisorScore
from ..services.metrics_service import (
    PERIODS,
    build_scoreboard,
    drilldown,
    persist_snapshot,
)

router = APIRouter(prefix="/scoreboard", tags=["scoreboard"])

# The published formulas.  These strings are shown in the UI next to every
# metric.  Nothing on this board is a black box a technician cannot check.
FORMULAS = {
    "efficiency": "flagged (sold) hrs ÷ actual clocked hrs on jobs",
    "productivity": "clocked hrs on jobs ÷ total clocked hrs",
    "utilization": "flagged hrs ÷ available shift capacity",
    "promise_pct": "ROs completed before promise ÷ ROs with a promise",
    "comeback_rate": "same-concern reopens ≤30 days ÷ completed ROs",
    "first_time_fix": "1 − (returns for same concern ≤30 days ÷ completed ROs)",
}

LABELS = {
    "efficiency": "Efficiency",
    "productivity": "Productivity",
    "utilization": "Utilization",
    "promise_pct": "Promise-time %",
    "comeback_rate": "Comeback rate",
    "first_time_fix": "First-time fix",
}

# Lower is better for exactly one of them.
LOWER_IS_BETTER = {"comeback_rate"}


# --------------------------------------------------------------------------- #
# Leaderboard board (the "Service Scoreboard" screen)                          #
# --------------------------------------------------------------------------- #

# Technician columns shown on the leaderboard, mapped to the real metrics. Each:
# key, header, kind (percent|number|hours), higher_is_better, goal.
TECH_COLUMNS = [
    {"key": "total_hours", "header": "Total Hours", "kind": "hours", "higher": True, "goal": 150},
    {"key": "ro_count", "header": "RO's", "kind": "number", "higher": True, "goal": 80},
    {"key": "hrs_per_ro", "header": "Hrs / RO", "kind": "number", "higher": False, "goal": None},
    {"key": "efficiency", "header": "Eff %", "kind": "percent", "higher": True, "goal": 100},
    {"key": "productivity", "header": "Productivity", "kind": "percent", "higher": True, "goal": 90},
    {"key": "comeback_rate", "header": "Comeback %", "kind": "percent", "higher": False, "goal": 5},
]


ADVISOR_COLUMNS = [
    {"key": "csi", "header": "CSI", "kind": "percent", "higher": True, "goal": 90.0},
    {"key": "cp_ros", "header": "CP ROs", "kind": "number", "higher": True, "goal": 110},
    {"key": "sales_ro", "header": "Sales / RO", "kind": "money", "higher": True, "goal": 260},
    {"key": "recs_ro", "header": "Recs / RO", "kind": "number", "higher": True, "goal": 2.8},
    {"key": "hrs_vs_rec", "header": "Hrs vs Rec", "kind": "number", "higher": True, "goal": 1.0},
    {"key": "video_sent", "header": "Video Sent", "kind": "percent", "higher": True, "goal": 70},
]


async def _advisor_board(session, dealer_id) -> dict:
    from ..models import AdvisorScore

    advisors = list(
        (
            await session.execute(
                select(AdvisorScore).where(AdvisorScore.dealer_id == dealer_id)
            )
        ).scalars()
    )
    rows = []
    for a in advisors:
        rows.append({
            "advisor_id": str(a.id),
            "name": a.name,
            "qualifies": True,
            "values": {
                "csi": float(a.csi) if a.csi is not None else None,
                "cp_ros": a.cp_ros,
                "sales_ro": float(a.sales_ro) if a.sales_ro is not None else None,
                "sales_breakdown": {
                    "yest": float(a.sales_yest) if a.sales_yest is not None else None,
                    "lmo": float(a.sales_lmo) if a.sales_lmo is not None else None,
                    "pmo": float(a.sales_pmo) if a.sales_pmo is not None else None,
                },
                "recs_ro": float(a.recs_ro) if a.recs_ro is not None else None,
                "hrs_vs_rec": float(a.hrs_vs_rec) if a.hrs_vs_rec is not None else None,
                "video_sent": float(a.video_sent) if a.video_sent is not None else None,
            },
        })
    # Rank by CSI when it's available; otherwise (DMS export has no CSI feed)
    # rank by Sales/RO so the board surfaces top billers, not a random order.
    rank_key = "csi" if any(r["values"].get("csi") is not None for r in rows) else "sales_ro"
    # When ranking on Sales/RO, gate out tiny samples (an advisor with 1 RO at
    # $1,000 shouldn't outrank one with 50 ROs) — low-volume advisors sort last.
    min_ros = 5 if rank_key == "sales_ro" else 0

    def a_sort(r):
        v = r["values"].get(rank_key) or 0
        qualifies = (r["values"].get("cp_ros") or 0) >= min_ros
        return (0 if qualifies else 1, -v, r["name"])

    rows.sort(key=a_sort)
    for i, r in enumerate(rows):
        r["rank"] = i + 1

    store = {}
    for c in ADVISOR_COLUMNS:
        vals = [r["values"][c["key"]] for r in rows if r["values"].get(c["key"]) is not None]
        if not vals:
            store[c["key"]] = None
        elif c["key"] == "cp_ros":
            store[c["key"]] = int(round(sum(vals)))           # total ROs
        else:
            store[c["key"]] = round(sum(vals) / len(vals), 1)  # averages

    return {
        "view": "advisors",
        "available": True,
        "period_label": "Last Business Day",
        "rank_key": rank_key,
        "columns": ADVISOR_COLUMNS,
        "rows": rows,
        "goals": {c["key"]: c["goal"] for c in ADVISOR_COLUMNS},
        "store": store,
        "facility_utilization": 82,   # placeholder per the mockup; wire to real capacity later
    }


# --------------------------------------------------------------------------- #
# Scoreboard Settings screen                                                   #
# --------------------------------------------------------------------------- #

def _merge_config(saved: dict | None) -> dict:
    """Fill any missing keys from the defaults so an empty/old blob still works."""
    cfg = copy.deepcopy(DEFAULT_SCOREBOARD_CONFIG)
    if not saved:
        return cfg
    if saved.get("display"):
        cfg["display"].update(saved["display"])
    for key in ("advisor_metrics", "tech_metrics"):
        if saved.get(key):
            cfg[key] = saved[key]
    return cfg


class MetricIn(BaseModel):
    name: str
    source: str
    goal: str = ""
    show: bool = False


class ScoreboardConfigIn(BaseModel):
    display: dict[str, Any]
    advisor_metrics: list[MetricIn]
    tech_metrics: list[MetricIn]


class ManualAdvisorIn(BaseModel):
    advisor_id: str
    csi_out_of_5: Optional[float] = None
    survey_responses: Optional[int] = None
    survey_response_pct: Optional[float] = None


class ManualEntryIn(BaseModel):
    period: str
    advisors: list[ManualAdvisorIn]


@router.get("/settings")
async def get_scoreboard_settings(session: SessionDep, current: CurrentUserDep):
    ds = await get_dealer_settings(session, current.dealer_id)
    cfg = _merge_config(ds.scoreboard_config)

    advisors = list(
        (await session.execute(
            select(AdvisorScore).where(AdvisorScore.dealer_id == current.dealer_id)
        )).scalars()
    )
    advisors.sort(key=lambda a: -(float(a.csi) if a.csi is not None else 0))
    manual = [
        {
            "advisor_id": str(a.id),
            "name": a.name,
            # CSI is stored as a percent; advisors enter it out of 5
            "csi_out_of_5": round(float(a.csi) / 20, 2) if a.csi is not None else None,
            "survey_responses": a.survey_responses,
            "survey_response_pct": float(a.survey_response_pct) if a.survey_response_pct is not None else None,
        }
        for a in advisors
    ]
    await session.commit()

    # the manual-entry section is scoped to the current period in V1
    from ..clock import default_now
    period = default_now().strftime("%B %Y") + " (current)"
    return {
        "config": cfg,
        "manual": manual,
        "period_label": period,
        "period_options": [period],
        "max_per_board": 8,
    }


@router.put("/settings")
async def update_scoreboard_settings(body: ScoreboardConfigIn, session: SessionDep, current: CurrentUserDep):
    current.require_role("SERVICE_MANAGER")
    ds = await get_dealer_settings(session, current.dealer_id)
    ds.scoreboard_config = {
        "display": body.display,
        "advisor_metrics": [m.model_dump() for m in body.advisor_metrics],
        "tech_metrics": [m.model_dump() for m in body.tech_metrics],
    }
    await audit.record(
        session, dealer_id=current.dealer_id, actor=current.user_id,
        action=audit.SETTINGS_UPDATED, entity="scoreboard_config", entity_id=current.dealer_id,
        payload={"display": body.display},
    )
    await session.commit()
    return await get_scoreboard_settings(session, current)


@router.put("/manual")
async def update_manual_entry(body: ManualEntryIn, session: SessionDep, current: CurrentUserDep):
    """Manual advisor data an API can't deliver (CSI + survey counts). Writes to
    the real advisor_scores rows so it flows into the scoreboard and Match Score."""
    current.require_role("SERVICE_MANAGER")
    by_id = {a.advisor_id: a for a in body.advisors}
    rows = list(
        (await session.execute(
            select(AdvisorScore).where(AdvisorScore.dealer_id == current.dealer_id)
        )).scalars()
    )
    updated = 0
    for r in rows:
        payload = by_id.get(str(r.id))
        if not payload:
            continue
        if payload.csi_out_of_5 is not None:
            r.csi = round(payload.csi_out_of_5 * 20, 1)   # out of 5 -> percent
        r.survey_responses = payload.survey_responses
        r.survey_response_pct = payload.survey_response_pct
        updated += 1
    await audit.record(
        session, dealer_id=current.dealer_id, actor=current.user_id,
        action=audit.SETTINGS_UPDATED, entity="advisor_scores", entity_id=current.dealer_id,
        payload={"period": body.period, "advisors_updated": updated},
    )
    await session.commit()
    return await get_scoreboard_settings(session, current)


@router.get("/board")
async def scoreboard_board(
    session: SessionDep, current: CurrentUserDep, view: str = "technicians", period: str = "T90"
):
    """The ranked leaderboard for the Service Scoreboard screen.

    Technician view is real (T90 metrics). Advisor view has no data source in V1
    (CSI / sales / video are the Module-2 Advisor Scoreboard) — it returns a
    clear 'no data' state rather than fabricating numbers.
    """
    if view == "advisors":
        return await _advisor_board(session, current.dealer_id)

    result = await build_scoreboard(session, current.dealer_id, period)
    rank_key = "efficiency"

    rows = []
    for c in result.cards:
        m = {k: getattr(c, k) for k in ("efficiency", "productivity", "comeback_rate")}
        # "Total Hours" = flagged (billed) hours. Clock hours aren't in the DMS RO
        # export (no attendance feed), so flagged hours are the real work signal;
        # efficiency (flagged/clocked) stays unavailable until a clock feed lands.
        total_hours = c.flagged_hours
        hrs_per_ro = (c.flagged_hours / c.ro_count) if c.ro_count else None
        def rnd(mv):
            return round(mv.value, 1) if mv.available and mv.value is not None else None

        values = {
            "total_hours": int(round(total_hours)) if total_hours else 0,
            "ro_count": c.ro_count,
            "hrs_per_ro": round(hrs_per_ro, 1) if hrs_per_ro is not None else None,
            "efficiency": rnd(m["efficiency"]),
            "productivity": rnd(m["productivity"]),
            "comeback_rate": rnd(m["comeback_rate"]),
        }
        rows.append({
            "technician_id": c.technician_id,
            "name": c.name,
            "team": c.team,
            "level": c.skill_level,
            "qualifies": c.qualifies_for_ranking,
            "data_issues": c.data_issues,
            "values": values,
        })

    # If no tech has an efficiency (no clock feed — e.g. a DMS RO-only export),
    # rank by billed hours so the board still surfaces top performers first
    # instead of falling back to alphabetical.
    if not any(r["values"].get("efficiency") is not None for r in rows):
        rank_key = "total_hours"

    # rank: qualified techs by the rank metric (desc), unqualified last
    def sort_key(r):
        v = r["values"].get(rank_key)
        return (0 if r["qualifies"] and v is not None else 1, -(v or 0), r["name"])

    rows.sort(key=sort_key)
    for i, r in enumerate(rows):
        r["rank"] = i + 1

    # store average / total
    qual = [r for r in rows if r["qualifies"]]
    store = {}
    for col in TECH_COLUMNS:
        k = col["key"]
        vals = [r["values"][k] for r in qual if r["values"].get(k) is not None]
        if not vals:
            store[k] = None
        elif col["kind"] == "hours" or (col["kind"] == "number" and col["key"] == "ro_count"):
            store[k] = round(sum(vals), 0)            # totals
        else:
            store[k] = round(sum(vals) / len(vals), 1)  # averages

    return {
        "view": "technicians",
        "available": True,
        "period": result.period,
        "period_label": "Last 90 Days",
        "rank_key": rank_key,
        "columns": TECH_COLUMNS,
        "rows": rows,
        "goals": {c["key"]: c["goal"] for c in TECH_COLUMNS},
        "store": store,
    }


@router.get("")
async def get_scoreboard(session: SessionDep, current: CurrentUserDep, period: str = "MTD"):
    if period not in PERIODS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"period must be one of {', '.join(PERIODS)}"
        )
    result = await build_scoreboard(session, current.dealer_id, period)
    payload = result.to_dict()
    payload["formulas"] = FORMULAS
    payload["labels"] = LABELS
    payload["lower_is_better"] = sorted(LOWER_IS_BETTER)
    payload["gates"] = {
        "min_ros_to_rank": int((await _settings(session, current)).min_ros_to_rank or 10),
        "min_flagged_hours_to_rank": float(
            (await _settings(session, current)).min_flagged_hours_to_rank or 15
        ),
    }
    return payload


async def _settings(session, current):
    from ..deps import get_dealer_settings

    return await get_dealer_settings(session, current.dealer_id)


@router.post("/recompute")
async def recompute(session: SessionDep, current: CurrentUserDep, period: str = "MTD"):
    """Snapshot the current cards into tech_metrics (NFR-3)."""
    if period not in PERIODS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"period must be one of {', '.join(PERIODS)}"
        )
    result = await build_scoreboard(session, current.dealer_id, period)
    written = await persist_snapshot(session, current.dealer_id, result)

    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action=audit.METRICS_COMPUTE,
        entity="tech_metrics",
        payload={
            "period": period,
            "technicians": written,
            "source_data_age_hours": result.source_data_age_hours,
        },
    )
    await session.commit()
    return {"period": period, "technicians_written": written}


@router.get("/{tech_id}/drilldown")
async def get_drilldown(
    tech_id: uuid.UUID,
    session: SessionDep,
    current: CurrentUserDep,
    period: str = "MTD",
    metric: str = "efficiency",
):
    """FR-6.8 — the source rows behind a number.

    This is the endpoint that settles arguments.  If a technician says his
    efficiency is wrong, this returns the exact ROs the number was computed
    from, including the ones that were excluded and why.
    """
    if period not in PERIODS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"period must be one of {', '.join(PERIODS)}"
        )
    rows = await drilldown(session, current.dealer_id, tech_id, period)
    return {
        "technician_id": str(tech_id),
        "period": period,
        "metric": metric,
        "formula": FORMULAS.get(metric),
        "rows": rows,
        "counted_rows": sum(1 for r in rows if r["counted"]),
        "excluded_rows": sum(1 for r in rows if not r["counted"]),
    }
