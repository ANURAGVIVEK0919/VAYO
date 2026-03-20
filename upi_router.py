from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from .database import db_manager
from pydantic import BaseModel, Field
import qrcode
import io

router = APIRouter(prefix="/api/v1", tags=["UPI"])

class UpdateUPIRequest(BaseModel):
    upi_id: str = Field(...)

@router.put("/users/{user_id}/upi")
async def update_upi_id(user_id: str, request: UpdateUPIRequest, requesting_user: str):
    async with db_manager.pg_pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT user_id FROM users WHERE user_id = $1",
            user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if requesting_user != user_id:
            raise HTTPException(status_code=403, detail="You can only update your own UPI ID")
        upi_id = request.upi_id
        if "@" not in upi_id:
            raise HTTPException(status_code=400, detail="Invalid UPI ID format. Example: name@paytm")
        await conn.execute(
            "UPDATE users SET upi_id = $2 WHERE user_id = $1",
            user_id, upi_id
        )
        return {
            "user_id": user_id,
            "upi_id": upi_id,
            "message": "UPI ID updated successfully"
        }

@router.get("/users/{user_id}/upi")
async def get_upi_details(user_id: str):
    async with db_manager.pg_pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT user_id, username, upi_id FROM users WHERE user_id = $1",
            user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {
            "user_id": user["user_id"],
            "username": user["username"],
            "upi_id": user["upi_id"] if user["upi_id"] else None,
            "has_upi": bool(user["upi_id"])
        }

@router.get("/users/{user_id}/upi/qr")
async def generate_upi_qr(user_id: str, amount: int = 0):
    async with db_manager.pg_pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT username, upi_id FROM users WHERE user_id = $1",
            user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        upi_id = user["upi_id"]
        username = user["username"]
        if not upi_id:
            raise HTTPException(status_code=400, detail="User has not set up UPI ID")
        if amount > 0:
            upi_url = f"upi://pay?pa={upi_id}&pn={username}&am={amount/100:.2f}&cu=INR"
        else:
            upi_url = f"upi://pay?pa={upi_id}&pn={username}&cu=INR"
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(upi_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="image/png",
            headers={
                "Content-Disposition": f"inline; filename=upi_qr_{user_id}.png"
            }
        )
