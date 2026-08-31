"""DB -> scoreboard engine.

Loads the history rows, the time clock, the comeback pairs and the shift
schedules, hands them to the pure formulas in engine/metrics.py, and persists a
snapshot to tech_metrics so every number that was ever displayed can be
reconstructed later.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import default_now
from ..engine.metrics import (
    HistoryRow,
    MetricsConfig,
    TechScorecard,
    build_scorecard,
    is_countable,
    rank_like_for_like,
    shift_capacity_hours,
    team_averages,
)
from ..models import (
    ComebackPairRow,
    Dealer,
    DealerSettings,
    ImportRun,
    OpCodeMap,
    ROHistory,
    TechMetrics,
    Technician,
    TimeClockDay,
)

PERIODS = ("DAILY", "MTD", "T90")

# Which windows each metric is defined for (per the spec — do not improvise).
METRIC_WINDOWS = {
    "efficiency": ("DAILY", "MTD", "T90"),
    "productivity": ("DAILY", "MTD"),
    "utilization": ("DAILY", "MTD"),
    "promise_pct": ("MTD",),
    "comeback_rate": ("T90",),
    "first_time_fix": ("T90",),
}


async def single_scorecard(
    session: AsyncSession,
    dealer_id: uuid.UUID,
    technician_id: uuid.UUID,
    period: str = "T90",
    now: Optional[datetime] = None,
) -> Optional[TechScorecard]:
    """One tech's scorecard WITHOUT scoring the whole store. The tech profile
    needs a single card; running the full build_scoreboard (every tech, every
    history row) took ~6s and made the profile page look hung. This filters the
    history to just this tech, so it returns in well under a second."""
    now = now or default_now()
    tech = await session.get(Technician, technician_id)
    if tech is None or tech.dealer_id != dealer_id:
        return None

    dealer = await session.get(Dealer, dealer_id)
    ds = await session.get(DealerSettings, dealer_id) or DealerSettings(dealer_id=dealer_id)
    tz = ZoneInfo(dealer.timezone or "America/Chicago") if dealer else ZoneInfo("UTC")
    today = now.astimezone(tz).date()
    start_d, end_d = period_bounds(period, today)
    start_dt = datetime.combine(start_d, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    end_dt = (datetime.combine(end_d, time(0, 0), tzinfo=tz) + timedelta(days=1)).astimezone(timezone.utc)

    config = MetricsConfig(
        min_ros_to_rank=int(ds.min_ros_to_rank or 10),
        min_flagged_hours_to_rank=float(ds.min_flagged_hours_to_rank or 15),
        comeback_window_days=int(ds.comeback_window_days or 30),
        data_staleness_hours=int(ds.data_staleness_hours or 48),
    )

    excluded_ops = {
        r.op_code
        for r in (
            await session.execute(
                select(OpCodeMap).where(
                    OpCodeMap.dealer_id == dealer_id, OpCodeMap.excluded.is_(True)
                )
            )
        ).scalars()
    }

    hist = list(
        (
            await session.execute(
                select(ROHistory).where(
                    ROHistory.dealer_id == dealer_id,
                    ROHistory.technician_id == technician_id,
                    ROHistory.closed_at.is_not(None),
                    ROHistory.closed_at >= start_dt,
                    ROHistory.closed_at < end_dt,
                )
            )
        ).scalars()
    )
    rows: list[HistoryRow] = []
    for h in hist:
        row = HistoryRow(
            id=str(h.id),
            ro_number=h.ro_number,
            technician_id=str(h.technician_id),
            opened_at=h.opened_at,
            closed_at=h.closed_at,
            concern_category=h.concern_category,
            op_code=h.op_code,
            flagged_hours=float(h.flagged_hours or 0),
            actual_clocked_hours=float(h.actual_clocked_hours or 0),
            labor_type=h.labor_type,
            promise_time=h.promise_time,
            vin=h.vin,
            excluded_from_metrics=bool(h.excluded_from_metrics) or (h.op_code in excluded_ops),
            exclusion_reason=h.exclusion_reason,
        )
        if is_countable(row):
            rows.append(row)

    comeback_count = (
        await session.execute(
            select(func.count()).select_from(ComebackPairRow).where(
                ComebackPairRow.dealer_id == dealer_id,
                ComebackPairRow.original_tech_id == technician_id,
                ComebackPairRow.original_closed_at >= start_dt,
                ComebackPairRow.original_closed_at < end_dt,
            )
        )
    ).scalar() or 0

    clocked_total = (
        await session.execute(
            select(func.sum(TimeClockDay.total_clocked_hours)).where(
                TimeClockDay.dealer_id == dealer_id,
                TimeClockDay.technician_id == technician_id,
                TimeClockDay.work_date >= start_d,
                TimeClockDay.work_date <= end_d,
            )
        )
    ).scalar()

    capacity = shift_capacity_hours(
        start_d, end_d, tech.shift_start, tech.shift_end, tech.work_days or [],
        tech.lunch_start, tech.lunch_end,
    )
    age = await _source_age_hours(session, dealer_id, now)

    return build_scorecard(
        technician_id=str(tech.id),
        name=tech.name,
        team=tech.team,
        skill_level=tech.skill_level,
        period=period,
        period_start=start_d,
        period_end=end_d,
        rows=rows,
        capacity_hours=capacity,
        total_clocked_hours=float(clocked_total) if clocked_total is not None else None,
        comeback_count=int(comeback_count),
        config=config,
        source_data_age_hours=age,
    )


def period_bounds(period: str, today: date) -> tuple[date, date]:
    if period == "DAILY":
        return today, today
    if period == "MTD":
        return today.replace(day=1), today
    if period == "T90":
        return today - timedelta(days=89), today
    raise ValueError(f"Unknown period {period!r}")


@dataclass
class ScoreboardResult:
    period: str
    period_start: date
    period_end: date
    cards: list[TechScorecard]
    rankings: dict[str, dict[str, list[str]]]     # metric -> group -> [tech_id]
    team_averages: dict[str, dict[str, Optional[float]]]  # metric -> team -> value
    metric_windows: dict[str, tuple[str, ...]]
    source_data_age_hours: Optional[float]

    def to_dict(self) -> dict:
        return {
            "period": self.period,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "cards": [c.to_dict() for c in self.cards],
            "rankings": self.rankings,
            "team_averages": {
                m: {k: (round(v, 1) if v is not None else None) for k, v in teams.items()}
                for m, teams in self.team_averages.items()
            },
            "metric_windows": {k: list(v) for k, v in self.metric_windows.items()},
            "source_data_age_hours": (
                round(self.source_data_age_hours, 1)
                if self.source_data_age_hours is not None
                else None
            ),
        }


async def _source_age_hours(
    session: AsyncSession, dealer_id: uuid.UUID, now: datetime
) -> Optional[float]:
    last = (
        await session.execute(
            select(func.max(ImportRun.completed_at)).where(
                ImportRun.dealer_id == dealer_id,
                ImportRun.kind == "DMS_RO_HISTORY",
                ImportRun.status == "COMPLETED",
            )
        )
    ).scalar()
    if last is None:
        last = (
            await session.execute(
                select(func.max(ROHistory.imported_at)).where(ROHistory.dealer_id == dealer_id)
            )
        ).scalar()
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() / 3600.0


async def build_scoreboard(
    session: AsyncSession,
    dealer_id: uuid.UUID,
    period: str = "MTD",
    now: Optional[datetime] = None,
) -> ScoreboardResult:
    now = now or default_now()

    dealer = await session.get(Dealer, dealer_id)
    ds = await session.get(DealerSettings, dealer_id) or DealerSettings(dealer_id=dealer_id)
    tz = ZoneInfo(dealer.timezone or "America/Chicago") if dealer else ZoneInfo("UTC")
    today = now.astimezone(tz).date()

    start_d, end_d = period_bounds(period, today)
    start_dt = datetime.combine(start_d, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    end_dt = (
        datetime.combine(end_d, time(0, 0), tzinfo=tz) + timedelta(days=1)
    ).astimezone(timezone.utc)

    config = MetricsConfig(
        min_ros_to_rank=int(ds.min_ros_to_rank or 10),
        min_flagged_hours_to_rank=float(ds.min_flagged_hours_to_rank or 15),
        comeback_window_days=int(ds.comeback_window_days or 30),
        data_staleness_hours=int(ds.data_staleness_hours or 48),
    )

    techs = list(
        (
            await session.execute(
                select(Technician)
                .where(Technician.dealer_id == dealer_id, Technician.active.is_(True))
                .order_by(Technician.name)
            )
        ).scalars()
    )

    # Op codes the dealer has marked as excluded work (training, shop time,
    # policy adjustments).  FR-6.5: these never enter the math.
    excluded_ops = {
        row.op_code
        for row in (
            await session.execute(
                select(OpCodeMap).where(
                    OpCodeMap.dealer_id == dealer_id, OpCodeMap.excluded.is_(True)
                )
            )
        ).scalars()
    }

    hist = list(
        (
            await session.execute(
                select(ROHistory).where(
                    ROHistory.dealer_id == dealer_id,
                    ROHistory.closed_at.is_not(None),
                    ROHistory.closed_at >= start_dt,
                    ROHistory.closed_at < end_dt,
                )
            )
        ).scalars()
    )

    rows_by_tech: dict[uuid.UUID, list[HistoryRow]] = {}
    for h in hist:
        if h.technician_id is None:
            continue
        row = HistoryRow(
            id=str(h.id),
            ro_number=h.ro_number,
            technician_id=str(h.technician_id),
            opened_at=h.opened_at,
            closed_at=h.closed_at,
            concern_category=h.concern_category,
            op_code=h.op_code,
            flagged_hours=float(h.flagged_hours or 0),
            actual_clocked_hours=float(h.actual_clocked_hours or 0),
            labor_type=h.labor_type,
            promise_time=h.promise_time,
            vin=h.vin,
            excluded_from_metrics=bool(h.excluded_from_metrics) or (h.op_code in excluded_ops),
            exclusion_reason=h.exclusion_reason,
        )
        if is_countable(row):
            rows_by_tech.setdefault(h.technician_id, []).append(row)

    # Comebacks are attributed to whoever did the ORIGINAL repair.
    cb_rows = list(
        (
            await session.execute(
                select(ComebackPairRow).where(
                    ComebackPairRow.dealer_id == dealer_id,
                    ComebackPairRow.original_closed_at >= start_dt,
                    ComebackPairRow.original_closed_at < end_dt,
                )
            )
        ).scalars()
    )
    comebacks: dict[uuid.UUID, int] = {}
    for cb in cb_rows:
        if cb.original_tech_id:
            comebacks[cb.original_tech_id] = comebacks.get(cb.original_tech_id, 0) + 1

    # Time clock — the denominator for Productivity.  Absent => Guardian-gated.
    clock_rows = list(
        (
            await session.execute(
                select(
                    TimeClockDay.technician_id,
                    func.sum(TimeClockDay.total_clocked_hours),
                )
                .where(
                    TimeClockDay.dealer_id == dealer_id,
                    TimeClockDay.work_date >= start_d,
                    TimeClockDay.work_date <= end_d,
                )
                .group_by(TimeClockDay.technician_id)
            )
        ).all()
    )
    clocked_total: dict[uuid.UUID, float] = {
        tid: float(total or 0) for tid, total in clock_rows if tid
    }

    age = await _source_age_hours(session, dealer_id, now)

    cards: list[TechScorecard] = []
    for t in techs:
        capacity = shift_capacity_hours(
            start_d, end_d, t.shift_start, t.shift_end, t.work_days or [],
            t.lunch_start, t.lunch_end,
        )
        cards.append(
            build_scorecard(
                technician_id=str(t.id),
                name=t.name,
                team=t.team,
                skill_level=t.skill_level,
                period=period,
                period_start=start_d,
                period_end=end_d,
                rows=rows_by_tech.get(t.id, []),
                capacity_hours=capacity,
                total_clocked_hours=clocked_total.get(t.id),
                comeback_count=comebacks.get(t.id, 0),
                config=config,
                source_data_age_hours=age,
            )
        )

    metrics = list(METRIC_WINDOWS.keys())
    return ScoreboardResult(
        period=period,
        period_start=start_d,
        period_end=end_d,
        cards=cards,
        rankings={m: rank_like_for_like(cards, m) for m in metrics},
        team_averages={m: team_averages(cards, m) for m in metrics},
        metric_windows=METRIC_WINDOWS,
        source_data_age_hours=age,
    )


async def persist_snapshot(
    session: AsyncSession, dealer_id: uuid.UUID, result: ScoreboardResult
) -> int:
    """Write the computed cards into tech_metrics.

    NFR-3: a number that was on somebody's screen must still be reconstructable
    six months later, even after the underlying history has been re-imported.
    """
    written = 0
    for card in result.cards:
        tech_id = uuid.UUID(card.technician_id)
        existing = (
            await session.execute(
                select(TechMetrics).where(
                    TechMetrics.dealer_id == dealer_id,
                    TechMetrics.technician_id == tech_id,
                    TechMetrics.period == result.period,
                    TechMetrics.period_start == result.period_start,
                    TechMetrics.period_end == result.period_end,
                )
            )
        ).scalar_one_or_none()

        row = existing or TechMetrics(
            dealer_id=dealer_id,
            technician_id=tech_id,
            period=result.period,
            period_start=result.period_start,
            period_end=result.period_end,
        )
        row.efficiency = card.efficiency.value
        row.productivity = card.productivity.value
        row.utilization = card.utilization.value
        row.promise_pct = card.promise_pct.value
        row.comeback_rate = card.comeback_rate.value
        row.first_time_fix = card.first_time_fix.value
        row.ro_count = card.ro_count
        row.flagged_hours = card.flagged_hours
        row.clocked_hours = card.clocked_hours
        row.cp_flagged_hours = card.cp_flagged_hours
        row.warranty_flagged_hours = card.warranty_flagged_hours
        row.internal_flagged_hours = card.internal_flagged_hours
        row.qualifies_for_ranking = card.qualifies_for_ranking
        row.data_complete = card.data_complete
        row.data_issues = card.data_issues
        row.computed_at = datetime.now(timezone.utc)

        if existing is None:
            session.add(row)
        written += 1
    return written


async def drilldown(
    session: AsyncSession,
    dealer_id: uuid.UUID,
    technician_id: uuid.UUID,
    period: str,
    now: Optional[datetime] = None,
) -> list[dict]:
    """FR-6.8 — the source rows behind a displayed number.

    Every number on the scoreboard is clickable, and this is what it opens.
    If a tech disputes his efficiency, this is the list of ROs he is disputing.
    """
    now = now or default_now()
    dealer = await session.get(Dealer, dealer_id)
    tz = ZoneInfo(dealer.timezone or "America/Chicago") if dealer else ZoneInfo("UTC")
    today = now.astimezone(tz).date()
    start_d, end_d = period_bounds(period, today)
    start_dt = datetime.combine(start_d, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    end_dt = (datetime.combine(end_d, time(0, 0), tzinfo=tz) + timedelta(days=1)).astimezone(
        timezone.utc
    )

    excluded_ops = {
        row.op_code
        for row in (
            await session.execute(
                select(OpCodeMap).where(
                    OpCodeMap.dealer_id == dealer_id, OpCodeMap.excluded.is_(True)
                )
            )
        ).scalars()
    }

    rows = list(
        (
            await session.execute(
                select(ROHistory)
                .where(
                    ROHistory.dealer_id == dealer_id,
                    ROHistory.technician_id == technician_id,
                    ROHistory.closed_at >= start_dt,
                    ROHistory.closed_at < end_dt,
                )
                .order_by(ROHistory.closed_at.desc())
            )
        ).scalars()
    )

    out = []
    for h in rows:
        excluded = bool(h.excluded_from_metrics) or (h.op_code in excluded_ops)
        out.append(
            {
                "ro_number": h.ro_number,
                "closed_at": h.closed_at.isoformat() if h.closed_at else None,
                "op_code": h.op_code,
                "concern_category": h.concern_category,
                "flagged_hours": float(h.flagged_hours or 0),
                "actual_clocked_hours": float(h.actual_clocked_hours or 0),
                "labor_type": h.labor_type,
                "promise_time": h.promise_time.isoformat() if h.promise_time else None,
                "made_promise": (
                    (h.closed_at <= h.promise_time)
                    if (h.promise_time and h.closed_at)
                    else None
                ),
                "vin": h.vin,
                "counted": not excluded,
                "exclusion_reason": (
                    h.exclusion_reason
                    or ("Op code marked as excluded work" if excluded else None)
                ),
            }
        )
    return out
