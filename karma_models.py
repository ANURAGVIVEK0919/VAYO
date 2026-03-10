"""
Karma Models
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
    # Signup actions
    SIGNUP_EMAIL_VERIFY     = "SIGNUP_EMAIL_VERIFY"
    SIGNUP_PROFILE_PHOTO    = "SIGNUP_PROFILE_PHOTO"
    SIGNUP_VIBE_QUESTIONS   = "SIGNUP_VIBE_QUESTIONS"
    SIGNUP_CLAIM_ID         = "SIGNUP_CLAIM_ID"
    # Event actions
    EVENT_RSVP              = "EVENT_RSVP"
    GPS_CHECKIN             = "GPS_CHECKIN"
    EVENT_PHOTO_POST        = "EVENT_PHOTO_POST"
    EVENT_HIGH_RATING       = "EVENT_HIGH_RATING"
    PEER_ENDORSEMENT        = "PEER_ENDORSEMENT"
    HOST_EVENT              = "HOST_EVENT"
    # Penalties
    NO_SHOW_PENALTY         = "NO_SHOW_PENALTY"
    HOST_CANCEL_PENALTY     = "HOST_CANCEL_PENALTY"
    NEGATIVE_REVIEW_PENALTY = "NEGATIVE_REVIEW_PENALTY"
    # Admin
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



KARMA_RULES: Dict[KarmaActionType, int] = {
    KarmaActionType.SIGNUP_EMAIL_VERIFY:      10,
    KarmaActionType.SIGNUP_PROFILE_PHOTO:     10,
    KarmaActionType.SIGNUP_VIBE_QUESTIONS:    20,
    KarmaActionType.SIGNUP_CLAIM_ID:          10,
    KarmaActionType.EVENT_RSVP:               10,
    KarmaActionType.GPS_CHECKIN:              20,
    KarmaActionType.EVENT_PHOTO_POST:         15,
    KarmaActionType.EVENT_HIGH_RATING:        15,
    KarmaActionType.PEER_ENDORSEMENT:         25,
    KarmaActionType.HOST_EVENT:               50,
    KarmaActionType.NO_SHOW_PENALTY:         -20,
    KarmaActionType.HOST_CANCEL_PENALTY:     -30,
    KarmaActionType.NEGATIVE_REVIEW_PENALTY: -15,
}

PENALTY_ACTIONS = {
    KarmaActionType.NO_SHOW_PENALTY,
    KarmaActionType.HOST_CANCEL_PENALTY,
    KarmaActionType.NEGATIVE_REVIEW_PENALTY,
}


def compute_tier(score: int) -> Optional[KarmaTier]:
    """Maps a karma score to a tier dynamically from TIER_CONFIG. Returns None if score < 100."""
    for tier, config in TIER_CONFIG.items():
        if score >= config["min"] and (config["max"] is None or score <= config["max"]):
            return tier
    return None


def get_next_tier_threshold(score: int) -> Optional[int]:
    """Returns the point threshold for the next tier. Derived dynamically from TIER_CONFIG."""
    thresholds = sorted([v["min"] for v in TIER_CONFIG.values()])
    for t in thresholds:
        if score < t:
            return t
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
        if action in PENALTY_ACTIONS and v > 0:
            raise ValueError(f"Penalty action '{action}' must have a negative point_delta")
        if action and action not in PENALTY_ACTIONS and action != KarmaActionType.ADMIN_ADJUSTMENT and v < 0:
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


async def add_karma(
    user_id: str,
    action_type: KarmaActionType,
    reference_id: str = None
):
    """
    Add or deduct karma for a user.
    Inserts into karma_ledger — trigger auto-updates karma_score on users table.
    Raises ValueError if user does not exist.
    """

    # Support both raw string and enum input
    if isinstance(action_type, str):
        action_type = KarmaActionType(action_type.upper())

    point_delta = KARMA_RULES.get(action_type)

    if point_delta is None:
        # ADMIN_ADJUSTMENT has no fixed rule
        return

    async with db_manager.pg_pool.acquire() as conn:
        async with conn.transaction():

            # Check user exists
            user_row = await conn.fetchrow(
                "SELECT user_id FROM users WHERE user_id = $1",
                user_id
            )
            if not user_row:
                raise ValueError(
                    f"User '{user_id}' not found in users table. "
                    f"Insert the user before adding karma."
                )

            # Insert into karma_ledger
            # Trigger automatically updates karma_score on users table
            await conn.execute(
                """
                INSERT INTO karma_ledger (user_id, action_type, point_delta, reference_id)
                VALUES ($1, $2, $3, $4)
                """,
                user_id,
                action_type.value,
                point_delta,
                reference_id
            )

            # Enforce karma floor at 0
            await conn.execute(
                """
                UPDATE users
                SET karma_score = GREATEST(0, karma_score)
                WHERE user_id = $1
                """,
                user_id
            )

            # Update tier
            row = await conn.fetchrow(
                "SELECT karma_score FROM users WHERE user_id = $1",
                user_id
            )
            tier = compute_tier(row["karma_score"])
            await conn.execute(
                """
                UPDATE users SET tier_level = $1 WHERE user_id = $2
                """,
                tier.value if tier else None,
                user_id
            )


async def get_user_karma(user_id: str) -> int:
    """
    Get total karma score of a user.
    Returns 0 if user not found.
    """
    row = await db_manager.pg_pool.fetchrow(
        "SELECT karma_score FROM users WHERE user_id = $1",
        user_id
    )
    return row["karma_score"] if row else 0


async def get_karma_history(
    user_id: str,
    limit: int = 50,
    offset: int = 0
) -> List[Dict]:
    """
    Returns user's karma ledger history with pagination.
    """
    rows = await db_manager.pg_pool.fetch(
        """
        SELECT id, action_type, point_delta, reference_id, created_at
        FROM karma_ledger
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        user_id,
        limit,
        offset
    )
    return [dict(r) for r in rows]


async def has_required_karma(user_id: str, required_karma: int) -> bool:
    """
    Check if user has enough karma for an event.
    """
    row = await db_manager.pg_pool.fetchrow(
        """
        SELECT karma_score >= $2 AS allowed
        FROM users
        WHERE user_id = $1
        """,
        user_id,
        required_karma
    )
    return row["allowed"] if row else False
