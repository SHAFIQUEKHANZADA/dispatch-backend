"""DMS CSV importer — parsing, validation, and the derived baselines (FR-2).

Pure: takes text and a column mapping, returns parsed rows plus a rejection
report.  It never writes to the database — the router does that.

"Never import silently-bad data."  Every row that does not survive validation
comes back in `rejects` with its row number, the raw values, and the specific
reason.  Nothing is coerced quietly.
"""

from __future__ import annotations

import csv
import io
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dateutil import parser as dateparser

# The canonical fields we need out of any dealership's export.  Every DMS names
# them differently, which is why FR-2.1 demands a column-mapping step.
DMS_FIELDS: dict[str, dict] = {
    "ro_number":            {"label": "RO #",                 "required": True},
    "opened_at":            {"label": "Open timestamp",       "required": True},
    "closed_at":            {"label": "Close timestamp",      "required": True},
    "dms_tech_no":          {"label": "Tech ID",              "required": True},
    "advisor_id":           {"label": "Advisor ID",           "required": False},
    "op_code":              {"label": "Labor op code",        "required": True},
    "flagged_hours":        {"label": "Flagged (sold) hrs",   "required": True},
    "actual_clocked_hours": {"label": "Actual clocked hrs",   "required": True},
    "labor_type":           {"label": "Labor type (CP/W/I)",  "required": True},
    "promise_time":         {"label": "Promise time",         "required": False},
    "vin":                  {"label": "VIN",                  "required": False},
    "vehicle_ymm":          {"label": "Vehicle year/make/model", "required": False},
}

TIME_CLOCK_FIELDS: dict[str, dict] = {
    "dms_tech_no":         {"label": "Tech ID",             "required": True},
    "work_date":           {"label": "Work date",           "required": True},
    "total_clocked_hours": {"label": "Total clocked hours", "required": True},
}

# Guesses for the column-mapping UI to pre-fill.  The user can always override.
_HEADER_HINTS: dict[str, list[str]] = {
    "ro_number":            ["ro", "ro #", "ro_number", "ronumber", "repair order", "ro no"],
    "opened_at":            ["open", "opened", "open date", "open_ts", "date opened", "openedat"],
    "closed_at":            ["close", "closed", "close date", "close_ts", "date closed", "closedat"],
    "dms_tech_no":          ["tech", "tech id", "tech no", "technician", "tech #", "techno"],
    "advisor_id":           ["advisor", "advisor id", "service advisor", "sa"],
    "op_code":              ["op", "op code", "opcode", "labor op", "labor code", "operation"],
    "flagged_hours":        ["flagged", "flag hrs", "sold hours", "sold hrs", "flagged hours", "book time"],
    "actual_clocked_hours": ["actual", "clocked", "actual hours", "clock hours", "actual hrs"],
    "labor_type":           ["labor type", "pay type", "type", "paytype", "labor_type"],
    "promise_time":         ["promise", "promise time", "promised"],
    "vin":                  ["vin"],
    "vehicle_ymm":          ["vehicle", "ymm", "year make model", "make model", "description"],
    "work_date":            ["date", "work date", "day"],
    "total_clocked_hours":  ["total", "total hours", "total clocked", "hours"],
}

LABOR_TYPE_ALIASES: dict[str, str] = {
    "cp": "CP", "c": "CP", "customer": "CP", "customer pay": "CP", "custpay": "CP", "cust": "CP",
    "w": "WARRANTY", "wty": "WARRANTY", "war": "WARRANTY", "warranty": "WARRANTY",
    "i": "INTERNAL", "int": "INTERNAL", "internal": "INTERNAL",
}


@dataclass
class ParsedRow:
    row_number: int
    ro_number: str
    opened_at: Optional[datetime]
    closed_at: Optional[datetime]
    dms_tech_no: str
    advisor_id: Optional[str]
    op_code: str
    flagged_hours: float
    actual_clocked_hours: float
    labor_type: str
    promise_time: Optional[datetime]
    vin: Optional[str]
    vehicle_ymm: Optional[str]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Reject:
    row_number: int
    reason: str
    raw: dict[str, Any]

    def to_dict(self) -> dict:
        return {"row": self.row_number, "reason": self.reason, "raw": self.raw}


@dataclass
class ParseResult:
    rows: list[ParsedRow]
    rejects: list[Reject]
    rows_total: int

    @property
    def rows_imported(self) -> int:
        return len(self.rows)


# --------------------------------------------------------------------------- #
# Header sniffing + mapping suggestion                                         #
# --------------------------------------------------------------------------- #


def sniff_csv(text: str, sample_rows: int = 5) -> dict:
    """Read the header + a few rows so the mapping UI has something to show."""
    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        return {"headers": [], "sample": [], "suggested_mapping": {}}
    headers = [h.strip() for h in headers]
    sample = []
    for i, row in enumerate(reader):
        if i >= sample_rows:
            break
        sample.append(row)
    return {
        "headers": headers,
        "sample": sample,
        "suggested_mapping": suggest_mapping(headers),
    }


def suggest_mapping(headers: list[str], fields: Optional[dict] = None) -> dict[str, Optional[str]]:
    fields = fields or DMS_FIELDS
    lowered = {h: h.strip().lower().replace("_", " ") for h in headers}
    out: dict[str, Optional[str]] = {}
    for field_key in fields:
        hints = _HEADER_HINTS.get(field_key, [])
        match = None
        # Exact hint match beats a substring match.
        for h, low in lowered.items():
            if low in hints:
                match = h
                break
        if match is None:
            for h, low in lowered.items():
                if any(hint in low for hint in hints):
                    match = h
                    break
        out[field_key] = match
    return out


# --------------------------------------------------------------------------- #
# Coercion — strict, and loud when it fails                                    #
# --------------------------------------------------------------------------- #


def _parse_dt(value: str, field_name: str) -> datetime:
    if value is None or not str(value).strip():
        raise ValueError(f"{field_name} is empty")
    try:
        dt = dateparser.parse(str(value).strip())
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} '{value}' is not a date/time we can read") from exc
    if dt is None:
        raise ValueError(f"{field_name} '{value}' is not a date/time we can read")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_hours(value: str, field_name: str) -> float:
    if value is None or not str(value).strip():
        raise ValueError(f"{field_name} is empty")
    cleaned = str(value).strip().replace(",", "").replace("hrs", "").replace("hr", "").strip()
    try:
        hours = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"{field_name} '{value}' is not a number") from exc
    if math.isnan(hours) or math.isinf(hours):
        raise ValueError(f"{field_name} '{value}' is not a finite number")
    if hours < 0:
        raise ValueError(f"{field_name} is negative ({hours})")
    if hours > 100:
        raise ValueError(f"{field_name} of {hours} hrs on a single line is implausible")
    return hours


def _parse_labor_type(value: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError("Labor type is empty")
    key = str(value).strip().lower()
    if key in LABOR_TYPE_ALIASES:
        return LABOR_TYPE_ALIASES[key]
    upper = str(value).strip().upper()
    if upper in ("CP", "WARRANTY", "INTERNAL"):
        return upper
    raise ValueError(f"Labor type '{value}' is not one of CP / WARRANTY / INTERNAL")


# --------------------------------------------------------------------------- #
# The parse                                                                    #
# --------------------------------------------------------------------------- #


def parse_dms_csv(text: str, mapping: dict[str, Optional[str]]) -> ParseResult:
    """Validate every row.  Reject loudly; never coerce silently."""
    missing_required = [
        spec["label"]
        for key, spec in DMS_FIELDS.items()
        if spec["required"] and not mapping.get(key)
    ]
    if missing_required:
        raise ValueError(
            "These required columns are not mapped: " + ", ".join(missing_required)
        )

    reader = csv.DictReader(io.StringIO(text))
    rows: list[ParsedRow] = []
    rejects: list[Reject] = []
    total = 0
    seen: set[tuple[str, str, str]] = set()

    for i, raw in enumerate(reader, start=2):  # row 1 is the header
        total += 1
        raw = {(k.strip() if k else k): v for k, v in raw.items()}

        def col(key: str) -> Any:
            src = mapping.get(key)
            return raw.get(src) if src else None

        try:
            ro_number = str(col("ro_number") or "").strip()
            if not ro_number:
                raise ValueError("RO # is empty")

            dms_tech_no = str(col("dms_tech_no") or "").strip()
            if not dms_tech_no:
                raise ValueError("Tech ID is empty")

            op_code = str(col("op_code") or "").strip()
            if not op_code:
                raise ValueError("Labor op code is empty")

            opened_at = _parse_dt(col("opened_at"), "Open timestamp")
            closed_at = _parse_dt(col("closed_at"), "Close timestamp")
            if closed_at < opened_at:
                raise ValueError(
                    f"Close timestamp ({closed_at:%Y-%m-%d %H:%M}) is before the open "
                    f"timestamp ({opened_at:%Y-%m-%d %H:%M})"
                )

            flagged = _parse_hours(col("flagged_hours"), "Flagged hours")
            clocked = _parse_hours(col("actual_clocked_hours"), "Actual clocked hours")
            labor_type = _parse_labor_type(col("labor_type"))

            promise_raw = col("promise_time")
            promise_time = (
                _parse_dt(promise_raw, "Promise time")
                if promise_raw and str(promise_raw).strip()
                else None
            )

            vin = (str(col("vin")).strip().upper() or None) if col("vin") else None
            if vin and len(vin) not in (0, 17):
                # Not fatal — plenty of exports carry a partial VIN — but comeback
                # detection keys on it, so the user needs to know.
                raise ValueError(f"VIN '{vin}' is {len(vin)} characters (expected 17)")

            ymm = (str(col("vehicle_ymm")).strip() or None) if col("vehicle_ymm") else None
            advisor = (str(col("advisor_id")).strip() or None) if col("advisor_id") else None

            dedupe_key = (ro_number, op_code, dms_tech_no)
            if dedupe_key in seen:
                raise ValueError(
                    f"Duplicate line: RO {ro_number} / op {op_code} / tech {dms_tech_no} "
                    f"already appears earlier in this file"
                )
            seen.add(dedupe_key)

            rows.append(
                ParsedRow(
                    row_number=i,
                    ro_number=ro_number,
                    opened_at=opened_at,
                    closed_at=closed_at,
                    dms_tech_no=dms_tech_no,
                    advisor_id=advisor,
                    op_code=op_code,
                    flagged_hours=flagged,
                    actual_clocked_hours=clocked,
                    labor_type=labor_type,
                    promise_time=promise_time,
                    vin=vin,
                    vehicle_ymm=ymm,
                    raw=dict(raw),
                )
            )
        except ValueError as exc:
            rejects.append(Reject(i, str(exc), dict(raw)))

    return ParseResult(rows=rows, rejects=rejects, rows_total=total)


# --------------------------------------------------------------------------- #
# Derived: familiarity map + comeback pairs                                    #
# --------------------------------------------------------------------------- #


@dataclass
class FamilarityRow:
    dms_tech_no: str
    concern_category: str
    repairs_completed: int
    flagged_hours: float
    clocked_hours: float
    avg_efficiency: Optional[float]
    first_time_fix: Optional[float]
    last_performed_at: Optional[datetime]


@dataclass
class ComebackPair:
    vin: str
    concern_category: str
    original_ro_number: str
    original_closed_at: datetime
    original_dms_tech_no: str
    repeat_ro_number: str
    repeat_opened_at: datetime
    days_between: float


def find_comeback_pairs(
    rows: list[ParsedRow],
    categories: dict[str, str],           # op_code -> concern_category
    window_days: int = 30,
) -> list[ComebackPair]:
    """Same VIN + same concern category within the window = a comeback.

    The pair is attributed to the technician who did the ORIGINAL repair — they
    are the one whose first-time-fix it dents.
    """
    by_key: dict[tuple[str, str], list[ParsedRow]] = defaultdict(list)
    for r in rows:
        if not r.vin or not r.closed_at:
            continue
        category = categories.get(r.op_code)
        if not category:
            continue
        by_key[(r.vin, category)].append(r)

    pairs: list[ComebackPair] = []
    for (vin, category), group in by_key.items():
        # One visit can carry several lines; collapse to distinct ROs first.
        visits: dict[str, ParsedRow] = {}
        for r in group:
            existing = visits.get(r.ro_number)
            if existing is None or (r.closed_at and existing.closed_at and r.closed_at < existing.closed_at):
                visits[r.ro_number] = r
        ordered = sorted(visits.values(), key=lambda r: (r.closed_at, r.ro_number))

        for i in range(len(ordered) - 1):
            first, second = ordered[i], ordered[i + 1]
            if not second.opened_at or not first.closed_at:
                continue
            delta = (second.opened_at - first.closed_at).total_seconds() / 86400.0
            if 0 <= delta <= window_days:
                pairs.append(
                    ComebackPair(
                        vin=vin,
                        concern_category=category,
                        original_ro_number=first.ro_number,
                        original_closed_at=first.closed_at,
                        original_dms_tech_no=first.dms_tech_no,
                        repeat_ro_number=second.ro_number,
                        repeat_opened_at=second.opened_at,
                        days_between=round(delta, 2),
                    )
                )

    pairs.sort(key=lambda p: (p.vin, p.concern_category, p.original_ro_number))
    return pairs


def build_familiarity(
    rows: list[ParsedRow],
    categories: dict[str, str],
    comebacks: list[ComebackPair],
    excluded_op_codes: Optional[set[str]] = None,
) -> list[FamilarityRow]:
    """The per-technician, per-category capability map — the Match Score's fuel."""
    excluded = excluded_op_codes or set()

    agg: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"ros": set(), "flagged": 0.0, "clocked": 0.0, "last": None}
    )
    for r in rows:
        if r.op_code in excluded:
            continue
        category = categories.get(r.op_code)
        if not category:
            continue
        bucket = agg[(r.dms_tech_no, category)]
        bucket["ros"].add(r.ro_number)
        bucket["flagged"] += r.flagged_hours
        bucket["clocked"] += r.actual_clocked_hours
        if r.closed_at and (bucket["last"] is None or r.closed_at > bucket["last"]):
            bucket["last"] = r.closed_at

    # Comebacks that dent this tech's first-time-fix, per category.
    cb_counts: dict[tuple[str, str], int] = defaultdict(int)
    for p in comebacks:
        cb_counts[(p.original_dms_tech_no, p.concern_category)] += 1

    out: list[FamilarityRow] = []
    for (tech_no, category), bucket in sorted(agg.items()):
        ro_count = len(bucket["ros"])
        clocked = bucket["clocked"]
        flagged = bucket["flagged"]
        eff = (flagged / clocked * 100.0) if clocked > 0 else None
        cb = cb_counts.get((tech_no, category), 0)
        ftf = (1.0 - cb / ro_count) if ro_count > 0 else None
        out.append(
            FamilarityRow(
                dms_tech_no=tech_no,
                concern_category=category,
                repairs_completed=ro_count,
                flagged_hours=round(flagged, 2),
                clocked_hours=round(clocked, 2),
                avg_efficiency=round(eff, 1) if eff is not None else None,
                first_time_fix=round(ftf, 4) if ftf is not None else None,
                last_performed_at=bucket["last"],
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Time-clock export                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class ClockRow:
    row_number: int
    dms_tech_no: str
    work_date: datetime
    total_clocked_hours: float


@dataclass
class ClockParseResult:
    rows: list[ClockRow]
    rejects: list[Reject]
    rows_total: int


def parse_time_clock_csv(text: str, mapping: dict[str, Optional[str]]) -> ClockParseResult:
    missing = [
        spec["label"]
        for key, spec in TIME_CLOCK_FIELDS.items()
        if spec["required"] and not mapping.get(key)
    ]
    if missing:
        raise ValueError("These required columns are not mapped: " + ", ".join(missing))

    reader = csv.DictReader(io.StringIO(text))
    rows: list[ClockRow] = []
    rejects: list[Reject] = []
    total = 0

    for i, raw in enumerate(reader, start=2):
        total += 1
        raw = {(k.strip() if k else k): v for k, v in raw.items()}

        def col(key: str) -> Any:
            src = mapping.get(key)
            return raw.get(src) if src else None

        try:
            tech_no = str(col("dms_tech_no") or "").strip()
            if not tech_no:
                raise ValueError("Tech ID is empty")
            work_date = _parse_dt(col("work_date"), "Work date")
            hours = _parse_hours(col("total_clocked_hours"), "Total clocked hours")
            if hours > 24:
                raise ValueError(f"Total clocked hours of {hours} exceeds 24 in a single day")
            rows.append(ClockRow(i, tech_no, work_date, hours))
        except ValueError as exc:
            rejects.append(Reject(i, str(exc), dict(raw)))

    return ClockParseResult(rows=rows, rejects=rejects, rows_total=total)
