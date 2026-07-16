"""The Match Score engine.

RULE 1 — THIS IS A DETERMINISTIC ALGORITHM, NOT AN LLM.
    Every function in this module is pure.  No I/O, no clock, no randomness, no
    network, no model call.  The same ROInput + TechInput + ScoringContext will
    produce byte-identical output forever.  A technician must be able to
    reproduce any number in here with a calculator.

RULE 2 — EVERY SCORE EXPLAINS ITSELF.
    Nothing returns a bare number.  score_technician() returns the score AND the
    list of reasons, with the points each factor contributed, plus warnings.

RULE 3 — NEVER HIDE UNCERTAINTY.
    Where a factor has no sample behind it (a tech who has never done this kind
    of job, a shop with no history import), the factor contributes ZERO and says
    so out loud.  It is never quietly imputed to an average.  When the caller
    marks a tech's data as stale, the score comes back with confident=False and
    the specific issue named; such a tech can never hold BEST FIT.

Stage 1 is a hard pass/fail filter (safety, liability, OEM warranty).
Stage 2 scores the survivors 0-100.
Stage 3 is the explanation, and it is not optional.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

from .types import (
    AVAIL_MARGIN_SHARE,
    AVAIL_WAIT_SHARE,
    AVAILABILITY_COMFORT_MARGIN_H,
    AVAILABILITY_WAIT_HORIZON_MIN,
    EFFICIENCY_FULL_MARKS_PCT,
    ENGINE_VERSION,
    EXCL_CANNOT_MAKE_PROMISE,
    EXCL_INACTIVE,
    EXCL_MISSING_CERT,
    EXCL_OFF_SHIFT,
    EXCL_RESTRICTED_WORK,
    EXCL_WRONG_TEAM,
    OVERQUALIFIED_FLOOR,
    OVERQUALIFIED_PENALTY_PER_RANK,
    PERF_EFFICIENCY_SHARE,
    PERF_FTF_SHARE,
    TIER_TARGET_RANK,
    UNDERQUALIFIED_PENALTY_PER_RANK,
    WORKLOAD_FREE_FRACTION,
    Candidate,
    NotEligible,
    RankingResult,
    ROInput,
    Reason,
    RankingResult as _RankingResult,  # noqa: F401  (kept for re-export clarity)
    ScoringContext,
    TechInput,
)

__all__ = [
    "rank_technicians",
    "score_technician",
    "check_hard_constraints",
    "project_finish",
    "ENGINE_VERSION",
]


# --------------------------------------------------------------------------- #
# Small pure helpers                                                           #
# --------------------------------------------------------------------------- #


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _round_half_up(value: float) -> int:
    """Deterministic rounding.

    Python's built-in round() uses banker's rounding, so round(2.5) == 2 and
    round(3.5) == 4.  A dispatcher checking our arithmetic by hand would call
    that a bug.  Half always goes up.
    """
    return int(math.floor(value + 0.5))


def _fmt_time(dt: Optional[datetime], tz=None) -> str:
    """'1:42 PM' — the format on the dispatch board, in the dealer's timezone.

    `tz` affects only how the clock time is spelled; it never enters any
    comparison or score.  With tz=None the datetime is rendered as-is.
    """
    if dt is None:
        return "—"
    if tz is not None and dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {ampm}"


def _fmt_wait(minutes: float) -> str:
    if minutes <= 1:
        return "Available now"
    if minutes < 60:
        return f"Available in {int(round(minutes))} minutes"
    hours = minutes / 60.0
    if abs(hours - round(hours)) < 0.05:
        return f"Available in {int(round(hours))} hr"
    return f"Available in {hours:.1f} hrs"


def project_finish(
    start: datetime,
    hours: float,
    lunch_start: Optional[datetime] = None,
    lunch_end: Optional[datetime] = None,
) -> datetime:
    """When a job started at `start` and taking `hours` of wrench time finishes.

    Pure calendar arithmetic.  If the work spans the tech's lunch, lunch is
    pushed out of the way and the finish slides later by the length of it.
    """
    finish = start + timedelta(hours=hours)
    if lunch_start and lunch_end and lunch_end > lunch_start:
        # The job overlaps lunch if it is still running when lunch begins.
        if start < lunch_end and finish > lunch_start:
            overlap_start = max(start, lunch_start)
            finish += lunch_end - overlap_start
    return finish


# --------------------------------------------------------------------------- #
# Stage 1 — hard constraints (pass / fail)                                      #
# --------------------------------------------------------------------------- #


def check_hard_constraints(
    ro: ROInput, tech: TechInput, ctx: ScoringContext
) -> Optional[NotEligible]:
    """Return a NotEligible if this tech is disqualified outright, else None.

    This is a filter, not a penalty.  Safety, liability and OEM warranty rules
    depend on it: an EV job does not go to a tech without an HV/EV cert at ANY
    score.
    """
    if not tech.active:
        return NotEligible(
            tech.id, tech.name, tech.level_label, EXCL_INACTIVE, "Technician is inactive"
        )

    # --- required certifications: ALL of them, no exceptions -----------------
    missing = [c for c in ro.required_certs if c not in tech.certs]
    if missing:
        pretty = ", ".join(m.replace("_", "/") for m in missing)
        return NotEligible(
            tech.id,
            tech.name,
            tech.level_label,
            EXCL_MISSING_CERT,
            f"Missing required certification: {pretty}",
        )

    # --- restricted work types ----------------------------------------------
    if ro.work_type and ro.work_type in tech.restricted_work_types:
        return NotEligible(
            tech.id,
            tech.name,
            tech.level_label,
            EXCL_RESTRICTED_WORK,
            f"{ro.work_type.title()} work is on this technician's restricted list",
        )

    # --- team separation ------------------------------------------------------
    if ctx.enforce_team_separation and ro.required_team and tech.team != ro.required_team:
        return NotEligible(
            tech.id,
            tech.name,
            tech.level_label,
            EXCL_WRONG_TEAM,
            f"Job is reserved for the {ro.required_team} team (tech is on {tech.team or 'no team'})",
        )

    # --- on shift -------------------------------------------------------------
    if not tech.on_shift:
        return NotEligible(
            tech.id, tech.name, tech.level_label, EXCL_OFF_SHIFT, "Not on shift today"
        )

    # --- can they start AND finish before the promise time? -------------------
    free_at = tech.free_at or ctx.now
    start_at = max(free_at, ctx.now)
    finish = project_finish(start_at, ro.est_hours, tech.lunch_start_at, tech.lunch_end_at)

    if ro.promise_at and finish > ro.promise_at:
        over = (finish - ro.promise_at).total_seconds() / 3600.0
        return NotEligible(
            tech.id,
            tech.name,
            tech.level_label,
            EXCL_CANNOT_MAKE_PROMISE,
            f"Cannot finish before the {_fmt_time(ro.promise_at, ctx.display_tz)} promise "
            f"(earliest completion {_fmt_time(finish, ctx.display_tz)}, {over:.1f} hrs late)",
        )

    # If there is no promise time we still refuse to hand them a job they cannot
    # physically finish before they go home.
    if tech.shift_end_at and finish > tech.shift_end_at and ro.promise_at is None:
        over = (finish - tech.shift_end_at).total_seconds() / 3600.0
        if over > 0.5:  # half an hour of stay-late is normal; a whole job is not
            return NotEligible(
                tech.id,
                tech.name,
                tech.level_label,
                EXCL_OFF_SHIFT,
                f"Job runs {over:.1f} hrs past end of shift "
                f"({_fmt_time(tech.shift_end_at, ctx.display_tz)})",
            )

    return None


# --------------------------------------------------------------------------- #
# Stage 2 — the five weighted factors                                          #
# --------------------------------------------------------------------------- #


def _score_skill(ro: ROInput, tech: TechInput, ctx: ScoringContext) -> tuple[float, Reason]:
    """Tech skill level vs RO tier.

    Exact match = full marks.  Over-qualified = a slight reduction (a Master on
    a lube job is a waste of a Master, not a mistake).  Under-qualified = a
    heavy reduction (wrong body on the job).
    """
    weight = ctx.weights.skill
    target = TIER_TARGET_RANK.get(ro.tier, 4)
    rank = tech.skill_rank

    if rank == 0:
        # No skill level on file.  We do not guess one.
        return 0.0, Reason(
            "skill",
            f"No skill level recorded — cannot assess fit for a Tier {ro.tier} job",
            0.0,
        )

    delta = rank - target
    if delta == 0:
        factor = 1.0
        text = f"{tech.skill_level} — exact match for a Tier {ro.tier} job"
    elif delta > 0:
        factor = max(OVERQUALIFIED_FLOOR, 1.0 - OVERQUALIFIED_PENALTY_PER_RANK * delta)
        text = f"{tech.skill_level} — over-qualified for Tier {ro.tier} ({delta} level(s) above)"
    else:
        factor = max(0.0, 1.0 - UNDERQUALIFIED_PENALTY_PER_RANK * abs(delta))
        text = f"{tech.skill_level} — under-qualified for Tier {ro.tier} ({abs(delta)} level(s) below)"

    points = weight * factor
    return points, Reason("skill", text, points)


def _score_familiarity(ro: ROInput, tech: TechInput, ctx: ScoringContext) -> tuple[float, Reason]:
    """How many of this exact kind of repair has this tech actually done?

    Normalised on a log scale against the shop's best in this category, so the
    gap between 0 and 20 repairs matters far more than the gap between 300 and
    320 — which is how competence actually works.
    """
    weight = ctx.weights.familiarity
    done = tech.category.repairs_completed if tech.category else 0
    shop_max = max(ctx.shop_max_category_repairs, done)

    if shop_max <= 0:
        # Nobody in the shop has any history in this category — usually a shop
        # that has not imported its 90 days yet.  We do not invent a number.
        return 0.0, Reason(
            "familiarity",
            f"No {ro.concern_category} history in the shop yet — familiarity unscored",
            0.0,
        )

    if done == 0:
        return 0.0, Reason(
            "familiarity",
            f"No recorded {ro.concern_category} repairs in the last 90 days",
            0.0,
        )

    factor = math.log1p(done) / math.log1p(shop_max)
    points = weight * _clamp(factor)
    return points, Reason(
        "familiarity",
        f"Completed {done} similar {ro.concern_category} repairs",
        points,
    )


def _score_performance(ro: ROInput, tech: TechInput, ctx: ScoringContext) -> tuple[float, Reason]:
    """Efficiency + first-time-fix, WITHIN this concern category.

    Store-wide efficiency is not used here on purpose: a tech who is 130% on
    brakes and 70% on electrical is not a 100% tech for an A/C job.
    """
    weight = ctx.weights.performance
    stats = tech.category

    if stats is None or stats.repairs_completed == 0:
        return 0.0, Reason(
            "performance",
            f"No {ro.concern_category} performance sample — factor scored 0, not assumed",
            0.0,
        )

    have_eff = stats.avg_efficiency is not None
    have_ftf = stats.first_time_fix is not None

    if not have_eff and not have_ftf:
        return 0.0, Reason(
            "performance",
            f"No efficiency or first-time-fix data for {ro.concern_category}",
            0.0,
        )

    eff_norm = _clamp((stats.avg_efficiency or 0.0) / EFFICIENCY_FULL_MARKS_PCT)
    ftf_norm = _clamp(stats.first_time_fix or 0.0)

    if have_eff and have_ftf:
        factor = PERF_EFFICIENCY_SHARE * eff_norm + PERF_FTF_SHARE * ftf_norm
        text = (
            f"{stats.avg_efficiency:.0f}% efficiency and "
            f"{stats.first_time_fix * 100:.0f}% first-time-fix on {ro.concern_category}"
        )
    elif have_eff:
        # Only score the half we actually have.  We do not backfill the other.
        factor = PERF_EFFICIENCY_SHARE * eff_norm
        text = f"{stats.avg_efficiency:.0f}% efficiency on {ro.concern_category} (no first-time-fix sample)"
    else:
        factor = PERF_FTF_SHARE * ftf_norm
        text = f"{stats.first_time_fix * 100:.0f}% first-time-fix on {ro.concern_category} (no efficiency sample)"

    points = weight * factor
    return points, Reason("performance", text, points)


def _score_availability(
    ro: ROInput, tech: TechInput, ctx: ScoringContext
) -> tuple[float, list[Reason], Optional[datetime], Optional[float]]:
    """How soon are they free, and how much slack is left against the promise?

    Returns (points, reasons, projected_finish, promise_margin_hours).
    A tech who cannot make the promise never gets here — that is a Stage 1
    exclusion — so the margin is always >= 0 for anyone we score.
    """
    weight = ctx.weights.availability
    free_at = max(tech.free_at or ctx.now, ctx.now)
    wait_min = (free_at - ctx.now).total_seconds() / 60.0
    finish = project_finish(free_at, ro.est_hours, tech.lunch_start_at, tech.lunch_end_at)

    wait_factor = _clamp(1.0 - wait_min / AVAILABILITY_WAIT_HORIZON_MIN)

    if ro.promise_at:
        margin_h = (ro.promise_at - finish).total_seconds() / 3600.0
        margin_factor = _clamp(margin_h / AVAILABILITY_COMFORT_MARGIN_H)
    else:
        margin_h = None
        margin_factor = 1.0  # no promise to protect

    factor = AVAIL_WAIT_SHARE * wait_factor + AVAIL_MARGIN_SHARE * margin_factor
    points = weight * factor

    reasons = [Reason("availability", _fmt_wait(wait_min), points)]

    if ro.promise_at:
        reasons.append(
            Reason(
                "promise",
                f"Expected completion {_fmt_time(finish, ctx.display_tz)} — "
                f"customer promise ({_fmt_time(ro.promise_at, ctx.display_tz)}) protected "
                f"with {margin_h:.1f} hrs to spare",
                0.0,  # the promise line is context; its points are inside availability
            )
        )

    return points, reasons, finish, margin_h


def _score_workload(
    ro: ROInput, tech: TechInput, ctx: ScoringContext
) -> tuple[float, Reason, list[str], float]:
    """Penalty as the tech approaches their daily cap / overtime threshold.

    Returns (points, reason, warnings, projected_hours_today).
    """
    weight = ctx.weights.workload
    warnings: list[str] = []

    max_hours = tech.max_daily_hours or 8.0
    projected = tech.assigned_hours_today + ro.est_hours

    free_ceiling = WORKLOAD_FREE_FRACTION * max_hours
    if projected <= free_ceiling:
        factor = 1.0
    elif projected >= max_hours:
        factor = 0.0
    else:
        span = max_hours - free_ceiling
        factor = _clamp(1.0 - (projected - free_ceiling) / span) if span > 0 else 0.0

    points = weight * factor

    ot = tech.overtime_threshold or max_hours
    if projected > ot:
        warnings.append(f"{projected - ot:.1f} hrs into overtime if assigned")
    elif ot - projected <= 0.5:
        warnings.append(f"{ot - projected:.1f} hrs from the overtime threshold")

    if projected > max_hours:
        warnings.append(
            f"Exceeds max daily workload ({projected:.1f} / {max_hours:.1f} hrs)"
        )

    reason = Reason(
        "workload",
        f"{tech.assigned_hours_today:.1f} / {max_hours:.1f} hrs assigned today "
        f"({projected:.1f} hrs with this RO)",
        points,
    )
    return points, reason, warnings, projected


def _score_specialty(ro: ROInput, tech: TechInput, ctx: ScoringContext) -> tuple[float, Optional[Reason]]:
    """Optional soft bonus: this is their preferred work type or their vehicle."""
    bonus = ctx.weights.specialty_bonus
    if bonus <= 0:
        return 0.0, None

    hits: list[str] = []
    if ro.work_type and ro.work_type in tech.specialty_work_types:
        hits.append(ro.work_type.title())
    if ro.vehicle_model and ro.vehicle_model in tech.vehicle_specialties:
        hits.append(ro.vehicle_model)

    if not hits:
        return 0.0, None

    return bonus, Reason(
        "specialty", f"Declared specialty: {' · '.join(hits)}", bonus
    )


# --------------------------------------------------------------------------- #
# score_technician — one tech, one RO                                          #
# --------------------------------------------------------------------------- #


def score_technician(ro: ROInput, tech: TechInput, ctx: ScoringContext) -> Candidate:
    """Score ONE eligible technician against ONE repair order.

    The caller must have already run check_hard_constraints().  Scoring a tech
    who fails a hard constraint is a bug, not a low score.
    """
    reasons: list[Reason] = []
    warnings: list[str] = []
    total = 0.0

    # A cert the RO demanded and the tech holds is worth saying out loud even
    # though it carries no points — it is why they are on the list at all.
    for cert in ro.required_certs:
        reasons.append(
            Reason("cert", f"{cert.replace('_', '/')} certified (required for this RO)", 0.0)
        )

    skill_pts, skill_reason = _score_skill(ro, tech, ctx)
    total += skill_pts
    reasons.append(skill_reason)

    fam_pts, fam_reason = _score_familiarity(ro, tech, ctx)
    total += fam_pts
    reasons.append(fam_reason)

    perf_pts, perf_reason = _score_performance(ro, tech, ctx)
    total += perf_pts
    reasons.append(perf_reason)

    avail_pts, avail_reasons, finish, margin = _score_availability(ro, tech, ctx)
    total += avail_pts
    reasons.extend(avail_reasons)

    work_pts, work_reason, work_warnings, projected_hours = _score_workload(ro, tech, ctx)
    total += work_pts
    reasons.append(work_reason)
    warnings.extend(work_warnings)

    spec_pts, spec_reason = _score_specialty(ro, tech, ctx)
    total += spec_pts
    if spec_reason:
        reasons.append(spec_reason)

    # The base factors are weighted to 100; the specialty bonus can push past it.
    score = _round_half_up(_clamp(total, 0.0, 100.0))

    # RULE 3 — Guardian.  If the caller told us this tech's source data is stale
    # or below sample, we return the score but refuse to call it confident, and
    # we name the issue.  Never silently computed.
    confident = len(tech.data_issues) == 0
    if not confident:
        warnings.append("Score is provisional — source data incomplete")

    return Candidate(
        technician_id=tech.id,
        name=tech.name,
        level=tech.level_label,
        score=score,
        best_fit=False,  # assigned by rank_technicians, which sees the whole field
        confident=confident,
        reasons=reasons,
        warnings=warnings,
        data_issues=list(tech.data_issues),
        free_at=max(tech.free_at or ctx.now, ctx.now),
        projected_finish=finish,
        promise_margin_hours=margin,
        projected_hours_today=projected_hours,
        familiarity_repairs=tech.category.repairs_completed if tech.category else 0,
    )


# --------------------------------------------------------------------------- #
# rank_technicians — the entry point                                           #
# --------------------------------------------------------------------------- #


def _sort_key(c: Candidate):
    """Ranking order, fully deterministic.

    Primary:   confident techs above provisional ones (Guardian).
    Then:      score, descending.
    Tie-break, in the order the spec requires:
        1. promise safety   — more margin against the promise time wins
        2. lower workload   — the less loaded tech wins
        3. higher familiarity — more repairs in this category wins
        4. technician_id    — a stable final tiebreak so the order can NEVER
                              flip between two runs on identical input.
    """
    margin = c.promise_margin_hours if c.promise_margin_hours is not None else float("inf")
    return (
        0 if c.confident else 1,          # confident first
        -c.score,                          # higher score first
        -margin,                           # more promise slack first
        c.projected_hours_today,           # lighter load first
        -c.familiarity_repairs,            # more familiar first
        c.technician_id,                   # stable
    )


def rank_technicians(
    ro: ROInput,
    techs: list[TechInput],
    ctx: ScoringContext,
) -> RankingResult:
    """Rank every technician for one RO.

    Stage 1: hard filter.  Stage 2: score the survivors.  Stage 3: explain.

    Pure.  Given the same ro/techs/ctx this returns the same result every time,
    including the order of ties.
    """
    eligible: list[Candidate] = []
    not_eligible: list[NotEligible] = []

    for tech in techs:
        failure = check_hard_constraints(ro, tech, ctx)
        if failure is not None:
            not_eligible.append(failure)
            continue
        eligible.append(score_technician(ro, tech, ctx))

    eligible.sort(key=_sort_key)

    # BEST FIT goes to #1 — but only if we actually trust the data behind it.
    # A provisional score never wears the badge.
    ranked: list[Candidate] = []
    for i, c in enumerate(eligible):
        best = i == 0 and c.confident
        ranked.append(
            Candidate(
                technician_id=c.technician_id,
                name=c.name,
                level=c.level,
                score=c.score,
                best_fit=best,
                confident=c.confident,
                reasons=c.reasons,
                warnings=c.warnings,
                data_issues=c.data_issues,
                free_at=c.free_at,
                projected_finish=c.projected_finish,
                promise_margin_hours=c.promise_margin_hours,
                projected_hours_today=c.projected_hours_today,
                familiarity_repairs=c.familiarity_repairs,
            )
        )

    not_eligible.sort(key=lambda n: (n.code, n.name, n.technician_id))

    top_n = max(1, ctx.top_n)
    return RankingResult(
        ro_id=ro.id,
        engine_version=ENGINE_VERSION,
        weights=ctx.weights,
        candidates=ranked[:top_n],
        all_candidates=ranked,
        not_eligible=not_eligible,
    )
