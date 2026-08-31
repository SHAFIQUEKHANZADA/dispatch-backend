"""Pure mapping: myKaarma Order v2 JSON -> 3D Dispatch repair_orders / ro_lines.

Pure functions only — no DB, no HTTP. Same discipline as the scoring engine, so
the mapping is unit-testable against captured payloads and a field change shows
up as a failing test rather than silently-wrong ROs on the board.

Field contract per the v2 Integration Data Contract:
    header.status / dmsStatus          -> status bucket
    header.promisedDate + promisedTime -> promise_at
    header.waiter                      -> WAITING flag
    header.soldHours / actualHours     -> hours
    header.createDate/Time             -> written_at
    header.mileageIn/Out               -> mileage
    jobs[].laborOpCode / laborType     -> ro_lines + concern category lookup
    jobs[].techNo                      -> current DMS assignment (drift detection)
    jobs[].parts[]                     -> waiting-on-parts detection
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from dateutil import parser as dateparser

# myKaarma / DMS status strings -> our board buckets. Anything unrecognised
# falls to OPEN rather than being guessed into a dispatchable state.
STATUS_MAP: dict[str, str] = {
    # ready to work
    "READY": "READY_TO_DISPATCH",
    "READY_TO_DISPATCH": "READY_TO_DISPATCH",
    "DISPATCH": "READY_TO_DISPATCH",
    "OPEN": "OPEN",
    "NEW": "OPEN",
    "CREATED": "OPEN",
    # blocked
    "PENDING_AUTHORIZATION": "PENDING_AUTHORIZATION",
    "PENDING_AUTH": "PENDING_AUTHORIZATION",
    "AWAITING_APPROVAL": "PENDING_AUTHORIZATION",
    "APPROVAL": "PENDING_AUTHORIZATION",
    "WAITING_ON_PARTS": "WAITING_ON_PARTS",
    "WAITING_PARTS": "WAITING_ON_PARTS",
    "PARTS": "WAITING_ON_PARTS",
    # working / done
    "IN_PROGRESS": "IN_PROGRESS",
    "WIP": "IN_PROGRESS",
    "WORKING": "IN_PROGRESS",
    "CLOSED": "COMPLETED",
    "COMPLETE": "COMPLETED",
    "COMPLETED": "COMPLETED",
    "FINALIZED": "COMPLETED",
}


@dataclass
class MappedLine:
    op_code: Optional[str]
    description: str
    flagged_hours: float               # soldHours = booked/claimed labor
    labor_type: Optional[str]          # CP | WARRANTY | INTERNAL
    tech_no: Optional[str]             # DMS tech on this line (drift detection)
    dispatch_line_status: Optional[str]
    waiting_on_parts: bool
    actual_hours: float = 0.0          # actualHours = punch/clock time on the job
    parts_count: int = 0               # how many parts are documented on the line


@dataclass
class MappedRO:
    ro_number: str
    order_uuid: Optional[str]
    status: str
    vin: Optional[str]
    vehicle_year: Optional[int]
    vehicle_make: Optional[str]
    vehicle_model: Optional[str]
    mileage: Optional[int]
    est_hours: float
    written_at: Optional[datetime]
    promise_at: Optional[datetime]
    flags: list[str]
    advisor_id: Optional[str]
    appointment_number: Optional[str]
    customer_uuid: Optional[str]
    vehicle_uuid: Optional[str]
    read_checksum: Optional[str]
    lines: list[MappedLine] = field(default_factory=list)
    # every DMS tech number seen on the lines — used to detect assignment drift
    dms_tech_nos: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _num(v: Any, default: float = 0.0) -> float:
    """Parse a number, rounded to 2dp.

    Labor hours are quoted to a tenth in the shop; raw float division leaves
    noise like 1.1999999999999999 which would render on the dispatch board as-is.
    Round at the boundary so nothing downstream inherits the fuzz.
    """
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return default


def _int(v: Any) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> Optional[datetime]:
    """Parse a myKaarma date/datetime into aware UTC. None if unparseable —
    never guess a time; a wrong promise time is worse than a missing one."""
    if not value:
        return None
    try:
        dt = dateparser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _combine_date_time(date_val: Any, time_val: Any) -> Optional[datetime]:
    """header.promisedDate + header.promisedTime arrive as separate fields."""
    if not date_val:
        return None
    if not time_val:
        return _dt(date_val)
    combined = f"{str(date_val).strip()} {str(time_val).strip()}"
    return _dt(combined) or _dt(date_val)


def map_status(header: dict) -> str:
    """Prefer the DMS status; fall back to the myKaarma status. Unknown -> OPEN."""
    for key in ("dmsStatus", "status"):
        raw = header.get(key)
        if raw:
            token = str(raw).strip().upper().replace(" ", "_").replace("-", "_")
            if token in STATUS_MAP:
                return STATUS_MAP[token]
    return "OPEN"


def map_labor_type(raw: Any) -> Optional[str]:
    if not raw:
        return None
    t = str(raw).strip().upper()
    if t.startswith("C"):
        return "CP"
    if t.startswith("W"):
        return "WARRANTY"
    if t.startswith("I"):
        return "INTERNAL"
    return None


# Op-codes / descriptions that are consent, notification, or signature boilerplate
# rather than actual work — hidden from the RO card.
_BOILERPLATE_OPCODES = {"TCPA", "2WAYSPAY", "CONSENT", "ESIGN", "DISCLAIMER"}
_BOILERPLATE_PHRASES = (
    "i consent", "consent to being contacted", "2 ways to pay", "client signature",
    "signature x", "processing fee", "not a required condition",
)


def _is_boilerplate_line(op_code: Any, desc: Any) -> bool:
    oc = str(op_code or "").strip().upper()
    if oc in _BOILERPLATE_OPCODES:
        return True
    d = str(desc or "").lower()
    return any(p in d for p in _BOILERPLATE_PHRASES)


def line_waiting_on_parts(job: dict) -> bool:
    """A line is parts-blocked when any part has been ordered but not sold/filled."""
    for p in _parts_list(job):
        ordered = _num(p.get("quantityOrdered"))
        sold = _num(p.get("quantitySold"))
        if ordered > sold:
            return True
    return False


def _parts_list(job: dict) -> list[dict]:
    """myKaarma's `parts` can be a list, or the string 'None' when there are none."""
    p = job.get("parts")
    return p if isinstance(p, list) else []


def _count_parts(job: dict) -> int:
    """How many real parts are documented on the job (a part number or a quantity)."""
    n = 0
    for p in _parts_list(job):
        if str(p.get("partNumber") or "").strip() or _num(p.get("quantityOrdered")) or _num(p.get("quantitySold")):
            n += 1
    return n


# --------------------------------------------------------------------------- #
# the mapping                                                                  #
# --------------------------------------------------------------------------- #


def map_order(payload: dict) -> MappedRO:
    """Map one Order v2 `global_order` response into our RO shape."""
    order = payload.get("order") or payload.get("globalOrder") or payload
    # The Order v2 read nests the body one level deeper: {uuid, order:{header,...}}.
    # Unwrap to the real order, keeping the outer order UUID for write-back.
    _outer_uuid = order.get("uuid") if isinstance(order, dict) else None
    if isinstance(order, dict) and isinstance(order.get("order"), dict) and "header" in order["order"]:
        order = order["order"]
    header = order.get("header") or {}
    customer = order.get("customer") or {}
    vehicle = order.get("vehicle") or {}
    jobs = order.get("jobs") or []

    lines: list[MappedLine] = []
    tech_nos: list[str] = []
    any_parts_block = False

    for job in jobs:
        # Skip consent/notification boilerplate lines (TCPA, payment notices,
        # signature blocks) — they're legal text, not work, and clutter the RO card.
        if _is_boilerplate_line(job.get("laborOpCode"), job.get("laborOpCodeDesc") or job.get("description")):
            continue
        blocked = line_waiting_on_parts(job)
        any_parts_block = any_parts_block or blocked
        # The technician lives in techHours[].techNo. myKaarma's top-level `techNos`
        # is usually blank, so read the per-tech breakdown first, then fall back.
        tech_hours = job.get("techHours") or []
        line_techs = [str(th.get("techNo")).strip() for th in tech_hours if th.get("techNo")]
        if not line_techs:
            raw_tech = job.get("techNos") or job.get("techNo") or job.get("technicianNumber") or ""
            line_techs = [tn.strip() for tn in str(raw_tech).replace("/", ",").split(",") if tn.strip()]
        tech_nos.extend(line_techs)
        # Punch time: prefer the job-level actualHours, else sum the per-tech punches.
        actual = _num(job.get("actualHours"))
        if actual == 0 and tech_hours:
            actual = sum(_num(th.get("actualHours")) for th in tech_hours)
        lines.append(
            MappedLine(
                op_code=(str(job.get("laborOpCode")).strip() if job.get("laborOpCode") else None),
                description=(
                    job.get("laborOpCodeDesc")
                    or job.get("description")
                    or job.get("laborOpCode")
                    or "Labor line"
                ),
                flagged_hours=_num(job.get("soldHours")),
                labor_type=map_labor_type(job.get("laborType")),
                tech_no=(line_techs[0] if line_techs else None),
                dispatch_line_status=job.get("dispatchLineStatus"),
                waiting_on_parts=blocked,
                actual_hours=actual,
                parts_count=_count_parts(job),
            )
        )

    status = map_status(header)
    # A parts-blocked line overrides a "ready" status — you cannot dispatch work
    # whose parts have not arrived, whatever the DMS header says.
    if any_parts_block and status in ("READY_TO_DISPATCH", "OPEN"):
        status = "WAITING_ON_PARTS"
    # myKaarma marks every active RO simply "open" (status O), with no separate
    # "ready to dispatch" flag. Derive the dispatch lifecycle from the signals we
    # DO have: a tech already on a line -> in progress; otherwise, parts are in and
    # no one is on it yet -> ready for a tech to be assigned.
    if status == "OPEN":
        status = "IN_PROGRESS" if tech_nos else "READY_TO_DISPATCH"

    flags: list[str] = []
    if header.get("waiter"):
        flags.append("WAITING")

    est = _num(header.get("soldHours"))
    if est <= 0:
        est = round(sum(l.flagged_hours for l in lines), 2)

    return MappedRO(
        ro_number=str(
            header.get("roNumber") or header.get("orderNumber") or header.get("number") or ""
        ).strip(),
        order_uuid=_outer_uuid or order.get("uuid") or header.get("orderUuid"),
        status=status,
        vin=(str(vehicle.get("vin")).strip().upper() if vehicle.get("vin") else None),
        vehicle_year=_int(vehicle.get("year") or vehicle.get("vehicleYear")),
        vehicle_make=vehicle.get("make") or vehicle.get("vehicleMake"),
        vehicle_model=vehicle.get("model") or vehicle.get("vehicleModel"),
        mileage=_int(header.get("mileageIn") or header.get("mileageOut")),
        est_hours=est,
        written_at=_combine_date_time(
            header.get("orderDate") or header.get("createDate"),
            header.get("orderTime") or header.get("createTime"),
        ),
        promise_at=_combine_date_time(header.get("promisedDate"), header.get("promisedTime")),
        flags=flags,
        advisor_id=(
            str(header.get("advisorNumber")).strip() if header.get("advisorNumber") else None
        ),
        appointment_number=(
            str(header.get("appointmentNumber")).strip()
            if header.get("appointmentNumber")
            else None
        ),
        customer_uuid=customer.get("uuid"),
        vehicle_uuid=vehicle.get("uuid"),
        # Required header for any future write-back call.
        read_checksum=payload.get("orderReadChecksum") or order.get("orderReadChecksum"),
        lines=lines,
        dms_tech_nos=sorted(set(tech_nos)),
    )
