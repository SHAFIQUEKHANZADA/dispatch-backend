"""The bridge between the database and the pure engine.

This is the ONLY place that turns rows into engine inputs.  The engine itself
never touches a session — that is what keeps it deterministic and testable.

It is also where the Guardian gate is actually evaluated: this module knows how
old the store's source data is, so this module is what tells the engine which
technicians it may not speak confidently about.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import default_now
from ..engine.match_score import project_finish, rank_technicians
from ..engine.optimizer import SmartPlan, build_smart_plan
from ..engine.types import (
    CategoryStats,
    MatchWeights,
    ROInput,
    RankingResult,
    ScoringContext,
    TechInput,
)
from ..models import (
    Assignment,
    Dealer,
    DealerSettings,
    ImportRun,
    RepairOrder,
    ROHistory,
    TechCategoryFamiliarity,
    Technician,
)

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class ShopSnapshot:
    """Everything the engine needs about the shop right now, loaded once."""

    now: datetime
    dealer: Dealer
    settings: DealerSettings
    technicians: list[Technician]
    tech_inputs: dict[uuid.UUID, TechInput]
    familiarity: dict[tuple[uuid.UUID, str], TechCategoryFamiliarity]
    category_shop_max: dict[str, int]
    source_data_age_hours: Optional[float]
    assigned_hours: dict[uuid.UUID, float]
    current_ro: dict[uuid.UUID, RepairOrder]

    def context(self, concern_category: Optional[str], top_n: Optional[int] = None) -> ScoringContext:
        return ScoringContext(
            now=self.now,
            weights=MatchWeights.from_dict(self.settings.match_weights),
            enforce_team_separation=bool(self.settings.enforce_team_separation),
            top_n=top_n or int(self.settings.default_top_n or 3),
            shop_max_category_repairs=self.category_shop_max.get(concern_category or "", 0),
            data_staleness_hours=int(self.settings.data_staleness_hours or 48),
            display_tz=_tzinfo(self.dealer),
        )


def _tzinfo(dealer: Dealer) -> ZoneInfo:
    try:
        return ZoneInfo(dealer.timezone or "America/Chicago")
    except Exception:  # unknown tz string on the dealer row — do not crash the board
        return ZoneInfo("America/Chicago")


def _local_dt(day: date, t: Optional[time], tz: ZoneInfo) -> Optional[datetime]:
    if t is None:
        return None
    return datetime.combine(day, t, tzinfo=tz).astimezone(timezone.utc)


async def load_shop(
    session: AsyncSession,
    dealer_id: uuid.UUID,
    *,
    now: Optional[datetime] = None,
    include_inactive: bool = False,
) -> ShopSnapshot:
    now = now or default_now()

    dealer = await session.get(Dealer, dealer_id)
    if dealer is None:
        raise ValueError("Dealer not found")

    ds = await session.get(DealerSettings, dealer_id)
    if ds is None:
        ds = DealerSettings(dealer_id=dealer_id)
        session.add(ds)
        await session.flush()

    tz = _tzinfo(dealer)
    local_now = now.astimezone(tz)
    today = local_now.date()
    today_name = _DAY_NAMES[today.weekday()]

    q = select(Technician).where(Technician.dealer_id == dealer_id)
    if not include_inactive:
        q = q.where(Technician.active.is_(True))
    techs = list((await session.execute(q.order_by(Technician.name))).scalars())

    # --- familiarity map --------------------------------------------------- #
    fam_rows = list(
        (
            await session.execute(
                select(TechCategoryFamiliarity).where(
                    TechCategoryFamiliarity.dealer_id == dealer_id
                )
            )
        ).scalars()
    )
    familiarity = {(f.technician_id, f.concern_category): f for f in fam_rows}

    category_shop_max: dict[str, int] = {}
    for f in fam_rows:
        current = category_shop_max.get(f.concern_category, 0)
        category_shop_max[f.concern_category] = max(current, int(f.repairs_completed or 0))

    # --- Guardian: how old is the store's source data? --------------------- #
    last_import = (
        await session.execute(
            select(func.max(ImportRun.completed_at)).where(
                ImportRun.dealer_id == dealer_id,
                ImportRun.kind == "DMS_RO_HISTORY",
                ImportRun.status == "COMPLETED",
            )
        )
    ).scalar()

    if last_import is None:
        # Fall back to the newest imported history row before declaring nothing.
        last_import = (
            await session.execute(
                select(func.max(ROHistory.imported_at)).where(ROHistory.dealer_id == dealer_id)
            )
        ).scalar()

    if last_import is None:
        source_age_hours = None
    else:
        if last_import.tzinfo is None:
            last_import = last_import.replace(tzinfo=timezone.utc)
        source_age_hours = (now - last_import).total_seconds() / 3600.0

    # --- live workload ------------------------------------------------------ #
    day_start_local = datetime.combine(today, time(0, 0), tzinfo=tz)
    day_start = day_start_local.astimezone(timezone.utc)
    day_end = (day_start_local + timedelta(days=1)).astimezone(timezone.utc)

    open_assignments = list(
        (
            await session.execute(
                select(Assignment, RepairOrder)
                .join(RepairOrder, RepairOrder.id == Assignment.ro_id)
                .where(
                    Assignment.dealer_id == dealer_id,
                    Assignment.assigned_at >= day_start,
                    Assignment.assigned_at < day_end,
                )
            )
        ).all()
    )

    assigned_hours: dict[uuid.UUID, float] = {}
    busy_until: dict[uuid.UUID, datetime] = {}
    current_ro: dict[uuid.UUID, RepairOrder] = {}

    for assignment, ro in open_assignments:
        tid = assignment.technician_id
        assigned_hours[tid] = assigned_hours.get(tid, 0.0) + float(ro.est_hours or 0)
        if assignment.completed_at is None and ro.status == "IN_PROGRESS":
            current_ro[tid] = ro
            start = assignment.started_at or assignment.assigned_at
            finish = project_finish(start, float(ro.est_hours or 0))
            if tid not in busy_until or finish > busy_until[tid]:
                busy_until[tid] = finish

    # --- build the engine inputs ------------------------------------------- #
    tech_inputs: dict[uuid.UUID, TechInput] = {}
    staleness_limit = int(ds.data_staleness_hours or 48)

    for t in techs:
        work_days = {d.strip()[:3].title() for d in (t.work_days or []) if d}
        shift_start_at = _local_dt(today, t.shift_start, tz)
        shift_end_at = _local_dt(today, t.shift_end, tz)

        on_shift = bool(work_days) and today_name in work_days
        if on_shift and shift_end_at is not None and now >= shift_end_at:
            on_shift = False  # their day is already over

        free_at = busy_until.get(t.id, now)
        if shift_start_at and free_at < shift_start_at:
            free_at = shift_start_at  # cannot start before they clock in
        free_at = max(free_at, now)

        # ---- Guardian: name the specific problem, do not average it away ---
        issues: list[str] = []
        if source_age_hours is None:
            issues.append("No DMS history imported — familiarity and performance unknown")
        elif source_age_hours > staleness_limit:
            issues.append(
                f"DMS export is {source_age_hours / 24:.1f} days old "
                f"(stale past {staleness_limit}h)"
            )
        if not any(k[0] == t.id for k in familiarity):
            issues.append("No repair history on file for this technician")
        if t.missing_fields():
            issues.append("Technician setup incomplete: " + ", ".join(t.missing_fields()))

        tech_inputs[t.id] = TechInput(
            id=str(t.id),
            name=t.name,
            skill_level=t.skill_level or "",
            team=t.team,
            active=bool(t.active),
            certs=tuple(sorted(c.cert_type for c in t.certs)),
            restricted_work_types=tuple(sorted(r.blocked_work_type for r in t.restrictions)),
            specialty_work_types=tuple(
                sorted(s.work_type for s in t.specialties if s.work_type)
            ),
            vehicle_specialties=tuple(
                sorted(s.vehicle_specialty for s in t.specialties if s.vehicle_specialty)
            ),
            on_shift=on_shift,
            shift_end_at=shift_end_at,
            lunch_start_at=_local_dt(today, t.lunch_start, tz),
            lunch_end_at=_local_dt(today, t.lunch_end, tz),
            free_at=free_at,
            assigned_hours_today=assigned_hours.get(t.id, 0.0),
            max_daily_hours=float(t.max_daily_hours or 8.0),
            overtime_threshold=float(t.overtime_threshold or t.max_daily_hours or 8.0),
            category=None,  # filled per-RO — familiarity is category specific
            data_issues=tuple(issues),
        )

    return ShopSnapshot(
        now=now,
        dealer=dealer,
        settings=ds,
        technicians=techs,
        tech_inputs=tech_inputs,
        familiarity=familiarity,
        category_shop_max=category_shop_max,
        source_data_age_hours=source_age_hours,
        assigned_hours=assigned_hours,
        current_ro=current_ro,
    )


def ro_to_input(ro: RepairOrder) -> ROInput:
    return ROInput(
        id=str(ro.id),
        ro_number=ro.ro_number,
        concern_category=ro.concern_category or "Uncategorised",
        tier=(ro.tier or "B"),
        est_hours=float(ro.est_hours or 0),
        required_certs=tuple(ro.required_certs or []),
        work_type=ro.work_type,
        required_team=ro.required_team,
        promise_at=ro.promise_at,
        vehicle_model=ro.vehicle_model,
    )


def techs_for_ro(shop: ShopSnapshot, ro: RepairOrder) -> list[TechInput]:
    """Attach each tech's stats *for this RO's concern category*.

    A tech who is 130% on brakes is not a 130% tech for an A/C job, so the
    category stats are bound here, per RO, not once per tech.
    """
    category = ro.concern_category or "Uncategorised"
    out: list[TechInput] = []
    for t in shop.technicians:
        base = shop.tech_inputs[t.id]
        fam = shop.familiarity.get((t.id, category))
        stats = (
            CategoryStats(
                repairs_completed=int(fam.repairs_completed or 0),
                avg_efficiency=float(fam.avg_efficiency) if fam.avg_efficiency is not None else None,
                first_time_fix=float(fam.first_time_fix) if fam.first_time_fix is not None else None,
                last_performed_at=fam.last_performed_at,
            )
            if fam
            else None
        )
        out.append(replace(base, category=stats))
    return out


def rank_for_ro(
    shop: ShopSnapshot, ro: RepairOrder, top_n: Optional[int] = None
) -> RankingResult:
    return rank_technicians(
        ro_to_input(ro),
        techs_for_ro(shop, ro),
        shop.context(ro.concern_category, top_n),
    )


def plan_smart_decision(shop: ShopSnapshot, ros: list[RepairOrder]) -> SmartPlan:
    """The shop-wide optimizer ("Make Smart Decision").

    Familiarity and in-category performance are, by definition, per concern
    category — but the dispatch queue mixes categories freely.  So we hand the
    optimizer two pure resolvers rather than one flat tech list: for each RO it
    asks for the roster with that RO's category bound, and for the scoring
    context carrying that category's shop-wide familiarity max.  Without this,
    a tech's brake history would quietly inflate his score on an A/C job.
    """
    ro_inputs = [ro_to_input(r) for r in ros]
    by_id = {str(r.id): r for r in ros}
    base_techs = [shop.tech_inputs[t.id] for t in shop.technicians]
    flagged = {str(r.id) for r in ros if r.is_flagged}

    def ctx_for(ro_in: ROInput) -> ScoringContext:
        return shop.context(ro_in.concern_category)

    def techs_for(ro_in: ROInput) -> list[TechInput]:
        return techs_for_ro(shop, by_id[ro_in.id])

    if not ros:
        # Nothing to plan; still return a well-formed empty plan with real
        # before/after numbers so the UI does not have to special-case it.
        return build_smart_plan(
            [], base_techs, lambda _ro: shop.context(None), None, set()
        )

    return build_smart_plan(ro_inputs, base_techs, ctx_for, techs_for, flagged)
