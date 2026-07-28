"""Per-dealer configuration.

NFR-6: weights, thresholds and gates are editable WITHOUT a redeploy.  A store
that cares most about promise times turns availability up here, and the next
board refresh scores differently.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from .. import audit
from ..deps import CurrentUserDep, SessionDep, get_dealer_settings
from ..models import DEFAULT_STORE_CONFIG, Dealer, Technician

router = APIRouter(prefix="/dealer", tags=["dealer"])

# The Store Settings page shows the owner's names for the six factors; the engine
# keys stay exactly as the deterministic scorer expects. This is the whole bridge
# between the two — dragging "Job Fit" up really does raise the `skill` weight.
WEIGHT_LABEL_TO_KEY = {
    "bio_baseline": "familiarity",
    "tech_quality": "performance",
    "job_fit": "skill",
    "availability": "availability",
    "pay_pacing": "workload",
    "cost_efficiency": "specialty_bonus",
}


def weights_to_labels(mw: dict) -> dict:
    """engine keys -> the Store Settings slider labels."""
    return {label: float(mw.get(key, 0)) for label, key in WEIGHT_LABEL_TO_KEY.items()}


def labels_to_weights(labels: dict) -> dict:
    """Store Settings slider labels -> engine keys (what the scorer reads)."""
    return {key: float(labels.get(label, 0)) for label, key in WEIGHT_LABEL_TO_KEY.items()}


class MatchWeightsIn(BaseModel):
    skill: float = 25
    familiarity: float = 25
    performance: float = 20
    availability: float = 20
    workload: float = 10
    specialty_bonus: float = 3

    @field_validator("skill", "familiarity", "performance", "availability", "workload")
    @classmethod
    def non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("weights cannot be negative")
        return v

    def base_total(self) -> float:
        return self.skill + self.familiarity + self.performance + self.availability + self.workload


class SettingsIn(BaseModel):
    match_weights: MatchWeightsIn
    enforce_team_separation: bool = True
    min_ros_to_rank: int = Field(default=10, ge=0)
    min_flagged_hours_to_rank: float = Field(default=15, ge=0)
    comeback_window_days: int = Field(default=30, ge=1)
    advisor_csi_min_surveys: int = Field(default=5, ge=0)
    data_staleness_hours: int = Field(default=48, ge=1)
    default_top_n: int = Field(default=3, ge=1, le=10)


@router.get("")
async def get_dealer(session: SessionDep, current: CurrentUserDep):
    dealer = await session.get(Dealer, current.dealer_id)
    if dealer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer not found")
    ds = await get_dealer_settings(session, current.dealer_id)

    # real per-team headcount, so the Teams panel reads "· N techs" from the DB
    rows = (
        await session.execute(
            select(Technician.team, func.count())
            .where(Technician.dealer_id == current.dealer_id, Technician.active.is_(True))
            .group_by(Technician.team)
        )
    ).all()
    team_counts = {(t or "Unassigned"): n for t, n in rows}

    store_config = ds.store_config or copy.deepcopy(DEFAULT_STORE_CONFIG)
    await session.commit()
    return {
        "id": str(dealer.id),
        "name": dealer.name,
        "timezone": dealer.timezone,
        "role": current.role,
        "store_config": store_config,
        "team_counts": team_counts,
        "weights": weights_to_labels(ds.match_weights),
        "settings": {
            "match_weights": ds.match_weights,
            "enforce_team_separation": ds.enforce_team_separation,
            "min_ros_to_rank": ds.min_ros_to_rank,
            "min_flagged_hours_to_rank": float(ds.min_flagged_hours_to_rank or 0),
            "comeback_window_days": ds.comeback_window_days,
            "advisor_csi_min_surveys": ds.advisor_csi_min_surveys,
            "data_staleness_hours": ds.data_staleness_hours,
            "default_top_n": ds.default_top_n,
        },
    }


class StoreSettingsIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    store_config: dict[str, Any]
    # the six sliders, by their Store Settings labels — must total 100
    weights: dict[str, float]

    @field_validator("weights")
    @classmethod
    def total_100(cls, v: dict) -> dict:
        missing = set(WEIGHT_LABEL_TO_KEY) - set(v)
        if missing:
            raise ValueError(f"missing weights: {', '.join(sorted(missing))}")
        if any(x < 0 for x in v.values()):
            raise ValueError("weights cannot be negative")
        total = sum(v[k] for k in WEIGHT_LABEL_TO_KEY)
        if abs(total - 100.0) > 0.01:
            raise ValueError(f"the six weights must total 100 (they total {total:g})")
        return v


@router.put("/store-settings")
async def update_store_settings(body: StoreSettingsIn, session: SessionDep, current: CurrentUserDep):
    current.require_role("SERVICE_MANAGER")

    dealer = await session.get(Dealer, current.dealer_id)
    if dealer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer not found")
    dealer.name = body.name.strip()

    ds = await get_dealer_settings(session, current.dealer_id)
    ds.store_config = body.store_config
    ds.match_weights = labels_to_weights(body.weights)

    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action=audit.SETTINGS_UPDATED,
        entity="store_settings",
        entity_id=current.dealer_id,
        payload={"name": dealer.name, "weights": body.weights},
    )
    await session.commit()
    return await get_dealer(session, current)


@router.put("/settings")
async def update_settings(body: SettingsIn, session: SessionDep, current: CurrentUserDep):
    current.require_role("SERVICE_MANAGER")

    # The five base factors are what make a score out of 100.  If they do not
    # sum to 100 the number stops meaning what the UI says it means, and a
    # technician comparing a 91 today to a 91 last week is comparing nothing.
    total = body.match_weights.base_total()
    if abs(total - 100.0) > 0.01:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"The five base weights must sum to 100 (they currently sum to {total:g}). "
            f"The specialty bonus sits on top and is not part of the 100.",
        )

    ds = await get_dealer_settings(session, current.dealer_id)
    ds.match_weights = body.match_weights.model_dump()
    ds.enforce_team_separation = body.enforce_team_separation
    ds.min_ros_to_rank = body.min_ros_to_rank
    ds.min_flagged_hours_to_rank = body.min_flagged_hours_to_rank
    ds.comeback_window_days = body.comeback_window_days
    ds.advisor_csi_min_surveys = body.advisor_csi_min_surveys
    ds.data_staleness_hours = body.data_staleness_hours
    ds.default_top_n = body.default_top_n

    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action=audit.SETTINGS_UPDATED,
        entity="dealer_settings",
        entity_id=current.dealer_id,
        payload=body.model_dump(),
    )
    await session.commit()
    return await get_dealer(session, current)
