"""Align the technician roster to the owner's Tech Settings mockup.

Renames the eight seeded techs IN PLACE (their ids are preserved, so every
assignment, RO-history row and scoreboard metric stays linked) and adds the two
extra techs from the mockup as recent hires. Also seeds the roster fields
($/hr, cert badges, bio status/labels) and two pending bio updates so the
Bio-Approval workflow has real rows to act on.

skill_level stays a SKILL_RANKS value (scoring depends on it) and team stays
"Main"/"Lube" (team separation depends on it); the mockup's role/team labels are
derived for display in the roster endpoint.
"""

import asyncio
from datetime import time

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Dealer, Technician

DEALER_NAME_HINT = None  # first dealer

EMILY_BIO = {
    "kind": "Quarterly bio review",
    "submitted_label": "Jun 22, 2026",
    "ase_current": ["A1", "A4", "A5", "A8"],
    "ase_added": [{"code": "A6", "label": "Electrical / Electronic Systems", "attachment": "ASE_A6_EmilyS.pdf"}],
    "honda_training": {"label": "Hybrid / EV Level 1 — completed Jun 12, 2026", "attachment": "Honda_HV1_cert.pdf"},
    "self_ratings": [
        {"label": "Diagnostics", "from": 3, "to": 4},
        {"label": "Electrical", "from": 3, "to": 4},
    ],
    "career_goal": "Reach Master Tech within 18 months — wants more diag & electrical work",
    "impact": "Approving raises Emily's Bio Baseline and unlocks eligibility for HV jobs (once HV cert verified). Job-fit for diagnostics/electrical ROs will increase.",
    "cert_proof": "2 of 2",
}

JASON_BIO = {
    "kind": "Added: Honda Maintenance Express certification · updated tools list",
    "submitted_label": "Jun 21, 2026",
    "ase_current": ["A1", "A4", "A5"],
    "ase_added": [],
    "honda_training": {"label": "Honda Maintenance Express — completed Jun 18, 2026", "attachment": "Honda_MaintExpress_JasonK.pdf"},
    "self_ratings": [],
    "career_goal": "Build toward diagnostic and drivability work",
    "impact": "Approving adds the Maintenance Express certification to Jason's bio and gives a small Bio Baseline increase.",
    "cert_proof": "1 of 1",
}

# current name -> (new name, hourly_rate, cert_badges, bio_status, reviewed_label, submitted_label, pending_bio)
RENAMES = {
    "Dave Kowalski":  ("Mark W.",  55, ["Honda Master", "ASE Master", "HV", "ADAS"], "approved", "Apr 2026", None, None),
    "Mike Hernandez": ("Mike H.",  48, ["Honda Master", "ASE Master", "HV", "ADAS"], "approved", "Apr 2026", None, None),
    "Reid Richards":  ("Rob T.",   42, ["Honda Master", "ASE Master", "Align"],      "approved", "Apr 2026", None, None),
    "Emily Chen":     ("Emily S.", 35, ["Pro-Tech T3", "ASE x4"],                    "pending",  None, "Jun 22, 2026", EMILY_BIO),
    "Josh Barrett":   ("Jason K.", 28, ["Pro-Tech T2", "ASE x3"],                    "pending",  None, "Jun 21, 2026", JASON_BIO),
    "Tara Nguyen":    ("Sam W.",   22, ["Pro-Tech T1"],                              "approved", "May 2026", None, None),
    "Chris McGrath":  ("Ethan D.", 18, [],                                           "approved", "May 2026", None, None),
    "Shafique":       ("Todd R.",  17, [],                                           "approved", "May 2026", None, None),
}

# new techs (name, skill_level, team, hourly_rate, cert_badges, reviewed_label, dms_tech_no)
NEW = [
    ("Steven M.", "Master", "Main", 45, ["Honda Master", "ASE Master"], "Apr 2026", "T109"),
    ("Amanda L.", "Master", "Main", 46, ["Honda Master", "ASE Master", "HV", "Align"], "Jun 2026 (hire)", "T110"),
]


async def main():
    async with SessionLocal() as s:
        dealer = (await s.execute(select(Dealer))).scalars().first()
        did = dealer.id

        techs = list((await s.execute(select(Technician).where(Technician.dealer_id == did))).scalars())
        by_name = {t.name: t for t in techs}

        renamed = 0
        for old, (new, rate, badges, status, reviewed, submitted, bio) in RENAMES.items():
            t = by_name.get(old)
            if t is None:
                print(f"  ! not found: {old}")
                continue
            t.name = new
            t.hourly_rate = rate
            t.cert_badges = badges
            t.bio_status = status
            t.bio_reviewed_label = reviewed
            t.bio_submitted_label = submitted
            t.pending_bio = bio
            renamed += 1

        added = 0
        for name, level, team, rate, badges, reviewed, dms in NEW:
            if name in by_name:
                continue
            t = Technician(
                dealer_id=did, name=name, dms_tech_no=dms, team=team, skill_level=level, active=True,
                shift_start=time(7, 0), shift_end=time(16, 0),
                work_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
                lunch_start=time(12, 0), lunch_end=time(12, 30),
                max_daily_hours=9, efficiency_target=115, productivity_target=85,
                hourly_rate=rate, cert_badges=badges, bio_status="approved", bio_reviewed_label=reviewed,
            )
            s.add(t)
            added += 1

        await s.commit()
        total = len(list((await s.execute(select(Technician).where(
            Technician.dealer_id == did, Technician.active.is_(True)))).scalars()))
        print(f"  renamed {renamed}, added {added}, active roster now {total}")


if __name__ == "__main__":
    asyncio.run(main())
