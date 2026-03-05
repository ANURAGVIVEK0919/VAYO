"""
FastAPI Endpoints - Production Hybrid Matching System
"""
import os
from dotenv import load_dotenv
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from celery.result import AsyncResult

from matching_system.celery_tasks import celery_app, process_match_task
from .models import (
    UserProfileInput,
    TaskStatusResponse,
    MatchResult,
)
from .database import db_manager
from matching_system.karma_router import router as karma_router
from matching_system.events_router import router as events_router



load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))



@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Initialize database connections at startup
    Cleanup on shutdown
    """
    await db_manager.initialize_postgres()
    db_manager.initialize_pinecone()
    yield
    await db_manager.close()



app = FastAPI(
    title="AI-Powered Community Matching System v2.0",
    description="Intelligent onboarding with <2s matching using hybrid algorithms",
    version="2.0.0",
    lifespan=lifespan,
)


app.include_router(karma_router)
app.include_router(events_router)



app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post(
    "/api/v1/match",
    response_model=TaskStatusResponse,
    status_code=202
)
async def initiate_match(profile: UserProfileInput):
    """
    Phase 1:
    Immediately return task ID (async processing).
    """

    task = process_match_task.apply_async(
        kwargs={"user_data": profile.dict()},
        expires=60,
    )

    return TaskStatusResponse(
        task_id=task.id,
        status="processing",
        estimated_time_ms=2000,
        websocket_channel=f"match_updates_{profile.user_id}",
    )



@app.get("/api/v1/match/{task_id}")
async def get_match_result(task_id: str):
    """
    Poll task result.
    Stable + decoupled from Celery internal states.
    """

    task_result = AsyncResult(task_id, app=celery_app)

    if task_result.state in ["PENDING", "STARTED"]:
        return {
            "task_id": task_id,
            "status": "processing"
        }

    if task_result.state == "FAILURE":
        return {
            "task_id": task_id,
            "status": "failed",
            "error": str(task_result.info)
        }

    if task_result.state == "SUCCESS":
        if isinstance(task_result.result, dict):
            return task_result.result
        try:
            return MatchResult(**task_result.result)
        except Exception:
            return task_result.result

    return {
        "task_id": task_id,
        "status": task_result.state.lower()
    }



@app.get("/api/v1/health")
async def health_check():
    return {
        "status": "healthy",
        "postgres": "connected" if db_manager.pg_pool else "disconnected",
        "pinecone": "connected" if db_manager.pinecone_index else "disconnected",
        "redis": "connected"
    }

@app.get("/api/v1/popular-communities")
async def get_popular_communities(limit: int = 10):
    communities = await db_manager.get_popular_communities(limit=limit)
    return {"communities": communities}
