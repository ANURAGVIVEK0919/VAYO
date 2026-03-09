"""
Celery Tasks for Async Match Processing
"""
from dotenv import load_dotenv
import os
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import logging
import asyncio
import time
from typing import Dict, List
from collections import Counter

from celery import Celery
from celery.signals import worker_process_init

from .database import db_manager
from .ai_services import ai_service
from .models import MatchTier, CommunityMatch, MatchResult
from .cache import cache_manager

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

celery_app = Celery(
    "matching_system",
    broker=os.getenv("REDIS_BROKER_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_BACKEND_URL", "redis://localhost:6379/1"),
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
)


worker_loop = None

@worker_process_init.connect
def init_worker(**kwargs):
    global worker_loop
    worker_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(worker_loop)

    worker_loop.run_until_complete(db_manager.initialize_postgres())
    db_manager.initialize_pinecone()

def run_async(coro):
    return worker_loop.run_until_complete(coro)


@celery_app.task(name="process_match_task")
def process_match_task(user_data: Dict) -> Dict:

    task_id = process_match_task.request.id
    start_time = time.time()

    try:

        sanitized_bio, enriched_tags, pii_removed = ai_service.sanitize_and_enrich_profile(
            user_data["bio"],
            user_data["interest_tags"],
        )

        embedding_text = ai_service.create_embedding_payload(
            sanitized_bio,
            enriched_tags,
        )

        
        user_vector = cache_manager.get_user_vector(user_data["user_id"])

        
        if user_vector is None:
            user_vector = db_manager.get_user_vector_from_pinecone(
                user_data["user_id"]
            )
            if user_vector is not None:
                cache_manager.set_user_vector(
                    user_data["user_id"], user_vector
                )

       
        if user_vector is None:
            user_vector = ai_service.generate_embedding(embedding_text)
            cache_manager.set_user_vector(
                user_data["user_id"], user_vector
            )
            db_manager.save_user_vector_to_pinecone(
                user_data["user_id"], user_vector
            )

        communities = run_async(
            db_manager.filter_communities_by_location(
                user_data["city"],
                user_data["timezone"],
            )
        )

        if not communities:
            popular = run_async(db_manager.get_popular_communities())
            matches = [
                CommunityMatch(
                    community_id=c["community_id"],
                    community_name=c["community_name"],
                    category=c["category"],
                    match_score=0.0,
                    member_count=c["member_count"],
                    recent_activity=c.get("recent_activity", 0),
                )
                for c in popular
            ]

            result = MatchResult(
                task_id=task_id,
                user_id=user_data["user_id"],
                tier=MatchTier.FALLBACK,
                matches=matches,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

            return result.model_dump(mode="json")


        community_ids = [c["community_id"] for c in communities]

        vector_results = db_manager.vector_search(
            query_vector=user_vector,
            community_ids=community_ids,
            top_k=10,
        )


        community_map = {c["community_id"]: c for c in communities}

        enriched_results = []
        for v in vector_results:
            cid = v["community_id"]
            if cid in community_map:
                sql_data = community_map[cid]
                enriched_results.append({
                    "community_id": cid,
                    "community_name": sql_data["community_name"],
                    "category": sql_data["category"],
                    "match_score": v["match_score"],
                    "member_count": sql_data["member_count"],
                    "recent_activity": sql_data["recent_activity"]
                })

        if not enriched_results:
            return {
                "task_id": task_id,
                "status": "failed",
                "error": "No matching communities after merge"
            }

        enriched_results = _apply_diversity(enriched_results)

        top_score = enriched_results[0]["match_score"]

        if top_score > 0.87:
            tier = MatchTier.SOULMATE
        elif top_score < 0.55:
            tier = MatchTier.FALLBACK
        else:
            tier = MatchTier.EXPLORER

        matches = [
            CommunityMatch(**m)
            for m in enriched_results[:5]
        ]

        result = MatchResult(
            task_id=task_id,
            user_id=user_data["user_id"],
            tier=tier,
            matches=matches,
            processing_time_ms=int((time.time() - start_time) * 1000),
        )

        cache_manager.publish_match_result(
            user_data["user_id"], result.model_dump(mode="json")
        )

        return result.model_dump(mode="json")

    except Exception as e:
        logger.exception("Task failed")
        return {
            "task_id": task_id,
            "status": "failed",
            "error": str(e),
        }


def _apply_diversity(matches: List[Dict]):

    if len(matches) < 4:
        return matches

    top_categories = [m["category"] for m in matches[:3]]
    category_counts = Counter(top_categories)

    if len(category_counts) == 1:
        dominant = top_categories[0]
        for i in range(3, len(matches)):
            if matches[i]["category"] != dominant:
                diverse = matches.pop(i)
                matches.insert(2, diverse)
                break

    return matches
