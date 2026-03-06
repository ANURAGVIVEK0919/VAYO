"""
Events API Router
Endpoints:
- POST   /api/v1/events                        — Create event
- GET    /api/v1/events                        — List all events
- GET    /api/v1/events/{event_id}             — Get event details
- POST   /api/v1/events/{event_id}/rsvp        — RSVP to event
- POST   /api/v1/events/{event_id}/checkin     — GPS check-in
"""

import uuid
import math
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from .database import db_manager
from .karma_module import add_karma, has_required_karma

router = APIRouter(prefix="/api/v1", tags=["Events"])



CHECKIN_RADIUS_METERS = 200  


class CreateEventRequest(BaseModel):
    title: str = Field(..., description="Event title")
    description: Optional[str] = Field(None, description="Event description")
    host_id: str = Field(..., description="User ID of the host")
    min_karma_required: int = Field(0, ge=0, description="Minimum karma to RSVP")
    entry_fee: int = Field(0, ge=0, description="Entry fee in INR")
    max_participants: Optional[int] = Field(None, description="Max attendees (None = unlimited)")
    event_date: datetime = Field(..., description="Event date and time")
    latitude: Optional[float] = Field(None, description="Event location latitude")
    longitude: Optional[float] = Field(None, description="Event location longitude")


class RSVPRequest(BaseModel):
    user_id: str = Field(..., description="User ID of the attendee")


class CheckinRequest(BaseModel):
    user_id: str = Field(..., description="User ID checking in")
    latitude: float = Field(..., description="User's current latitude")
    longitude: float = Field(..., description="User's current longitude")



def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate distance in meters between two GPS coordinates.
    """
    R = 6_371_000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


@router.post(
    "/events",
    status_code=status.HTTP_201_CREATED,
    summary="Create a new event. Host automatically receives HOST_EVENT karma.",
)
async def create_event(body: CreateEventRequest):
    """
    Creates an event and awards HOST_EVENT karma to the host.
    """

    host_row = await db_manager.pg_pool.fetchrow(
        "SELECT user_id FROM users WHERE user_id = $1",
        body.host_id
    )
    if host_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Host '{body.host_id}' not found."
        )

    event_id = f"evt_{uuid.uuid4().hex[:10]}"

    await db_manager.pg_pool.execute(
        """
        INSERT INTO events (
            event_id, title, description, host_id,
            min_karma_required, entry_fee, max_participants,
            event_date, latitude, longitude
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """,
        event_id,
        body.title,
        body.description,
        body.host_id,
        body.min_karma_required,
        body.entry_fee,
        body.max_participants,
        body.event_date,
        body.latitude,
        body.longitude,
    )

   
    await add_karma(body.host_id, "HOST_EVENT", event_id=event_id)

    return {
        "event_id": event_id,
        "title": body.title,
        "host_id": body.host_id,
        "message": "Event created successfully. HOST_EVENT karma awarded.",
    }




@router.get(
    "/events",
    summary="List all upcoming events",
)
async def list_events(limit: int = 20, offset: int = 0):
    """
    Returns upcoming events ordered by event_date.
    """

    rows = await db_manager.pg_pool.fetch(
        """
        SELECT
            event_id, title, description, host_id,
            min_karma_required, entry_fee, max_participants,
            event_date, latitude, longitude, created_at
        FROM events
        WHERE event_date >= NOW()
        ORDER BY event_date ASC
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
    )

    return {"events": [dict(r) for r in rows]}




@router.get(
    "/events/{event_id}",
    summary="Get event details including participant count",
)
async def get_event(event_id: str):
    """
    Returns event details + current participant count.
    """

    row = await db_manager.pg_pool.fetchrow(
        """
        SELECT
            event_id, title, description, host_id,
            min_karma_required, entry_fee, max_participants,
            event_date, latitude, longitude, created_at
        FROM events
        WHERE event_id = $1
        """,
        event_id,
    )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event '{event_id}' not found."
        )


    count_row = await db_manager.pg_pool.fetchrow(
        """
        SELECT COUNT(*) as participant_count
        FROM event_participants
        WHERE event_id = $1 AND payment_status = 'paid'
        """,
        event_id,
    )

    event = dict(row)
    event["participant_count"] = count_row["participant_count"]

    return event




@router.post(
    "/events/{event_id}/rsvp",
    status_code=status.HTTP_200_OK,
    summary="RSVP to an event. Blocked if karma too low or event full.",
)
async def rsvp_event(event_id: str, body: RSVPRequest):
    """
    RSVP flow:
    1. Check event exists
    2. Check user karma meets min_karma_required
    3. Check event is not full
    4. Check user hasn't already RSVP'd
    5. Insert participant + award EVENT_RSVP karma
    """


    event = await db_manager.pg_pool.fetchrow(
        "SELECT * FROM events WHERE event_id = $1",
        event_id
    )
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event '{event_id}' not found."
        )

    
    user_row = await db_manager.pg_pool.fetchrow(
        "SELECT user_id FROM users WHERE user_id = $1",
        body.user_id
    )
    if user_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{body.user_id}' not found."
        )


    if event["min_karma_required"] > 0:
        eligible = await has_required_karma(body.user_id, event["min_karma_required"])
        if not eligible:
            user_karma = await db_manager.pg_pool.fetchrow(
                "SELECT karma_points FROM users WHERE user_id = $1",
                body.user_id
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Not enough karma. "
                    f"Required: {event['min_karma_required']}, "
                    f"You have: {user_karma['karma_points']}."
                )
            )

    # Check event capacity
    if event["max_participants"] is not None:
        count_row = await db_manager.pg_pool.fetchrow(
            "SELECT COUNT(*) as count FROM event_participants WHERE event_id = $1",
            event_id
        )
        if count_row["count"] >= event["max_participants"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Event is fully booked."
            )

  
    existing = await db_manager.pg_pool.fetchrow(
        "SELECT id FROM event_participants WHERE event_id = $1 AND user_id = $2",
        event_id,
        body.user_id,
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already RSVP'd to this event."
        )

  
    payment_status = "pending" if event["entry_fee"] > 0 else "paid"

    await db_manager.pg_pool.execute(
        """
        INSERT INTO event_participants (user_id, event_id, payment_status)
        VALUES ($1, $2, $3)
        """,
        body.user_id,
        event_id,
        payment_status,
    )

    # Award EVENT_RSVP karma
    await add_karma(body.user_id, "EVENT_RSVP", event_id=event_id)

    return {
        "event_id": event_id,
        "user_id": body.user_id,
        "payment_status": payment_status,
        "message": "RSVP successful. EVENT_RSVP karma awarded.",
    }




@router.post(
    "/events/{event_id}/checkin",
    status_code=status.HTTP_200_OK,
    summary="GPS check-in. Must be within 200m of event. Awards GPS_CHECKIN karma.",
)
async def checkin_event(event_id: str, body: CheckinRequest):
    """
    Check-in flow:
    1. Check event exists and has GPS coordinates
    2. Verify user has RSVP'd
    3. Verify user is within 200m of event location
    4. Mark attendance + award GPS_CHECKIN karma
    """


    event = await db_manager.pg_pool.fetchrow(
        "SELECT * FROM events WHERE event_id = $1",
        event_id
    )
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event '{event_id}' not found."
        )

    if event["latitude"] is None or event["longitude"] is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This event does not have a GPS location set. Check-in not available."
        )

   
    participant = await db_manager.pg_pool.fetchrow(
        """
        SELECT id, attendance_status
        FROM event_participants
        WHERE event_id = $1 AND user_id = $2
        """,
        event_id,
        body.user_id,
    )
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must RSVP before checking in."
        )

  
    if participant["attendance_status"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already checked in to this event."
        )

    # Verify GPS distance
    distance = haversine_distance(
        body.latitude, body.longitude,
        event["latitude"], event["longitude"]
    )

    if distance > CHECKIN_RADIUS_METERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"You are too far from the event location. "
                f"Distance: {round(distance)}m, Required: within {CHECKIN_RADIUS_METERS}m."
            )
        )

   
    await db_manager.pg_pool.execute(
        """
        UPDATE event_participants
        SET attendance_status = TRUE
        WHERE event_id = $1 AND user_id = $2
        """,
        event_id,
        body.user_id,
    )

    await add_karma(body.user_id, "GPS_CHECKIN", event_id=event_id)

    return {
        "event_id": event_id,
        "user_id": body.user_id,
        "distance_meters": round(distance),
        "checked_in": True,
        "message": "Check-in successful. GPS_CHECKIN karma awarded.",
    }
