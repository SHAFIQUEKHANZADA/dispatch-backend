"""Load a DealerBuilt closed-RO export into 3D Dispatch as REAL data.

Brings in the real technician roster + closed-job history for a store, so the
scoreboard and Match engine run on actual DMS data instead of seed data.

Usage:  python import_dealerbuilt.py "<export.txt>" [dealer_key]
        dealer_key defaults to mcgrath_honda_stcharles.

What it does (scoped to that ONE store — never touches other tenants):
  * upserts real technicians keyed by their DMS tech number
  * deactivates seed/demo techs that aren't in the export
  * replaces ro_history with the export's deduped job lines
  * refreshes the import timestamp so the scoreboard's staleness gate passes

Notes / honest limits:
  * TECH_HOURS is flagged (billed) hours from the DMS — there is no clock feed
    in this export, so clock-based efficiency isn't computed (left at 0).
  * A job counts once per (RO, job#); it repeats across part lines in the file.
"""

from __future__ import annotations

import asyncio
import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import (
    Assignment,
    Dealer,
    ImportRun,
    ROHistory,
    Technician,
)

csv.field_size_limit(20_000_000)


def _num(v: str) -> float:
    try:
        return float((v or "").strip() or 0)
    except ValueError:
        return 0.0


_LABOR_MAP = {"C": "CP", "W": "WARRANTY", "I": "INTERNAL"}  # S (sublet)/blank -> None


def _labor_type(v: str):
    return _LABOR_MAP.get((v or "").strip().upper())


def _date(v: str):
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse(path: str):
    """Return (techs, jobs). techs: {tech_no: name}. jobs: list of dicts."""
    techs: dict[str, str] = {}
    jobs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh, delimiter="\t", quotechar='"'):
            tnum = (row.get("JOBS_TECH_NUM") or "").strip()
            tname = (row.get("JOBS_TECH_NAME") or "").strip()
            jrn = (row.get("JOBS_RO_NUM") or "").strip()
            jnum = (row.get("JOB_NUM") or "").strip()
            if not tnum or not jrn or not jnum:
                continue
            key = (jrn, jnum)
            if key in seen:  # same job repeats across part lines
                continue
            seen.add(key)
            if "sublet" in tname.lower():
                continue
            techs.setdefault(tnum, tname)
            op = (row.get("JOBS_DEALER_OP_CODE") or "").strip()
            jobs.append({
                "ro_number": jrn,
                "tech_no": tnum,
                "opened_at": _date(row.get("RO_OPENDATE")),
                "closed_at": _date(row.get("JOBS_CLOSED_DATE") or row.get("RO_CLOSEDATE")),
                "labor_type": _labor_type(row.get("JOBS_LABOR_TYPE")),
                "op_code": op or None,
                "concern_category": op or "Uncategorised",
                "flagged_hours": _num(row.get("TECH_HOURS")),
                "advisor_id": (row.get("ADVISOR_NUMBER") or "").strip() or None,
                "vin": (row.get("VIN") or "").strip() or None,
                "vehicle_ymm": " ".join(
                    x for x in ((row.get("YEAR") or "").strip(),
                                (row.get("MAKE") or "").strip(),
                                (row.get("MODEL") or "").strip()) if x
                ) or None,
            })
    return techs, jobs


async def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "../RoDateLocation1_2026-07-31-13-58-49..txt"
    dealer_key = sys.argv[2] if len(sys.argv) > 2 else "mcgrath_honda_stcharles"

    print(f"Parsing {path} …")
    techs, jobs = parse(path)
    print(f"  {len(techs)} technicians, {len(jobs)} jobs")

    async with SessionLocal() as s:
        dealer = (
            await s.execute(select(Dealer).where(Dealer.dealer_key == dealer_key))
        ).scalar_one_or_none()
        if dealer is None:
            print(f"No dealer with key '{dealer_key}'."); return
        did = dealer.id

        # --- technicians: upsert by dms_tech_no ---
        existing = {
            t.dms_tech_no: t
            for t in (
                await s.execute(select(Technician).where(Technician.dealer_id == did))
            ).scalars()
            if t.dms_tech_no
        }
        tech_by_no: dict[str, Technician] = {}
        added = 0
        for tno, name in techs.items():
            t = existing.get(tno)
            if t is None:
                t = Technician(dealer_id=did, name=name, dms_tech_no=tno, team="Main", active=True)
                s.add(t)
                added += 1
            else:
                t.name = name
                t.active = True
            tech_by_no[tno] = t
        # deactivate roster rows not present in the export (seed/demo techs)
        deactivated = 0
        for tno, t in existing.items():
            if tno not in techs:
                t.active = False
                deactivated += 1
        await s.flush()

        # --- history: replace this store's ro_history with the export ---
        await s.execute(delete(ROHistory).where(ROHistory.dealer_id == did))
        run = ImportRun(dealer_id=did, kind="DMS_RO_HISTORY", status="COMPLETED",
                        filename="DealerBuilt export", rows_total=len(jobs),
                        rows_imported=len(jobs), completed_at=datetime.now(timezone.utc))
        s.add(run)
        await s.flush()

        BATCH = 1000
        for i in range(0, len(jobs), BATCH):
            for j in jobs[i:i + BATCH]:
                t = tech_by_no.get(j["tech_no"])
                s.add(ROHistory(
                    dealer_id=did, import_run_id=run.id, ro_number=j["ro_number"],
                    opened_at=j["opened_at"], closed_at=j["closed_at"],
                    dms_tech_no=j["tech_no"], technician_id=t.id if t else None,
                    advisor_id=j["advisor_id"], op_code=j["op_code"],
                    concern_category=j["concern_category"],
                    flagged_hours=j["flagged_hours"], actual_clocked_hours=0,
                    labor_type=j["labor_type"], vin=j["vin"], vehicle_ymm=j["vehicle_ymm"],
                ))
            await s.flush()

        await s.commit()
        print(f"  technicians: +{added} new, {len(tech_by_no)} active, {deactivated} deactivated")
        print(f"  ro_history:  {len(jobs)} rows loaded for {dealer.name}")
        print("  Scoreboard + Available Techs now run on real DMS data.")


if __name__ == "__main__":
    asyncio.run(main())
