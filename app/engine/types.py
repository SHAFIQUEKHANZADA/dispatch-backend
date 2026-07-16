"""Immutable input/output types for the Match Score engine.

Everything here is a frozen dataclass with no database, network, or clock
dependency.  The engine is fed values; it never fetches them.  That is what
makes RULE 1 (determinism) testable: same inputs -> same score, forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Optional

# Bump when the scoring math changes.  Frozen onto every assignment row so an
# old dispatch decision can always be explained with the math that produced it.
ENGINE_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# Skill levels                                                                 #
# --------------------------------------------------------------------------- #

# Ordinal rank of each skill level.  Used for the skill-vs-tier comparison.
SKILL_RANKS: dict[str, int] = {
    "Apprentice 1": 1,
    "Apprentice 2": 2,
    "Apprentice 3": 3,
    "General Tech": 4,
    "Diagnostic Tech": 5,
    "Master": 6,
    "Sr. Master": 7,
}

# The skill level a job of each tier is *written for*.
#   C = maintenance / lube        -> an Apprentice 2 is the right body
#   B = moderate repair           -> a General Tech is the right body
#   A = diagnostic / heavy line   -> a Master is the right body
TIER_TARGET_RANK: dict[str, int] = {"C": 2, "B": 4, "A": 6}

# Penalty per rank of mismatch, applied to the skill factor.
OVERQUALIFIED_PENALTY_PER_RANK = 0.08   # slight: don't burn a Master on a lube
UNDERQUALIFIED_PENALTY_PER_RANK = 0.30  # heavy: wrong body on the job
OVERQUALIFIED_FLOOR = 0.50

# Normalisation constants for the performance factor.
EFFICIENCY_FULL_MARKS_PCT = 120.0   # 120% efficiency in-category earns full credit
PERF_EFFICIENCY_SHARE = 0.60        # efficiency is 60% of the performance factor
PERF_FTF_SHARE = 0.40               # first-time-fix is the other 40%

# Availability shaping.
AVAILABILITY_WAIT_HORIZON_MIN = 120.0  # a tech free 2h+ from now scores 0 on "how soon"
AVAILABILITY_COMFORT_MARGIN_H = 2.0    # 2h+ of slack before promise = full marks
AVAIL_WAIT_SHARE = 0.50
AVAIL_MARGIN_SHARE = 0.50

# Workload shaping: no penalty until the tech is 70% of the way to their cap.
WORKLOAD_FREE_FRACTION = 0.70


@dataclass(frozen=True)
class MatchWeights:
    """Per-dealer weights (dealer_settings.match_weights). Editable without a redeploy."""

    skill: float = 25.0
    familiarity: float = 25.0
    performance: float = 20.0
    availability: float = 20.0
    workload: float = 10.0
    specialty_bonus: float = 3.0  # optional soft bonus, additive on top

    @property
    def base_total(self) -> float:
        return self.skill + self.familiarity + self.performance + self.availability + self.workload

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "MatchWeights":
        d = d or {}
        default = cls()
        return cls(
            skill=float(d.get("skill", default.skill)),
            familiarity=float(d.get("familiarity", default.familiarity)),
            performance=float(d.get("performance", default.performance)),
            availability=float(d.get("availability", default.availability)),
            workload=float(d.get("workload", default.workload)),
            specialty_bonus=float(d.get("specialty_bonus", default.specialty_bonus)),
        )


@dataclass(frozen=True)
class ScoringContext:
    """Everything about the *shop* that the score depends on, resolved up front."""

    now: datetime
    weights: MatchWeights
    enforce_team_separation: bool = True
    top_n: int = 3
    # Highest repairs_completed any tech in this shop has for the RO's concern
    # category.  Familiarity is normalised against this, so "317 repairs" means
    # something relative to the shop, not an absolute.
    shop_max_category_repairs: int = 0
    # Guardian: how old the source data may be before we stop trusting it.
    data_staleness_hours: int = 48
    # Display only — the dealer's timezone, used solely to render times ("4:00 PM")
    # in the reason text. The scoring math is timezone-agnostic; this never
    # affects a number, only how a clock time is spelled to the reader.
    display_tz: Optional[tzinfo] = None


@dataclass(frozen=True)
class CategoryStats:
    """A technician's track record *within the RO's concern category*."""

    repairs_completed: int = 0
    avg_efficiency: Optional[float] = None   # percent, e.g. 112.0
    first_time_fix: Optional[float] = None   # 0..1
    last_performed_at: Optional[datetime] = None


@dataclass(frozen=True)
class ROInput:
    id: str
    ro_number: str
    concern_category: str
    tier: str                                    # A | B | C
    est_hours: float
    required_certs: tuple[str, ...] = ()
    work_type: Optional[str] = None              # matched against restrictions
    required_team: Optional[str] = None
    promise_at: Optional[datetime] = None
    vehicle_model: Optional[str] = None


@dataclass(frozen=True)
class TechInput:
    id: str
    name: str
    skill_level: str
    team: Optional[str] = None
    active: bool = True

    certs: tuple[str, ...] = ()
    restricted_work_types: tuple[str, ...] = ()
    specialty_work_types: tuple[str, ...] = ()
    vehicle_specialties: tuple[str, ...] = ()

    # Shift state, pre-resolved for *today* by the caller.
    on_shift: bool = True
    shift_end_at: Optional[datetime] = None
    lunch_start_at: Optional[datetime] = None
    lunch_end_at: Optional[datetime] = None

    # The moment this tech is free to pick up new work.
    free_at: Optional[datetime] = None
    assigned_hours_today: float = 0.0
    max_daily_hours: float = 8.0
    overtime_threshold: float = 8.0

    # Track record in the RO's concern category (None = no sample at all).
    category: Optional[CategoryStats] = None

    # Guardian: specific, named reasons this tech's data cannot be trusted.
    # Non-empty => the score is returned but marked NOT confident.
    data_issues: tuple[str, ...] = ()

    @property
    def skill_rank(self) -> int:
        return SKILL_RANKS.get(self.skill_level, 0)

    @property
    def level_label(self) -> str:
        """'Master · HV' — level plus the certs worth showing on the card."""
        badge = [c for c in ("HV_EV", "HYBRID", "DIAGNOSTIC") if c in self.certs]
        suffix = {"HV_EV": "HV", "HYBRID": "Hybrid", "DIAGNOSTIC": "Diag"}
        if badge:
            return f"{self.skill_level} · {suffix[badge[0]]}"
        return self.skill_level


@dataclass(frozen=True)
class Reason:
    """One line of the 'why'. `points` is what this factor actually contributed."""

    factor: str   # cert | skill | familiarity | performance | availability | promise | workload | specialty
    text: str
    points: float

    def to_dict(self) -> dict:
        return {"factor": self.factor, "text": self.text, "points": round(self.points, 1)}


@dataclass(frozen=True)
class Candidate:
    technician_id: str
    name: str
    level: str
    score: int
    best_fit: bool
    confident: bool                       # False => Guardian flagged the source data
    reasons: list[Reason] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data_issues: list[str] = field(default_factory=list)

    # Surfaced for tie-breaking, the UI, and the audit trail.
    free_at: Optional[datetime] = None
    projected_finish: Optional[datetime] = None
    promise_margin_hours: Optional[float] = None
    projected_hours_today: float = 0.0
    familiarity_repairs: int = 0

    def to_dict(self) -> dict:
        return {
            "technician_id": self.technician_id,
            "name": self.name,
            "level": self.level,
            "score": self.score,
            "best_fit": self.best_fit,
            "confident": self.confident,
            "reasons": [r.to_dict() for r in self.reasons],
            "warnings": list(self.warnings),
            "data_issues": list(self.data_issues),
            "free_at": self.free_at.isoformat() if self.free_at else None,
            "projected_finish": self.projected_finish.isoformat() if self.projected_finish else None,
            "promise_margin_hours": (
                round(self.promise_margin_hours, 2) if self.promise_margin_hours is not None else None
            ),
            "projected_hours_today": round(self.projected_hours_today, 2),
            "familiarity_repairs": self.familiarity_repairs,
        }


# Exclusion codes — a machine-readable reason a tech was hard-filtered out.
EXCL_INACTIVE = "INACTIVE"
EXCL_MISSING_CERT = "MISSING_CERT"
EXCL_RESTRICTED_WORK = "RESTRICTED_WORK_TYPE"
EXCL_OFF_SHIFT = "OFF_SHIFT"
EXCL_CANNOT_MAKE_PROMISE = "CANNOT_MAKE_PROMISE"
EXCL_WRONG_TEAM = "WRONG_TEAM"


@dataclass(frozen=True)
class NotEligible:
    technician_id: str
    name: str
    level: str
    code: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "technician_id": self.technician_id,
            "name": self.name,
            "level": self.level,
            "code": self.code,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RankingResult:
    ro_id: str
    engine_version: str
    weights: MatchWeights
    candidates: list[Candidate]          # eligible, ranked, length <= top_n
    all_candidates: list[Candidate]      # every eligible tech, ranked
    not_eligible: list[NotEligible]

    def to_dict(self) -> dict:
        return {
            "ro_id": self.ro_id,
            "engine_version": self.engine_version,
            "weights": {
                "skill": self.weights.skill,
                "familiarity": self.weights.familiarity,
                "performance": self.weights.performance,
                "availability": self.weights.availability,
                "workload": self.weights.workload,
                "specialty_bonus": self.weights.specialty_bonus,
            },
            "candidates": [c.to_dict() for c in self.candidates],
            "all_candidates": [c.to_dict() for c in self.all_candidates],
            "not_eligible": [n.to_dict() for n in self.not_eligible],
        }
