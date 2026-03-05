"""
Karma Points System — Pydantic Models & Enums
"""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator
from enum import Enum


class KarmaActionType(str, Enum):
    """Mirrors karma_action_type_enum in karma_migration.sql."""
    SIGNUP_EMAIL_VERIFY   = "SIGNUP_EMAIL_VERIFY"
    SIGNUP_PROFILE_PHOTO  = "SIGNUP_PROFILE_PHOTO"
    SIGNUP_VIBE_QUESTIONS = "SIGNUP_VIBE_QUESTIONS"
    SIGNUP_CLAIM_ID       = "SIGNUP_CLAIM_ID"
    EVENT_RSVP            = "EVENT_RSVP"
    GPS_CHECKIN           = "GPS_CHECKIN"
    EVENT_PHOTO_POST      = "EVENT_PHOTO_POST"
    PEER_ENDORSEMENT      = "PEER_ENDORSEMENT"
    HOST_EVENT            = "HOST_EVENT"
    NO_SHOW_PENALTY           = "NO_SHOW_PENALTY"
    HOST_CANCEL_PENALTY       = "HOST_CANCEL_PENALTY"
    NEGATIVE_REVIEW_PENALTY   = "NEGATIVE_REVIEW_PENALTY"
    ADMIN_ADJUSTMENT      = "ADMIN_ADJUSTMENT"


class KarmaTier(str, Enum):
    """Credibility tiers — thresholds defined in compute_tier()."""
    BEGINNER   = "beginner"
    PATHFINDER = "pathfinder"
    EXPLORER   = "explorer"
    CONQUEROR  = "conqueror"


TIER_CONFIG = {
    KarmaTier.BEGINNER:   {"label": "Beginner",   "min": 100,  "max": 299,  "level": 1},
    KarmaTier.PATHFINDER: {"label": "Pathfinder", "min": 300,  "max": 499,  "level": 2},
    KarmaTier.EXPLORER:   {"label": "Explorer",   "min": 500,  "max": 999,  "level": 3},
    KarmaTier.CONQUEROR:  {"label": "Conqueror",  "min": 1000, "max": None, "level": 4},
}


def compute_tier(score: int) -> Optional[KarmaTier]:
    """Maps a karma score to a tier. Returns None if score < 100."""
    if score >= 1000:
        return KarmaTier.CONQUEROR
    elif score >= 500:
        return KarmaTier.EXPLORER
    elif score >= 300:
        return KarmaTier.PATHFINDER
    elif score >= 100:
        return KarmaTier.BEGINNER
    return None


def get_next_tier_threshold(score: int) -> Optional[int]:
    """Returns the point threshold for the next tier, or None if at max."""
    if score < 100:
        return 100
    elif score < 300:
        return 300
    elif score < 500:
        return 500
    elif score < 1000:
        return 1000
    return None


def get_tier_level(tier: Optional[KarmaTier]) -> int:
    """Returns numeric level (0 if no tier)."""
    if tier is None:
        return 0
    return TIER_CONFIG[tier]["level"]


class KarmaAwardRequest(BaseModel):
    """POST /api/v1/karma/award body."""
    user_id: str = Field(..., description="Clerk user ID")
    action_type: KarmaActionType
    point_delta: int = Field(..., description="Positive for gains, negative for deductions")
    reference_id: Optional[str] = Field(None, description="Optional event_id, endorsement_id, etc.")

    @field_validator("point_delta")
    @classmethod
    def validate_point_delta(cls, v: int, info) -> int:
        action = info.data.get("action_type")
        penalty_actions = {
            KarmaActionType.NO_SHOW_PENALTY,
            KarmaActionType.HOST_CANCEL_PENALTY,
            KarmaActionType.NEGATIVE_REVIEW_PENALTY,
        }
        if action in penalty_actions and v > 0:
            raise ValueError(f"Penalty action '{action}' must have a negative point_delta")
        if action and action not in penalty_actions and action != KarmaActionType.ADMIN_ADJUSTMENT and v < 0:
            raise ValueError(f"Reward action '{action}' must have a positive point_delta")
        return v


class InboxShieldUpdate(BaseModel):
    """PATCH /api/v1/users/{user_id}/inbox-shield body."""
    threshold: int = Field(..., ge=0, description="Minimum karma for inbound DMs")


class KarmaLedgerEntry(BaseModel):
    """Single karma_ledger row for history responses."""
    id: str
    action_type: KarmaActionType
    point_delta: int
    reference_id: Optional[str] = None
    created_at: datetime


class KarmaProfileResponse(BaseModel):
    """GET /api/v1/users/{user_id}/karma response."""
    user_id: str
    karma_score: int
    tier: Optional[KarmaTier] = None
    tier_label: Optional[str] = None
    tier_level: int = 0
    next_tier_threshold: Optional[int] = None
    inbox_shield_threshold: int = 0
    ledger: Optional[List[KarmaLedgerEntry]] = None

    class Config:
        json_schema_extra = {
            "example": {
                "user_id": "user_abc123",
                "karma_score": 350,
                "tier": "pathfinder",
                "tier_label": "Pathfinder",
                "tier_level": 2,
                "next_tier_threshold": 500,
                "inbox_shield_threshold": 100,
                "ledger": []
            }
        }


class MessageEligibilityResponse(BaseModel):
    """GET /api/v1/users/{user_id}/karma/can-message/{target_user_id} response."""
    allowed: bool
    reason: Optional[str] = None
    sender_score: int
    target_score: int
    target_inbox_shield: int
