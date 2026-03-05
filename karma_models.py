"""
Karma Module
Contains:
- Pydantic Models & Enums
- Tier Calculation
- Service Functions (add_karma, get_user_karma, etc.)
"""

from datetime import datetime
from typing import List, Optional, Dict
from pydantic import BaseModel, Field, field_validator
from enum import Enum
from .database import db_manager



class KarmaActionType(str, Enum):
    SIGNUP_EMAIL_VERIFY     = "SIGNUP_EMAIL_VERIFY"
    SIGNUP_PROFILE_PHOTO    = "SIGNUP_PROFILE_PHOTO"
    SIGNUP_VIBE_QUESTIONS   = "SIGNUP_VIBE_QUESTIONS"
    SIGNUP_CLAIM_ID         = "SIGNUP_CLAIM_ID"
    EVENT_RSVP              = "EVENT_RSVP"
    GPS_CHECKIN             = "GPS_CHECKIN"
    EVENT_PHOTO_POST        = "EVENT_PHOTO_POST"
    PEER_ENDORSEMENT        = "PEER_ENDORSEMENT"
    HOST_EVENT              = "HOST_EVENT"
    NO_SHOW_PENALTY         = "NO_SHOW_PENALTY"
    HOST_CANCEL_PENALTY     = "HOST_CANCEL_PENALTY"
    NEGATIVE_REVIEW_PENALTY = "NEGATIVE_REVIEW_PENALTY"
    ADMIN_ADJUSTMENT        = "ADMIN_ADJUSTMENT"


class KarmaTier(str, Enum):
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



async def add_karma(user_id: str, action_type: str, event_id: str = None):
    """
    Add or deduct karma for a user.
    Raises ValueError if user does not exist.
    """

    user_row = await db_manager.pg_pool.fetchrow(
        "SELECT user_id FROM users WHERE user_id = $1",
        user_id
    )
    if user_row is None:
        raise ValueError(
            f"User '{user_id}' not found in users table. "
            f"Insert the user before adding karma."
        )


    karma_rules = {
        "SIGNUP_EMAIL_VERIFY":     10,
        "SIGNUP_PROFILE_PHOTO":    10,
        "SIGNUP_VIBE_QUESTIONS":   20,
        "SIGNUP_CLAIM_ID":         10,
        "EVENT_RSVP":              10,
        "GPS_CHECKIN":             20,
        "EVENT_PHOTO_POST":        15,
        "PEER_ENDORSEMENT":        25,
        "HOST_EVENT":              50,
        "NO_SHOW_PENALTY":        -20,
        "HOST_CANCEL_PENALTY":    -30,
        "NEGATIVE_REVIEW_PENALTY": -15,
    }

    action_key = action_type.upper() if isinstance(action_type, str) else action_type
    points = karma_rules.get(action_key, 0)

    if points == 0:
        return

    await db_manager.pg_pool.execute(
        """
        INSERT INTO karma_transactions (user_id, action_type, points, event_id)
        VALUES ($1, $2, $3, $4)
        """,
        user_id,
        action_key,
        points,
        event_id
    )

    await db_manager.pg_pool.execute(
        """
        UPDATE users
        SET karma_points = GREATEST(0, karma_points + $1)
        WHERE user_id = $2
        """,
        points,
        user_id
    )

   
    row = await db_manager.pg_pool.fetchrow(
        "SELECT karma_points FROM users WHERE user_id = $1",
        user_id
    )
    tier = compute_tier(row["karma_points"])

    await db_manager.pg_pool.execute(
        """
        UPDATE users
        SET tier_level = $1
        WHERE user_id = $2
        """,
        tier.value if tier else None,
        user_id
    )


async def get_user_karma(user_id: str) -> int:
    """
    Get total karma points of a user.
    Returns 0 if user not found.
    """
    row = await db_manager.pg_pool.fetchrow(
        "SELECT karma_points FROM users WHERE user_id = $1",
        user_id
    )
    if row:
        return row["karma_points"]
    return 0


async def get_karma_history(user_id: str) -> List[Dict]:
    """
    Returns user's karma transaction history.
    """
    rows = await db_manager.pg_pool.fetch(
        """
        SELECT id, action_type, points, event_id, created_at
        FROM karma_transactions
        WHERE user_id = $1
        ORDER BY created_at DESC
        """,
        user_id
    )
    return [dict(r) for r in rows]


async def has_required_karma(user_id: str, required_karma: int) -> bool:
    """
    Check if user has enough karma for an event.
    """
    karma = await get_user_karma(user_id)
    return karma >= required_karma
