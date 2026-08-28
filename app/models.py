"""SQLAlchemy models — mirrors supabase/migrations/0001_init.sql.

Every tenant table carries dealer_id.  There is no exception, and there is no
table you can reach without going through one.
"""

from __future__ import annotations

import copy
import uuid
from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, JSONBType, StringArray, UTCDateTime, utcnow


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Dealer(Base):
    __tablename__ = "dealers"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    # Stable human-readable store key (aka dealer_key / store_id). Matches the
    # connector's keys, e.g. "mcgrath_honda_stcharles". Adding a store = adding a
    # dealer row with a new key + its myKaarma creds — never a new deployment.
    dealer_key: Mapped[Optional[str]] = mapped_column(Text, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, default="America/Chicago")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


DEFAULT_MATCH_WEIGHTS = {
    "skill": 25,
    "familiarity": 25,
    "performance": 20,
    "availability": 20,
    "workload": 10,
    "specialty_bonus": 3,
}

# Operational shop config that isn't a dedicated column yet — hours, days, bay
# count, technician levels, teams, and dashboard display preferences. Kept as a
# single JSON blob so the Store Settings page can persist the whole form without
# a table per section. Everything here is real and reloads on refresh.
DEFAULT_STORE_CONFIG = {
    "hours": {"open": "07:00", "close": "19:00"},
    "days_open": [True, True, True, True, True, True, False],  # Mon..Sun
    "bays": 18,
    "tech_levels": [
        {"name": "L1 Apprentice", "set": "Main Shop", "bio_points": 330, "floor": 25, "cap": 65},
        {"name": "L3 Apprentice", "set": "Main Shop", "bio_points": 370, "floor": 28, "cap": 74},
        {"name": "L5 Apprentice", "set": "Main Shop", "bio_points": 410, "floor": 30, "cap": 82},
        {"name": "Master", "set": "Main Shop", "bio_points": 455, "floor": 35, "cap": 90},
        {"name": "Senior Master", "set": "Main Shop", "bio_points": 500, "floor": 38, "cap": 95},
        {"name": "Lube Tech", "set": "Lube", "bio_points": 380, "floor": 35, "cap": 85},
        {"name": "Senior Lube", "set": "Lube", "bio_points": 420, "floor": 38, "cap": 92},
    ],
    "teams": [
        {"name": "Main Shop", "color": "blue"},
        {"name": "Lube Team", "color": "amber"},
    ],
    "dashboard": {
        "tech_sort": "priority_team",
        "time_window": "full_day",
        "finishing_soon_min": 20,
        "group_by_team": True,
        "show_off_shift": False,
    },
}


# Scoreboard Settings: which metrics show on the TV boards, their goals + data
# source, the display/rotation prefs, and the facility-utilization goal. Stored
# as one JSON blob; the endpoint fills these defaults when the column is empty.
def _metric(name, source, goal, show):
    return {"name": name, "source": source, "goal": goal, "show": show}


DEFAULT_SCOREBOARD_CONFIG = {
    "display": {
        "rotate_every_sec": 12,
        "pause_resume_min": 30,
        "rows_per_page": 14,
        "default_rank_advisors": "CSI",
        "default_rank_techs": "Eff %",
        "facility_utilization_goal": 80,
    },
    "advisor_metrics": [
        _metric("CSI", "MANUAL", "90%", True),
        _metric("CP ROs", "MYKAARMA", "110", True),
        _metric("Sales / RO", "DMS", "$260", True),
        _metric("Recs / RO", "MYKAARMA", "2.8", True),
        _metric("Hrs/RO vs Rec", "MYKAARMA", "1.00", True),
        _metric("Video Sent", "MYKAARMA", "70%", True),
        _metric("Survey Response %", "MANUAL", "35%", False),
        _metric("Effective Labor Rate", "DMS", "$210", False),
        _metric("Parts / RO", "DMS", "$170", False),
        _metric("CP Sales", "DMS", "$40k", False),
        _metric("Declined $ Recovered", "MYKAARMA", "15%", False),
        _metric("2-Way Text Response", "MYKAARMA", "60%", False),
        _metric("Appointment Show %", "DMS", "85%", False),
        _metric("Tire Units", "DMS", "20", False),
        _metric("Avg RO $", "DMS", "$480", False),
    ],
    "tech_metrics": [
        _metric("Total Hours", "DMS", "150", True),
        _metric("Hours (Week)", "DMS", "38", True),
        _metric("RO's", "DMS", "80", True),
        _metric("Hrs / RO", "DMS", "—", True),
        _metric("Eff %", "DMS", "100%", True),
        _metric("Fail Rec Closing %", "MYKAARMA", "25%", True),
        _metric("Comeback Rate", "DMS", "3%", False),
        _metric("Fixed Right First Time", "DMS", "90%", False),
        _metric("Upsold Hours", "DMS", "12", False),
        _metric("Proficiency %", "3D MATCH", "90%", False),
        _metric("Hrs / Day", "DMS", "8.0", False),
        _metric("Rework Hours", "DMS", "2.0", False),
        _metric("MPI Completion %", "MYKAARMA", "95%", False),
        _metric("Diag Accuracy %", "3D MATCH", "90%", False),
    ],
}


class DealerSettings(Base):
    __tablename__ = "dealer_settings"
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), primary_key=True
    )
    match_weights: Mapped[dict] = mapped_column(JSONBType(), default=lambda: dict(DEFAULT_MATCH_WEIGHTS))
    store_config: Mapped[dict] = mapped_column(JSONBType(), default=lambda: copy.deepcopy(DEFAULT_STORE_CONFIG))
    scoreboard_config: Mapped[dict] = mapped_column(JSONBType(), default=lambda: copy.deepcopy(DEFAULT_SCOREBOARD_CONFIG))
    enforce_team_separation: Mapped[bool] = mapped_column(Boolean, default=True)
    min_ros_to_rank: Mapped[int] = mapped_column(Integer, default=10)
    min_flagged_hours_to_rank: Mapped[float] = mapped_column(Numeric, default=15)
    comeback_window_days: Mapped[int] = mapped_column(Integer, default=30)
    advisor_csi_min_surveys: Mapped[int] = mapped_column(Integer, default=5)
    data_staleness_hours: Mapped[int] = mapped_column(Integer, default=48)
    default_top_n: Mapped[int] = mapped_column(Integer, default=3)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class UserProfile(Base):
    __tablename__ = "user_profiles"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[Optional[str]] = mapped_column(Text)
    full_name: Mapped[Optional[str]] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, default="DISPATCHER")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ======================= TECHNICIANS ======================================= #


class Technician(Base):
    __tablename__ = "technicians"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    employee_id: Mapped[Optional[str]] = mapped_column(Text)
    dms_tech_no: Mapped[Optional[str]] = mapped_column(Text)
    # Contact for instant assignment notification (SMS via GHL, email fallback).
    phone: Mapped[Optional[str]] = mapped_column(Text)
    email: Mapped[Optional[str]] = mapped_column(Text)
    # GHL contact id, cached after the first upsert so the tech's assignment
    # texts thread into one conversation in GHL instead of creating duplicates.
    ghl_contact_id: Mapped[Optional[str]] = mapped_column(Text)
    team: Mapped[Optional[str]] = mapped_column(Text)
    skill_level: Mapped[Optional[str]] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Tech Settings roster + bio-approval workflow
    hourly_rate: Mapped[Optional[float]] = mapped_column(Numeric)      # $/hr on the roster
    cert_badges: Mapped[list[str]] = mapped_column(StringArray, default=list)  # roster badge labels
    bio_status: Mapped[str] = mapped_column(Text, default="approved")  # approved | pending
    bio_reviewed_label: Mapped[Optional[str]] = mapped_column(Text)    # "Apr 2026", "Jun 2026 (hire)"
    bio_submitted_label: Mapped[Optional[str]] = mapped_column(Text)   # "Jun 22, 2026"
    pending_bio: Mapped[Optional[dict]] = mapped_column(JSONBType())   # the submitted update card, or null

    shift_start: Mapped[Optional[time]] = mapped_column(Time)
    shift_end: Mapped[Optional[time]] = mapped_column(Time)
    work_days: Mapped[list[str]] = mapped_column(StringArray, default=list)
    lunch_start: Mapped[Optional[time]] = mapped_column(Time)
    lunch_end: Mapped[Optional[time]] = mapped_column(Time)

    max_daily_hours: Mapped[Optional[float]] = mapped_column(Numeric)
    overtime_threshold: Mapped[Optional[float]] = mapped_column(Numeric)
    efficiency_target: Mapped[Optional[float]] = mapped_column(Numeric)
    productivity_target: Mapped[Optional[float]] = mapped_column(Numeric)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    certs: Mapped[list["TechnicianCert"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    restrictions: Mapped[list["TechnicianRestriction"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    specialties: Mapped[list["TechnicianSpecialty"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )

    # FR-1.6 — the onboarding form must show what is still missing.
    REQUIRED_FOR_DISPATCH = [
        ("name", "Name"),
        ("dms_tech_no", "DMS tech #"),
        ("team", "Team"),
        ("skill_level", "Skill level"),
        ("shift_start", "Shift start"),
        ("shift_end", "Shift end"),
        ("work_days", "Work days"),
        ("max_daily_hours", "Max daily hours"),
    ]

    def missing_fields(self) -> list[str]:
        missing = []
        for attr, label in self.REQUIRED_FOR_DISPATCH:
            value = getattr(self, attr, None)
            if value is None or value == "" or value == []:
                missing.append(label)
        return missing

    def completeness_pct(self) -> int:
        total = len(self.REQUIRED_FOR_DISPATCH)
        return int(round((total - len(self.missing_fields())) / total * 100))


class TechnicianCert(Base):
    __tablename__ = "technician_certs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("dealers.id", ondelete="CASCADE"))
    technician_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("technicians.id", ondelete="CASCADE"), index=True
    )
    cert_type: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[Optional[str]] = mapped_column(Text)
    expires_on: Mapped[Optional[date]] = mapped_column(Date)


class TechnicianRestriction(Base):
    __tablename__ = "technician_restrictions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("dealers.id", ondelete="CASCADE"))
    technician_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("technicians.id", ondelete="CASCADE"), index=True
    )
    blocked_work_type: Mapped[str] = mapped_column(Text, nullable=False)


class TechnicianSpecialty(Base):
    __tablename__ = "technician_specialties"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("dealers.id", ondelete="CASCADE"))
    technician_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("technicians.id", ondelete="CASCADE"), index=True
    )
    work_type: Mapped[Optional[str]] = mapped_column(Text)
    vehicle_specialty: Mapped[Optional[str]] = mapped_column(Text)


# ===================== REPAIR ORDERS (live) ================================ #


class RepairOrder(Base):
    __tablename__ = "repair_orders"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ro_number: Mapped[str] = mapped_column(Text, nullable=False)
    vin: Mapped[Optional[str]] = mapped_column(Text)
    vehicle_year: Mapped[Optional[int]] = mapped_column(Integer)
    vehicle_make: Mapped[Optional[str]] = mapped_column(Text)
    vehicle_model: Mapped[Optional[str]] = mapped_column(Text)
    mileage: Mapped[Optional[int]] = mapped_column(Integer)

    concern_category: Mapped[Optional[str]] = mapped_column(Text)
    work_type: Mapped[Optional[str]] = mapped_column(Text)
    tier: Mapped[Optional[str]] = mapped_column(String(1))
    required_certs: Mapped[list[str]] = mapped_column(StringArray, default=list)
    required_team: Mapped[Optional[str]] = mapped_column(Text)
    est_hours: Mapped[float] = mapped_column(Numeric, default=0)

    written_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    promise_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)

    status: Mapped[str] = mapped_column(Text, default="OPEN", index=True)
    flags: Mapped[list[str]] = mapped_column(StringArray, default=list)
    priority: Mapped[str] = mapped_column(Text, default="MEDIUM")
    advisor_id: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    lines: Mapped[list["ROLine"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="ROLine.sort_order"
    )

    # Flagged = customer waiting, heat case, comeback, or manager flag.
    FLAG_SET = {"MGR_FLAG", "HEAT_CASE", "COMEBACK", "WAITING"}

    @property
    def is_flagged(self) -> bool:
        return bool(set(self.flags or []) & self.FLAG_SET)


class ROLine(Base):
    __tablename__ = "ro_lines"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("dealers.id", ondelete="CASCADE"))
    ro_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repair_orders.id", ondelete="CASCADE"), index=True
    )
    op_code: Mapped[Optional[str]] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    flagged_hours: Mapped[float] = mapped_column(Numeric, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


# ============================ HISTORY ====================================== #


class ROHistory(Base):
    __tablename__ = "ro_history"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    import_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    ro_number: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    closed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, index=True)
    dms_tech_no: Mapped[Optional[str]] = mapped_column(Text)
    technician_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("technicians.id", ondelete="SET NULL"), index=True
    )
    advisor_id: Mapped[Optional[str]] = mapped_column(Text)
    op_code: Mapped[Optional[str]] = mapped_column(Text)
    concern_category: Mapped[Optional[str]] = mapped_column(Text)
    flagged_hours: Mapped[float] = mapped_column(Numeric, default=0)
    actual_clocked_hours: Mapped[float] = mapped_column(Numeric, default=0)
    labor_type: Mapped[Optional[str]] = mapped_column(Text)
    promise_time: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    vin: Mapped[Optional[str]] = mapped_column(Text)
    vehicle_ymm: Mapped[Optional[str]] = mapped_column(Text)
    excluded_from_metrics: Mapped[bool] = mapped_column(Boolean, default=False)
    exclusion_reason: Mapped[Optional[str]] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TimeClockDay(Base):
    __tablename__ = "time_clock_days"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("dealers.id", ondelete="CASCADE"))
    import_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    technician_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("technicians.id", ondelete="CASCADE")
    )
    dms_tech_no: Mapped[Optional[str]] = mapped_column(Text)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_clocked_hours: Mapped[float] = mapped_column(Numeric, default=0)
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OpCodeMap(Base):
    __tablename__ = "op_code_map"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), index=True
    )
    op_code: Mapped[str] = mapped_column(Text, nullable=False)
    concern_category: Mapped[str] = mapped_column(Text, nullable=False)
    work_type: Mapped[Optional[str]] = mapped_column(Text)
    tier: Mapped[Optional[str]] = mapped_column(String(1))
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    exclusion_reason: Mapped[Optional[str]] = mapped_column(Text)
    mykaarma_uuid: Mapped[Optional[str]] = mapped_column(Text)
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text, default="MANUAL")  # MANUAL | CSV | MYKAARMA
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MyKaarmaDealer(Base):
    __tablename__ = "mykaarma_dealers"
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), primary_key=True
    )
    username: Mapped[str] = mapped_column(Text, nullable=False)
    password: Mapped[str] = mapped_column(Text, nullable=False)
    dealer_uuid: Mapped[str] = mapped_column(Text, nullable=False)
    department_uuid: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ro_scope_granted: Mapped[bool] = mapped_column(Boolean, default=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    last_sync_status: Mapped[Optional[str]] = mapped_column(Text)
    last_sync_detail: Mapped[dict] = mapped_column(JSONBType(), default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class GHLDealer(Base):
    """Per-store GoHighLevel credentials. Each McGrath store is its own GHL
    sub-account (Location) with its own Private Integration Token, warranty
    custom object, and webhook secret. This lets a Honda audit write back into
    Honda's GHL and never into Acura's — one row per store, keyed by dealer."""

    __tablename__ = "ghl_dealers"
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), primary_key=True
    )
    api_key: Mapped[str] = mapped_column(Text, nullable=False)          # Private Integration Token (pit-...)
    location_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    object_key: Mapped[str] = mapped_column(Text, default="custom_objects.warranty_ro_audits")
    object_id: Mapped[Optional[str]] = mapped_column(Text)
    webhook_secret: Mapped[Optional[str]] = mapped_column(Text)          # shared secret GHL sends on the upload webhook
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ImportRun(Base):
    __tablename__ = "import_runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(Text, default="DMS_RO_HISTORY")
    filename: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="PENDING")
    column_mapping: Mapped[dict] = mapped_column(JSONBType(), default=dict)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    rejects: Mapped[list] = mapped_column(JSONBType(), default=list)
    unmatched_tech_nos: Mapped[list[str]] = mapped_column(StringArray, default=list)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)


class ComebackPairRow(Base):
    __tablename__ = "comeback_pairs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), index=True
    )
    vin: Mapped[str] = mapped_column(Text, nullable=False)
    concern_category: Mapped[str] = mapped_column(Text, nullable=False)
    original_ro_number: Mapped[str] = mapped_column(Text, nullable=False)
    original_closed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    original_tech_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("technicians.id", ondelete="SET NULL"), index=True
    )
    repeat_ro_number: Mapped[str] = mapped_column(Text, nullable=False)
    repeat_opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    days_between: Mapped[float] = mapped_column(Numeric)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# =========================== ASSIGNMENT ==================================== #


class Assignment(Base):
    __tablename__ = "assignments"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), index=True
    )
    ro_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repair_orders.id", ondelete="CASCADE"), index=True
    )
    technician_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("technicians.id", ondelete="RESTRICT"), index=True
    )

    # Frozen at dispatch time. This is the audit trail: the decision is
    # explainable forever, even after the tech's stats have moved on.
    match_score: Mapped[Optional[float]] = mapped_column(Numeric)
    score_reasons: Mapped[list] = mapped_column(JSONBType(), default=list)
    score_warnings: Mapped[list] = mapped_column(JSONBType(), default=list)
    score_confident: Mapped[bool] = mapped_column(Boolean, default=True)
    recommended_rank: Mapped[Optional[int]] = mapped_column(Integer)
    was_ai_recommendation: Mapped[bool] = mapped_column(Boolean, default=True)
    override_reason: Mapped[Optional[str]] = mapped_column(Text)
    engine_version: Mapped[Optional[str]] = mapped_column(Text)
    weights_used: Mapped[dict] = mapped_column(JSONBType(), default=dict)

    assigned_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    assigned_by: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)

    # Instant tech notification (SMS via GHL, email fallback). Tracked so a
    # failed text surfaces on the board instead of a job sitting unacknowledged.
    notify_channel: Mapped[Optional[str]] = mapped_column(Text)   # SMS | EMAIL
    notify_status: Mapped[Optional[str]] = mapped_column(Text)    # queued | sent | delivered | failed
    notified_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    notify_error: Mapped[Optional[str]] = mapped_column(Text)
    notify_ref: Mapped[Optional[str]] = mapped_column(Text)       # GHL message / conversation id


# ======================== COMPUTED METRICS ================================= #


class TechMetrics(Base):
    __tablename__ = "tech_metrics"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("dealers.id", ondelete="CASCADE"))
    technician_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("technicians.id", ondelete="CASCADE")
    )
    period: Mapped[str] = mapped_column(Text)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)

    efficiency: Mapped[Optional[float]] = mapped_column(Numeric)
    productivity: Mapped[Optional[float]] = mapped_column(Numeric)
    utilization: Mapped[Optional[float]] = mapped_column(Numeric)
    promise_pct: Mapped[Optional[float]] = mapped_column(Numeric)
    comeback_rate: Mapped[Optional[float]] = mapped_column(Numeric)
    first_time_fix: Mapped[Optional[float]] = mapped_column(Numeric)

    ro_count: Mapped[int] = mapped_column(Integer, default=0)
    flagged_hours: Mapped[float] = mapped_column(Numeric, default=0)
    clocked_hours: Mapped[float] = mapped_column(Numeric, default=0)
    cp_flagged_hours: Mapped[float] = mapped_column(Numeric, default=0)
    warranty_flagged_hours: Mapped[float] = mapped_column(Numeric, default=0)
    internal_flagged_hours: Mapped[float] = mapped_column(Numeric, default=0)

    qualifies_for_ranking: Mapped[bool] = mapped_column(Boolean, default=False)
    data_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    data_issues: Mapped[list[str]] = mapped_column(StringArray, default=list)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TechCategoryFamiliarity(Base):
    __tablename__ = "tech_category_familiarity"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), index=True
    )
    technician_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("technicians.id", ondelete="CASCADE"), index=True
    )
    concern_category: Mapped[str] = mapped_column(Text, nullable=False)
    repairs_completed: Mapped[int] = mapped_column(Integer, default=0)
    flagged_hours: Mapped[float] = mapped_column(Numeric, default=0)
    clocked_hours: Mapped[float] = mapped_column(Numeric, default=0)
    avg_efficiency: Mapped[Optional[float]] = mapped_column(Numeric)
    first_time_fix: Mapped[Optional[float]] = mapped_column(Numeric)
    last_performed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AdvisorScore(Base):
    """Advisor performance for the Service Scoreboard (advisor board).

    V1 has no live advisor/CSI data source, so these rows are seeded for the
    demo. Kept as real DB rows (not UI mock) so the board renders from data and
    a real CSI/DMS feed can later replace the seed.
    """

    __tablename__ = "advisor_scores"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    csi: Mapped[Optional[float]] = mapped_column(Numeric)              # % (CSI out of 5 × 20)
    survey_responses: Mapped[Optional[int]] = mapped_column(Integer)   # count, manual entry
    survey_response_pct: Mapped[Optional[float]] = mapped_column(Numeric)  # %, manual entry
    cp_ros: Mapped[Optional[int]] = mapped_column(Integer)            # customer-pay RO count
    sales_ro: Mapped[Optional[float]] = mapped_column(Numeric)        # $ sales per RO (today)
    sales_yest: Mapped[Optional[float]] = mapped_column(Numeric)
    sales_lmo: Mapped[Optional[float]] = mapped_column(Numeric)       # last month
    sales_pmo: Mapped[Optional[float]] = mapped_column(Numeric)       # prior month
    recs_ro: Mapped[Optional[float]] = mapped_column(Numeric)         # recommendations per RO
    hrs_vs_rec: Mapped[Optional[float]] = mapped_column(Numeric)      # hours vs recommended
    video_sent: Mapped[Optional[float]] = mapped_column(Numeric)      # % ROs with a video
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ======================= WARRANTY RO AUDIT ================================= #

# The 12-check warranty documentation rubric (Honda-based, configurable).
# `key`  -> stable id + GHL single-option field key.
# `name` -> exact check name Claude must echo back.
# `needs`-> what the check requires (drives the Guardian "needs_review" default).
# Stored per-dealer in warranty_rubrics; this is the seed the app falls back to.
DEFAULT_WARRANTY_RUBRIC = [
    {"key": "complaintcausecorrection", "name": "Complaint-Cause-Correction",
     "needs": "All three C's — Complaint, Cause, and Correction — present and complete.",
     "dom_section": "Job / concern lines"},
    {"key": "rovin_documentation", "name": "RO/VIN Documentation",
     "needs": "RO number and full 17-char VIN present, legible, and matching the vehicle.",
     "dom_section": "RO header"},
    {"key": "mileage__authorization", "name": "Mileage & Authorization",
     "needs": "Mileage recorded and customer authorization present.",
     "dom_section": "RO header / signature"},
    {"key": "technician_id_match", "name": "Technician ID Match",
     "needs": "The tech who did the work is identified and matches the claim.",
     "dom_section": "Job line tech"},
    {"key": "labor_ops__hours", "name": "Labor Ops & Hours",
     "needs": "Labor op codes and claimed hours documented and valid.",
     "dom_section": "Job / labor lines"},
    {"key": "punch_records_vs_claimed_time", "name": "Punch Records vs Claimed Time",
     "needs": "Actual punch/clock time supports the claimed labor time.",
     "dom_section": "Time punch records"},
    {"key": "parts_documentation", "name": "Parts Documentation",
     "needs": "Parts listed with part numbers/quantities; defective parts noted if required.",
     "dom_section": "Parts lines"},
    {"key": "dtc__ihds_documentation", "name": "DTC / i-HDS Documentation",
     "needs": "Diagnostic codes / i-HDS session documented where applicable.",
     "dom_section": "Diagnostic notes"},
    {"key": "tech_line_paperwork", "name": "Tech Line Paperwork",
     "needs": "Tech Line case number/authorization present if Tech Line was used.",
     "dom_section": "Tech Line reference"},
    {"key": "battery_tester_documentation", "name": "Battery Tester Documentation",
     "needs": "Battery/charging test printout for battery claims.",
     "dom_section": "Battery test printout"},
    {"key": "sublet_documentation", "name": "Sublet Documentation",
     "needs": "Sublet invoices attached and documented for sublet work.",
     "dom_section": "Sublet lines"},
    {"key": "straighttimedpsm_authorization", "name": "Straight-Time/DPSM Authorization",
     "needs": "DPSM or straight-time authorization present where required.",
     "dom_section": "DPSM authorization"},
]

WARRANTY_RESULTS = ("pass", "needs_review", "fail", "na")
WARRANTY_STATUSES = ("pending", "pass", "needs_review", "fail")


class WarrantyROAudit(Base):
    __tablename__ = "warranty_ro_audits"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ro_number: Mapped[str] = mapped_column(Text, nullable=False)
    vin: Mapped[Optional[str]] = mapped_column(Text)
    technician_id: Mapped[Optional[str]] = mapped_column(Text)
    job_line_type: Mapped[Optional[str]] = mapped_column(Text)
    source_ro_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("repair_orders.id", ondelete="SET NULL")
    )
    audit_status: Mapped[str] = mapped_column(Text, default="pending", index=True)
    findings: Mapped[list] = mapped_column(JSONBType(), default=list)
    reviewer_decision: Mapped[str] = mapped_column(Text, default="not_reviewed")
    reviewer_notes: Mapped[Optional[str]] = mapped_column(Text)
    submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    date_submitted: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class WarrantyRubric(Base):
    __tablename__ = "warranty_rubrics"
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), primary_key=True
    )
    checks: Mapped[list] = mapped_column(JSONBType(), default=lambda: copy.deepcopy(DEFAULT_WARRANTY_RUBRIC))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), index=True
    )
    actor: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    payload: Mapped[dict] = mapped_column(JSONBType(), default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
