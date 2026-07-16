"""FR-2 — the DMS export importer.

"Never import silently-bad data."  Two-step by design: preview (sniff headers,
suggest a mapping) then commit (validate every row, report every rejection).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import delete, select

from .. import audit
from ..deps import CurrentUserDep, SessionDep, get_dealer_settings
from ..engine.importer import (
    DMS_FIELDS,
    TIME_CLOCK_FIELDS,
    build_familiarity,
    find_comeback_pairs,
    parse_dms_csv,
    parse_time_clock_csv,
    sniff_csv,
    suggest_mapping,
)
from ..models import (
    ComebackPairRow,
    ImportRun,
    OpCodeMap,
    ROHistory,
    TechCategoryFamiliarity,
    Technician,
    TimeClockDay,
)

router = APIRouter(prefix="/imports", tags=["imports"])


async def _read_csv(file: UploadFile) -> str:
    raw = await file.read()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Could not decode the file as text")


@router.get("/fields")
async def fields():
    """The canonical fields the mapping UI has to fill."""
    return {
        "dms": [
            {"key": k, "label": v["label"], "required": v["required"]}
            for k, v in DMS_FIELDS.items()
        ],
        "time_clock": [
            {"key": k, "label": v["label"], "required": v["required"]}
            for k, v in TIME_CLOCK_FIELDS.items()
        ],
    }


@router.post("/preview")
async def preview(
    current: CurrentUserDep,
    file: UploadFile = File(...),
    kind: str = Form("DMS_RO_HISTORY"),
):
    """FR-2.1 — read the headers and propose a column mapping.

    Every dealership's export is shaped differently, so we never guess silently:
    we show what we think each column is and let the SM correct it.
    """
    text = await _read_csv(file)
    fields_spec = DMS_FIELDS if kind == "DMS_RO_HISTORY" else TIME_CLOCK_FIELDS
    sniffed = sniff_csv(text)
    return {
        "filename": file.filename,
        "kind": kind,
        "headers": sniffed["headers"],
        "sample": sniffed["sample"],
        "suggested_mapping": suggest_mapping(sniffed["headers"], fields_spec),
        "fields": [
            {"key": k, "label": v["label"], "required": v["required"]}
            for k, v in fields_spec.items()
        ],
    }


@router.post("/commit")
async def commit(
    session: SessionDep,
    current: CurrentUserDep,
    file: UploadFile = File(...),
    mapping_json: str = Form(...),
    kind: str = Form("DMS_RO_HISTORY"),
    replace_existing: bool = Form(True),
):
    """Validate, import, and derive the baselines (FR-2.3 / 2.4 / 2.5)."""
    import json

    current.require_role("SERVICE_MANAGER")
    text = await _read_csv(file)

    try:
        mapping = json.loads(mapping_json)
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mapping_json is not valid JSON")

    if kind == "TIME_CLOCK":
        return await _commit_time_clock(session, current, file.filename, text, mapping, replace_existing)
    return await _commit_dms(session, current, file.filename, text, mapping, replace_existing)


async def _commit_dms(
    session, current, filename, text, mapping, replace_existing: bool
) -> dict:
    ds = await get_dealer_settings(session, current.dealer_id)

    try:
        parsed = parse_dms_csv(text, mapping)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    run = ImportRun(
        dealer_id=current.dealer_id,
        kind="DMS_RO_HISTORY",
        filename=filename,
        column_mapping=mapping,
        rows_total=parsed.rows_total,
        rows_rejected=len(parsed.rejects),
        rejects=[r.to_dict() for r in parsed.rejects][:500],  # cap the payload; count is exact
        created_by=current.user_id,
    )
    session.add(run)
    await session.flush()

    # --- FR-2.5: map dms_tech_no -> technicians, and report what did not match
    techs = list(
        (
            await session.execute(
                select(Technician).where(Technician.dealer_id == current.dealer_id)
            )
        ).scalars()
    )
    by_no = {t.dms_tech_no: t for t in techs if t.dms_tech_no}

    seen_nos = {r.dms_tech_no for r in parsed.rows}
    unmatched = sorted(n for n in seen_nos if n not in by_no)
    run.unmatched_tech_nos = unmatched

    # --- FR-2.6: op_code -> concern_category (editable per dealer) -----------
    op_rows = list(
        (
            await session.execute(
                select(OpCodeMap).where(OpCodeMap.dealer_id == current.dealer_id)
            )
        ).scalars()
    )
    categories = {o.op_code: o.concern_category for o in op_rows}
    excluded_ops = {o.op_code for o in op_rows if o.excluded}

    # Op codes the dealer has never classified.  We do NOT invent a category —
    # they land in the map as "Uncategorised" for the SM to fix, and until then
    # they contribute nothing to familiarity.
    unmapped_ops = sorted({r.op_code for r in parsed.rows} - set(categories))
    for op in unmapped_ops:
        session.add(
            OpCodeMap(
                dealer_id=current.dealer_id,
                op_code=op,
                concern_category="Uncategorised",
                tier="B",
            )
        )
        categories[op] = "Uncategorised"

    if replace_existing:
        await session.execute(
            delete(ROHistory).where(ROHistory.dealer_id == current.dealer_id)
        )
        await session.execute(
            delete(ComebackPairRow).where(ComebackPairRow.dealer_id == current.dealer_id)
        )
        await session.execute(
            delete(TechCategoryFamiliarity).where(
                TechCategoryFamiliarity.dealer_id == current.dealer_id
            )
        )

    now = datetime.now(timezone.utc)
    for r in parsed.rows:
        tech = by_no.get(r.dms_tech_no)
        category = categories.get(r.op_code, "Uncategorised")
        session.add(
            ROHistory(
                dealer_id=current.dealer_id,
                import_run_id=run.id,
                ro_number=r.ro_number,
                opened_at=r.opened_at,
                closed_at=r.closed_at,
                dms_tech_no=r.dms_tech_no,
                technician_id=tech.id if tech else None,
                advisor_id=r.advisor_id,
                op_code=r.op_code,
                concern_category=category,
                flagged_hours=r.flagged_hours,
                actual_clocked_hours=r.actual_clocked_hours,
                labor_type=r.labor_type,
                promise_time=r.promise_time,
                vin=r.vin,
                vehicle_ymm=r.vehicle_ymm,
                excluded_from_metrics=r.op_code in excluded_ops,
                exclusion_reason=("Op code marked as excluded work" if r.op_code in excluded_ops else None),
                imported_at=now,
            )
        )

    # --- FR-2.4b/c: comeback pairs + the familiarity map ---------------------
    window = int(ds.comeback_window_days or 30)
    comebacks = find_comeback_pairs(parsed.rows, categories, window)
    for cb in comebacks:
        tech = by_no.get(cb.original_dms_tech_no)
        session.add(
            ComebackPairRow(
                dealer_id=current.dealer_id,
                vin=cb.vin,
                concern_category=cb.concern_category,
                original_ro_number=cb.original_ro_number,
                original_closed_at=cb.original_closed_at,
                original_tech_id=tech.id if tech else None,
                repeat_ro_number=cb.repeat_ro_number,
                repeat_opened_at=cb.repeat_opened_at,
                days_between=cb.days_between,
            )
        )

    familiarity = build_familiarity(parsed.rows, categories, comebacks, excluded_ops)
    for f in familiarity:
        tech = by_no.get(f.dms_tech_no)
        if tech is None:
            continue  # an unmatched tech # cannot have a capability map
        session.add(
            TechCategoryFamiliarity(
                dealer_id=current.dealer_id,
                technician_id=tech.id,
                concern_category=f.concern_category,
                repairs_completed=f.repairs_completed,
                flagged_hours=f.flagged_hours,
                clocked_hours=f.clocked_hours,
                avg_efficiency=f.avg_efficiency,
                first_time_fix=f.first_time_fix,
                last_performed_at=f.last_performed_at,
            )
        )

    run.rows_imported = len(parsed.rows)
    run.status = "COMPLETED"
    run.completed_at = now

    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action=audit.IMPORT_DMS,
        entity="import_run",
        entity_id=run.id,
        payload={
            "filename": filename,
            "rows_total": parsed.rows_total,
            "rows_imported": len(parsed.rows),
            "rows_rejected": len(parsed.rejects),
            "unmatched_tech_nos": unmatched,
            "unmapped_op_codes": unmapped_ops,
            "comeback_pairs": len(comebacks),
            "familiarity_rows": len(familiarity),
        },
    )
    await session.commit()

    return {
        "import_run_id": str(run.id),
        "rows_total": parsed.rows_total,
        "rows_imported": len(parsed.rows),
        "rows_rejected": len(parsed.rejects),
        "rejects": [r.to_dict() for r in parsed.rejects][:200],
        "unmatched_tech_nos": unmatched,
        "unmapped_op_codes": unmapped_ops,
        "comeback_pairs": len(comebacks),
        "familiarity_rows": len(familiarity),
        "warnings": (
            [
                f"{len(unmatched)} DMS tech # did not match a technician on file: "
                f"{', '.join(unmatched[:10])}"
                + (" …" if len(unmatched) > 10 else "")
            ]
            if unmatched
            else []
        )
        + (
            [
                f"{len(unmapped_ops)} op code(s) had no category mapping and were filed as "
                f"'Uncategorised'. Map them in Settings → Op Codes so they count toward familiarity."
            ]
            if unmapped_ops
            else []
        ),
    }


async def _commit_time_clock(
    session, current, filename, text, mapping, replace_existing: bool
) -> dict:
    try:
        parsed = parse_time_clock_csv(text, mapping)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    run = ImportRun(
        dealer_id=current.dealer_id,
        kind="TIME_CLOCK",
        filename=filename,
        column_mapping=mapping,
        rows_total=parsed.rows_total,
        rows_rejected=len(parsed.rejects),
        rejects=[r.to_dict() for r in parsed.rejects][:500],
        created_by=current.user_id,
    )
    session.add(run)
    await session.flush()

    techs = list(
        (
            await session.execute(
                select(Technician).where(Technician.dealer_id == current.dealer_id)
            )
        ).scalars()
    )
    by_no = {t.dms_tech_no: t for t in techs if t.dms_tech_no}
    unmatched = sorted({r.dms_tech_no for r in parsed.rows} - set(by_no))
    run.unmatched_tech_nos = unmatched

    if replace_existing:
        await session.execute(
            delete(TimeClockDay).where(TimeClockDay.dealer_id == current.dealer_id)
        )

    for r in parsed.rows:
        tech = by_no.get(r.dms_tech_no)
        session.add(
            TimeClockDay(
                dealer_id=current.dealer_id,
                import_run_id=run.id,
                technician_id=tech.id if tech else None,
                dms_tech_no=r.dms_tech_no,
                work_date=r.work_date.date(),
                total_clocked_hours=r.total_clocked_hours,
            )
        )

    run.rows_imported = len(parsed.rows)
    run.status = "COMPLETED"
    run.completed_at = datetime.now(timezone.utc)

    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action=audit.IMPORT_TIME_CLOCK,
        entity="import_run",
        entity_id=run.id,
        payload={
            "filename": filename,
            "rows_imported": len(parsed.rows),
            "rows_rejected": len(parsed.rejects),
            "unmatched_tech_nos": unmatched,
        },
    )
    await session.commit()

    return {
        "import_run_id": str(run.id),
        "rows_total": parsed.rows_total,
        "rows_imported": len(parsed.rows),
        "rows_rejected": len(parsed.rejects),
        "rejects": [r.to_dict() for r in parsed.rejects][:200],
        "unmatched_tech_nos": unmatched,
        "warnings": [],
    }


@router.get("")
async def list_runs(session: SessionDep, current: CurrentUserDep):
    runs = list(
        (
            await session.execute(
                select(ImportRun)
                .where(ImportRun.dealer_id == current.dealer_id)
                .order_by(ImportRun.created_at.desc())
                .limit(50)
            )
        ).scalars()
    )
    return {
        "runs": [
            {
                "id": str(r.id),
                "kind": r.kind,
                "filename": r.filename,
                "status": r.status,
                "rows_total": r.rows_total,
                "rows_imported": r.rows_imported,
                "rows_rejected": r.rows_rejected,
                "unmatched_tech_nos": list(r.unmatched_tech_nos or []),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in runs
        ]
    }


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, session: SessionDep, current: CurrentUserDep):
    run = await session.get(ImportRun, run_id)
    if run is None or run.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import run not found")
    return {
        "id": str(run.id),
        "kind": run.kind,
        "filename": run.filename,
        "status": run.status,
        "column_mapping": run.column_mapping,
        "rows_total": run.rows_total,
        "rows_imported": run.rows_imported,
        "rows_rejected": run.rows_rejected,
        "rejects": run.rejects,
        "unmatched_tech_nos": list(run.unmatched_tech_nos or []),
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


# --------------------------------------------------------------------------- #
# Op-code map (FR-2.6)                                                         #
# --------------------------------------------------------------------------- #


class OpCodeIn(BaseModel):
    op_code: str
    concern_category: str
    work_type: Optional[str] = None
    tier: Optional[str] = None
    excluded: bool = False
    exclusion_reason: Optional[str] = None


@router.get("/op-codes/map")
async def get_op_code_map(session: SessionDep, current: CurrentUserDep):
    rows = list(
        (
            await session.execute(
                select(OpCodeMap)
                .where(OpCodeMap.dealer_id == current.dealer_id)
                .order_by(OpCodeMap.op_code)
            )
        ).scalars()
    )
    return {
        "op_codes": [
            {
                "id": str(r.id),
                "op_code": r.op_code,
                "concern_category": r.concern_category,
                "work_type": r.work_type,
                "tier": r.tier,
                "excluded": r.excluded,
                "exclusion_reason": r.exclusion_reason,
            }
            for r in rows
        ]
    }


@router.put("/op-codes/map")
async def put_op_code_map(
    body: list[OpCodeIn], session: SessionDep, current: CurrentUserDep
):
    current.require_role("SERVICE_MANAGER")
    existing = {
        r.op_code: r
        for r in (
            await session.execute(
                select(OpCodeMap).where(OpCodeMap.dealer_id == current.dealer_id)
            )
        ).scalars()
    }

    for item in body:
        row = existing.get(item.op_code)
        if row is None:
            row = OpCodeMap(dealer_id=current.dealer_id, op_code=item.op_code)
            session.add(row)
        row.concern_category = item.concern_category
        row.work_type = item.work_type
        row.tier = item.tier
        row.excluded = item.excluded
        row.exclusion_reason = item.exclusion_reason

    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action=audit.OPCODE_MAP_UPDATED,
        entity="op_code_map",
        payload={"count": len(body)},
    )
    await session.commit()
    return await get_op_code_map(session, current)
