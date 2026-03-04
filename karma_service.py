"""
Karma Service
Handles:
- Adding karma
- Getting user karma
- Determining user tier
- Fetching karma history
"""

from typing import List, Dict
from .database import db_manager



KARMA_RULES = {
    "event_attended": 20,
    "event_hosted": 30,
    "event_high_rating": 15,
    "daily_login": 2,
}


def get_user_tier(karma_points: int) -> str:
    """
    Determines tier based on karma points
    """

    if karma_points >= 500:
        return "Elite Member"

    if karma_points >= 300:
        return "Influencer"

    if karma_points >= 150:
        return "Contributor"

    if karma_points >= 50:
        return "Explorer"

    return "Beginner"




async def add_karma(user_id: str, action_type: str, event_id: str = None):
    """
    Add karma to a user.
    Raises ValueError if user does not exist in the users table.
    """

    points = KARMA_RULES.get(action_type, 0)

    if points == 0:
        return

 
    user_row = await db_manager.pg_pool.fetchrow(
        """
        SELECT user_id FROM users
        WHERE user_id = $1
        """,
        user_id
    )

    if user_row is None:
        raise ValueError(
            f"User '{user_id}' not found in users table. "
            f"Insert the user before adding karma."
        )

    
    await db_manager.pg_pool.execute(
        """
        INSERT INTO karma_transactions (user_id, action_type, points, event_id)
        VALUES ($1, $2, $3, $4)
        """,
        user_id,
        action_type,
        points,
        event_id
    )

    await db_manager.pg_pool.execute(
        """
        UPDATE users
        SET karma_points = karma_points + $1
        WHERE user_id = $2
        """,
        points,
        user_id
    )

    row = await db_manager.pg_pool.fetchrow(
        """
        SELECT karma_points FROM users
        WHERE user_id = $1
        """,
        user_id
    )

    karma_points = row["karma_points"]

    tier = get_user_tier(karma_points)


    await db_manager.pg_pool.execute(
        """
        UPDATE users
        SET tier_level = $1
        WHERE user_id = $2
        """,
        tier,
        user_id
    )



async def get_user_karma(user_id: str) -> int:
    """
    Get total karma points of a user.
    Returns 0 if user not found.
    """

    row = await db_manager.pg_pool.fetchrow(
        """
        SELECT karma_points
        FROM users
        WHERE user_id = $1
        """,
        user_id
    )

    if row:
        return row["karma_points"]

    return 0



async def get_karma_history(user_id: str) -> List[Dict]:
    """
    Returns user's karma transaction history
    """

    rows = await db_manager.pg_pool.fetch(
        """
        SELECT action_type, points, created_at
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
