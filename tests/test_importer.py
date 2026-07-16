"""Tests for the DMS importer: strict validation, and the derived baselines.

The importer's promise is "never import silently-bad data" — so the tests here
are mostly about what gets REJECTED, and why the rejection message is specific.
"""

from __future__ import annotations

from app.engine.importer import (
    build_familiarity,
    find_comeback_pairs,
    parse_dms_csv,
    suggest_mapping,
)

GOOD_CSV = """RO,Open,Close,Tech,Advisor,OpCode,Flagged,Actual,Type,Promise,VIN,Vehicle
5001,2026-06-01 08:00,2026-06-01 10:00,T1,A1,ACRCHG,2.0,1.7,CP,2026-06-01 16:00,11111111111111111,2019 Honda Odyssey
5002,2026-06-02 09:00,2026-06-02 11:30,T1,A1,ACRCHG,2.5,2.0,W,,22222222222222222,2020 Honda CR-V
"""

MAPPING = {
    "ro_number": "RO", "opened_at": "Open", "closed_at": "Close",
    "dms_tech_no": "Tech", "advisor_id": "Advisor", "op_code": "OpCode",
    "flagged_hours": "Flagged", "actual_clocked_hours": "Actual",
    "labor_type": "Type", "promise_time": "Promise", "vin": "VIN",
    "vehicle_ymm": "Vehicle",
}


def test_suggest_mapping_matches_common_headers():
    m = suggest_mapping(["RO #", "Open Date", "Close Date", "Tech ID", "Op Code",
                         "Flagged Hrs", "Actual Hrs", "Pay Type"])
    assert m["ro_number"] == "RO #"
    assert m["closed_at"] == "Close Date"
    assert m["labor_type"] == "Pay Type"


def test_clean_rows_parse_and_labor_types_normalise():
    res = parse_dms_csv(GOOD_CSV, MAPPING)
    assert res.rows_imported == 2
    assert not res.rejects
    assert res.rows[0].labor_type == "CP"
    assert res.rows[1].labor_type == "WARRANTY"   # "W" normalised


def test_missing_required_mapping_raises():
    bad = dict(MAPPING)
    del bad["flagged_hours"]
    try:
        parse_dms_csv(GOOD_CSV, bad)
        assert False, "should have raised"
    except ValueError as exc:
        assert "Flagged" in str(exc)


def test_bad_rows_are_rejected_with_specific_reasons():
    csv = """RO,Open,Close,Tech,Advisor,OpCode,Flagged,Actual,Type,Promise,VIN,Vehicle
6001,2026-06-01 08:00,2026-06-01 07:00,T1,A1,LOF,0.5,0.4,CP,,111,x
6002,not-a-date,2026-06-01 10:00,T1,A1,LOF,0.5,0.4,CP,,111,x
6003,2026-06-01 08:00,2026-06-01 10:00,T1,A1,LOF,abc,0.4,CP,,111,x
6004,2026-06-01 08:00,2026-06-01 10:00,T1,A1,LOF,0.5,0.4,BOGUS,,111,x
6005,2026-06-01 08:00,2026-06-01 10:00,,A1,LOF,0.5,0.4,CP,,111,x
"""
    res = parse_dms_csv(csv, MAPPING)
    assert res.rows_imported == 0
    reasons = " || ".join(r.reason for r in res.rejects)
    assert "before the open" in reasons          # 6001 close < open
    assert "date/time" in reasons                 # 6002 bad date
    assert "not a number" in reasons              # 6003 bad hours
    assert "CP / WARRANTY / INTERNAL" in reasons  # 6004 bad labor type
    assert "Tech ID is empty" in reasons          # 6005 no tech


def test_duplicate_lines_are_rejected():
    csv = """RO,Open,Close,Tech,Advisor,OpCode,Flagged,Actual,Type,Promise,VIN,Vehicle
7001,2026-06-01 08:00,2026-06-01 10:00,T1,A1,LOF,0.5,0.4,CP,,11111111111111111,x
7001,2026-06-01 08:00,2026-06-01 10:00,T1,A1,LOF,0.5,0.4,CP,,11111111111111111,x
"""
    res = parse_dms_csv(csv, MAPPING)
    assert res.rows_imported == 1
    assert any("Duplicate" in r.reason for r in res.rejects)


def test_comeback_pairs_detect_same_vin_same_category_in_window():
    csv = """RO,Open,Close,Tech,Advisor,OpCode,Flagged,Actual,Type,Promise,VIN,Vehicle
8001,2026-06-01 08:00,2026-06-01 10:00,T1,A1,BRKFRT,2.0,1.8,CP,,VIN_A,x
8002,2026-06-10 08:00,2026-06-10 09:00,T2,A1,BRKFRT,1.0,1.0,CP,,VIN_A,x
8003,2026-06-01 08:00,2026-06-01 10:00,T1,A1,BRKFRT,2.0,1.8,CP,,VIN_B,x
"""
    m = dict(MAPPING); m["vin"] = "VIN"  # header is VIN; values VIN_A etc.
    # loosen VIN length check by not mapping? VINs here are 5 chars -> rejected.
    # Use the raw parser path but with short vins accepted: they are not 17,
    # which the importer flags. So craft 17-char vins instead.
    csv = csv.replace("VIN_A", "AAAAAAAAAAAAAAAAA").replace("VIN_B", "BBBBBBBBBBBBBBBBB")
    res = parse_dms_csv(csv, MAPPING)
    assert res.rows_imported == 3
    cats = {"BRKFRT": "Brakes"}
    pairs = find_comeback_pairs(res.rows, cats, window_days=30)
    assert len(pairs) == 1
    assert pairs[0].original_ro_number == "8001"       # attributed to the first
    assert pairs[0].original_dms_tech_no == "T1"
    # measured close(Jun 1 10:00) -> reopen(Jun 10 08:00) = 8.92 days, inside window
    assert pairs[0].days_between == 8.92


def test_familiarity_counts_repairs_and_computes_in_category_efficiency():
    csv = """RO,Open,Close,Tech,Advisor,OpCode,Flagged,Actual,Type,Promise,VIN,Vehicle
9001,2026-06-01 08:00,2026-06-01 10:00,T1,A1,ACRCHG,2.0,1.6,CP,,11111111111111111,x
9002,2026-06-02 08:00,2026-06-02 10:00,T1,A1,ACRCHG,2.0,1.6,CP,,22222222222222222,x
9003,2026-06-03 08:00,2026-06-03 10:00,T1,A1,LOF,0.5,0.5,CP,,33333333333333333,x
"""
    res = parse_dms_csv(csv, MAPPING)
    cats = {"ACRCHG": "Electrical/AC", "LOF": "Maintenance"}
    fam = build_familiarity(res.rows, cats, comebacks=[], excluded_op_codes=set())
    ac = next(f for f in fam if f.concern_category == "Electrical/AC")
    assert ac.repairs_completed == 2
    # (2.0+2.0) / (1.6+1.6) = 125%
    assert ac.avg_efficiency == 125.0
