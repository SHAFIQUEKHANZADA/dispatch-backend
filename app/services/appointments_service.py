"""Appointments board — enriches live myKaarma appointments with the analytics
the dispatcher cares about: job complexity, a show-likelihood estimate, and a
capacity-hold decision (reserve a qualified tech for the hard, likely-to-show
jobs so a no-show can't idle a bench).

Honesty notes:
  * time / customer / vehicle / concern / status  -> REAL, from myKaarma.
  * complexity                                     -> derived from the concern.
  * capacity hold + reserved tech                  -> derived (real roster).
  * show-likelihood                                -> ESTIMATE from real signals
    (confirmation prefs, transport, lead time). A true no-show model needs
    historical no-show data; this is a transparent placeholder until then.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import default_now
from ..models import Dealer, ROHistory, Technician
from ..mykaarma.connector import upcoming_appointments

_HIGH = ("transmission", "trans ", "hybrid", "ev ", "no-start", "no start", "no-crank",
         "no crank", "diagnos", "drivab", "electric", "parasit", "cel", "check engine",
         "recall", "engine", " hv", "hv ", "stall", "misfire", "warning")
_LOW = ("oil", "lof", "lube", "rotat", "tire", "mpi", "inspect", "filter", "wiper", "fluid")
_MID = ("brake", "align", "suspens", "steer", "ac ", "a/c", "heat", "coolant")


def _complexity(text: str | None) -> str:
    t = (text or "").lower()
    if any(k in t for k in _HIGH):
        return "HIGH"
    if any(k in t for k in _LOW):
        return "LOW"
    if any(k in t for k in _MID):
        return "MID"
    return "MID"


def _lead_days(booked_at: str | None, start: str | None) -> float:
    try:
        b = datetime.fromisoformat((booked_at or "").replace(" ", "T"))
        s = datetime.fromisoformat((start or "").replace(" ", "T"))
        return max(0.0, (s - b).total_seconds() / 86400)
    except Exception:
        return 0.0


def _show_estimate(appt: dict) -> int:
    """Show-likelihood estimate from real signals (not a trained model)."""
    pct = 88
    if appt.get("text_reminder"):
        pct += 4
    if "wait" in (appt.get("transport") or "").lower():
        pct += 4  # waiters almost always show
    lead = _lead_days(appt.get("booked_at"), appt.get("start_time"))
    if lead > 7:
        pct -= 10
    elif lead > 3:
        pct -= 4
    return max(52, min(97, pct))


def _lifecycle(appt: dict, now_local: datetime) -> str:
    """scheduled | arrived | no_show — judged against the (demo) clock, then the
    myKaarma status. Future appointments are always 'scheduled'."""
    try:
        start = datetime.fromisoformat((appt.get("start_time") or "").replace(" ", "T"))
        start = start.replace(tzinfo=now_local.tzinfo)
    except Exception:
        start = None
    if start and start > now_local:
        return "scheduled"                       # upcoming — ignore real-time status
    status = (appt.get("status") or "").lower()
    if any(k in status for k in ("no-show", "no show", "cancel", "missed")):
        return "no_show"
    return "arrived"                             # a past appointment that happened


async def _reserve_pool(session: AsyncSession, dealer_id: uuid.UUID) -> list[str]:
    """Top real technicians by billed hours — the pool we reserve from."""
    rows = (
        await session.execute(
            select(Technician.name, func.coalesce(func.sum(ROHistory.flagged_hours), 0).label("h"))
            .join(ROHistory, ROHistory.technician_id == Technician.id)
            .where(Technician.dealer_id == dealer_id, Technician.active.is_(True))
            .group_by(Technician.name)
            .order_by(func.sum(ROHistory.flagged_hours).desc())
            .limit(10)
        )
    ).all()
    return [r[0] for r in rows]


def _short_name(name: str) -> str:
    parts = name.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return name


async def build_appointments_board(session: AsyncSession, dealer_id: uuid.UUID, days: int = 1) -> dict:
    # enrich=False: the board list only needs time/customer/vehicle/concern (all
    # on the appointment itself). The heavy per-customer phone/email lookups are
    # skipped here — on a busy store they are 1,000+ calls and the page hangs.
    raw = await upcoming_appointments(session, dealer_id, days, enrich=False)
    if not raw.get("available"):
        return {"available": False, "reason": raw.get("reason"), "appointments": [], "stats": {}}

    dealer = await session.get(Dealer, dealer_id)
    tz = ZoneInfo(dealer.timezone) if dealer and dealer.timezone else ZoneInfo("America/Chicago")
    now_local = default_now().astimezone(tz)
    today = now_local.date().isoformat()

    pool = await _reserve_pool(session, dealer_id)
    appts = raw.get("appointments", [])

    out = []
    high_i = 0
    for a in appts:
        concern = a.get("service_requested") or a.get("vehicle") or ""
        cx = _complexity(concern)
        life = _lifecycle(a, now_local)
        show = None if life == "no_show" else (100 if life == "arrived" else _show_estimate(a))

        reserve = None
        if life == "arrived":
            hold = {"kind": "checked_in", "label": "Checked in"}
        elif life == "no_show":
            hold = {"kind": "no_show", "label": "No-show — freed"}
        elif cx == "HIGH":
            reserve = _short_name(pool[high_i % len(pool)]) if pool else None
            high_i += 1
            if show is not None and show >= 60:
                hold = {"kind": "soft_hold", "label": "Soft hold", "reserve": reserve}
            else:
                hold = {"kind": "alert", "label": "Alert · low show", "reserve": reserve}
        else:
            hold = {"kind": "routine", "label": "Routine — no hold"}

        out.append({
            **a,
            "complexity": cx,
            "show_pct": show,
            "lifecycle": life,
            "capacity_hold": hold,
        })

    stats = {
        "upcoming_today": sum(1 for a in out if (a.get("start_time") or "").startswith(today)),
        "high_complexity": sum(1 for a in out if a["complexity"] == "HIGH"),
        "windows_held": sum(1 for a in out if a["capacity_hold"]["kind"] == "soft_hold"),
        "no_show_freed": sum(1 for a in out if a["lifecycle"] == "no_show"),
    }

    return {
        "available": True,
        "count": len(out),
        "capacity_policy": {
            "level": "Medium (soft-reserve)",
            "text": (
                "reserving a qualified tech for high-complexity appointments at or above "
                "60% show-likelihood (lower ones alert only, so a no-show doesn't idle a tech)."
            ),
        },
        "stats": stats,
        "appointments": out,
    }
