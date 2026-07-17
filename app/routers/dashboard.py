"""FR-7 — the dashboard, and FR-8's audit reader."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from ..clock import default_now
from ..deps import CurrentUserDep, SessionDep
from ..models import AuditLog, RepairOrder
from ..services.dispatch_service import load_shop, rank_for_ro

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
async def dashboard(session: SessionDep, current: CurrentUserDep):
    shop = await load_shop(session, current.dealer_id)
    now = shop.now

    ros = list(
        (
            await session.execute(
                select(RepairOrder).where(
                    RepairOrder.dealer_id == current.dealer_id,
                    RepairOrder.status != "COMPLETED",
                )
            )
        ).scalars()
    )

    ready = [r for r in ros if r.status == "READY_TO_DISPATCH"]
    in_progress = [r for r in ros if r.status == "IN_PROGRESS"]

    # "At risk" is not a vibe — an RO is at risk when NO eligible technician can
    # still start it and finish before its promise time.  That is exactly the
    # hard constraint the engine already evaluates, so we ask the engine rather
    # than inventing a second, softer definition that would disagree with it.
    at_risk = []
    for ro in ready:
        if not ro.promise_at:
            continue
        ranking = rank_for_ro(shop, ro, top_n=1)
        if not ranking.all_candidates:
            hours_left = (ro.promise_at - now).total_seconds() / 3600.0
            at_risk.append(
                {
                    "id": str(ro.id),
                    "ro_number": ro.ro_number,
                    "promise_at": ro.promise_at.isoformat(),
                    "hours_to_promise": round(hours_left, 1),
                    "est_hours": float(ro.est_hours or 0),
                    "reason": (
                        ranking.not_eligible[0].reason
                        if ranking.not_eligible
                        else "No technician is eligible for this RO"
                    ),
                }
            )

    idle, overloaded, on_shift = [], [], []
    capacity_today = 0.0
    for t in shop.technicians:
        ti = shop.tech_inputs[t.id]
        if not (ti.on_shift and ti.active):
            continue
        on_shift.append(t)
        capacity_today += float(t.max_daily_hours or 8.0)
        assigned = shop.assigned_hours.get(t.id, 0.0)
        if assigned <= 0.01:
            idle.append({"id": str(t.id), "name": t.name, "team": t.team})
        if assigned > float(t.max_daily_hours or 8.0):
            overloaded.append(
                {
                    "id": str(t.id),
                    "name": t.name,
                    "assigned_hours": round(assigned, 1),
                    "capacity_hours": float(t.max_daily_hours or 8.0),
                }
            )

    hours_sold_today = sum(float(r.est_hours or 0) for r in ready + in_progress)

    # Promise-time picture. "Protected" is not a guess: it's every open RO that
    # carries a promise and is NOT in the at-risk list — i.e. someone can still
    # physically finish it in time (or it's already on a bench).
    promised = [r for r in ready + in_progress if r.promise_at is not None]
    promise_total = len(promised)
    promise_at_risk = len(at_risk)
    promise_protected = max(0, promise_total - promise_at_risk)

    return {
        "open_ros": sum(1 for r in ros if r.status == "OPEN"),
        "pending_authorization": sum(1 for r in ros if r.status == "PENDING_AUTHORIZATION"),
        "waiting_on_parts": sum(1 for r in ros if r.status == "WAITING_ON_PARTS"),
        "unassigned": len(ready),
        "in_progress": len(in_progress),
        "waiting_customers": sum(1 for r in ros if "WAITING" in (r.flags or [])),
        "heat_cases": sum(1 for r in ros if "HEAT_CASE" in (r.flags or [])),
        "comebacks": sum(1 for r in ros if "COMEBACK" in (r.flags or [])),
        "techs_on_shift": len(on_shift),
        "techs_idle": len(idle),
        "techs_overloaded": len(overloaded),
        "idle_technicians": idle,
        "overloaded_technicians": overloaded,
        "ros_at_risk": at_risk,
        "promise_total": promise_total,
        "promise_protected": promise_protected,
        "promise_at_risk": promise_at_risk,
        "hours_sold_today": round(hours_sold_today, 1),
        "capacity_hours_today": round(capacity_today, 1),
        "guardian": {
            "source_data_age_hours": (
                round(shop.source_data_age_hours, 1)
                if shop.source_data_age_hours is not None
                else None
            ),
            "staleness_threshold_hours": int(shop.settings.data_staleness_hours or 48),
            "stale": (
                shop.source_data_age_hours is None
                or shop.source_data_age_hours > float(shop.settings.data_staleness_hours or 48)
            ),
        },
    }


@router.get("/audit")
async def audit_log(session: SessionDep, current: CurrentUserDep, limit: int = 100):
    """FR-8 — every dispatch, override, import and metric computation."""
    rows = list(
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.dealer_id == current.dealer_id)
                .order_by(AuditLog.created_at.desc())
                .limit(min(limit, 500))
            )
        ).scalars()
    )
    return {
        "entries": [
            {
                "id": str(r.id),
                "action": r.action,
                "entity": r.entity,
                "entity_id": str(r.entity_id) if r.entity_id else None,
                "actor": str(r.actor) if r.actor else None,
                "payload": r.payload,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }
