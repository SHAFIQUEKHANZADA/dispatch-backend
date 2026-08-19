"""Loaner board — the first piece of the Service Loaner module.

The full loaner picture needs three systems (myKaarma appointments, DealerBuilt RO
status, TSD checkout list) merged on RO number. We already have two of them; the
TSD checkout feed (which physical loaner is out, to whom) is not wired yet.

So this V1 delivers the part that is REAL and immediately useful, without faking
what we can't yet know:
  * Live LOANER DEMAND — today's + upcoming appointments that need a loaner,
    pulled straight from myKaarma (transport option = Loaner). The store currently
    has no single view of this and finds out at check-in.
  * A genuine OVER-BOOKING check — if the day's loaner appointments exceed the
    loaner fleet size, that is a guaranteed shortfall no matter what TSD says.

Exact live availability (how many loaners are free right now) is intentionally NOT
computed here — it needs the TSD feed. We surface demand and flag over-booking; we
do not invent an availability number.
"""

from __future__ import annotations

import uuid
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import default_now
from ..models import Dealer, DealerSettings
from ..mykaarma.connector import upcoming_appointments

DEFAULT_FLEET = 25  # placeholder until each store's real fleet size is set


def _needs_loaner(appt: dict) -> bool:
    t = (appt.get("transport") or "").lower()
    return "loaner" in t or "courtesy" in t


async def build_loaner_board(session: AsyncSession, dealer_id: uuid.UUID, days: int = 7) -> dict:
    raw = await upcoming_appointments(session, dealer_id, days, enrich=False)
    if not raw.get("available"):
        return {"available": False, "reason": raw.get("reason"), "appointments": [], "stats": {}}

    dealer = await session.get(Dealer, dealer_id)
    tz = ZoneInfo(dealer.timezone) if dealer and dealer.timezone else ZoneInfo("America/Chicago")
    today = default_now().astimezone(tz).date().isoformat()

    ds = await session.get(DealerSettings, dealer_id)
    cfg = (ds.store_config or {}) if ds else {}
    fleet = int(cfg.get("loaner_fleet_size") or DEFAULT_FLEET)
    fleet_is_default = "loaner_fleet_size" not in cfg

    loaner_appts = [a for a in raw.get("appointments", []) if _needs_loaner(a)]
    loaner_appts.sort(key=lambda a: a.get("start_time") or "")

    today_appts = [a for a in loaner_appts if (a.get("start_time") or "").startswith(today)]

    # Guaranteed over-booking: more loaner appointments today than the whole fleet
    # can cover, even if every loaner were free. This is real with zero TSD data.
    overbooked = max(0, len(today_appts) - fleet)

    rows = []
    for i, a in enumerate(loaner_appts):
        is_today = (a.get("start_time") or "").startswith(today)
        # coverage flag only against the hard fleet ceiling (not a real availability
        # count — that waits for TSD). Position within today's demand.
        pos = None
        status = "demand"
        if is_today:
            pos = sum(1 for x in today_appts if (x.get("start_time") or "") <= (a.get("start_time") or ""))
            status = "over_fleet" if pos > fleet else "within_fleet"
        rows.append({
            "appointment_uuid": a.get("appointment_uuid"),
            "start_time": a.get("start_time"),
            "customer_name": a.get("customer_name"),
            "vehicle": a.get("vehicle"),
            "transport": a.get("transport"),
            "service_requested": a.get("service_requested"),
            "is_today": is_today,
            "position": pos,
            "status": status,
        })

    return {
        "available": True,
        "tsd_connected": False,   # flips true once the TSD checkout feed is wired
        "fleet_size": fleet,
        "fleet_is_default": fleet_is_default,
        "stats": {
            "loaner_today": len(today_appts),
            "loaner_window": len(loaner_appts),
            "fleet_size": fleet,
            "overbooked_today": overbooked,
        },
        "appointments": rows,
    }
