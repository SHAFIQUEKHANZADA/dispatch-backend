"""Tests for the scoreboard formulas + the honesty gates.

These lock the exact arithmetic from the spec ("do not improvise the math") and
prove Rule 3: a metric with no source data comes back unavailable with a named
issue, never a fabricated number.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from app.engine.metrics import (
    HistoryRow,
    MetricsConfig,
    build_scorecard,
    comeback_rate,
    efficiency,
    first_time_fix,
    is_countable,
    productivity,
    promise_pct,
    rank_like_for_like,
    shift_capacity_hours,
    utilization,
)

UTC = timezone.utc


def row(**kw) -> HistoryRow:
    base = dict(
        id="h1", ro_number="1000", technician_id="t1",
        opened_at=datetime(2026, 7, 1, 8, tzinfo=UTC),
        closed_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        concern_category="Brakes", op_code="BRK",
        flagged_hours=2.0, actual_clocked_hours=1.6, labor_type="CP",
        promise_time=None, vin="1" * 17,
    )
    base.update(kw)
    return HistoryRow(**base)


# --------------------------- exact formulas -------------------------------- #


def test_efficiency_is_flagged_over_clocked():
    rows = [row(flagged_hours=2.0, actual_clocked_hours=1.6),
            row(ro_number="1001", flagged_hours=3.0, actual_clocked_hours=3.0)]
    mv = efficiency(rows)
    # (2.0 + 3.0) / (1.6 + 3.0) = 5.0 / 4.6 = 108.7%
    assert mv.available
    assert mv.value == pytest.approx(5.0 / 4.6 * 100.0)
    assert mv.numerator == 5.0 and mv.denominator == pytest.approx(4.6)


def test_productivity_is_gated_when_there_is_no_time_clock():
    """Rule 3 — no total-clocked denominator => unavailable, not approximated."""
    mv = productivity([row()], total_clocked_hours=None)
    assert not mv.available
    assert mv.value is None
    assert "time-clock" in mv.issue.lower()


def test_productivity_when_time_clock_present():
    mv = productivity([row(actual_clocked_hours=6.0)], total_clocked_hours=8.0)
    assert mv.value == pytest.approx(75.0)


def test_utilization_needs_capacity():
    assert not utilization([row()], capacity_hours=None).available
    mv = utilization([row(flagged_hours=4.0)], capacity_hours=8.0)
    assert mv.value == pytest.approx(50.0)


def test_promise_pct_counts_only_ros_with_a_promise():
    rows = [
        row(ro_number="a", promise_time=datetime(2026, 7, 1, 17, tzinfo=UTC),
            closed_at=datetime(2026, 7, 1, 15, tzinfo=UTC)),   # made it
        row(ro_number="b", promise_time=datetime(2026, 7, 1, 12, tzinfo=UTC),
            closed_at=datetime(2026, 7, 1, 14, tzinfo=UTC)),   # missed it
        row(ro_number="c", promise_time=None),                 # excluded
    ]
    mv = promise_pct(rows)
    assert mv.sample_size == 2
    assert mv.value == pytest.approx(50.0)


def test_comeback_and_first_time_fix_are_complementary():
    rows = [row(ro_number=str(i)) for i in range(10)]  # 10 completed ROs
    assert comeback_rate(rows, comeback_count=2).value == pytest.approx(20.0)
    assert first_time_fix(rows, comeback_count=2).value == pytest.approx(80.0)


def test_excluded_work_is_not_countable():
    assert is_countable(row()) is True
    assert is_countable(row(excluded_from_metrics=True)) is False
    assert is_countable(row(concern_category="Training")) is False
    assert is_countable(row(closed_at=None)) is False  # open at period close


# --------------------------- the gates ------------------------------------- #


def config() -> MetricsConfig:
    return MetricsConfig(min_ros_to_rank=10, min_flagged_hours_to_rank=15,
                         comeback_window_days=30, data_staleness_hours=48)


def test_minimum_volume_gate_grays_out_a_thin_sample():
    """2 ROs at 140% must not rank against 40 ROs at 112%."""
    thin = build_scorecard(
        technician_id="t1", name="Rookie", team="Main", skill_level="Apprentice 1",
        period="T90", period_start=date(2026, 4, 1), period_end=date(2026, 7, 1),
        rows=[row(ro_number="1", flagged_hours=2.0, actual_clocked_hours=1.4),
              row(ro_number="2", flagged_hours=2.0, actual_clocked_hours=1.4)],
        capacity_hours=400.0, total_clocked_hours=None, comeback_count=0,
        config=config(), source_data_age_hours=1.0,
    )
    assert thin.qualifies_for_ranking is False
    assert any("building sample" in i.lower() for i in thin.data_issues)


def test_guardian_gate_flags_stale_source_data():
    card = build_scorecard(
        technician_id="t1", name="Vet", team="Main", skill_level="Master",
        period="T90", period_start=date(2026, 4, 1), period_end=date(2026, 7, 1),
        rows=[row(ro_number=str(i)) for i in range(20)],
        capacity_hours=400.0, total_clocked_hours=None, comeback_count=1,
        config=config(), source_data_age_hours=120.0,   # 5 days — stale
    )
    assert card.data_complete is False
    assert card.qualifies_for_ranking is False  # stale data can't rank
    assert any("stale" in i.lower() for i in card.data_issues)


def test_no_history_import_is_named_not_hidden():
    card = build_scorecard(
        technician_id="t1", name="Nobody", team="Main", skill_level="Master",
        period="T90", period_start=date(2026, 4, 1), period_end=date(2026, 7, 1),
        rows=[], capacity_hours=None, total_clocked_hours=None, comeback_count=0,
        config=config(), source_data_age_hours=None,
    )
    assert card.data_complete is False
    assert any("no dms history" in i.lower() for i in card.data_issues)


def test_warranty_cp_split_is_always_visible():
    rows = [
        row(ro_number="a", flagged_hours=2.0, labor_type="CP"),
        row(ro_number="b", flagged_hours=3.0, labor_type="WARRANTY"),
        row(ro_number="c", flagged_hours=1.0, labor_type="INTERNAL"),
    ]
    card = build_scorecard(
        technician_id="t1", name="Split", team="Main", skill_level="Master",
        period="MTD", period_start=date(2026, 7, 1), period_end=date(2026, 7, 15),
        rows=rows, capacity_hours=100.0, total_clocked_hours=None, comeback_count=0,
        config=config(), source_data_age_hours=1.0,
    )
    assert card.cp_flagged_hours == 2.0
    assert card.warranty_flagged_hours == 3.0
    assert card.internal_flagged_hours == 1.0


def test_like_for_like_never_mixes_teams_or_levels():
    def card(name, team, level, eff_rows):
        return build_scorecard(
            technician_id=name, name=name, team=team, skill_level=level,
            period="T90", period_start=date(2026, 4, 1), period_end=date(2026, 7, 1),
            rows=eff_rows, capacity_hours=400.0, total_clocked_hours=None,
            comeback_count=0, config=config(), source_data_age_hours=1.0,
        )
    many = [row(ro_number=str(i)) for i in range(12)]
    cards = [
        card("Lube A", "Lube", "Apprentice 2", many),
        card("Main A", "Main", "Master", many),
        card("Main B", "Main", "Master", many),
    ]
    groups = rank_like_for_like(cards, "efficiency")
    assert "Lube|Apprentice 2" in groups
    assert "Main|Master" in groups
    assert len(groups["Main|Master"]) == 2
    assert len(groups["Lube|Apprentice 2"]) == 1


# --------------------------- capacity -------------------------------------- #


def test_shift_capacity_counts_only_work_days_and_subtracts_lunch():
    # Mon–Fri, 8h shift, 0.5h lunch => 7.5 billable hrs/day.
    cap = shift_capacity_hours(
        date(2026, 7, 6), date(2026, 7, 10),   # Mon..Fri
        time(8, 0), time(16, 0), ["Mon", "Tue", "Wed", "Thu", "Fri"],
        time(12, 0), time(12, 30),
    )
    assert cap == pytest.approx(5 * 7.5)


def test_shift_capacity_is_none_when_schedule_unknown():
    assert shift_capacity_hours(date(2026, 7, 6), date(2026, 7, 10), None, None, []) is None
