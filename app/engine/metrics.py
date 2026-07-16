"""The Scoreboard — the six metrics, exactly as specified.

    Efficiency      = flagged (sold) hrs / actual clocked hrs on jobs   Daily/MTD/T90
    Productivity    = clocked hrs on jobs / total clocked hrs           Daily/MTD
    Utilization     = flagged hrs / available shift capacity            Daily/MTD
    Promise-time %  = ROs completed before promise / ROs with a promise MTD
    Comeback rate   = same-concern reopens <=30d / completed ROs        T90
    First-time fix  = 1 - (returns for same concern <=30d / completed)  T90

The math is not improvised and it is not negotiable.

Every metric comes back as a MetricValue that carries its own numerator,
denominator and sample size — so a technician who disputes a number can be
shown the exact division that produced it, and then the exact ROs underneath it.

RULE 3 lives here.  A MetricValue can be `available=False` with a named
`issue`.  It is NEVER a number we made up because the real one was missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

# Work that must never enter the math (FR-6.5).  Recognised by concern category
# or by the `excluded` flag on the dealer's op-code map.
EXCLUDED_CATEGORIES = {"TRAINING", "SHOP_TIME", "POLICY", "POLICY_ADJUSTMENT"}


@dataclass(frozen=True)
class HistoryRow:
    """One closed RO line out of the 90-day DMS export."""

    id: str
    ro_number: str
    technician_id: Optional[str]
    opened_at: Optional[datetime]
    closed_at: Optional[datetime]
    concern_category: Optional[str]
    op_code: Optional[str]
    flagged_hours: float = 0.0
    actual_clocked_hours: float = 0.0
    labor_type: Optional[str] = None      # CP | WARRANTY | INTERNAL
    promise_time: Optional[datetime] = None
    vin: Optional[str] = None
    excluded_from_metrics: bool = False
    exclusion_reason: Optional[str] = None


@dataclass(frozen=True)
class MetricValue:
    """A number, its arithmetic, and its honesty flag."""

    key: str
    value: Optional[float]          # None when it cannot be honestly computed
    available: bool
    numerator: Optional[float] = None
    denominator: Optional[float] = None
    sample_size: int = 0
    issue: Optional[str] = None     # named reason it is unavailable / untrusted
    unit: str = "percent"           # percent | ratio

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": round(self.value, 1) if self.value is not None else None,
            "available": self.available,
            "numerator": round(self.numerator, 2) if self.numerator is not None else None,
            "denominator": round(self.denominator, 2) if self.denominator is not None else None,
            "sample_size": self.sample_size,
            "issue": self.issue,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class TechScorecard:
    technician_id: str
    name: str
    team: Optional[str]
    skill_level: Optional[str]
    period: str
    period_start: date
    period_end: date

    efficiency: MetricValue
    productivity: MetricValue
    utilization: MetricValue
    promise_pct: MetricValue
    comeback_rate: MetricValue
    first_time_fix: MetricValue

    ro_count: int
    flagged_hours: float
    clocked_hours: float
    cp_flagged_hours: float
    warranty_flagged_hours: float
    internal_flagged_hours: float

    qualifies_for_ranking: bool       # minimum-volume gate
    data_complete: bool               # Guardian gate
    data_issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "technician_id": self.technician_id,
            "name": self.name,
            "team": self.team,
            "skill_level": self.skill_level,
            "period": self.period,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "metrics": {
                "efficiency": self.efficiency.to_dict(),
                "productivity": self.productivity.to_dict(),
                "utilization": self.utilization.to_dict(),
                "promise_pct": self.promise_pct.to_dict(),
                "comeback_rate": self.comeback_rate.to_dict(),
                "first_time_fix": self.first_time_fix.to_dict(),
            },
            "ro_count": self.ro_count,
            "flagged_hours": round(self.flagged_hours, 1),
            "clocked_hours": round(self.clocked_hours, 1),
            "cp_flagged_hours": round(self.cp_flagged_hours, 1),
            "warranty_flagged_hours": round(self.warranty_flagged_hours, 1),
            "internal_flagged_hours": round(self.internal_flagged_hours, 1),
            "qualifies_for_ranking": self.qualifies_for_ranking,
            "data_complete": self.data_complete,
            "data_issues": list(self.data_issues),
        }


@dataclass(frozen=True)
class MetricsConfig:
    min_ros_to_rank: int = 10
    min_flagged_hours_to_rank: float = 15.0
    comeback_window_days: int = 30
    data_staleness_hours: int = 48


# --------------------------------------------------------------------------- #
# Row filtering — excluded work never enters the math                          #
# --------------------------------------------------------------------------- #


def is_countable(row: HistoryRow) -> bool:
    """FR-6.5 — training, shop time, policy adjustments and ROs still open at
    period close are excluded from every metric."""
    if row.excluded_from_metrics:
        return False
    if row.closed_at is None:  # still open at period close
        return False
    if row.concern_category and row.concern_category.strip().upper().replace(" ", "_") in EXCLUDED_CATEGORIES:
        return False
    return True


def rows_in_period(
    rows: Iterable[HistoryRow], start: datetime, end: datetime
) -> list[HistoryRow]:
    return [
        r for r in rows
        if is_countable(r) and r.closed_at is not None and start <= r.closed_at <= end
    ]


# --------------------------------------------------------------------------- #
# The six formulas                                                             #
# --------------------------------------------------------------------------- #


def efficiency(rows: list[HistoryRow]) -> MetricValue:
    """flagged (sold) hrs / actual clocked hrs on jobs."""
    flagged = sum(r.flagged_hours for r in rows)
    clocked = sum(r.actual_clocked_hours for r in rows)
    if clocked <= 0:
        return MetricValue(
            "efficiency", None, False, flagged, clocked, len(rows),
            issue="No clocked hours on jobs in this period — efficiency cannot be computed",
        )
    return MetricValue("efficiency", flagged / clocked * 100.0, True, flagged, clocked, len(rows))


def productivity(rows: list[HistoryRow], total_clocked_hours: Optional[float]) -> MetricValue:
    """clocked hrs on jobs / total clocked hrs.

    The DMS export gives us on-job clocked hours.  TOTAL clocked hours come from
    the time clock.  If the store has not imported a time-clock export, we do
    NOT approximate the denominator with shift capacity and call it
    productivity — that would be exactly the fabricated number Rule 3 exists to
    prevent.  We say we do not have it.
    """
    on_job = sum(r.actual_clocked_hours for r in rows)
    if total_clocked_hours is None:
        return MetricValue(
            "productivity", None, False, on_job, None, len(rows),
            issue="No time-clock import — total clocked hours unknown. Import a time-clock export to enable Productivity.",
        )
    if total_clocked_hours <= 0:
        return MetricValue(
            "productivity", None, False, on_job, total_clocked_hours, len(rows),
            issue="Time clock reports 0 total hours for this period",
        )
    return MetricValue(
        "productivity", on_job / total_clocked_hours * 100.0, True,
        on_job, total_clocked_hours, len(rows),
    )


def utilization(rows: list[HistoryRow], capacity_hours: Optional[float]) -> MetricValue:
    """flagged hrs / available shift capacity."""
    flagged = sum(r.flagged_hours for r in rows)
    if capacity_hours is None:
        return MetricValue(
            "utilization", None, False, flagged, None, len(rows),
            issue="Technician shift hours / work days not set — shift capacity unknown",
        )
    if capacity_hours <= 0:
        return MetricValue(
            "utilization", None, False, flagged, capacity_hours, len(rows),
            issue="No scheduled shift capacity in this period",
        )
    return MetricValue(
        "utilization", flagged / capacity_hours * 100.0, True, flagged, capacity_hours, len(rows)
    )


def promise_pct(rows: list[HistoryRow]) -> MetricValue:
    """ROs completed before promise / ROs with a promise."""
    with_promise = [r for r in rows if r.promise_time is not None and r.closed_at is not None]
    if not with_promise:
        return MetricValue(
            "promise_pct", None, False, 0, 0, 0,
            issue="No promise times captured on this technician's ROs in this period",
        )
    hit = sum(1 for r in with_promise if r.closed_at <= r.promise_time)
    return MetricValue(
        "promise_pct", hit / len(with_promise) * 100.0, True,
        float(hit), float(len(with_promise)), len(with_promise),
    )


def comeback_rate(rows: list[HistoryRow], comeback_count: int) -> MetricValue:
    """same-concern reopens <= window / completed ROs.

    `comeback_count` is the number of comeback pairs where THIS tech performed
    the original repair (computed once at import: same VIN + same concern
    category inside the window).
    """
    completed = len({r.ro_number for r in rows})
    if completed == 0:
        return MetricValue(
            "comeback_rate", None, False, float(comeback_count), 0, 0,
            issue="No completed ROs in this period",
        )
    return MetricValue(
        "comeback_rate", comeback_count / completed * 100.0, True,
        float(comeback_count), float(completed), completed,
    )


def first_time_fix(rows: list[HistoryRow], comeback_count: int) -> MetricValue:
    """1 - (returns for same concern <= window / completed ROs)."""
    completed = len({r.ro_number for r in rows})
    if completed == 0:
        return MetricValue(
            "first_time_fix", None, False, float(comeback_count), 0, 0,
            issue="No completed ROs in this period",
        )
    return MetricValue(
        "first_time_fix", (1.0 - comeback_count / completed) * 100.0, True,
        float(completed - comeback_count), float(completed), completed,
    )


# --------------------------------------------------------------------------- #
# Shift capacity                                                               #
# --------------------------------------------------------------------------- #

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def shift_capacity_hours(
    start: date,
    end: date,
    shift_start,           # datetime.time | None
    shift_end,             # datetime.time | None
    work_days: Iterable[str],
    lunch_start=None,
    lunch_end=None,
) -> Optional[float]:
    """Scheduled, available bench hours between two dates.  None if unknowable."""
    if shift_start is None or shift_end is None:
        return None
    days = {d.strip()[:3].title() for d in work_days if d}
    if not days:
        return None

    daily = (
        datetime.combine(date(2000, 1, 1), shift_end)
        - datetime.combine(date(2000, 1, 1), shift_start)
    ).total_seconds() / 3600.0
    if daily <= 0:
        return None

    if lunch_start and lunch_end:
        lunch = (
            datetime.combine(date(2000, 1, 1), lunch_end)
            - datetime.combine(date(2000, 1, 1), lunch_start)
        ).total_seconds() / 3600.0
        daily -= max(0.0, lunch)

    total = 0.0
    cursor = start
    while cursor <= end:
        if _DAY_NAMES[cursor.weekday()] in days:
            total += daily
        cursor += timedelta(days=1)
    return total


# --------------------------------------------------------------------------- #
# The scorecard                                                                #
# --------------------------------------------------------------------------- #


def build_scorecard(
    *,
    technician_id: str,
    name: str,
    team: Optional[str],
    skill_level: Optional[str],
    period: str,                       # DAILY | MTD | T90
    period_start: date,
    period_end: date,
    rows: list[HistoryRow],            # already filtered to this tech + period
    capacity_hours: Optional[float],
    total_clocked_hours: Optional[float],
    comeback_count: int,
    config: MetricsConfig,
    source_data_age_hours: Optional[float],
) -> TechScorecard:
    """Assemble one technician's card for one window, gates and all."""
    issues: list[str] = []

    # --- Guardian: is the source data current? -------------------------------
    if source_data_age_hours is None:
        issues.append("No DMS history has been imported for this store")
    elif source_data_age_hours > config.data_staleness_hours:
        days = source_data_age_hours / 24.0
        issues.append(
            f"DMS export is {days:.1f} days old (stale past "
            f"{config.data_staleness_hours}h) — metrics not current"
        )

    if total_clocked_hours is None:
        issues.append("No time-clock import — Productivity unavailable")

    ro_count = len({r.ro_number for r in rows})
    flagged = sum(r.flagged_hours for r in rows)
    clocked = sum(r.actual_clocked_hours for r in rows)

    cp = sum(r.flagged_hours for r in rows if r.labor_type == "CP")
    warranty = sum(r.flagged_hours for r in rows if r.labor_type == "WARRANTY")
    internal = sum(r.flagged_hours for r in rows if r.labor_type == "INTERNAL")

    # --- Minimum-volume gate -------------------------------------------------
    # 2 ROs at 140% must never outrank 40 ROs at 112%.
    qualifies = (
        ro_count >= config.min_ros_to_rank
        or flagged >= config.min_flagged_hours_to_rank
    )
    if not qualifies:
        issues.append(
            f"Building sample — {ro_count} ROs / {flagged:.1f} flagged hrs "
            f"(needs {config.min_ros_to_rank} ROs or "
            f"{config.min_flagged_hours_to_rank:.0f} hrs to rank)"
        )

    data_complete = source_data_age_hours is not None and (
        source_data_age_hours <= config.data_staleness_hours
    )

    return TechScorecard(
        technician_id=technician_id,
        name=name,
        team=team,
        skill_level=skill_level,
        period=period,
        period_start=period_start,
        period_end=period_end,
        efficiency=efficiency(rows),
        productivity=productivity(rows, total_clocked_hours),
        utilization=utilization(rows, capacity_hours),
        promise_pct=promise_pct(rows),
        comeback_rate=comeback_rate(rows, comeback_count),
        first_time_fix=first_time_fix(rows, comeback_count),
        ro_count=ro_count,
        flagged_hours=flagged,
        clocked_hours=clocked,
        cp_flagged_hours=cp,
        warranty_flagged_hours=warranty,
        internal_flagged_hours=internal,
        qualifies_for_ranking=qualifies and data_complete,
        data_complete=data_complete,
        data_issues=issues,
    )


def rank_like_for_like(cards: list[TechScorecard], metric: str) -> dict[str, list[str]]:
    """FR-6.4 — rank WITHIN team and skill level, never store-wide.

    Lube book times are not heavy-line book times.  Returns
    {"<team>|<skill_level>": [technician_id, ...]} in rank order, containing
    only technicians who passed BOTH the volume gate and the Guardian gate.
    """
    groups: dict[str, list[TechScorecard]] = {}
    for c in cards:
        if not c.qualifies_for_ranking:
            continue
        mv: MetricValue = getattr(c, metric)
        if not mv.available or mv.value is None:
            continue
        groups.setdefault(f"{c.team or '—'}|{c.skill_level or '—'}", []).append(c)

    # Lower is better for comeback rate; higher is better for everything else.
    ascending = metric == "comeback_rate"
    out: dict[str, list[str]] = {}
    for key, members in groups.items():
        members.sort(
            key=lambda c: (
                getattr(c, metric).value if ascending else -getattr(c, metric).value,
                c.technician_id,
            )
        )
        out[key] = [c.technician_id for c in members]
    return out


def team_averages(cards: list[TechScorecard], metric: str) -> dict[str, Optional[float]]:
    """Team averages alongside the store average (FR-6.4)."""
    buckets: dict[str, list[float]] = {}
    for c in cards:
        if not c.qualifies_for_ranking:
            continue
        mv: MetricValue = getattr(c, metric)
        if not mv.available or mv.value is None:
            continue
        buckets.setdefault(c.team or "—", []).append(mv.value)
        buckets.setdefault("__STORE__", []).append(mv.value)
    return {k: (sum(v) / len(v) if v else None) for k, v in buckets.items()}
