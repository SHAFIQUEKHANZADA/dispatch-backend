"""Esther daily performance report — built server-side and pushed to a GHL
inbound webhook once a day (11 PM Central by default — captures the full day
including appointments booked after 6 PM). Reid asked for:

  * AI Resolution Rate (containment)
  * TWO conversion numbers: overall (booked / all calls) vs appointment-specific
    (booked / booking attempts — the ~80% he wants to show)
  * a group summary for him AND a per-store report for each dealership.

Delivery is decoupled: the backend POSTs a JSON payload (with a ready-to-send
`html` body and a `subject`) to the GHL workflow's inbound-webhook URL. The GHL
workflow decides who to email, keyed on `scope` / `store_key`. Nothing here sends
email directly, so no mail credentials live in this service.
"""

from __future__ import annotations

import logging
import os
from datetime import date as date_cls, datetime
from zoneinfo import ZoneInfo

import httpx

from app.db import engine

log = logging.getLogger("esther.daily_report")
CT = ZoneInfo("America/Chicago")

# Kia forwards every caller through one number, so GHL collapses its callers into
# a single contact — its counts must dedup by call, not contact. Same rule as the
# rollup (esther_ingest).
PER_CALL_STORE_KEYS = {"mcgrath_kia_stcharles"}


def _pct(n: float | int, d: float | int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


async def _store_row(pg, store: dict, d: date_cls) -> dict:
    """Metrics for one store-day. Reads the settled figures from the rollup and
    computes the appointment-specific denominator (booking attempts) live."""
    dm = await pg.fetchrow(
        """select total_calls, appointments_booked, eligible_calls, contained_calls,
                  transfers, ai_spend, booking_attempts
           from esther_daily_metrics where store_id=$1 and local_date=$2""",
        store["id"], d,
    )
    total = (dm["total_calls"] if dm else 0) or 0
    booked = (dm["appointments_booked"] if dm else 0) or 0
    eligible = (dm["eligible_calls"] if dm else 0) or 0
    contained = (dm["contained_calls"] if dm else 0) or 0
    transfers = (dm["transfers"] if dm else 0) or 0

    # Appt Intent (booking attempts) = calls where the customer reached a booking
    # DECISION (booked / needed a callback / dropped). Excludes pure info &
    # no-transcript calls — people who weren't trying to book. Read the EVENT-based
    # value straight from the rollup so it shares the same basis as appointments_booked
    # (also events) and matches the dashboard exactly — booked ÷ attempts is then a
    # clean like-for-like ratio, not events over distinct callers. Falls back to a
    # live event count only when the rollup hasn't populated the column yet.
    attempts = dm["booking_attempts"] if dm else None
    if attempts is None:
        attempts = await pg.fetchval(
            """select count(*) from esther_calls
                where store_id=$1 and local_date=$2
                  and (tags is null or not (tags @> array['qa-line']))
                  and outcome in ('booked','callback_needed','dropped')""",
            store["id"], d,
        ) or 0
    recovered = await pg.fetchval(
        "select coalesce(sum(value),0) from esther_recovered_opportunities where store_id=$1 and local_date=$2",
        store["id"], d,
    ) or 0

    # Outcome breakdown — where every call went. Same population as total_calls
    # (excludes qa-line test calls), so booked + dropped + info + callbacks + other
    # sums to Total Calls exactly. This is the "makes sense at a glance" answer to
    # "what happened to the rest of the calls" (Reid) — nothing left to explain.
    oc = await pg.fetch(
        """select coalesce(outcome,'no_summary') o, count(*) n from esther_calls
             where store_id=$1 and local_date=$2
               and (tags is null or not (tags @> array['qa-line']))
             group by 1""",
        store["id"], d,
    )
    om = {r["o"]: r["n"] for r in oc}
    o_booked = om.get("booked", 0)
    o_dropped = om.get("dropped", 0)
    o_callback = om.get("callback_needed", 0)
    o_info = om.get("info_only", 0)
    o_other = max(0, total - (o_booked + o_dropped + o_callback + o_info))  # no-summary / no-transcript
    # transfers that ended in voicemail (a transfer AND a drop) — explains the overlap.
    transfers_voicemail = await pg.fetchval(
        """select count(*) from esther_calls
             where store_id=$1 and local_date=$2
               and (tags is null or not (tags @> array['qa-line']))
               and transferred and department is distinct from 'sales' and outcome='dropped'""",
        store["id"], d,
    ) or 0

    return {
        "store_key": store["key"],
        "store_name": store["name"],
        "total_calls": total,
        "appointments_booked": booked,
        "eligible_calls": eligible,
        "contained_calls": contained,
        "booking_attempts": attempts,
        "transfers": transfers,
        "recovered_value": int(recovered),
        "ai_resolution_rate": _pct(contained, eligible),
        "conversion_overall": _pct(booked, total),
        "conversion_appointment": _pct(booked, attempts),
        # Share of all calls that were transferred to a person (service transfers).
        "transfer_pct": _pct(transfers, total),
        # Outcome breakdown (sums to total_calls) + the transfer→voicemail overlap.
        "o_booked": o_booked,
        "o_dropped": o_dropped,
        "o_callback": o_callback,
        "o_info": o_info,
        "o_other": o_other,
        "transfers_voicemail": transfers_voicemail,
    }


def _metric_cards(m: dict) -> str:
    def card(label, value, sub=""):
        return (
            f'<td style="padding:10px 14px;border:1px solid #e5e7eb;border-radius:10px;'
            f'background:#fff;vertical-align:top">'
            f'<div style="font-size:12px;color:#6b7280">{label}</div>'
            f'<div style="font-size:24px;font-weight:800;color:#111827">{value}</div>'
            f'<div style="font-size:11px;color:#9ca3af">{sub}</div></td>'
        )
    aiRes = "—" if m["ai_resolution_rate"] is None else f'{m["ai_resolution_rate"]}%'
    ov = "—" if m["conversion_overall"] is None else f'{m["conversion_overall"]}%'
    ap = "—" if m["conversion_appointment"] is None else f'{m["conversion_appointment"]}%'
    tpct = "" if m.get("transfer_pct") is None else f'{m["transfer_pct"]}% of calls'
    intent = m.get("booking_attempts", 0)
    qa = m.get("o_info", 0)
    # Row 1 is the funnel in order — Calls -> Appt Intent -> Booked -> Questions
    # Answered — so the denominators are visible (Chris: "a column between Calls and
    # Booked for Appt Intent... that is what Appt% is based on") and info calls read
    # as their own success. Row 2 is the rates + transfers.
    return (
        '<table cellspacing="8" cellpadding="0" style="border-collapse:separate;width:100%"><tr>'
        + card("Total Calls", m["total_calls"])
        + card("Appt Intent", intent, "callers trying to book")
        + card("Appointments Booked", m["appointments_booked"])
        + card("Questions Answered", qa, "info handled by Esther")
        + "</tr><tr>"
        + card("AI Resolution Rate", aiRes, "booked + info + correct transfers ÷ eligible")
        + card("Conversion — Overall", ov, "booked ÷ all calls")
        + card("Conversion — Appointment", ap, "booked ÷ Appt Intent")
        + card("Service Transfers", m["transfers"], tpct)
        + "</tr></table>"
    )


def _store_table(rows: list[dict]) -> str:
    head = (
        '<tr style="background:#f9fafb">'
        + "".join(
            # white-space:nowrap so a header never breaks mid-word ("Call s",
            # "Transfe rs") when the column is tight; a smaller 10px header keeps
            # all 10 columns inside the 640px email width (Reid — headers spilling).
            f'<th style="text-align:{a};white-space:nowrap;padding:6px 6px;font-size:10px;color:#6b7280;border-bottom:1px solid #e5e7eb">{h}</th>'
            # Appt Intent sits between Calls and Booked (Chris); Q Ans = questions answered.
            for h, a in [("Store", "left"), ("Calls", "right"), ("Appt Intent", "right"),
                         ("Booked", "right"), ("Q Ans", "right"),
                         ("AI Res.", "right"), ("Overall", "right"), ("Appt %", "right"),
                         ("Transfers", "right"), ("Transfer %", "right")]
        )
        + "</tr>"
    )
    body = ""
    for m in rows:
        cells = [
            (m["store_name"], "left"),
            (str(m["total_calls"]), "right"),
            (str(m.get("booking_attempts", 0)), "right"),
            (str(m["appointments_booked"]), "right"),
            (str(m.get("o_info", 0)), "right"),
            ("—" if m["ai_resolution_rate"] is None else f'{m["ai_resolution_rate"]}%', "right"),
            ("—" if m["conversion_overall"] is None else f'{m["conversion_overall"]}%', "right"),
            ("—" if m["conversion_appointment"] is None else f'{m["conversion_appointment"]}%', "right"),
            (str(m["transfers"]), "right"),
            ("—" if m.get("transfer_pct") is None else f'{m["transfer_pct"]}%', "right"),
        ]
        body += "<tr>" + "".join(
            # numbers never wrap ("94.2 %"); only the left-aligned store name wraps.
            f'<td style="text-align:{a};{"white-space:nowrap;" if a == "right" else ""}padding:6px 6px;font-size:12px;color:#111827;border-bottom:1px solid #f3f4f6">{v}</td>'
            for v, a in cells
        ) + "</tr>"
    return f'<table style="border-collapse:collapse;width:100%;margin-top:12px">{head}{body}</table>'


def _breakdown(m: dict) -> str:
    """"Where every call went" — chips that add up to Total Calls, grouped into
    HANDLED (a success — booked OR a question answered) vs NEEDS FOLLOW-UP, so a
    question Esther answered counts as a win in its own category (Chris) and the
    report explains itself with no follow-up. Transfers is shown as an overlay tag,
    not a bucket, so nobody subtracts it from the total."""
    total = m["total_calls"]

    def chip(label, n, tone):
        bg, fg, strong = {
            "good": ("#ecfdf5", "#047857", "#065f46"),
            "warn": ("#fff7ed", "#b45309", "#9a3412"),
            "muted": ("#f3f4f6", "#4b5563", "#111827"),
        }[tone]
        return (
            f'<span style="display:inline-block;margin:2px 8px 2px 0;padding:4px 10px;border-radius:999px;'
            f'background:{bg};font-size:12px;color:{fg}"><b style="color:{strong}">{n}</b> {label}</span>'
        )

    def heading(text, color):
        return (f'<div style="font-size:11px;font-weight:700;letter-spacing:.03em;text-transform:uppercase;'
                f'color:{color};margin:10px 0 3px">{text}</div>')

    handled = chip("Booked", m["o_booked"], "good") + chip("Questions answered", m["o_info"], "good")
    followup = chip("Dropped", m["o_dropped"], "warn") + chip("Callbacks", m["o_callback"], "warn")
    if m["o_other"]:
        followup += chip("No summary", m["o_other"], "muted")

    note = ""
    if m.get("transfers"):
        vm = m.get("transfers_voicemail") or 0
        vmtxt = f", {vm} of which reached voicemail" if vm else ""
        note = (
            '<div style="font-size:12px;color:#6b7280;margin-top:10px">'
            f'{m["transfers"]} of these {total} calls were also transferred to a person{vmtxt}. '
            'A transfer is a tag on a call, not a separate call — that is why it overlaps the '
            'outcomes above instead of adding to them.'
            '</div>'
        )
    return (
        '<div style="margin-top:16px;padding:12px 14px;background:#fff;border:1px solid #e5e7eb;border-radius:10px">'
        f'<div style="font-size:13px;font-weight:800;color:#111827">Where the {total} calls went</div>'
        + heading("Handled", "#047857") + f'<div>{handled}</div>'
        + heading("Needs follow-up", "#b45309") + f'<div>{followup}</div>'
        + note + '</div>'
    )


def _store_sections(rows: list[dict]) -> str:
    """Per-store detail blocks, stacked below the group summary so the whole
    group reads as ONE email — scroll down to see each store one by one."""
    out = ('<h3 style="margin:26px 0 2px;font-size:16px;color:#111827">By Store</h3>'
           '<div style="color:#6b7280;font-size:12px;margin-bottom:6px">Each store\'s own numbers</div>')
    for m in rows:
        out += (
            '<div style="margin-top:16px;padding-top:12px;border-top:1px solid #eef2f7">'
            f'<div style="font-weight:700;font-size:14px;color:#111827;margin-bottom:8px">{m["store_name"]}</div>'
            f'{_metric_cards(m)}</div>'
        )
    return out


def _render_html(title: str, d: date_cls, cards: str, extra: str = "") -> str:
    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:640px;margin:0 auto;color:#111827">'
        f'<h2 style="margin:0 0 2px">{title}</h2>'
        f'<div style="color:#6b7280;font-size:13px;margin-bottom:14px">Esther AI · {d.strftime("%A, %B %-d, %Y") if os.name!="nt" else d.strftime("%A, %B %d, %Y")}</div>'
        f'{cards}{extra}'
        f'<div style="color:#9ca3af;font-size:11px;margin-top:16px">Turning every conversation into opportunity. People + AI + More Sales Tomorrow.</div>'
        f'</div>'
    )


async def build_report_payloads(pg, d: date_cls) -> list[dict]:
    """One group payload + one payload per active store."""
    stores = [dict(s) for s in await pg.fetch(
        "select id, key, name from esther_stores where active order by sort_order")]
    store_rows = [await _store_row(pg, s, d) for s in stores]

    # group aggregate from sums (rates recomputed from totals, never an avg of %s)
    g_total = sum(r["total_calls"] for r in store_rows)
    g_booked = sum(r["appointments_booked"] for r in store_rows)
    g_elig = sum(r["eligible_calls"] for r in store_rows)
    g_contained = sum(r["contained_calls"] for r in store_rows)
    g_attempts = sum(r["booking_attempts"] for r in store_rows)
    g_transfers = sum(r["transfers"] for r in store_rows)
    g_recovered = sum(r["recovered_value"] for r in store_rows)
    group = {
        "store_key": "group", "store_name": "All Stores",
        "total_calls": g_total, "appointments_booked": g_booked,
        "transfers": g_transfers, "recovered_value": g_recovered,
        "ai_resolution_rate": _pct(g_contained, g_elig),
        "conversion_overall": _pct(g_booked, g_total),
        "conversion_appointment": _pct(g_booked, g_attempts),
        "transfer_pct": _pct(g_transfers, g_total),
        "booking_attempts": g_attempts,
        "o_booked": sum(r["o_booked"] for r in store_rows),
        "o_dropped": sum(r["o_dropped"] for r in store_rows),
        "o_callback": sum(r["o_callback"] for r in store_rows),
        "o_info": sum(r["o_info"] for r in store_rows),
        "o_other": sum(r["o_other"] for r in store_rows),
        "transfers_voicemail": sum(r["transfers_voicemail"] for r in store_rows),
    }

    payloads: list[dict] = []
    payloads.append({
        "scope": "group",
        "store_key": "group",
        "store_name": "All Stores",
        "date": d.isoformat(),
        "subject": f"Esther Daily Report — All Stores — {d.isoformat()}",
        "metrics": group,
        # ONE email for the whole group: summary cards + the all-stores table.
        # (Per-store detail is available as separate payloads / a stacked section,
        # kept off for now — Reid only wants the group email at this point.)
        "html": _render_html("Esther Daily Report — All Stores", d,
                             _metric_cards(group), _breakdown(group) + _store_table(store_rows)),
    })
    for r in store_rows:
        payloads.append({
            "scope": "store",
            "store_key": r["store_key"],
            "store_name": r["store_name"],
            "date": d.isoformat(),
            "subject": f"Esther Daily Report — {r['store_name']} — {d.isoformat()}",
            "metrics": r,
            "html": _render_html(f"Esther Daily Report — {r['store_name']}", d, _metric_cards(r) + _breakdown(r)),
        })
    return payloads


async def send_daily_reports(d: date_cls | None = None, webhook: str | None = None) -> dict:
    """Build the reports and POST each to the GHL inbound webhook. Returns a
    summary. Safe to call manually (endpoint) or from the scheduler."""
    webhook = webhook or os.getenv("ESTHER_DAILY_REPORT_WEBHOOK", "")
    d = d or datetime.now(CT).date()
    async with engine.begin() as conn:
        pg = (await conn.get_raw_connection()).driver_connection
        payloads = await build_report_payloads(pg, d)

    # Default: send ONE combined email (the group payload already contains a
    # per-store section for each store). Set ESTHER_DAILY_REPORT_PER_STORE=true to
    # ALSO push the individual per-store payloads (separate emails per dealership).
    include_per_store = os.getenv("ESTHER_DAILY_REPORT_PER_STORE", "").lower() in ("1", "true", "yes")
    if not include_per_store:
        payloads = [p for p in payloads if p["scope"] == "group"]

    if not webhook:
        log.warning("daily report: ESTHER_DAILY_REPORT_WEBHOOK not set — built %d reports, sent 0", len(payloads))
        return {"built": len(payloads), "sent": 0, "webhook_configured": False,
                "payloads": payloads}

    sent = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for p in payloads:
            try:
                r = await client.post(webhook, json=p)
                if r.status_code < 400:
                    sent += 1
                else:
                    log.warning("daily report POST %s -> %s", p["store_key"], r.status_code)
            except Exception:
                log.exception("daily report POST failed for %s", p["store_key"])
    return {"built": len(payloads), "sent": sent, "webhook_configured": True}
