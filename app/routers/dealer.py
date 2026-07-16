"""Per-dealer configuration.

NFR-6: weights, thresholds and gates are editable WITHOUT a redeploy.  A store
that cares most about promise times turns availability up here, and the next
board refresh scores differently.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from .. import audit
from ..deps import CurrentUserDep, SessionDep, get_dealer_settings
from ..models import Dealer

router = APIRouter(prefix="/dealer", tags=["dealer"])


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
    await session.commit()
    return {
        "id": str(dealer.id),
        "name": dealer.name,
        "timezone": dealer.timezone,
        "role": current.role,
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
