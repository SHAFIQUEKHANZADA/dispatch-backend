"""The dispatch board + the Match Score endpoints (FR-3, FR-4)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import audit
from ..deps import CurrentUserDep, SessionDep
from ..models import Assignment, RepairOrder, Technician
from ..services.dispatch_service import load_shop, plan_smart_decision, rank_for_ro

router = APIRouter(prefix="/dispatch", tags=["dispatch"])

TABS = [
    ("OPEN", "Open ROs"),
    ("PENDING_AUTHORIZATION", "Pending Authorization"),
    ("WAITING_ON_PARTS", "Waiting on Parts"),
    ("READY_TO_DISPATCH", "Ready to Dispatch"),
]

SORTS = {
    "flagged_written": "Flagged first, then earliest written (default)",
    "written": "Earliest written",
    "latest_written": "Latest written",
    "ro_number": "RO number",
}

_PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _ro_dict(ro: RepairOrder) -> dict:
    return {
        "id": str(ro.id),
        "ro_number": ro.ro_number,
        "vin": ro.vin,
        "vehicle_year": ro.vehicle_year,
        "vehicle_make": ro.vehicle_make,
        "vehicle_model": ro.vehicle_model,
        "mileage": ro.mileage,
        "concern_category": ro.concern_category,
        "work_type": ro.work_type,
        "tier": ro.tier,
        "required_certs": list(ro.required_certs or []),
        "required_team": ro.required_team,
        "est_hours": float(ro.est_hours or 0),
        "written_at": ro.written_at.isoformat() if ro.written_at else None,
        "promise_at": ro.promise_at.isoformat() if ro.promise_at else None,
        "status": ro.status,
        "flags": list(ro.flags or []),
        "priority": ro.priority,
        "is_flagged": ro.is_flagged,
        "lines": [
            {
                "op_code": ln.op_code,
                "description": ln.description,
                "flagged_hours": float(ln.flagged_hours or 0),
            }
            for ln in ro.lines
        ],
    }


def _sort_ros(ros: list[RepairOrder], sort: str) -> list[RepairOrder]:
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    far_past = datetime.min.replace(tzinfo=timezone.utc)

    if sort == "written":
        return sorted(ros, key=lambda r: (r.written_at or far_future, r.ro_number))
    if sort == "latest_written":
        return sorted(ros, key=lambda r: (r.written_at or far_past, r.ro_number), reverse=True)
    if sort == "ro_number":
        return sorted(ros, key=lambda r: r.ro_number)
    # default — flagged first, then earliest written
    return sorted(
        ros,
        key=lambda r: (0 if r.is_flagged else 1, r.written_at or far_future, r.ro_number),
    )


@router.get("/board")
async def get_board(
    session: SessionDep,
    current: CurrentUserDep,
    status_filter: str = "READY_TO_DISPATCH",
    sort: str = "flagged_written",
    top_n: Optional[int] = None,
):
    """Everything the primary screen needs, in one call.

    NFR-5: ~20 ROs x ~20 techs must score in well under a second.  It does,
    because it is arithmetic — the whole board is one DB load and a few hundred
    multiplications.  This is exactly what an LLM could not do.
    """
    shop = await load_shop(session, current.dealer_id)

    all_ros = list(
        (
            await session.execute(
                select(RepairOrder).where(
                    RepairOrder.dealer_id == current.dealer_id,
                    RepairOrder.status.in_([t[0] for t in TABS]),
                )
            )
        ).scalars()
    )

    counts = {key: sum(1 for r in all_ros if r.status == key) for key, _ in TABS}

    visible = _sort_ros([r for r in all_ros if r.status == status_filter], sort)

    cards = []
    for ro in visible:
        payload = _ro_dict(ro)
        # Only Ready-to-Dispatch ROs get a technician ranking; ranking an RO
        # that is still waiting on a part would be advice nobody can act on.
        if ro.status == "READY_TO_DISPATCH":
            ranking = rank_for_ro(shop, ro, top_n)
            payload["ranking"] = ranking.to_dict()
        else:
            payload["ranking"] = None
        cards.append(payload)

    available_techs = sum(
        1
        for t in shop.technicians
        if shop.tech_inputs[t.id].on_shift and shop.tech_inputs[t.id].active
    )

    return {
        "tabs": [
            {"key": key, "label": label, "count": counts.get(key, 0)} for key, label in TABS
        ],
        "active_tab": status_filter,
        "sorts": [{"key": k, "label": v} for k, v in SORTS.items()],
        "active_sort": sort,
        "unassigned": counts.get("READY_TO_DISPATCH", 0),
        "available_techs": available_techs,
        "ros": cards,
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


@router.get("/ro/{ro_id}/candidates")
async def get_candidates(
    ro_id: uuid.UUID,
    session: SessionDep,
    current: CurrentUserDep,
    top_n: Optional[int] = None,
):
    """The full ranking for one RO — every eligible tech, plus the not-eligible
    list WITH the reason each was excluded (FR-3.1)."""
    ro = await session.get(RepairOrder, ro_id)
    if ro is None or ro.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repair order not found")

    shop = await load_shop(session, current.dealer_id)
    return rank_for_ro(shop, ro, top_n).to_dict()


class AssignRequest(BaseModel):
    ro_id: uuid.UUID
    technician_id: uuid.UUID
    override_reason: Optional[str] = Field(
        default=None,
        description="Required-ish: why a lower-ranked tech was chosen. Logged either way.",
    )


@router.post("/assign")
async def assign(body: AssignRequest, session: SessionDep, current: CurrentUserDep):
    """Dispatch an RO to a technician (FR-4.6, FR-4.7).

    Freezes the score AND the reason list onto the assignment row.  Six months
    from now, when the tech's stats have moved on, this decision can still be
    explained with the numbers that actually produced it.
    """
    ro = await session.get(RepairOrder, body.ro_id)
    if ro is None or ro.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repair order not found")

    tech = await session.get(Technician, body.technician_id)
    if tech is None or tech.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Technician not found")

    if ro.status not in ("READY_TO_DISPATCH", "OPEN"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"RO {ro.ro_number} is {ro.status} — only a Ready-to-Dispatch RO can be dispatched",
        )

    shop = await load_shop(session, current.dealer_id)
    ranking = rank_for_ro(shop, ro, top_n=len(shop.technicians) or 1)

    chosen = next(
        (c for c in ranking.all_candidates if c.technician_id == str(body.technician_id)), None
    )

    if chosen is None:
        # They picked someone the engine hard-excluded.  Say exactly why, and
        # refuse: a cert or restriction failure is not an override, it is a
        # safety / warranty violation.
        blocked = next(
            (n for n in ranking.not_eligible if n.technician_id == str(body.technician_id)), None
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{tech.name} is not eligible for RO {ro.ro_number}: "
            f"{blocked.reason if blocked else 'failed a hard constraint'}",
        )

    rank = next(
        i + 1
        for i, c in enumerate(ranking.all_candidates)
        if c.technician_id == str(body.technician_id)
    )
    took_recommendation = rank == 1

    assignment = Assignment(
        dealer_id=current.dealer_id,
        ro_id=ro.id,
        technician_id=tech.id,
        match_score=chosen.score,
        score_reasons=[r.to_dict() for r in chosen.reasons],
        score_warnings=list(chosen.warnings),
        score_confident=chosen.confident,
        recommended_rank=rank,
        was_ai_recommendation=took_recommendation,
        override_reason=body.override_reason,
        engine_version=ranking.engine_version,
        weights_used=ranking.to_dict()["weights"],
        assigned_by=current.user_id,
        assigned_at=shop.now,
        started_at=shop.now,
    )
    session.add(assignment)

    ro.status = "IN_PROGRESS"

    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action=audit.DISPATCH if took_recommendation else audit.DISPATCH_OVERRIDE,
        entity="repair_order",
        entity_id=ro.id,
        payload={
            "ro_number": ro.ro_number,
            "technician_id": str(tech.id),
            "technician_name": tech.name,
            "score": chosen.score,
            "rank_chosen": rank,
            "top_recommendation": (
                ranking.all_candidates[0].name if ranking.all_candidates else None
            ),
            "was_ai_recommendation": took_recommendation,
            "override_reason": body.override_reason,
            "reasons": [r.to_dict() for r in chosen.reasons],
            "warnings": chosen.warnings,
            "engine_version": ranking.engine_version,
        },
    )

    await session.commit()

    return {
        "assignment_id": str(assignment.id),
        "ro_number": ro.ro_number,
        "technician_name": tech.name,
        "score": chosen.score,
        "rank": rank,
        "was_ai_recommendation": took_recommendation,
        "status": ro.status,
    }


@router.post("/smart-decision/preview")
async def smart_decision_preview(session: SessionDep, current: CurrentUserDep):
    """FR-3.7 — propose a shop-wide plan.  Applies NOTHING.

    Every number in `gain` is measured off the plan, not asserted.
    """
    shop = await load_shop(session, current.dealer_id)
    ros = list(
        (
            await session.execute(
                select(RepairOrder).where(
                    RepairOrder.dealer_id == current.dealer_id,
                    RepairOrder.status == "READY_TO_DISPATCH",
                )
            )
        ).scalars()
    )
    return plan_smart_decision(shop, ros).to_dict()


class SmartApplyItem(BaseModel):
    ro_id: uuid.UUID
    technician_id: uuid.UUID


class SmartApplyRequest(BaseModel):
    assignments: list[SmartApplyItem]


@router.post("/smart-decision/apply")
async def smart_decision_apply(
    body: SmartApplyRequest, session: SessionDep, current: CurrentUserDep
):
    """Apply a plan the dispatcher has confirmed.

    Re-scores every line before writing it.  The plan the user is looking at may
    be a few minutes old, and a tech may have picked something up in the
    meantime — we will not write a frozen score that was never true.
    """
    shop = await load_shop(session, current.dealer_id)
    applied, skipped = [], []

    for item in body.assignments:
        ro = await session.get(RepairOrder, item.ro_id)
        if ro is None or ro.dealer_id != current.dealer_id:
            skipped.append({"ro_id": str(item.ro_id), "reason": "RO not found"})
            continue
        if ro.status != "READY_TO_DISPATCH":
            skipped.append(
                {"ro_id": str(item.ro_id), "ro_number": ro.ro_number,
                 "reason": f"RO is now {ro.status} — it moved since the plan was built"}
            )
            continue

        tech = await session.get(Technician, item.technician_id)
        if tech is None or tech.dealer_id != current.dealer_id:
            skipped.append({"ro_id": str(item.ro_id), "reason": "Technician not found"})
            continue

        ranking = rank_for_ro(shop, ro, top_n=len(shop.technicians) or 1)
        chosen = next(
            (c for c in ranking.all_candidates if c.technician_id == str(item.technician_id)),
            None,
        )
        if chosen is None:
            blocked = next(
                (n for n in ranking.not_eligible if n.technician_id == str(item.technician_id)),
                None,
            )
            skipped.append(
                {
                    "ro_id": str(item.ro_id),
                    "ro_number": ro.ro_number,
                    "reason": f"{tech.name} is no longer eligible: "
                              f"{blocked.reason if blocked else 'hard constraint'}",
                }
            )
            continue

        rank = next(
            i + 1
            for i, c in enumerate(ranking.all_candidates)
            if c.technician_id == str(item.technician_id)
        )

        session.add(
            Assignment(
                dealer_id=current.dealer_id,
                ro_id=ro.id,
                technician_id=tech.id,
                match_score=chosen.score,
                score_reasons=[r.to_dict() for r in chosen.reasons],
                score_warnings=list(chosen.warnings),
                score_confident=chosen.confident,
                recommended_rank=rank,
                was_ai_recommendation=True,
                engine_version=ranking.engine_version,
                weights_used=ranking.to_dict()["weights"],
                assigned_by=current.user_id,
                started_at=shop.now,
            )
        )
        ro.status = "IN_PROGRESS"
        applied.append(
            {
                "ro_number": ro.ro_number,
                "technician_name": tech.name,
                "score": chosen.score,
            }
        )

        # Reflect the placement in-memory so the next RO in this batch is scored
        # against the shop as it now is, not as it was when the batch started.
        base = shop.tech_inputs[tech.id]
        shop.tech_inputs[tech.id] = replace(
            base,
            free_at=chosen.projected_finish or base.free_at,
            assigned_hours_today=base.assigned_hours_today + float(ro.est_hours or 0),
        )

    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action=audit.SMART_DECISION_APPLIED,
        entity="dealer",
        entity_id=current.dealer_id,
        payload={"applied": applied, "skipped": skipped},
    )
    await session.commit()

    return {"applied": applied, "skipped": skipped}
