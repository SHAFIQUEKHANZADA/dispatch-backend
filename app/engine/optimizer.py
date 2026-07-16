"""'Make Smart Decision' — the shop-wide optimizer (FR-3.7 / Step 4f).

Assigns the set of Ready-to-Dispatch ROs across the available technicians,
maximising, in strict priority order:

    1. promise times protected   (an RO nobody can finish in time is a failure)
    2. total match score         (the right tech on the right job)
    3. workload balance          (nobody drowning while somebody sits idle)

Like the Match Score itself this is a pure, deterministic algorithm — no LLM.
The plan is a *proposal*.  It is never applied without confirmation, and the
projected gain it reports is COMPUTED from the plan, not asserted.

Approach: greedy assignment over ROs ordered by urgency, with the technicians'
projected workload updated after each placement so the next RO is scored
against the shop as it will actually be, not as it was.  A single pass of
"steal-back" improvement is not attempted: a dispatcher needs to understand the
plan, and a greedy pass over an urgency-ordered queue is a plan a human can
follow line by line.  That is worth more than a few points of theoretical
optimality nobody can audit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Optional

from .match_score import rank_technicians
from .types import Candidate, ROInput, ScoringContext, TechInput


@dataclass(frozen=True)
class PlannedAssignment:
    ro_id: str
    ro_number: str
    technician_id: str
    technician_name: str
    score: int
    rank: int                       # where this tech sat in the ranking for this RO
    reasons: list[dict]
    warnings: list[str]
    confident: bool
    projected_finish: Optional[datetime]
    promise_at: Optional[datetime]
    est_hours: float

    def to_dict(self) -> dict:
        return {
            "ro_id": self.ro_id,
            "ro_number": self.ro_number,
            "technician_id": self.technician_id,
            "technician_name": self.technician_name,
            "score": self.score,
            "rank": self.rank,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "confident": self.confident,
            "projected_finish": self.projected_finish.isoformat() if self.projected_finish else None,
            "promise_at": self.promise_at.isoformat() if self.promise_at else None,
            "est_hours": self.est_hours,
        }


@dataclass(frozen=True)
class UnplacedRO:
    ro_id: str
    ro_number: str
    reason: str

    def to_dict(self) -> dict:
        return {"ro_id": self.ro_id, "ro_number": self.ro_number, "reason": self.reason}


@dataclass(frozen=True)
class PlanGain:
    """Every one of these is measured off the plan. None of it is a guess."""

    ros_assigned: int
    ros_unplaced: int
    hours_dispatched: float          # billable hours put on a bench by this plan
    promises_protected: int          # of the ROs with a promise, how many now finish in time
    promises_at_risk_before: int     # ROs with a promise and nobody assigned
    promises_at_risk_after: int
    idle_techs_before: int
    idle_techs_after: int
    workload_spread_before: float    # std dev of assigned hours across techs
    workload_spread_after: float
    avg_match_score: float

    def to_dict(self) -> dict:
        idle_delta_pct = (
            round(
                (self.idle_techs_after - self.idle_techs_before)
                / self.idle_techs_before
                * 100.0,
                1,
            )
            if self.idle_techs_before
            else 0.0
        )
        return {
            "ros_assigned": self.ros_assigned,
            "ros_unplaced": self.ros_unplaced,
            "hours_dispatched": round(self.hours_dispatched, 1),
            "promises_protected": self.promises_protected,
            "promises_at_risk_before": self.promises_at_risk_before,
            "promises_at_risk_after": self.promises_at_risk_after,
            "idle_techs_before": self.idle_techs_before,
            "idle_techs_after": self.idle_techs_after,
            "idle_change_pct": idle_delta_pct,
            "workload_spread_before": round(self.workload_spread_before, 2),
            "workload_spread_after": round(self.workload_spread_after, 2),
            "avg_match_score": round(self.avg_match_score, 1),
        }


@dataclass(frozen=True)
class SmartPlan:
    assignments: list[PlannedAssignment]
    unplaced: list[UnplacedRO]
    gain: PlanGain
    engine_version: str

    def to_dict(self) -> dict:
        return {
            "assignments": [a.to_dict() for a in self.assignments],
            "unplaced": [u.to_dict() for u in self.unplaced],
            "gain": self.gain.to_dict(),
            "engine_version": self.engine_version,
        }


# --------------------------------------------------------------------------- #


def _urgency_key(ro: ROInput, now: datetime, flagged: set[str]):
    """The order ROs get first claim on a technician.

    Flagged work (waiting customer, heat case, comeback, manager flag) first,
    then whatever is closest to blowing its promise, then the biggest job (it is
    the hardest to place late), then RO number for a stable tie-break.
    """
    is_flagged = 0 if ro.id in flagged else 1
    if ro.promise_at:
        slack = (ro.promise_at - now).total_seconds() / 3600.0 - ro.est_hours
    else:
        slack = float("inf")
    return (is_flagged, slack, -ro.est_hours, ro.ro_number)


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var)


def build_smart_plan(
    ros: list[ROInput],
    base_techs: list[TechInput],
    ctx_for: Callable[[ROInput], ScoringContext],
    techs_for: Optional[Callable[[ROInput], list[TechInput]]] = None,
    flagged_ro_ids: Optional[set[str]] = None,
) -> SmartPlan:
    """Produce the proposed shop-wide plan.  Pure; applies nothing.

    `base_techs` is the shop roster, used for the before/after idle and workload
    statistics.

    `techs_for(ro)` returns that same roster with each technician's CategoryStats
    bound to THIS RO's concern category, and `ctx_for(ro)` returns the scoring
    context (which carries the shop-wide familiarity max for that category).
    Both matter: a tech who is 130% on brakes is not a 130% tech on an A/C job,
    and a queue that mixes categories must not let one category's history leak
    into another's score.  Both callables must be pure — the plan is only
    deterministic if they are.
    """
    flagged = flagged_ro_ids or set()
    techs_for = techs_for or (lambda _ro: base_techs)
    now = ctx_for(ros[0]).now if ros else None

    # How the plan has moved each technician: when they are next free, and how
    # many hours they now hold.  Applied on top of whatever techs_for() returns.
    overlay: dict[str, tuple[Optional[datetime], float]] = {
        t.id: (t.free_at, t.assigned_hours_today) for t in base_techs
    }

    hours_before = [t.assigned_hours_today for t in base_techs]
    idle_before = sum(
        1 for t in base_techs if t.active and t.on_shift and t.assigned_hours_today <= 0.01
    )
    promises_before = sum(1 for ro in ros if ro.promise_at is not None)

    ordered = sorted(ros, key=lambda r: _urgency_key(r, now, flagged)) if ros else []

    planned: list[PlannedAssignment] = []
    unplaced: list[UnplacedRO] = []
    protected = 0

    for ro in ordered:
        ctx = ctx_for(ro)
        candidates_in = [
            replace(t, free_at=overlay[t.id][0], assigned_hours_today=overlay[t.id][1])
            for t in techs_for(ro)
            if t.id in overlay
        ]
        result = rank_technicians(ro, candidates_in, ctx)

        # Only ever auto-assign a tech we actually trust the data for.  A
        # provisional score is fine for a human to look at and override with;
        # it is not fine for the machine to act on unattended.
        choices = [c for c in result.all_candidates if c.confident]

        if not choices:
            why = "No eligible technician can start and finish this RO before its promise time"
            if result.all_candidates:
                why = (
                    "Only technicians with incomplete source data are eligible — "
                    "the optimizer will not auto-assign on a provisional score"
                )
            elif result.not_eligible:
                why = result.not_eligible[0].reason
            unplaced.append(UnplacedRO(ro.id, ro.ro_number, why))
            continue

        winner: Candidate = choices[0]
        rank = next(
            i + 1 for i, c in enumerate(result.all_candidates)
            if c.technician_id == winner.technician_id
        )

        planned.append(
            PlannedAssignment(
                ro_id=ro.id,
                ro_number=ro.ro_number,
                technician_id=winner.technician_id,
                technician_name=winner.name,
                score=winner.score,
                rank=rank,
                reasons=[r.to_dict() for r in winner.reasons],
                warnings=list(winner.warnings),
                confident=winner.confident,
                projected_finish=winner.projected_finish,
                promise_at=ro.promise_at,
                est_hours=ro.est_hours,
            )
        )

        if ro.promise_at and winner.projected_finish and winner.projected_finish <= ro.promise_at:
            protected += 1

        # Advance that technician: they are now busy until this job ends.
        prev_free_at, prev_hours = overlay[winner.technician_id]
        overlay[winner.technician_id] = (
            winner.projected_finish or prev_free_at,
            prev_hours + ro.est_hours,
        )

    hours_after = [overlay[t.id][1] for t in base_techs]
    idle_after = sum(
        1 for t in base_techs
        if t.active and t.on_shift and overlay[t.id][1] <= 0.01
    )
    promises_unplaced = sum(
        1 for u in unplaced
        for ro in ros
        if ro.id == u.ro_id and ro.promise_at is not None
    )

    gain = PlanGain(
        ros_assigned=len(planned),
        ros_unplaced=len(unplaced),
        hours_dispatched=sum(p.est_hours for p in planned),
        promises_protected=protected,
        promises_at_risk_before=promises_before,
        promises_at_risk_after=promises_unplaced,
        idle_techs_before=idle_before,
        idle_techs_after=idle_after,
        workload_spread_before=_stdev(hours_before),
        workload_spread_after=_stdev(hours_after),
        avg_match_score=(sum(p.score for p in planned) / len(planned)) if planned else 0.0,
    )

    from .types import ENGINE_VERSION

    return SmartPlan(
        assignments=planned, unplaced=unplaced, gain=gain, engine_version=ENGINE_VERSION
    )
