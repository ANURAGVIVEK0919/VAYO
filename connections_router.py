"""
Connections Router — Karma Connect System
Handles follow requests, connections, blocking, reporting, muting, privacy, and social sharing.

Endpoints:
--- Karma Connect (Follow Requests) ---
POST   /api/v1/connect/request                       — Send a Karma Connect request
PATCH  /api/v1/connect/request/{request_id}/accept   — Accept a request
PATCH  /api/v1/connect/request/{request_id}/decline  — Decline a request
DELETE /api/v1/connect/request/{request_id}/withdraw — Withdraw a pending request
GET    /api/v1/connect/requests/{user_id}            — Get all pending requests
GET    /api/v1/connect/connections/{user_id}         — Get all connections

--- Mutual Connections ---
GET    /api/v1/connect/mutual/{user_id_1}/{user_id_2} — Get mutual connections

--- Profile View ---
GET    /api/v1/connect/profile/{user_id}             — View a user profile (respects privacy)

--- Connection Management ---
DELETE /api/v1/connect/remove                        — Remove a connection

--- Block / Unblock ---
POST   /api/v1/connect/block                         — Block a user
DELETE /api/v1/connect/unblock                       — Unblock a user
GET    /api/v1/connect/blocked/{user_id}             — Get blocked users list

--- Report ---
POST   /api/v1/connect/report                        — Report a user

--- Mute / Unmute ---
POST   /api/v1/connect/mute                          — Mute a user
DELETE /api/v1/connect/unmute                        — Unmute a user

--- Privacy Settings ---
PATCH  /api/v1/connect/privacy/{user_id}             — Update privacy settings

--- Social Handle Sharing ---
POST   /api/v1/connect/share                         — Share Instagram/LinkedIn/Twitter
DELETE /api/v1/connect/share                         — Remove a shared detail
GET    /api/v1/connect/shared/{user_id}              — Get details shared with you

Rules:
- Karma tier check before sending Karma Connect request
- Blocked users cannot send requests or messages
- Chat only unlocks after connection is accepted
- Only Instagram, LinkedIn, Twitter can be shared — no phone/email
- Profile visibility respected — public/connections/hidden
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from .database import db_manager
from .karma_models import KarmaTier, TIER_CONFIG

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/connect", tags=["Karma Connect"])


class ConnectRequestBody(BaseModel):
    sender_id: str
    receiver_id: str

class RemoveConnectionBody(BaseModel):
    user_id: str
    other_user_id: str

class BlockBody(BaseModel):
    blocker_id: str
    blocked_id: str

class ReportBody(BaseModel):
    reporter_id: str
    reported_id: str
    reason: str = Field(..., min_length=5, max_length=500)

class MuteBody(BaseModel):
    muter_id: str
    muted_id: str

class PrivacySettingsBody(BaseModel):
    profile_visibility: Optional[str] = Field(None, pattern="^(public|connections|hidden)$")
    show_karma_score: Optional[bool] = None
    show_last_seen: Optional[bool] = None

class ShareDetailBody(BaseModel):
    shared_by: str
    shared_with: str
    detail_type: str = Field(..., pattern="^(instagram|linkedin|twitter)$")
    detail_value: str = Field(..., min_length=1, max_length=100)

class RemoveShareBody(BaseModel):
    shared_by: str
    shared_with: str
    detail_type: str


async def are_connected(user_id_1: str, user_id_2: str) -> bool:
    row = await db_manager.pg_pool.fetchrow(
        """
        SELECT 1 FROM connections
        WHERE (user_id_1 = $1 AND user_id_2 = $2)
           OR (user_id_1 = $2 AND user_id_2 = $1)
        """,
        user_id_1, user_id_2
    )
    return row is not None


async def is_blocked(blocker_id: str, blocked_id: str) -> bool:
    row = await db_manager.pg_pool.fetchrow(
        "SELECT 1 FROM blocked_users WHERE blocker_id = $1 AND blocked_id = $2",
        blocker_id, blocked_id
    )
    return row is not None



async def check_tier_reach(sender_id: str, receiver_id: str):
    rows = await db_manager.pg_pool.fetch(
        "SELECT user_id, tier_level FROM users WHERE user_id = ANY($1::text[])",
        [sender_id, receiver_id]
    )
    users = {r["user_id"]: dict(r) for r in rows}

    if sender_id not in users:
        raise HTTPException(status_code=404, detail=f"User '{sender_id}' not found.")
    if receiver_id not in users:
        raise HTTPException(status_code=404, detail=f"User '{receiver_id}' not found.")

    sender_tier = users[sender_id]["tier_level"]
    receiver_tier = users[receiver_id]["tier_level"]

    if sender_tier and receiver_tier:
        try:
            sender_level = TIER_CONFIG[KarmaTier(sender_tier)]["level"]
            receiver_level = TIER_CONFIG[KarmaTier(receiver_tier)]["level"]
            if sender_level < receiver_level:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Your tier ({sender_tier}) cannot Karma Connect with a higher tier ({receiver_tier})."
                )
        except ValueError:
            pass


@router.post(
    "/request",
    status_code=status.HTTP_201_CREATED,
    summary="Send a Karma Connect request",
)
async def send_connect_request(body: ConnectRequestBody):
    if body.sender_id == body.receiver_id:
        raise HTTPException(status_code=400, detail="You cannot connect with yourself.")

    if await is_blocked(body.receiver_id, body.sender_id):
        raise HTTPException(status_code=403, detail="You cannot send a request to this user.")

    await check_tier_reach(body.sender_id, body.receiver_id)

    if await are_connected(body.sender_id, body.receiver_id):
        raise HTTPException(status_code=409, detail="You are already connected with this user.")

    existing = await db_manager.pg_pool.fetchrow(
        "SELECT id, status FROM follow_requests WHERE sender_id = $1 AND receiver_id = $2",
        body.sender_id, body.receiver_id
    )
    if existing:
        if existing["status"] == "pending":
            raise HTTPException(status_code=409, detail="Karma Connect request already sent.")
        await db_manager.pg_pool.execute(
            "UPDATE follow_requests SET status = 'pending', created_at = NOW() WHERE id = $1",
            existing["id"]
        )
        return {"message": "Karma Connect request re-sent.", "status": "pending"}

    row = await db_manager.pg_pool.fetchrow(
        """
        INSERT INTO follow_requests (sender_id, receiver_id)
        VALUES ($1, $2)
        RETURNING id, sender_id, receiver_id, status, created_at
        """,
        body.sender_id, body.receiver_id
    )

    return {
        "request_id": str(row["id"]),
        "sender_id": body.sender_id,
        "receiver_id": body.receiver_id,
        "status": "pending",
        "message": "Karma Connect request sent!",
    }


@router.patch(
    "/request/{request_id}/accept",
    summary="Accept a Karma Connect request",
)
async def accept_connect_request(request_id: str):
    row = await db_manager.pg_pool.fetchrow(
        "SELECT * FROM follow_requests WHERE id = $1 AND status = 'pending'",
        request_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Pending request not found.")

    async with db_manager.pg_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE follow_requests SET status = 'accepted' WHERE id = $1",
                request_id
            )
            await conn.execute(
                """
                INSERT INTO connections (user_id_1, user_id_2)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                row["sender_id"], row["receiver_id"]
            )

    return {
        "message": "Karma Connect accepted! You are now connected.",
        "user_id_1": row["sender_id"],
        "user_id_2": row["receiver_id"],
    }


@router.patch(
    "/request/{request_id}/decline",
    summary="Decline a Karma Connect request",
)
async def decline_connect_request(request_id: str):
    result = await db_manager.pg_pool.execute(
        "UPDATE follow_requests SET status = 'declined' WHERE id = $1 AND status = 'pending'",
        request_id
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Pending request not found.")

    return {"message": "Karma Connect request declined.", "request_id": request_id}


@router.delete(
    "/request/{request_id}/withdraw",
    summary="Withdraw a pending Karma Connect request",
)
async def withdraw_connect_request(request_id: str):
    result = await db_manager.pg_pool.execute(
        "DELETE FROM follow_requests WHERE id = $1 AND status = 'pending'",
        request_id
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Pending request not found.")

    return {"message": "Karma Connect request withdrawn.", "request_id": request_id}


@router.get(
    "/requests/{user_id}",
    summary="Get all pending Karma Connect requests for a user",
)
async def get_pending_requests(user_id: str):
    rows = await db_manager.pg_pool.fetch(
        """
        SELECT id, sender_id, created_at
        FROM follow_requests
        WHERE receiver_id = $1 AND status = 'pending'
        ORDER BY created_at DESC
        """,
        user_id
    )
    return {
        "user_id": user_id,
        "total": len(rows),
        "requests": [
            {
                "request_id": str(r["id"]),
                "sender_id": r["sender_id"],
                "created_at": r["created_at"].isoformat()
            }
            for r in rows
        ]
    }


@router.get(
    "/connections/{user_id}",
    summary="Get all connections for a user",
)
async def get_connections(user_id: str):
    rows = await db_manager.pg_pool.fetch(
        """
        SELECT
            CASE WHEN user_id_1 = $1 THEN user_id_2 ELSE user_id_1 END AS connected_user,
            connected_at
        FROM connections
        WHERE user_id_1 = $1 OR user_id_2 = $1
        ORDER BY connected_at DESC
        """,
        user_id
    )
    return {
        "user_id": user_id,
        "total": len(rows),
        "connections": [
            {
                "connected_user": r["connected_user"],
                "connected_at": r["connected_at"].isoformat()
            }
            for r in rows
        ]
    }



@router.get(
    "/mutual/{user_id_1}/{user_id_2}",
    summary="Get mutual connections between two users — like LinkedIn",
)
async def get_mutual_connections(user_id_1: str, user_id_2: str):
    """
    Returns users that both user_id_1 and user_id_2 are connected with.
    Shows on profile page — 'You have 5 mutual connections'.
    """
    rows = await db_manager.pg_pool.fetch(
        """
        SELECT connected_user FROM (
            SELECT CASE WHEN user_id_1 = $1 THEN user_id_2 ELSE user_id_1 END AS connected_user
            FROM connections WHERE user_id_1 = $1 OR user_id_2 = $1
        ) a
        WHERE connected_user IN (
            SELECT CASE WHEN user_id_1 = $2 THEN user_id_2 ELSE user_id_1 END
            FROM connections WHERE user_id_1 = $2 OR user_id_2 = $2
        )
        AND connected_user != $1
        AND connected_user != $2
        """,
        user_id_1, user_id_2
    )

    mutual_users = [r["connected_user"] for r in rows]

    return {
        "user_id_1": user_id_1,
        "user_id_2": user_id_2,
        "mutual_count": len(mutual_users),
        "mutual_connections": mutual_users,
    }



@router.get(
    "/profile/{user_id}",
    summary="View a user profile — respects privacy settings",
)
async def view_profile(user_id: str, requester_id: str = None):
    """
    View a user's profile.
    - Public      → anyone can see
    - Connections → only connected users can see full profile
    - Hidden      → nobody can see (returns 403)

    Pass requester_id to check connection and privacy correctly.
    Shows mutual connections count if requester_id provided.
    """

    row = await db_manager.pg_pool.fetchrow(
        """
        SELECT
            user_id, username, bio, tier_level, city, region,
            karma_score, profile_visibility, show_karma_score,
            show_last_seen, last_seen, created_at
        FROM users
        WHERE user_id = $1
        """,
        user_id
    )

    if not row:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found.")

    visibility = row["profile_visibility"] or "public"
    is_own_profile = requester_id == user_id

    # Hidden profile
    if visibility == "hidden" and not is_own_profile:
        raise HTTPException(status_code=403, detail="This profile is private.")

    # Connections only
    if visibility == "connections" and not is_own_profile:
        connected = await are_connected(requester_id, user_id) if requester_id else False
        if not connected:
            raise HTTPException(
                status_code=403,
                detail="This profile is only visible to connections."
            )

    # Connection count
    conn_count = await db_manager.pg_pool.fetchval(
        "SELECT COUNT(*) FROM connections WHERE user_id_1 = $1 OR user_id_2 = $1",
        user_id
    )

    # Mutual connections count
    mutual_count = 0
    if requester_id and requester_id != user_id:
        mutual_rows = await db_manager.pg_pool.fetch(
            """
            SELECT connected_user FROM (
                SELECT CASE WHEN user_id_1 = $1 THEN user_id_2 ELSE user_id_1 END AS connected_user
                FROM connections WHERE user_id_1 = $1 OR user_id_2 = $1
            ) a
            WHERE connected_user IN (
                SELECT CASE WHEN user_id_1 = $2 THEN user_id_2 ELSE user_id_1 END
                FROM connections WHERE user_id_1 = $2 OR user_id_2 = $2
            )
            AND connected_user != $1
            AND connected_user != $2
            """,
            user_id, requester_id
        )
        mutual_count = len(mutual_rows)

    profile = {
        "user_id": row["user_id"],
        "username": row["username"],
        "bio": row["bio"],
        "tier_level": row["tier_level"],
        "city": row["city"],
        "region": row["region"],
        "connection_count": conn_count,
        "mutual_connections": mutual_count,
        "joined_at": row["created_at"].isoformat() if row["created_at"] else None,
        "karma_score": row["karma_score"] if (row["show_karma_score"] or is_own_profile) else None,
        "last_seen": row["last_seen"].isoformat() if (row["show_last_seen"] or is_own_profile) and row["last_seen"] else "Hidden",
    }

    return profile



@router.delete(
    "/remove",
    summary="Remove a connection",
)
async def remove_connection(body: RemoveConnectionBody):
    result = await db_manager.pg_pool.execute(
        """
        DELETE FROM connections
        WHERE (user_id_1 = $1 AND user_id_2 = $2)
           OR (user_id_1 = $2 AND user_id_2 = $1)
        """,
        body.user_id, body.other_user_id
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Connection not found.")

    return {"message": "Connection removed successfully."}


@router.post(
    "/block",
    status_code=status.HTTP_201_CREATED,
    summary="Block a user",
)
async def block_user(body: BlockBody):
    if body.blocker_id == body.blocked_id:
        raise HTTPException(status_code=400, detail="You cannot block yourself.")

    await db_manager.pg_pool.execute(
        """
        INSERT INTO blocked_users (blocker_id, blocked_id)
        VALUES ($1, $2)
        ON CONFLICT DO NOTHING
        """,
        body.blocker_id, body.blocked_id
    )

    async with db_manager.pg_pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM connections
            WHERE (user_id_1 = $1 AND user_id_2 = $2)
               OR (user_id_1 = $2 AND user_id_2 = $1)
            """,
            body.blocker_id, body.blocked_id
        )
        await conn.execute(
            """
            DELETE FROM follow_requests
            WHERE (sender_id = $1 AND receiver_id = $2)
               OR (sender_id = $2 AND receiver_id = $1)
            """,
            body.blocker_id, body.blocked_id
        )

    return {"message": f"User '{body.blocked_id}' has been blocked."}


@router.delete(
    "/unblock",
    summary="Unblock a user",
)
async def unblock_user(body: BlockBody):
    result = await db_manager.pg_pool.execute(
        "DELETE FROM blocked_users WHERE blocker_id = $1 AND blocked_id = $2",
        body.blocker_id, body.blocked_id
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Block not found.")

    return {"message": f"User '{body.blocked_id}' has been unblocked."}


@router.get(
    "/blocked/{user_id}",
    summary="Get list of blocked users",
)
async def get_blocked_users(user_id: str):
    rows = await db_manager.pg_pool.fetch(
        "SELECT blocked_id, created_at FROM blocked_users WHERE blocker_id = $1 ORDER BY created_at DESC",
        user_id
    )
    return {
        "user_id": user_id,
        "total": len(rows),
        "blocked_users": [
            {"blocked_id": r["blocked_id"], "blocked_at": r["created_at"].isoformat()}
            for r in rows
        ]
    }



@router.post(
    "/report",
    status_code=status.HTTP_201_CREATED,
    summary="Report a user",
)
async def report_user(body: ReportBody):
    if body.reporter_id == body.reported_id:
        raise HTTPException(status_code=400, detail="You cannot report yourself.")

    row = await db_manager.pg_pool.fetchrow(
        """
        INSERT INTO reported_users (reporter_id, reported_id, reason)
        VALUES ($1, $2, $3)
        RETURNING id, created_at
        """,
        body.reporter_id, body.reported_id, body.reason
    )

    return {
        "report_id": str(row["id"]),
        "message": "Report submitted. Our team will review it.",
        "created_at": row["created_at"].isoformat(),
    }



@router.post(
    "/mute",
    status_code=status.HTTP_201_CREATED,
    summary="Mute a user — stop notifications from them",
)
async def mute_user(body: MuteBody):
    if body.muter_id == body.muted_id:
        raise HTTPException(status_code=400, detail="You cannot mute yourself.")

    await db_manager.pg_pool.execute(
        """
        INSERT INTO muted_users (muter_id, muted_id)
        VALUES ($1, $2)
        ON CONFLICT DO NOTHING
        """,
        body.muter_id, body.muted_id
    )

    return {"message": f"User '{body.muted_id}' has been muted."}


@router.delete(
    "/unmute",
    summary="Unmute a user",
)
async def unmute_user(body: MuteBody):
    result = await db_manager.pg_pool.execute(
        "DELETE FROM muted_users WHERE muter_id = $1 AND muted_id = $2",
        body.muter_id, body.muted_id
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Mute not found.")

    return {"message": f"User '{body.muted_id}' has been unmuted."}


@router.patch(
    "/privacy/{user_id}",
    summary="Update privacy settings",
)
async def update_privacy(user_id: str, body: PrivacySettingsBody):
    updates = []
    values = []
    idx = 1

    if body.profile_visibility is not None:
        updates.append(f"profile_visibility = ${idx}")
        values.append(body.profile_visibility)
        idx += 1

    if body.show_karma_score is not None:
        updates.append(f"show_karma_score = ${idx}")
        values.append(body.show_karma_score)
        idx += 1

    if body.show_last_seen is not None:
        updates.append(f"show_last_seen = ${idx}")
        values.append(body.show_last_seen)
        idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="No privacy settings provided.")

    values.append(user_id)
    result = await db_manager.pg_pool.execute(
        f"UPDATE users SET {', '.join(updates)} WHERE user_id = ${idx}",
        *values
    )

    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found.")

    return {"message": "Privacy settings updated.", "user_id": user_id}


@router.post(
    "/share",
    status_code=status.HTTP_201_CREATED,
    summary="Share Instagram, LinkedIn or Twitter handle with a connection",
)
async def share_detail(body: ShareDetailBody):
    if not await are_connected(body.shared_by, body.shared_with):
        raise HTTPException(
            status_code=403,
            detail="You can only share details with your connections."
        )

    row = await db_manager.pg_pool.fetchrow(
        """
        INSERT INTO shared_details (shared_by, shared_with, detail_type, detail_value)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (shared_by, shared_with, detail_type)
        DO UPDATE SET detail_value = EXCLUDED.detail_value
        RETURNING id, created_at
        """,
        body.shared_by, body.shared_with, body.detail_type, body.detail_value
    )

    return {
        "message": f"{body.detail_type.capitalize()} handle shared successfully.",
        "detail_id": str(row["id"]),
        "shared_with": body.shared_with,
    }


@router.delete(
    "/share",
    summary="Remove a shared social handle",
)
async def remove_shared_detail(body: RemoveShareBody):
    result = await db_manager.pg_pool.execute(
        """
        DELETE FROM shared_details
        WHERE shared_by = $1 AND shared_with = $2 AND detail_type = $3
        """,
        body.shared_by, body.shared_with, body.detail_type
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Shared detail not found.")

    return {"message": f"{body.detail_type.capitalize()} handle removed."}


@router.get(
    "/shared/{user_id}",
    summary="Get all social handles shared with you by your connections",
)
async def get_shared_details(user_id: str):
    rows = await db_manager.pg_pool.fetch(
        """
        SELECT shared_by, detail_type, detail_value, created_at
        FROM shared_details
        WHERE shared_with = $1
        ORDER BY created_at DESC
        """,
        user_id
    )
    return {
        "user_id": user_id,
        "total": len(rows),
        "shared_details": [
            {
                "shared_by": r["shared_by"],
                "detail_type": r["detail_type"],
                "detail_value": r["detail_value"],
                "shared_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }
