"""
Chat API Router
"""

import json
import logging
import os

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from .database import db_manager
from .karma_models import KarmaTier, TIER_CONFIG

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Chat"])

_redis_client = None


async def get_redis():
    """Returns shared Redis client, initializing once if needed."""
    global _redis_client
    if _redis_client is None:
        redis_url = os.getenv("REDIS_BROKER_URL", "redis://localhost:6379/0")
        _redis_client = await aioredis.from_url(
            redis_url, encoding="utf-8", decode_responses=True
        )
    return _redis_client

class SendMessageRequest(BaseModel):
    sender_id: str = Field(..., description="User ID of the sender")
    receiver_id: str = Field(..., description="User ID of the receiver")
    content: str = Field(..., min_length=1, max_length=2000, description="Message content")


async def check_can_message(sender_id: str, receiver_id: str):
    """
    Checks if sender is allowed to message receiver.
    Rules:
    - Sender tier level must be >= receiver tier level (higher can reach down)
    - Sender karma must meet receiver's inbox shield threshold
    Raises HTTPException if not allowed.
    """

    rows = await db_manager.pg_pool.fetch(
        """
        SELECT user_id, karma_score, tier_level, inbox_shield_threshold
        FROM users
        WHERE user_id = ANY($1::text[])
        """,
        [sender_id, receiver_id]
    )

    users = {r["user_id"]: dict(r) for r in rows}

    if sender_id not in users:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sender '{sender_id}' not found."
        )
    if receiver_id not in users:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Receiver '{receiver_id}' not found."
        )

    sender = users[sender_id]
    receiver = users[receiver_id]


    shield = receiver["inbox_shield_threshold"] or 0
    if sender["karma_score"] < shield:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Your karma ({sender['karma_score']}) is below "
                f"this user's inbox shield ({shield})."
            )
        )

    sender_tier = sender["tier_level"]
    receiver_tier = receiver["tier_level"]

    if sender_tier and receiver_tier:
        try:
            sender_level = TIER_CONFIG[KarmaTier(sender_tier)]["level"]
            receiver_level = TIER_CONFIG[KarmaTier(receiver_tier)]["level"]

            if sender_level < receiver_level:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        f"Your tier ({sender_tier}) cannot message "
                        f"a higher tier ({receiver_tier})."
                    )
                )
        except ValueError:
            pass  


@router.post(
    "/chat/send",
    status_code=status.HTTP_201_CREATED,
    summary="Send a message. Karma gate and inbox shield are checked.",
)
async def send_message(body: SendMessageRequest):
    if body.sender_id == body.receiver_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot send a message to yourself."
        )

    await check_can_message(body.sender_id, body.receiver_id)

    row = await db_manager.pg_pool.fetchrow(
        """
        INSERT INTO messages (sender_id, receiver_id, content)
        VALUES ($1, $2, $3)
        RETURNING id, sender_id, receiver_id, content, is_read, created_at
        """,
        body.sender_id,
        body.receiver_id,
        body.content,
    )

    message = dict(row)
    message["id"] = str(message["id"])
    message["created_at"] = message["created_at"].isoformat()

    realtime_delivered = False
    try:
        rc = await get_redis()
        await rc.publish(f"chat_{body.receiver_id}", json.dumps(message))
        realtime_delivered = True
        logger.info(f"Message published to chat_{body.receiver_id}")
    except Exception as e:
        logger.error(f"Redis publish failed: {e} — message saved to DB only")

    return {
        "message_id": message["id"],
        "sender_id": body.sender_id,
        "receiver_id": body.receiver_id,
        "content": body.content,
        "created_at": message["created_at"],
        "realtime_delivered": realtime_delivered,
    }


@router.get(
    "/chat/conversations/{user_id}",
    summary="List all conversations with latest message and unread count",
)
async def get_conversations(user_id: str):
    rows = await db_manager.pg_pool.fetch(
        """
        SELECT
            other_user,
            content        AS last_message,
            created_at     AS last_message_at,
            sender_id,
            unread_count
        FROM (
            SELECT
                CASE
                    WHEN sender_id = $1 THEN receiver_id
                    ELSE sender_id
                END AS other_user,
                content,
                created_at,
                sender_id,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        CASE WHEN sender_id = $1 THEN receiver_id ELSE sender_id END
                    ORDER BY created_at DESC
                ) AS rn,
                COUNT(*) FILTER (
                    WHERE receiver_id = $1 AND is_read = FALSE
                ) OVER (
                    PARTITION BY
                        CASE WHEN sender_id = $1 THEN receiver_id ELSE sender_id END
                ) AS unread_count
            FROM messages
            WHERE sender_id = $1 OR receiver_id = $1
        ) sub
        WHERE rn = 1
        ORDER BY last_message_at DESC
        """,
        user_id,
    )

    conversations = []
    for r in rows:
        c = dict(r)
        c["last_message_at"] = c["last_message_at"].isoformat()
        conversations.append(c)

    return {
        "user_id": user_id,
        "total_conversations": len(conversations),
        "conversations": conversations,
    }


@router.get(
    "/chat/{user_id}/{other_user_id}",
    summary="Get conversation history between two users",
)
async def get_conversation(
    user_id: str,
    other_user_id: str,
    limit: int = 50,
    offset: int = 0
):
    rows = await db_manager.pg_pool.fetch(
        """
        SELECT id, sender_id, receiver_id, content, is_read, created_at
        FROM messages
        WHERE (sender_id = $1 AND receiver_id = $2)
           OR (sender_id = $2 AND receiver_id = $1)
        ORDER BY created_at ASC
        LIMIT $3 OFFSET $4
        """,
        user_id,
        other_user_id,
        limit,
        offset,
    )

    messages = []
    for r in rows:
        m = dict(r)
        m["id"] = str(m["id"])
        m["created_at"] = m["created_at"].isoformat()
        messages.append(m)

    return {
        "user_id": user_id,
        "other_user_id": other_user_id,
        "total": len(messages),
        "messages": messages,
    }


@router.patch(
    "/chat/{message_id}/read",
    status_code=status.HTTP_200_OK,
    summary="Mark a message as read",
)
async def mark_message_read(message_id: str):
    result = await db_manager.pg_pool.execute(
        "UPDATE messages SET is_read = TRUE WHERE id = $1",
        message_id,
    )

    if result == "UPDATE 0":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Message '{message_id}' not found."
        )

    return {
        "message_id": message_id,
        "is_read": True,
        "message": "Message marked as read.",
    }
