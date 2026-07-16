"""Tests for the Match Score engine.

The headline test in this file is test_determinism_* — RULE 1.  If those ever
fail, the product is dead: a technician can no longer reproduce his own number,
and Reid's line ("a number a technician can disprove kills the board") comes
true.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.engine.match_score import (
    check_hard_constraints,
    project_finish,
    rank_technicians,
    score_technician,
)
from app.engine.types import (
    EXCL_CANNOT_MAKE_PROMISE,
    EXCL_INACTIVE,
    EXCL_MISSING_CERT,
    EXCL_OFF_SHIFT,
    EXCL_RESTRICTED_WORK,
    EXCL_WRONG_TEAM,
    CategoryStats,
    MatchWeights,
    ROInput,
    ScoringContext,
    TechInput,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


def ctx(**kw) -> ScoringContext:
    base = dict(
        now=NOW,
        weights=MatchWeights(),
        enforce_team_separation=True,
        top_n=3,
        shop_max_category_repairs=320,
        data_staleness_hours=48,
    )
    base.update(kw)
    return ScoringContext(**base)


def ro(**kw) -> ROInput:
    base = dict(
        id="ro-4460",
        ro_number="4460",
        concern_category="Electrical/AC",
        tier="B",
        est_hours=2.0,
        required_certs=(),
        work_type="ELECTRICAL",
        required_team=None,
        promise_at=NOW + timedelta(hours=7),
        vehicle_model="Odyssey",
    )
    base.update(kw)
    return ROInput(**base)


def tech(**kw) -> TechInput:
    base = dict(
        id="t-mike",
        name="Mike H.",
        skill_level="Master",
        team="Main",
        active=True,
        certs=("HV_EV", "ASE"),
        restricted_work_types=(),
        specialty_work_types=(),
        vehicle_specialties=(),
        on_shift=True,
        shift_end_at=NOW.replace(hour=17),
        lunch_start_at=NOW.replace(hour=12),
        lunch_end_at=NOW.replace(hour=12, minute=30),
        free_at=NOW + timedelta(minutes=12),
        assigned_hours_today=4.0,
        max_daily_hours=8.0,
        overtime_threshold=8.0,
        category=CategoryStats(
            repairs_completed=317, avg_efficiency=112.0, first_time_fix=0.96
        ),
        data_issues=(),
    )
    base.update(kw)
    return TechInput(**base)


# =========================================================================== #
# RULE 1 — DETERMINISM                                                        #
# =========================================================================== #


def test_determinism_same_inputs_same_score_1000_times():
    """The same RO and the same tech state must always produce the same score."""
    r, t, c = ro(), tech(), ctx()
    first = score_technician(r, t, c)
    for _ in range(1000):
        again = score_technician(r, t, c)
        assert again.score == first.score
        assert [x.to_dict() for x in again.reasons] == [x.to_dict() for x in first.reasons]
        assert again.warnings == first.warnings


def test_determinism_ranking_is_stable_under_input_reordering():
    """Shuffling the technician list must not change the ranking.

    This is the subtle one.  A ranking that depends on the order rows came back
    from the database is not reproducible, and a tech who saw himself at #1
    yesterday and #2 today with identical data would be right to stop believing
    the board.
    """
    r = ro()
    techs = [
        tech(id=f"t-{i}", name=f"Tech {i}", category=CategoryStats(50, 100.0, 0.9))
        for i in range(10)
    ]
    baseline = rank_technicians(r, techs, ctx()).to_dict()

    rng = random.Random(1234)
    for _ in range(50):
        shuffled = techs[:]
        rng.shuffle(shuffled)
        assert rank_technicians(r, shuffled, ctx()).to_dict() == baseline


def test_determinism_identical_techs_break_ties_stably():
    """Two techs identical in every scored respect still get a stable order."""
    r = ro()
    a = tech(id="t-aaa", name="A")
    b = tech(id="t-bbb", name="B")
    result_1 = rank_technicians(r, [a, b], ctx())
    result_2 = rank_technicians(r, [b, a], ctx())
    assert result_1.candidates[0].technician_id == result_2.candidates[0].technician_id
    assert result_1.candidates[0].score == result_2.candidates[0].score


def test_no_llm_in_the_scoring_path():
    """A structural guard: the engine must not import any model client.

    Cheap, but it is the test that fails loudly the day somebody 'just adds a
    quick LLM sanity check' to the scorer.
    """
    import inspect

    from app.engine import match_score, optimizer

    for module in (match_score, optimizer):
        source = inspect.getsource(module)
        for forbidden in ("openai", "anthropic", "google.generativeai", "litellm", "requests.post"):
            assert forbidden not in source, f"{module.__name__} must not touch {forbidden}"


# =========================================================================== #
# Stage 1 — hard constraints                                                   #
# =========================================================================== #


def test_missing_required_cert_is_a_hard_exclusion_not_a_penalty():
    """An EV job does not go to a tech without an HV/EV cert at ANY score."""
    r = ro(required_certs=("HV_EV",))
    no_cert = tech(id="t-x", name="No Cert", certs=("ASE",))
    result = rank_technicians(r, [no_cert], ctx())

    assert result.candidates == []
    assert len(result.not_eligible) == 1
    assert result.not_eligible[0].code == EXCL_MISSING_CERT
    assert "HV/EV" in result.not_eligible[0].reason


def test_restricted_work_type_excludes():
    r = ro(work_type="ENGINE")
    t = tech(restricted_work_types=("ENGINE",))
    failure = check_hard_constraints(r, t, ctx())
    assert failure is not None and failure.code == EXCL_RESTRICTED_WORK


def test_inactive_tech_never_appears():
    failure = check_hard_constraints(ro(), tech(active=False), ctx())
    assert failure is not None and failure.code == EXCL_INACTIVE


def test_off_shift_tech_excluded():
    failure = check_hard_constraints(ro(), tech(on_shift=False), ctx())
    assert failure is not None and failure.code == EXCL_OFF_SHIFT


def test_cannot_finish_before_promise_is_excluded():
    """The engine will not recommend a tech who cannot make the promise time."""
    r = ro(est_hours=6.0, promise_at=NOW + timedelta(hours=2))
    failure = check_hard_constraints(r, tech(), ctx())
    assert failure is not None
    assert failure.code == EXCL_CANNOT_MAKE_PROMISE
    assert "promise" in failure.reason.lower()


def test_team_separation_enforced_only_when_configured():
    r = ro(required_team="Lube")
    t = tech(team="Main")

    failure = check_hard_constraints(r, t, ctx(enforce_team_separation=True))
    assert failure is not None and failure.code == EXCL_WRONG_TEAM

    assert check_hard_constraints(r, t, ctx(enforce_team_separation=False)) is None


def test_excluded_techs_are_returned_with_their_reason():
    """FR-3.1 — the 'not eligible' list must say WHY, not just omit them."""
    r = ro(required_certs=("HV_EV",), work_type="ENGINE")
    techs = [
        tech(id="t-1", name="No Cert", certs=()),
        tech(id="t-2", name="Restricted", restricted_work_types=("ENGINE",)),
        tech(id="t-3", name="Inactive", active=False),
    ]
    result = rank_technicians(r, techs, ctx())
    assert len(result.not_eligible) == 3
    assert all(n.reason for n in result.not_eligible)


# =========================================================================== #
# Stage 2 — weighted scoring                                                   #
# =========================================================================== #


def test_score_is_bounded_0_to_100():
    r = ro()
    perfect = tech(
        skill_level="General Tech",             # exact match for tier B
        category=CategoryStats(320, 130.0, 1.0),
        free_at=NOW,
        assigned_hours_today=0.0,
        specialty_work_types=("ELECTRICAL",),
        vehicle_specialties=("Odyssey",),
    )
    worst = tech(
        id="t-worst",
        skill_level="Apprentice 1",
        category=None,
        free_at=NOW + timedelta(hours=4),
        assigned_hours_today=8.0,
    )
    for t in (perfect, worst):
        # Give the worst tech a promise they can technically still make.
        c = score_technician(ro(promise_at=NOW + timedelta(hours=12)), t, ctx())
        assert 0 <= c.score <= 100


def test_exact_skill_match_outscores_any_mismatch():
    r = ro(tier="B")  # target is General Tech (rank 4)
    exact = tech(id="t-a", skill_level="General Tech", category=CategoryStats(50, 100.0, 0.9))
    over = tech(id="t-b", skill_level="Master", category=CategoryStats(50, 100.0, 0.9))
    under = tech(id="t-c", skill_level="Apprentice 3", category=CategoryStats(50, 100.0, 0.9))

    by_id = {c.technician_id: c.score for c in rank_technicians(r, [exact, over, under], ctx()).all_candidates}
    assert by_id["t-a"] > by_id["t-b"]
    assert by_id["t-a"] > by_id["t-c"]


def test_at_equal_distance_underqualified_is_penalised_far_harder_than_over():
    """The spec's asymmetry: over-qualified = slight reduction (a wasted Master),
    under-qualified = heavy reduction (the wrong body on the job).

    Compared at the SAME distance from the target — one level either side of a
    Tier B job.  Across *different* distances the comparison is meaningless, and
    the engine is right to prefer an Apprentice on a lube job over a Sr. Master:
    that is not a mis-assignment, it is good use of the bench.
    """
    r = ro(tier="B")  # target = General Tech (rank 4)
    over = tech(id="t-over", skill_level="Diagnostic Tech", category=CategoryStats(50, 100.0, 0.9))
    under = tech(id="t-under", skill_level="Apprentice 3", category=CategoryStats(50, 100.0, 0.9))

    def skill_points(t):
        return next(x.points for x in score_technician(r, t, ctx()).reasons if x.factor == "skill")

    over_pts, under_pts = skill_points(over), skill_points(under)
    assert over_pts > under_pts
    # "slight" vs "heavy": the under-qualified penalty is several times the over.
    assert (25.0 - under_pts) > (25.0 - over_pts) * 3


def test_a_master_is_not_burned_on_a_lube_job():
    r = ro(tier="C")  # target = Apprentice 2
    lube_tech = tech(id="t-lube", skill_level="Apprentice 2", category=CategoryStats(50, 100.0, 0.9))
    sr_master = tech(id="t-sr", skill_level="Sr. Master", category=CategoryStats(50, 100.0, 0.9))

    result = rank_technicians(r, [sr_master, lube_tech], ctx())
    assert result.candidates[0].technician_id == "t-lube"


def test_familiarity_uses_a_log_scale_against_the_shop_max():
    """The gap 0 -> 20 repairs must matter far more than 300 -> 320."""
    r = ro()
    c = ctx(shop_max_category_repairs=320)

    def fam_points(n: int) -> float:
        t = tech(category=CategoryStats(n, 100.0, 0.9))
        cand = score_technician(r, t, c)
        return next(x.points for x in cand.reasons if x.factor == "familiarity")

    jump_low = fam_points(20) - fam_points(0)
    jump_high = fam_points(320) - fam_points(300)
    assert jump_low > jump_high * 3


def test_zero_familiarity_scores_zero_and_says_so():
    """RULE 3 — a tech with no history in the category is not imputed an average."""
    t = tech(category=CategoryStats(repairs_completed=0))
    c = score_technician(ro(), t, ctx())
    fam = next(x for x in c.reasons if x.factor == "familiarity")
    assert fam.points == 0.0
    assert "No recorded" in fam.text


def test_no_performance_sample_scores_zero_and_is_not_assumed():
    t = tech(category=None)
    c = score_technician(ro(), t, ctx())
    perf = next(x for x in c.reasons if x.factor == "performance")
    assert perf.points == 0.0
    assert "not assumed" in perf.text


def test_workload_penalty_grows_as_the_tech_approaches_the_cap():
    r = ro(est_hours=1.0)
    light = tech(id="t-l", assigned_hours_today=0.0)
    heavy = tech(id="t-h", assigned_hours_today=7.0)

    def workload_points(t):
        return next(
            x.points for x in score_technician(r, t, ctx()).reasons if x.factor == "workload"
        )

    assert workload_points(light) > workload_points(heavy)


def test_overtime_warning_is_raised():
    r = ro(est_hours=1.0)
    t = tech(assigned_hours_today=7.8, overtime_threshold=8.0, max_daily_hours=10.0)
    c = score_technician(r, t, ctx())
    assert any("overtime" in w.lower() for w in c.warnings)


def test_specialty_bonus_applies_only_on_a_match():
    r = ro(work_type="ELECTRICAL", vehicle_model="Odyssey")
    plain = tech(id="t-p")
    special = tech(id="t-s", specialty_work_types=("ELECTRICAL",), vehicle_specialties=("Odyssey",))
    assert score_technician(r, special, ctx()).score > score_technician(r, plain, ctx()).score


# =========================================================================== #
# RULE 2 — every score explains itself                                         #
# =========================================================================== #


def test_every_score_returns_a_reason_list_with_points():
    c = score_technician(ro(required_certs=("HV_EV",)), tech(), ctx())
    factors = {r.factor for r in c.reasons}
    assert {"cert", "skill", "familiarity", "performance", "availability", "workload"} <= factors
    # The weighted factors must add up to the score (within rounding).
    scored = sum(r.points for r in c.reasons)
    assert abs(scored - c.score) < 1.0


def test_the_reasons_read_like_the_spec():
    """The example in the requirements doc, end to end."""
    r = ro(required_certs=("HV_EV",), concern_category="Electrical/AC")
    c = score_technician(r, tech(), ctx())
    text = " | ".join(x.text for x in c.reasons)
    assert "HV/EV certified (required for this RO)" in text
    assert "Completed 317 similar Electrical/AC repairs" in text
    assert "112% efficiency" in text
    assert "Available in 12 minutes" in text
    assert "promise" in text.lower()


# =========================================================================== #
# RULE 3 — never hide uncertainty                                              #
# =========================================================================== #


def test_stale_data_yields_a_provisional_score_never_a_confident_one():
    t = tech(data_issues=("Time clock stale 3d",))
    c = score_technician(ro(), t, ctx())
    assert c.confident is False
    assert "Time clock stale 3d" in c.data_issues
    assert any("provisional" in w.lower() for w in c.warnings)


def test_a_provisional_tech_can_never_wear_the_best_fit_badge():
    """Even if their raw score is the highest in the shop."""
    strong_but_stale = tech(
        id="t-stale",
        name="Stale",
        skill_level="General Tech",
        category=CategoryStats(320, 130.0, 1.0),
        free_at=NOW,
        assigned_hours_today=0.0,
        data_issues=("DMS export is 6 days old",),
    )
    weaker_but_clean = tech(
        id="t-clean",
        name="Clean",
        skill_level="General Tech",
        category=CategoryStats(40, 100.0, 0.9),
        assigned_hours_today=3.0,
    )
    result = rank_technicians(ro(), [strong_but_stale, weaker_but_clean], ctx())

    assert result.candidates[0].technician_id == "t-clean"
    assert result.candidates[0].best_fit is True
    stale = next(c for c in result.all_candidates if c.technician_id == "t-stale")
    assert stale.best_fit is False
    assert stale.confident is False


# =========================================================================== #
# Ranking / top-N                                                              #
# =========================================================================== #


def test_top_n_defaults_to_3_and_is_configurable():
    techs = [tech(id=f"t-{i}", name=f"T{i}") for i in range(8)]
    assert len(rank_technicians(ro(), techs, ctx()).candidates) == 3
    assert len(rank_technicians(ro(), techs, ctx(top_n=5)).candidates) == 5
    assert len(rank_technicians(ro(), techs, ctx()).all_candidates) == 8


def test_tie_break_order_is_promise_safety_then_workload_then_familiarity():
    r = ro(est_hours=1.0, promise_at=NOW + timedelta(hours=8))

    # Identical except for how soon they are free -> more promise margin wins.
    early = tech(id="t-early", free_at=NOW, assigned_hours_today=3.0)
    late = tech(id="t-late", free_at=NOW + timedelta(hours=2), assigned_hours_today=3.0)
    result = rank_technicians(r, [late, early], ctx())
    assert result.candidates[0].technician_id == "t-early"


def test_best_fit_is_set_on_exactly_one_candidate():
    techs = [tech(id=f"t-{i}", name=f"T{i}") for i in range(5)]
    result = rank_technicians(ro(), techs, ctx())
    assert sum(1 for c in result.all_candidates if c.best_fit) == 1


# =========================================================================== #
# Weights are per-dealer and take effect without a redeploy                     #
# =========================================================================== #


def test_reweighting_changes_the_winner():
    """A store that cares most about promise times can weight availability up."""
    r = ro(est_hours=1.0, promise_at=NOW + timedelta(hours=8))
    veteran = tech(  # deeply familiar, but busy for another 2 hours
        id="t-vet", name="Veteran",
        category=CategoryStats(320, 120.0, 0.98),
        free_at=NOW + timedelta(hours=2),
        assigned_hours_today=6.0,
    )
    rookie = tech(  # free right now, thin history
        id="t-rookie", name="Rookie",
        category=CategoryStats(12, 100.0, 0.9),
        free_at=NOW,
        assigned_hours_today=0.0,
    )

    familiarity_first = MatchWeights(skill=15, familiarity=45, performance=20, availability=10, workload=10)
    availability_first = MatchWeights(skill=15, familiarity=10, performance=15, availability=50, workload=10)

    top_fam = rank_technicians(r, [veteran, rookie], ctx(weights=familiarity_first)).candidates[0]
    top_avail = rank_technicians(r, [veteran, rookie], ctx(weights=availability_first)).candidates[0]

    assert top_fam.technician_id == "t-vet"
    assert top_avail.technician_id == "t-rookie"


# =========================================================================== #
# project_finish                                                                #
# =========================================================================== #


def test_project_finish_pushes_a_job_around_lunch():
    start = NOW.replace(hour=11, minute=30)
    lunch_start = NOW.replace(hour=12, minute=0)
    lunch_end = NOW.replace(hour=12, minute=30)
    finish = project_finish(start, 2.0, lunch_start, lunch_end)
    assert finish == NOW.replace(hour=14, minute=0)  # 2 hrs of wrench + 30 min lunch


def test_project_finish_ignores_lunch_when_the_job_does_not_span_it():
    start = NOW.replace(hour=14, minute=0)
    finish = project_finish(start, 2.0, NOW.replace(hour=12), NOW.replace(hour=12, minute=30))
    assert finish == NOW.replace(hour=16, minute=0)


# =========================================================================== #
# Reproduce-by-hand: the arithmetic must survive an audit                       #
# =========================================================================== #


def test_the_score_can_be_reproduced_by_hand():
    """A technician with a calculator must be able to rebuild the number.

    Tier B job (target = General Tech, rank 4), tech is a General Tech
    (rank 4) -> skill factor 1.0 -> 25.0 points.
    Familiarity: log1p(100)/log1p(100) = 1.0 with shop max 100 -> 25.0 points.
    Performance: eff 120/120 = 1.0, ftf 1.0 -> 0.6*1 + 0.4*1 = 1.0 -> 20.0 points.
    Availability: free now (wait factor 1.0), 8h promise on a 1h job -> margin
      factor 1.0 -> 20.0 points.
    Workload: 0 assigned + 1 est = 1.0 hrs, well under 70% of 8 -> 10.0 points.
    Total = 100.
    """
    r = ROInput(
        id="ro-hand",
        ro_number="HAND",
        concern_category="Brakes",
        tier="B",
        est_hours=1.0,
        promise_at=NOW + timedelta(hours=8),
    )
    t = TechInput(
        id="t-hand",
        name="Hand Check",
        skill_level="General Tech",
        team="Main",
        on_shift=True,
        shift_end_at=NOW.replace(hour=17),
        free_at=NOW,
        assigned_hours_today=0.0,
        max_daily_hours=8.0,
        overtime_threshold=8.0,
        category=CategoryStats(repairs_completed=100, avg_efficiency=120.0, first_time_fix=1.0),
    )
    c = score_technician(r, t, ctx(shop_max_category_repairs=100))

    points = {x.factor: x.points for x in c.reasons}
    assert points["skill"] == pytest.approx(25.0)
    assert points["familiarity"] == pytest.approx(25.0)
    assert points["performance"] == pytest.approx(20.0)
    assert points["availability"] == pytest.approx(20.0)
    assert points["workload"] == pytest.approx(10.0)
    assert c.score == 100
