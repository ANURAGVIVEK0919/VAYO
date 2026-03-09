"""
FastAPI Endpoints for Community Matching System
"""
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import socketio
import os

from .models import (
    UserProfileInput,
    TaskStatusResponse,
    MatchResult,
    MatchTier
)
from .celery_tasks import process_match_task
from .database import db_manager
from .cache import cache_manager
from .websocket_server import sio, initialize_redis, start_background_tasks, cleanup
from .webhooks import router as webhooks_router
from .dependencies import get_current_user
from .preferences import router as preferences_router
from .karma import router as karma_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup resources"""
    # Startup
    await db_manager.initialize_postgres()
    db_manager.initialize_pinecone()
    await initialize_redis()
    await start_background_tasks()
    yield
    # Shutdown
    await db_manager.close()
    await cleanup()


app = FastAPI(
    title="AI-Powered Community Matching System v2.0",
    description="Intelligent onboarding with <2s matching using hybrid algorithms",
    version="2.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure based on your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Clerk webhook router — must be included BEFORE any auth-protected routes
app.include_router(webhooks_router)

# User onboarding preferences router
app.include_router(preferences_router)

# Karma points system router
app.include_router(karma_router)

# Mount static files for demo
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.post("/api/v1/match", response_model=TaskStatusResponse, status_code=202)
async def initiate_match(profile: UserProfileInput):
    """
    Phase 1: Ingestion - Return Task ID immediately (<50ms)
    
    Client-side validation already performed.
    Returns Task ID for async processing.
    """
    # Dispatch to Celery (async)
    task = process_match_task.apply_async(
        kwargs={"user_data": profile.dict()},
        task_id=None,  # Auto-generate
        expires=10  # Task expires after 10 seconds
    )
    
    return TaskStatusResponse(
        task_id=task.id,
        status="processing",
        estimated_time_ms=2000,
        websocket_channel=f"match_updates_{profile.user_id}"
    )


@app.get("/api/v1/match/{task_id}", response_model=MatchResult)
async def get_match_result(task_id: str):
    """
    Poll for match result
    
    Alternative to WebSocket for clients that prefer HTTP polling
    """
    task = process_match_task.AsyncResult(task_id)
    
    if task.state == 'PENDING':
        raise HTTPException(status_code=202, detail="Task is still processing")
    elif task.state == 'PROCESSING':
        raise HTTPException(
            status_code=202, 
            detail=f"Task in progress: {task.info.get('step', 'unknown')}"
        )
    elif task.state == 'SUCCESS':
        return MatchResult(**task.result)
    elif task.state == 'FAILED':
        raise HTTPException(status_code=500, detail=str(task.info))
    else:
        raise HTTPException(status_code=500, detail="Unknown task state")


@app.get("/api/v1/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "postgres": "connected" if db_manager.pg_pool else "disconnected",
        "pinecone": "connected" if db_manager.pinecone_index else "disconnected",
        "redis": "connected"
    }


@app.get("/api/v1/popular-communities")
async def get_popular_communities(limit: int = 10):
    """Get popular communities (for fallback UI)"""
    communities = await db_manager.get_popular_communities(limit=limit)
    return {"communities": communities}


# Mount Socket.io ASGI app
socket_app = socketio.ASGIApp(
    sio,
    other_asgi_app=app,
    socketio_path='/socket.io'
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "matching_system.api:socket_app",  # Use socket_app instead of app
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
