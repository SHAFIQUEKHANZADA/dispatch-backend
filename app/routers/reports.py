"""Reports endpoints — the teaching reports (owner parity).

One shared job feed (real dispatch data) powers every report. The frontend
requests a single period; each report is computed on the fly so nothing is
stale. Reports with no data source return available=false + a named reason.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import CurrentUserDep, SessionDep
from ..services import reports_service as R

router = APIRouter(prefix="/reports", tags=["reports"])

_PERIOD_LABEL = {"week": "This week", "t30": "Last 30 days", "quarter": "This quarter"}


@router.get("")
async def reports(session: SessionDep, current: CurrentUserDep, period: str = "t30"):
    if period not in R.PERIODS:
        period = "t30"
    jobs = await R.load_jobs(session, current.dealer_id, period)
    return {
        "period": period,
        "period_label": _PERIOD_LABEL.get(period, "Last 30 days"),
        "job_count": len(jobs),
        "reports": {
            "top_tech_efficiency": R.top_tech_efficiency(jobs),
            "mentor_board": R.mentor_board(jobs),
            "inspection_upside": await R.inspection_upside(session, current.dealer_id),
            "speed_vs_quality": R.speed_vs_quality(jobs),
            "match_payoff": R.match_payoff(jobs),
            "dispatcher_overrides": R.dispatcher_overrides(jobs),
        },
    }
