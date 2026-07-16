"""Seed a demo dealership so the board can be demoed immediately.

    python seed.py

Creates McGrath Honda of St. Charles with 6 technicians, ~90 days of closed RO
history (which is what produces the familiarity map, the efficiency baselines
and the comeback pairs), and a live board of ROs waiting to be dispatched —
including the RO #4460 Odyssey A/C job from the concept document.

The history is generated from a FIXED SEED, so the demo numbers are the same on
every machine.  A demo where the Match Score changes between runs would
contradict the entire product.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import delete, select
from zoneinfo import ZoneInfo

from app.db import Base, SessionLocal, engine
from app.engine.importer import build_familiarity, find_comeback_pairs
from app.engine.importer import ParsedRow
from app.models import (
    Assignment,
    AuditLog,
    ComebackPairRow,
    Dealer,
    DealerSettings,
    ImportRun,
    OpCodeMap,
    RepairOrder,
    ROHistory,
    ROLine,
    TechCategoryFamiliarity,
    Technician,
    TechnicianCert,
    TechnicianRestriction,
    TechnicianSpecialty,
    TimeClockDay,
    UserProfile,
)

RNG = random.Random(20260715)  # fixed: the demo must be reproducible
TZ = ZoneInfo("America/Chicago")

# --------------------------------------------------------------------------- #
# Op codes -> concern category / tier / work type                              #
# --------------------------------------------------------------------------- #

OP_CODES = [
    # op_code, category,            work_type,     tier, excluded
    ("ACDIAG", "Electrical/AC",     "HVAC",        "B",  False),
    ("ACRCHG", "Electrical/AC",     "HVAC",        "B",  False),
    ("ELDIAG", "Electrical/AC",     "ELECTRICAL",  "A",  False),
    ("EVBATT", "EV/Hybrid",         "EV",          "A",  False),
    ("HYBSVC", "EV/Hybrid",         "EV",          "A",  False),
    ("DRDIAG", "Drivability/Diag",  "DIAGNOSTIC",  "A",  False),
    ("ENGREP", "Engine",            "ENGINE",      "A",  False),
    ("TRNSVC", "Transmission",      "TRANSMISSION","A",  False),
    ("BRKFRT", "Brakes",            "BRAKES",      "B",  False),
    ("BRKRR",  "Brakes",            "BRAKES",      "B",  False),
    ("SUSALN", "Suspension",        "ALIGNMENT",   "B",  False),
    ("LOF",    "Maintenance",       "LUBE",        "C",  False),
    ("TIREROT","Maintenance",       "MAINTENANCE", "C",  False),
    ("MULTIPT","Maintenance",       "MAINTENANCE", "C",  False),
    # Excluded work — training, shop time, policy.  Never enters the math.
    ("TRAIN",  "Training",          None,          None, True),
    ("SHOPTM", "Shop Time",         None,          None, True),
    ("POLADJ", "Policy",            None,          None, True),
]

# --------------------------------------------------------------------------- #
# The bench                                                                    #
# --------------------------------------------------------------------------- #

TECHS = [
    {
        "name": "Mike Hernandez", "dms_tech_no": "T101", "employee_id": "E-1041",
        "team": "Main", "skill_level": "Master",
        "certs": [("HV_EV", "Level 3"), ("ASE", "A6 Electrical"), ("HVAC", None),
                  ("OEM_HONDA_PACT", "PACT 4"), ("DIAGNOSTIC", None)],
        "restrictions": [],
        "specialties": [("HVAC", None), ("ELECTRICAL", "Odyssey")],
        "shift": (time(7, 0), time(16, 0)), "lunch": (time(12, 0), time(12, 30)),
        "max_daily_hours": 9.0, "overtime_threshold": 8.5,
        "efficiency_target": 115, "productivity_target": 90,
        # how this tech performs, per category, in the generated history
        "profile": {"Electrical/AC": 1.18, "EV/Hybrid": 1.12, "Drivability/Diag": 1.05,
                    "Brakes": 1.02, "Maintenance": 0.95},
        "volume": {"Electrical/AC": 0.42, "EV/Hybrid": 0.14, "Drivability/Diag": 0.22,
                   "Brakes": 0.14, "Maintenance": 0.08},
        "comeback_rate": 0.03,
    },
    {
        "name": "Emily Chen", "dms_tech_no": "T102", "employee_id": "E-1052",
        "team": "Main", "skill_level": "Diagnostic Tech",
        "certs": [("ASE", "A8 Engine Perf"), ("DIAGNOSTIC", None), ("OEM_HONDA_PACT", "PACT 3")],
        "restrictions": [],
        "specialties": [("DIAGNOSTIC", None)],
        "shift": (time(7, 0), time(16, 0)), "lunch": (time(12, 0), time(12, 30)),
        "max_daily_hours": 8.0, "overtime_threshold": 8.0,
        "efficiency_target": 110, "productivity_target": 88,
        "profile": {"Drivability/Diag": 1.22, "Electrical/AC": 1.04, "Engine": 1.08,
                    "Brakes": 1.0, "Maintenance": 0.92},
        "volume": {"Drivability/Diag": 0.45, "Electrical/AC": 0.18, "Engine": 0.19,
                   "Brakes": 0.12, "Maintenance": 0.06},
        "comeback_rate": 0.05,
    },
    {
        "name": "Dave Kowalski", "dms_tech_no": "T103", "employee_id": "E-0987",
        "team": "Main", "skill_level": "Sr. Master",
        "certs": [("ASE", "Master"), ("TRANSMISSION", None), ("OEM_HONDA_PACT", "PACT 4"),
                  ("DIAGNOSTIC", None), ("HVAC", None)],
        "restrictions": [],
        "specialties": [("TRANSMISSION", None), ("ENGINE", None)],
        "shift": (time(8, 0), time(17, 0)), "lunch": (time(12, 30), time(13, 0)),
        "max_daily_hours": 9.0, "overtime_threshold": 8.5,
        "efficiency_target": 120, "productivity_target": 92,
        "profile": {"Transmission": 1.24, "Engine": 1.19, "Drivability/Diag": 1.1,
                    "Electrical/AC": 1.0, "Brakes": 1.05},
        "volume": {"Transmission": 0.34, "Engine": 0.3, "Drivability/Diag": 0.2,
                   "Electrical/AC": 0.08, "Brakes": 0.08},
        "comeback_rate": 0.02,
    },
    {
        "name": "Tara Nguyen", "dms_tech_no": "T104", "employee_id": "E-1120",
        "team": "Main", "skill_level": "General Tech",
        "certs": [("ASE", "A5 Brakes"), ("ALIGNMENT", None)],
        "restrictions": ["ENGINE", "TRANSMISSION"],  # hard block — used by the engine
        "specialties": [("BRAKES", None), ("ALIGNMENT", None)],
        "shift": (time(7, 0), time(16, 0)), "lunch": (time(12, 0), time(12, 30)),
        "max_daily_hours": 8.0, "overtime_threshold": 8.0,
        "efficiency_target": 105, "productivity_target": 85,
        "profile": {"Brakes": 1.16, "Suspension": 1.12, "Maintenance": 1.05,
                    "Electrical/AC": 0.88},
        "volume": {"Brakes": 0.44, "Suspension": 0.3, "Maintenance": 0.18,
                   "Electrical/AC": 0.08},
        "comeback_rate": 0.06,
    },
    {
        "name": "Carlos Rivera", "dms_tech_no": "T105", "employee_id": "E-1203",
        "team": "Lube", "skill_level": "Apprentice 2",
        "certs": [("LUBE", None)],
        "restrictions": ["ENGINE", "TRANSMISSION", "ELECTRICAL"],
        "specialties": [("LUBE", None), ("MAINTENANCE", None)],
        "shift": (time(7, 0), time(15, 30)), "lunch": (time(11, 30), time(12, 0)),
        "max_daily_hours": 8.0, "overtime_threshold": 8.0,
        "efficiency_target": 100, "productivity_target": 90,
        "profile": {"Maintenance": 1.14, "Brakes": 0.95},
        "volume": {"Maintenance": 0.85, "Brakes": 0.15},
        "comeback_rate": 0.04,
    },
    {
        # Deliberately thin history — he is the "building sample" case on the
        # scoreboard, and the Guardian gate is what keeps him off the ranking.
        "name": "Josh Barrett", "dms_tech_no": "T106", "employee_id": "E-1301",
        "team": "Main", "skill_level": "Apprentice 3",
        "certs": [("ASE", "A4 Suspension")],
        "restrictions": ["ENGINE"],
        "specialties": [],
        "shift": (time(9, 0), time(18, 0)), "lunch": (time(13, 0), time(13, 30)),
        "max_daily_hours": 8.0, "overtime_threshold": 8.0,
        "efficiency_target": 95, "productivity_target": 80,
        "profile": {"Maintenance": 0.92, "Brakes": 0.9},
        "volume": {"Maintenance": 0.6, "Brakes": 0.4},
        "comeback_rate": 0.08,
        "thin": True,  # only a handful of ROs — below the minimum-volume gate
    },
]

CATEGORY_OPS: dict[str, list[str]] = {}
for code, cat, _wt, _tier, excluded in OP_CODES:
    if not excluded:
        CATEGORY_OPS.setdefault(cat, []).append(code)

BOOK_TIMES = {
    "Electrical/AC": (1.2, 3.0),
    "EV/Hybrid": (1.5, 4.0),
    "Drivability/Diag": (1.0, 3.5),
    "Engine": (2.5, 7.0),
    "Transmission": (2.5, 6.5),
    "Brakes": (1.0, 2.6),
    "Suspension": (1.0, 3.0),
    "Maintenance": (0.4, 1.2),
}

VEHICLES = [
    (2019, "Honda", "Odyssey"), (2021, "Honda", "CR-V"), (2018, "Honda", "Civic"),
    (2022, "Honda", "Accord"), (2020, "Honda", "Pilot"), (2023, "Honda", "Clarity"),
    (2017, "Honda", "Fit"), (2024, "Honda", "Prologue"), (2020, "Honda", "Ridgeline"),
]


def _vin(i: int) -> str:
    return f"1HGCM{i:06d}ZA{i % 100:05d}"[:17].ljust(17, "0")


# --------------------------------------------------------------------------- #
# 90 days of closed ROs                                                        #
# --------------------------------------------------------------------------- #


def generate_history(now: datetime) -> list[ParsedRow]:
    """Build a believable 90-day export, then run it through the SAME importer
    functions the real CSV path uses.  The demo baselines are therefore produced
    by production code, not by a special seeding shortcut."""
    rows: list[ParsedRow] = []
    ro_seq = 3000
    row_no = 2

    for tech in TECHS:
        n_ros = 8 if tech.get("thin") else RNG.randint(95, 140)
        categories = list(tech["volume"].keys())
        weights = [tech["volume"][c] for c in categories]

        for _ in range(n_ros):
            ro_seq += 1
            category = RNG.choices(categories, weights=weights, k=1)[0]
            op_code = RNG.choice(CATEGORY_OPS[category])

            days_ago = RNG.randint(1, 88)
            opened = (now - timedelta(days=days_ago)).replace(
                hour=RNG.randint(7, 11), minute=RNG.choice([0, 15, 30, 45]),
                second=0, microsecond=0,
            )

            lo, hi = BOOK_TIMES[category]
            flagged = round(RNG.uniform(lo, hi), 1)

            # Efficiency = flagged / clocked.  So to hit a target efficiency of
            # 1.18, the tech must clock flagged / 1.18.
            eff = tech["profile"].get(category, 1.0) * RNG.uniform(0.88, 1.12)
            clocked = round(max(0.2, flagged / max(eff, 0.4)), 1)

            closed = opened + timedelta(hours=clocked + RNG.uniform(0.5, 4.0))
            promise = opened.replace(hour=17, minute=0) if RNG.random() < 0.8 else None
            # Most promises are met; some are not — otherwise promise-time % is
            # a meaningless 100 across the board.
            if promise and RNG.random() < 0.12:
                closed = promise + timedelta(hours=RNG.uniform(0.3, 2.5))

            labor_type = RNG.choices(["CP", "WARRANTY", "INTERNAL"], [0.62, 0.31, 0.07])[0]
            year, make, model = RNG.choice(VEHICLES)

            rows.append(
                ParsedRow(
                    row_number=row_no,
                    ro_number=str(ro_seq),
                    opened_at=opened,
                    closed_at=closed,
                    dms_tech_no=tech["dms_tech_no"],
                    advisor_id=RNG.choice(["A-01", "A-02", "A-03"]),
                    op_code=op_code,
                    flagged_hours=flagged,
                    actual_clocked_hours=clocked,
                    labor_type=labor_type,
                    promise_time=promise,
                    vin=_vin(RNG.randint(1, 260)),  # a small VIN pool => real comebacks
                    vehicle_ymm=f"{year} {make} {model}",
                )
            )
            row_no += 1

        # A little excluded work, so the scoreboard has something to exclude.
        for op in ("TRAIN", "SHOPTM"):
            ro_seq += 1
            opened = now - timedelta(days=RNG.randint(1, 88))
            rows.append(
                ParsedRow(
                    row_number=row_no, ro_number=str(ro_seq),
                    opened_at=opened, closed_at=opened + timedelta(hours=3),
                    dms_tech_no=tech["dms_tech_no"], advisor_id="A-01",
                    op_code=op, flagged_hours=0.0, actual_clocked_hours=3.0,
                    labor_type="INTERNAL", promise_time=None,
                    vin=_vin(RNG.randint(1, 260)), vehicle_ymm="2020 Honda Pilot",
                )
            )
            row_no += 1

    return rows


# --------------------------------------------------------------------------- #
# Today's board                                                                #
# --------------------------------------------------------------------------- #


def board_ros(now: datetime) -> list[dict]:
    """The live board.  Written to exercise every branch of the engine:
    a cert-gated EV job, a restricted engine job, a team-separated lube job,
    a heat case, a comeback, and a promise time nobody can make."""
    today = now.astimezone(TZ).date()

    def local(h: int, m: int = 0) -> datetime:
        return datetime.combine(today, time(h, m), tzinfo=TZ).astimezone(timezone.utc)

    return [
        {
            "ro_number": "4460",
            "vehicle": (2019, "Honda", "Odyssey"), "mileage": 88700,
            "concern_category": "Electrical/AC", "work_type": "HVAC", "tier": "B",
            "required_certs": [], "est_hours": 2.0,
            "written_at": local(8, 45), "promise_at": local(16, 0),
            "status": "READY_TO_DISPATCH", "flags": ["WAITING"], "priority": "HIGH",
            "lines": [
                ("ACDIAG", "A/C blowing warm", 0.5),
                ("ACRCHG", "Evac & recharge", 1.0),
                ("ACDIAG", "Check for leaks", 0.5),
            ],
        },
        {
            "ro_number": "4461",
            "vehicle": (2024, "Honda", "Prologue"), "mileage": 12400,
            "concern_category": "EV/Hybrid", "work_type": "EV", "tier": "A",
            "required_certs": ["HV_EV"],  # only Mike holds this — a hard filter
            "est_hours": 3.0,
            "written_at": local(8, 10), "promise_at": local(17, 0),
            "status": "READY_TO_DISPATCH", "flags": ["MGR_FLAG"], "priority": "HIGH",
            "lines": [
                ("EVBATT", "HV battery state-of-health warning", 1.0),
                ("EVBATT", "Inspect HV harness & connectors", 2.0),
            ],
        },
        {
            "ro_number": "4462",
            "vehicle": (2018, "Honda", "Civic"), "mileage": 121300,
            "concern_category": "Brakes", "work_type": "BRAKES", "tier": "B",
            "required_certs": [], "est_hours": 1.8,
            "written_at": local(7, 55), "promise_at": local(14, 0),
            "status": "READY_TO_DISPATCH", "flags": ["COMEBACK"], "priority": "HIGH",
            "lines": [
                ("BRKFRT", "Squeal returned after front brake service", 0.6),
                ("BRKFRT", "Re-inspect pads & rotors", 1.2),
            ],
        },
        {
            "ro_number": "4463",
            "vehicle": (2020, "Honda", "Pilot"), "mileage": 64100,
            "concern_category": "Maintenance", "work_type": "LUBE", "tier": "C",
            "required_certs": [], "est_hours": 0.7,
            "required_team": "Lube",  # team separation — Main techs are excluded
            "written_at": local(9, 20), "promise_at": local(12, 30),
            "status": "READY_TO_DISPATCH", "flags": [], "priority": "LOW",
            "lines": [
                ("LOF", "Oil & filter change", 0.4),
                ("TIREROT", "Tire rotation", 0.3),
            ],
        },
        {
            "ro_number": "4464",
            "vehicle": (2017, "Honda", "Fit"), "mileage": 143900,
            "concern_category": "Engine", "work_type": "ENGINE", "tier": "A",
            "required_certs": [], "est_hours": 5.5,
            "written_at": local(7, 30), "promise_at": local(17, 30),
            "status": "READY_TO_DISPATCH", "flags": ["HEAT_CASE"], "priority": "HIGH",
            "lines": [
                ("DRDIAG", "Misfire under load", 1.5),
                ("ENGREP", "Replace ignition coils & plugs", 2.5),
                ("ENGREP", "Compression test", 1.5),
            ],
        },
        {
            "ro_number": "4465",
            "vehicle": (2021, "Honda", "CR-V"), "mileage": 39800,
            "concern_category": "Suspension", "work_type": "ALIGNMENT", "tier": "B",
            "required_certs": [], "est_hours": 1.5,
            "written_at": local(10, 5), "promise_at": local(15, 30),
            "status": "READY_TO_DISPATCH", "flags": [], "priority": "MEDIUM",
            "lines": [
                ("SUSALN", "Pulls right under braking", 0.5),
                ("SUSALN", "Four-wheel alignment", 1.0),
            ],
        },
        # --- other tabs --------------------------------------------------------
        {
            "ro_number": "4466",
            "vehicle": (2022, "Honda", "Accord"), "mileage": 28400,
            "concern_category": "Drivability/Diag", "work_type": "DIAGNOSTIC", "tier": "A",
            "required_certs": [], "est_hours": 2.5,
            "written_at": local(9, 50), "promise_at": local(17, 0),
            "status": "PENDING_AUTHORIZATION", "flags": [], "priority": "MEDIUM",
            "lines": [("DRDIAG", "Intermittent stall at idle", 2.5)],
        },
        {
            "ro_number": "4467",
            "vehicle": (2020, "Honda", "Ridgeline"), "mileage": 71200,
            "concern_category": "Transmission", "work_type": "TRANSMISSION", "tier": "A",
            "required_certs": [], "est_hours": 4.0,
            "written_at": local(8, 30), "promise_at": None,
            "status": "WAITING_ON_PARTS", "flags": ["WAITING"], "priority": "MEDIUM",
            "lines": [("TRNSVC", "Harsh 2-3 shift — awaiting valve body", 4.0)],
        },
        {
            "ro_number": "4468",
            "vehicle": (2023, "Honda", "Clarity"), "mileage": 18900,
            "concern_category": "Maintenance", "work_type": "MAINTENANCE", "tier": "C",
            "required_certs": [], "est_hours": 0.5,
            "written_at": local(10, 40), "promise_at": local(16, 30),
            "status": "OPEN", "flags": [], "priority": "LOW",
            "lines": [("MULTIPT", "Multi-point inspection", 0.5)],
        },
    ]


# --------------------------------------------------------------------------- #


def demo_anchor() -> datetime:
    """A stable shop-open moment: the nearest weekday at 9:00 AM Chicago.

    The whole demo is scored against this instant, so (a) the board is alive
    whatever hour a reviewer opens it, and (b) the Match Scores are identical on
    every run.  Written to .env as DEMO_NOW and read back by app/clock.py.
    """
    local = datetime.now(timezone.utc).astimezone(TZ)
    d = local.date()
    while d.weekday() > 4:  # roll Sat/Sun back to Friday
        d -= timedelta(days=1)
    return datetime.combine(d, time(9, 0), tzinfo=TZ).astimezone(timezone.utc)


def write_env_demo_now(anchor: datetime) -> None:
    """Persist the anchor so the running API uses the same clock the data was
    built for.  Idempotent: rewrites the DEMO_NOW line, leaves everything else."""
    import os

    env_path = os.path.join(os.path.dirname(__file__), ".env")
    line = f"DEMO_NOW={anchor.isoformat()}"
    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = [ln.rstrip("\n") for ln in fh if not ln.startswith("DEMO_NOW=")]
    lines.append(line)
    with open(env_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


async def main() -> None:
    # The demo is anchored to a fixed shop-open moment, not the wall clock, so
    # the board is alive at any hour and every score is reproducible.
    now = demo_anchor()
    write_env_demo_now(now)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        # Reseeding must be fully idempotent — clear EVERY prior demo dealership
        # (both stores), not just St. Charles, or the Elgin tenant accumulates a
        # duplicate on every run.
        demo_names = ["McGrath Honda of St. Charles", "McGrath Honda of Elgin"]
        prior = list(
            (await session.execute(select(Dealer).where(Dealer.name.in_(demo_names)))).scalars()
        )
        for existing in prior:
            print(f"Clearing existing demo dealership {existing.id} ...")
            dealer_id = existing.id
            for model in (
                AuditLog, Assignment, ROLine, RepairOrder, TechCategoryFamiliarity,
                ComebackPairRow, ROHistory, TimeClockDay, ImportRun, OpCodeMap,
                TechnicianCert, TechnicianRestriction, TechnicianSpecialty,
                Technician, UserProfile,
            ):
                await session.execute(delete(model).where(model.dealer_id == dealer_id))
            await session.execute(delete(DealerSettings).where(DealerSettings.dealer_id == dealer_id))
            await session.execute(delete(Dealer).where(Dealer.id == dealer_id))
        if prior:
            await session.commit()

        # ---- dealer + settings --------------------------------------------- #
        dealer = Dealer(name="McGrath Honda of St. Charles", timezone="America/Chicago")
        session.add(dealer)
        await session.flush()
        session.add(DealerSettings(dealer_id=dealer.id))

        # A second dealership, with nothing in it.  It exists to prove the
        # multi-tenant boundary is real: query the API with its dealer id and
        # you get an empty board, not McGrath's.
        other = Dealer(name="McGrath Honda of Elgin", timezone="America/Chicago")
        session.add(other)
        await session.flush()
        session.add(DealerSettings(dealer_id=other.id))

        # ---- op code map ---------------------------------------------------- #
        for code, cat, work_type, tier, excluded in OP_CODES:
            session.add(
                OpCodeMap(
                    dealer_id=dealer.id, op_code=code, concern_category=cat,
                    work_type=work_type, tier=tier, excluded=excluded,
                    exclusion_reason=("Training / shop time / policy — excluded work"
                                      if excluded else None),
                )
            )

        # ---- technicians ----------------------------------------------------- #
        tech_rows: dict[str, Technician] = {}
        for spec in TECHS:
            t = Technician(
                dealer_id=dealer.id,
                name=spec["name"],
                employee_id=spec["employee_id"],
                dms_tech_no=spec["dms_tech_no"],
                team=spec["team"],
                skill_level=spec["skill_level"],
                active=True,
                shift_start=spec["shift"][0],
                shift_end=spec["shift"][1],
                work_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
                lunch_start=spec["lunch"][0],
                lunch_end=spec["lunch"][1],
                max_daily_hours=spec["max_daily_hours"],
                overtime_threshold=spec["overtime_threshold"],
                efficiency_target=spec["efficiency_target"],
                productivity_target=spec["productivity_target"],
            )
            t.certs = [
                TechnicianCert(dealer_id=dealer.id, cert_type=c, level=lv)
                for c, lv in spec["certs"]
            ]
            t.restrictions = [
                TechnicianRestriction(dealer_id=dealer.id, blocked_work_type=w)
                for w in spec["restrictions"]
            ]
            t.specialties = [
                TechnicianSpecialty(dealer_id=dealer.id, work_type=w, vehicle_specialty=v)
                for w, v in spec["specialties"]
            ]
            session.add(t)
            tech_rows[spec["dms_tech_no"]] = t
        await session.flush()

        # ---- 90-day history, through the real importer ---------------------- #
        history = generate_history(now)
        categories = {code: cat for code, cat, _w, _t, _e in OP_CODES}
        excluded_ops = {code for code, _c, _w, _t, e in OP_CODES if e}

        run = ImportRun(
            dealer_id=dealer.id, kind="DMS_RO_HISTORY",
            filename="demo_90day_export.csv", status="COMPLETED",
            column_mapping={"note": "generated by seed.py"},
            rows_total=len(history), rows_imported=len(history), rows_rejected=0,
            completed_at=now,
        )
        session.add(run)
        await session.flush()

        for r in history:
            t = tech_rows[r.dms_tech_no]
            is_excluded = r.op_code in excluded_ops
            session.add(
                ROHistory(
                    dealer_id=dealer.id, import_run_id=run.id, ro_number=r.ro_number,
                    opened_at=r.opened_at, closed_at=r.closed_at,
                    dms_tech_no=r.dms_tech_no, technician_id=t.id,
                    advisor_id=r.advisor_id, op_code=r.op_code,
                    concern_category=categories[r.op_code],
                    flagged_hours=r.flagged_hours,
                    actual_clocked_hours=r.actual_clocked_hours,
                    labor_type=r.labor_type, promise_time=r.promise_time,
                    vin=r.vin, vehicle_ymm=r.vehicle_ymm,
                    excluded_from_metrics=is_excluded,
                    exclusion_reason=("Op code marked as excluded work" if is_excluded else None),
                    imported_at=now,
                )
            )

        comebacks = find_comeback_pairs(history, categories, 30)
        for cb in comebacks:
            session.add(
                ComebackPairRow(
                    dealer_id=dealer.id, vin=cb.vin, concern_category=cb.concern_category,
                    original_ro_number=cb.original_ro_number,
                    original_closed_at=cb.original_closed_at,
                    original_tech_id=tech_rows[cb.original_dms_tech_no].id,
                    repeat_ro_number=cb.repeat_ro_number,
                    repeat_opened_at=cb.repeat_opened_at,
                    days_between=cb.days_between,
                )
            )

        familiarity = build_familiarity(history, categories, comebacks, excluded_ops)
        for f in familiarity:
            session.add(
                TechCategoryFamiliarity(
                    dealer_id=dealer.id,
                    technician_id=tech_rows[f.dms_tech_no].id,
                    concern_category=f.concern_category,
                    repairs_completed=f.repairs_completed,
                    flagged_hours=f.flagged_hours,
                    clocked_hours=f.clocked_hours,
                    avg_efficiency=f.avg_efficiency,
                    first_time_fix=f.first_time_fix,
                    last_performed_at=f.last_performed_at,
                )
            )

        # ---- time clock (so Productivity is a real number, not a gate) ------ #
        clock_run = ImportRun(
            dealer_id=dealer.id, kind="TIME_CLOCK", filename="demo_time_clock.csv",
            status="COMPLETED", rows_total=0, rows_imported=0, completed_at=now,
        )
        session.add(clock_run)
        await session.flush()

        clock_rows = 0
        for spec in TECHS:
            t = tech_rows[spec["dms_tech_no"]]
            for d in range(90):
                day = (now - timedelta(days=d)).astimezone(TZ).date()
                if day.weekday() > 4:
                    continue
                # Productivity = on-job hours / total clocked. Total is always a
                # bit more than on-job: that gap is the story the metric tells.
                session.add(
                    TimeClockDay(
                        dealer_id=dealer.id, import_run_id=clock_run.id,
                        technician_id=t.id, dms_tech_no=spec["dms_tech_no"],
                        work_date=day,
                        total_clocked_hours=round(RNG.uniform(7.6, 8.4), 1),
                    )
                )
                clock_rows += 1
        clock_run.rows_total = clock_rows
        clock_run.rows_imported = clock_rows

        # ---- today's board --------------------------------------------------- #
        for spec in board_ros(now):
            year, make, model = spec["vehicle"]
            ro = RepairOrder(
                dealer_id=dealer.id,
                ro_number=spec["ro_number"],
                vin=_vin(RNG.randint(500, 900)),
                vehicle_year=year, vehicle_make=make, vehicle_model=model,
                mileage=spec["mileage"],
                concern_category=spec["concern_category"],
                work_type=spec["work_type"],
                tier=spec["tier"],
                required_certs=spec["required_certs"],
                required_team=spec.get("required_team"),
                est_hours=spec["est_hours"],
                written_at=spec["written_at"],
                promise_at=spec["promise_at"],
                status=spec["status"],
                flags=spec["flags"],
                priority=spec["priority"],
                advisor_id="A-01",
            )
            ro.lines = [
                ROLine(dealer_id=dealer.id, op_code=op, description=desc,
                       flagged_hours=hrs, sort_order=i)
                for i, (op, desc, hrs) in enumerate(spec["lines"])
            ]
            session.add(ro)

        await session.commit()

        print()
        print("  3D DISPATCH - demo data loaded")
        print("  " + "-" * 52)
        print(f"  Dealer            {dealer.name}")
        print(f"  dealer_id         {dealer.id}")
        print(f"  Second dealer     {other.name}  ({other.id})")
        print(f"  Technicians       {len(TECHS)}")
        print(f"  History rows      {len(history)}  (90 days of closed ROs)")
        print(f"  Comeback pairs    {len(comebacks)}")
        print(f"  Familiarity rows  {len(familiarity)}")
        print(f"  Time-clock days   {clock_rows}")
        print(f"  Board ROs         {len(board_ros(now))}")
        print(f"  Demo clock        {now.isoformat()}  (written to .env as DEMO_NOW)")
        print()
        print("  Next:  uvicorn app.main:app --reload --port 8000")
        print()


if __name__ == "__main__":
    asyncio.run(main())
