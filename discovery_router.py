"""
Discovery Router — Find People Nearby
Helps users discover other Vayo users based on location and karma tier.

Endpoints:
- GET /api/v1/discover/{user_id}   — Discover people nearby

Two modes:
1. AUTO (GPS)   — Pass lat & lng → shows users within radius (default 5km)
2. MANUAL       — Pass city name → shows users in that city

Filters applied:
- Tier reach    — only shows users this tier can connect with
- Excludes      — already connected users
- Excludes      — blocked users
- Excludes      — self
- Sorts by      — karma score descending
- Privacy       — respects show_karma_score setting
"""

import math
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, status

from .database import db_manager
from .karma_models import KarmaTier, TIER_CONFIG

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Discovery"])

DEFAULT_RADIUS_KM = 5
DEFAULT_LIMIT = 20

# Maps tier → all tiers it can see (itself + below)
TIER_REACH = {
    KarmaTier.BEGINNER:   [KarmaTier.BEGINNER],
    KarmaTier.PATHFINDER: [KarmaTier.BEGINNER, KarmaTier.PATHFINDER],
    KarmaTier.EXPLORER:   [KarmaTier.BEGINNER, KarmaTier.PATHFINDER, KarmaTier.EXPLORER],
    KarmaTier.CONQUEROR:  [KarmaTier.BEGINNER, KarmaTier.PATHFINDER, KarmaTier.EXPLORER, KarmaTier.CONQUEROR],
}



def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate distance in km between two GPS coordinates.
    Uses Haversine formula.
    """
    R = 6371  # Earth radius in km
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))



@router.get(
    "/discover/{user_id}",
    summary="Discover people nearby — GPS or manual city",
)
async def discover_people(
    user_id: str,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius: float = DEFAULT_RADIUS_KM,
    city: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
):
    """
    Discover people nearby based on location and karma tier.

    Mode 1 — GPS (auto):
        /discover/user_001?lat=12.9716&lng=77.5946&radius=5

    Mode 2 — Manual city:
        /discover/user_001?city=Mumbai

    Rules:
    - Must provide either lat+lng OR city
    - Tier reach applied — higher tier sees lower tiers
    - Excludes already connected users
    - Excludes blocked users
    - Excludes self
    - Sorted by karma score descending
    - Respects show_karma_score privacy setting
    """

    # Validate — must provide GPS or city
    if not (lat and lng) and not city:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either lat & lng for GPS mode or city for manual mode."
        )

    # Fetch requesting user
    me = await db_manager.pg_pool.fetchrow(
        "SELECT user_id, tier_level, karma_score FROM users WHERE user_id = $1",
        user_id
    )
    if not me:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found."
        )

    # Get tier reach
    try:
        my_tier = KarmaTier(me["tier_level"])
        visible_tiers = [t.value for t in TIER_REACH[my_tier]]
    except (ValueError, KeyError):
        visible_tiers = [t.value for t in KarmaTier]  # fallback — show all

    # Fetch all candidate users (apply tier + privacy filter in DB)
    rows = await db_manager.pg_pool.fetch(
        """
        SELECT
            u.user_id,
            u.username,
            u.bio,
            u.karma_score,
            u.tier_level,
            u.city,
            u.region,
            u.latitude,
            u.longitude,
            u.show_karma_score,
            u.profile_visibility
        FROM users u
        WHERE u.user_id != $1
          AND LOWER(u.tier_level) = ANY($2::text[])
          AND u.profile_visibility != 'hidden'
          AND u.user_id NOT IN (
              SELECT blocked_id FROM blocked_users WHERE blocker_id = $1
              UNION
              SELECT blocker_id FROM blocked_users WHERE blocked_id = $1
          )
          AND u.user_id NOT IN (
              SELECT
                CASE WHEN user_id_1 = $1 THEN user_id_2 ELSE user_id_1 END
              FROM connections
              WHERE user_id_1 = $1 OR user_id_2 = $1
          )
        """,
        user_id,
        visible_tiers,
    )

    # Apply location filter
    results = []

    for r in rows:
        user_data = dict(r)

        # GPS mode — filter by radius
        if lat and lng:
            if r["latitude"] is None or r["longitude"] is None:
                continue  # skip users without GPS coordinates
            distance = haversine_distance(lat, lng, r["latitude"], r["longitude"])
            if distance > radius:
                continue
            user_data["distance_km"] = round(distance, 2)

        elif city:
            if not r["city"] or r["city"].lower() != city.lower():
                continue
            user_data["distance_km"] = None

        if not r["show_karma_score"]:
            user_data["karma_score"] = None

        user_data.pop("show_karma_score", None)
        user_data.pop("latitude", None)
        user_data.pop("longitude", None)

        results.append(user_data)

    results.sort(key=lambda x: x["karma_score"] or 0, reverse=True)

    results = results[:limit]

    return {
        "user_id": user_id,
        "mode": "gps" if (lat and lng) else "city",
        "location": {"lat": lat, "lng": lng, "radius_km": radius} if (lat and lng) else {"city": city},
        "total": len(results),
        "users": results,
    }
