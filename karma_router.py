"""
Karma API Router
Endpoints:
- POST   /api/v1/karma/award
- GET    /api/v1/users/{user_id}/karma
- GET    /api/v1/users/{user_id}/karma/can-message/{target_user_id}
- PATCH  /api/v1/users/{user_id}/inbox-shield
"""

from fastapi import APIRouter, HTTPException, status
from .database import db_manager
from .karma_module import add_karma, get_user_karma, get_karma_history, has_required_karma
from .karma_module import (
    KarmaAwardRequest,
    KarmaProfileResponse,
    KarmaTier,
    KarmaLedgerEntry,
    MessageEligibilityResponse,
    InboxShieldUpdate,
    compute_tier,
    get_next_tier_threshold,
    get_tier_level,
    TIER_CONFIG,
)

router = APIRouter(prefix="/api/v1", tags=["Karma"])


@router.post(
    "/karma/award",
    status_code=status.HTTP_200_OK,
    summary="Award or deduct karma points for a user action",
)
async def award_karma(body: KarmaAwardRequest):
    """
    Award or deduct karma points.
    - Positive point_delta for rewards
    - Negative point_delta for penalties
    """

    user_row = await db_manager.pg_pool.fetchrow(
        "SELECT user_id FROM users WHERE user_id = $1",
        body.user_id
    )
    if user_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{body.user_id}' not found."
        )

    await db_manager.pg_pool.execute(
        """
        INSERT INTO karma_transactions (user_id, action_type, points, event_id)
        VALUES ($1, $2, $3, $4)
        """,
        body.user_id,
        body.action_type.value,
        body.point_delta,
        body.reference_id,
    )


    await db_manager.pg_pool.execute(
        """
        UPDATE users
        SET karma_points = GREATEST(0, karma_points + $1)
        WHERE user_id = $2
        """,
        body.point_delta,
        body.user_id,
    )

    row = await db_manager.pg_pool.fetchrow(
        "SELECT karma_points FROM users WHERE user_id = $1",
        body.user_id
    )
    new_score = row["karma_points"]
    tier = compute_tier(new_score)


    await db_manager.pg_pool.execute(
        """
        UPDATE users
        SET tier_level = $1
        WHERE user_id = $2
        """,
        tier.value if tier else None,
        body.user_id,
    )

    return {
        "user_id": body.user_id,
        "action_type": body.action_type.value,
        "points_awarded": body.point_delta,
        "new_karma_score": new_score,
        "tier": tier.value if tier else None,
    }



@router.get(
    "/users/{user_id}/karma",
    response_model=KarmaProfileResponse,
    summary="Get karma profile, tier, and ledger for a user",
)
async def get_karma_profile(user_id: str, include_ledger: bool = True):
    """
    Returns full karma profile:
    - karma_score, tier, tier_label, tier_level
    - next_tier_threshold
    - inbox_shield_threshold
    - ledger (optional, pass ?include_ledger=false to skip)
    """

    row = await db_manager.pg_pool.fetchrow(
        """
        SELECT karma_points, inbox_shield_threshold
        FROM users
        WHERE user_id = $1
        """,
        user_id
    )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found."
        )

    score = row["karma_points"]
    shield = row["inbox_shield_threshold"] or 0
    tier = compute_tier(score)
    tier_label = TIER_CONFIG[tier]["label"] if tier else None
    tier_level = get_tier_level(tier)
    next_threshold = get_next_tier_threshold(score)

    ledger = None
    if include_ledger:
        history = await get_karma_history(user_id)
        ledger = [
            KarmaLedgerEntry(
                id=str(entry.get("id", "")),
                action_type=entry["action_type"],
                point_delta=entry["points"],
                reference_id=entry.get("event_id"),
                created_at=entry["created_at"],
            )
            for entry in history
        ]

    return KarmaProfileResponse(
        user_id=user_id,
        karma_score=score,
        tier=tier,
        tier_label=tier_label,
        tier_level=tier_level,
        next_tier_threshold=next_threshold,
        inbox_shield_threshold=shield,
        ledger=ledger,
    )



@router.get(
    "/users/{user_id}/karma/can-message/{target_user_id}",
    response_model=MessageEligibilityResponse,
    summary="Check if a user has enough karma to DM another user",
)
async def can_message(user_id: str, target_user_id: str):
    """
    Checks inbox shield:
    - Fetches sender karma score
    - Fetches target's inbox_shield_threshold
    - Returns allowed=True if sender score >= target threshold
    """

    sender_row = await db_manager.pg_pool.fetchrow(
        "SELECT karma_points FROM users WHERE user_id = $1",
        user_id
    )
    if sender_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sender '{user_id}' not found."
        )


    target_row = await db_manager.pg_pool.fetchrow(
        "SELECT karma_points, inbox_shield_threshold FROM users WHERE user_id = $1",
        target_user_id
    )
    if target_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target user '{target_user_id}' not found."
        )

    sender_score = sender_row["karma_points"]
    target_score = target_row["karma_points"]
    target_shield = target_row["inbox_shield_threshold"] or 0

    allowed = sender_score >= target_shield
    reason = None if allowed else (
        f"Your karma ({sender_score}) is below "
        f"{target_user_id}'s inbox shield ({target_shield})."
    )

    return MessageEligibilityResponse(
        allowed=allowed,
        reason=reason,
        sender_score=sender_score,
        target_score=target_score,
        target_inbox_shield=target_shield,
    )




@router.patch(
    "/users/{user_id}/inbox-shield",
    status_code=status.HTTP_200_OK,
    summary="Update the minimum karma required to DM this user",
)
async def update_inbox_shield(user_id: str, body: InboxShieldUpdate):
    """
    Sets inbox_shield_threshold for a user.
    Only users with karma >= threshold can send DMs.
    """

    result = await db_manager.pg_pool.execute(
        """
        UPDATE users
        SET inbox_shield_threshold = $1
        WHERE user_id = $2
        """,
        body.threshold,
        user_id,
    )

    if result == "UPDATE 0":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found."
        )

    return {
        "user_id": user_id,
        "inbox_shield_threshold": body.threshold,
        "message": "Inbox shield updated successfully.",
    }
