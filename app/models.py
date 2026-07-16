"""SQLAlchemy models — mirrors supabase/migrations/0001_init.sql.

Every tenant table carries dealer_id.  There is no exception, and there is no
table you can reach without going through one.
"""

from __future__ import annotations

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


class DealerSettings(Base):
    __tablename__ = "dealer_settings"
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dealers.id", ondelete="CASCADE"), primary_key=True
    )
    match_weights: Mapped[dict] = mapped_column(JSONBType(), default=lambda: dict(DEFAULT_MATCH_WEIGHTS))
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
    team: Mapped[Optional[str]] = mapped_column(Text)
    skill_level: Mapped[Optional[str]] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


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
