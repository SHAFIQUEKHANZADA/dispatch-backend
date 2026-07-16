"""FR-6 — the Scoreboard."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from .. import audit
from ..deps import CurrentUserDep, SessionDep
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
