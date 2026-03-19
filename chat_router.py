"""
Chat API Router
Endpoints:
- POST  /api/v1/chat/send                          — Send a message
- GET   /api/v1/chat/conversations/{user_id}       — List all conversations
- GET   /api/v1/chat/{user_id}/{other_user_id}     — Get conversation history
- PATCH /api/v1/chat/{message_id}/read             — Mark message as read

Production Design:
- Own Redis connection pool initialized once on first use (no import from websocket_server)
- PostgreSQL connection pooling via db_manager (asyncpg pool)
- Karma gate + inbox shield enforced on every message
- Tier reach logic: higher tier can message lower, not vice versa
- Real-time delivery via Redis Pub/Sub to WebSocket
- Graceful fallback: message saved to DB even if Redis is down
- last_seen updated on every message send

IMPORTANT — Route order matters:
- /chat/conversations/{user_id} MUST be registered before /chat/{user_id}/{other_user_id}
  otherwise FastAPI will match 'conversations' as other_user_id
"""

import json
import logging
import os
import re

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from .database import db_manager
from .karma_models import KarmaTier, TIER_CONFIG

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Chat"])

# Single shared Redis client — initialized once on first use
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



PERSONAL_INFO_PATTERNS = [
    (r'\b\d{10}\b', "phone number"),
    (r'\+\d{1,3}[-.\s]?\d{5}[-.\s]?\d{5}\b', "phone number"),
    (r'\b(\+\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b', "phone number"),
    (r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', "email address"),
    (r'(https?://)?(www\.)?(wa\.me|whatsapp\.com)/\S+', "WhatsApp link"),
    (r'(https?://)?(t\.me|telegram\.me)/\S+', "Telegram link"),
    (r'(https?://)?(www\.)?snapchat\.com/add/\S+', "Snapchat link"),
    (r'\b\d{12}\b', "Aadhaar number"),
    (r'\b[A-Z]{5}[0-9]{4}[A-Z]\b', "PAN number"),
]


def check_personal_info(content: str):
    """
    Scans message content for personal info like phone numbers,
    emails, Aadhaar, PAN, WhatsApp/Telegram/Snapchat links.
    Raises HTTPException if any are found.
    """
    for pattern, label in PERSONAL_INFO_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Sharing personal information ({label}) is not allowed in chat. Use the sharing feature instead."
            )



async def are_connected(user_a: str, user_b: str) -> bool:
    """
    Returns True if both users have an accepted Karma Connect between them.
    """
    row = await db_manager.pg_pool.fetchrow(
        """
        SELECT 1 FROM connections
        WHERE (user_id_1 = $1 AND user_id_2 = $2)
           OR (user_id_1 = $2 AND user_id_2 = $1)
        """,
        user_a,
        user_b,
    )
    return row is not None



async def update_last_seen(user_id: str):
    """Updates last_seen timestamp for the user. Fire and forget."""
    try:
        await db_manager.pg_pool.execute(
            "UPDATE users SET last_seen = NOW() WHERE user_id = $1",
            user_id
        )
    except Exception as e:
        logger.error(f"Failed to update last_seen for {user_id}: {e}")



async def check_can_message(sender_id: str, receiver_id: str):
    """
    Checks if sender is allowed to message receiver.
    Rules:
    - Both users must exist
    - Must have an accepted Karma Connect
    - Sender karma must meet receiver's inbox shield threshold
    - Sender tier level must be >= receiver tier level
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

    # Connection gate — must be Karma Connected before chatting
    if not await are_connected(sender_id, receiver_id):
        raise HTTPException(
            status_code=403,
            detail="You must be connected to message this user. Send a Karma Connect request first."
        )

    sender = users[sender_id]
    receiver = users[receiver_id]

    # Check inbox shield
    shield = receiver["inbox_shield_threshold"] or 0
    if sender["karma_score"] < shield:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Your karma ({sender['karma_score']}) is below "
                f"this user's inbox shield ({shield})."
            )
        )

    # Check tier reach — sender tier must be >= receiver tier
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
            pass  # Unknown tier — allow for now




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

    check_personal_info(body.content)

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

    await update_last_seen(body.sender_id)

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
