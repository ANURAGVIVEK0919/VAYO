"""
Database Layer - PostgreSQL & Pinecone Integration
"""
import asyncpg
from pinecone import Pinecone, ServerlessSpec
from typing import List, Dict, Optional, Tuple
import os
from datetime import datetime, timedelta


class DatabaseManager:
    """Manages PostgreSQL and Pinecone connections"""
    
    def __init__(self):
        self.pg_pool: Optional[asyncpg.Pool] = None
        self.pinecone_client = None
        self.pinecone_index = None
        
    async def initialize_postgres(self):
        """Initialize PostgreSQL connection pool"""
        self.pg_pool = await asyncpg.create_pool(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", 5432)),
            user=os.getenv("POSTGRES_USER", "postgres"),
            password=os.getenv("POSTGRES_PASSWORD"),
            database=os.getenv("POSTGRES_DB", "community_matching"),
            min_size=10,
            max_size=50,
            command_timeout=5
        )
        
    def initialize_pinecone(self):
        """Initialize Pinecone vector database"""
        api_key = os.getenv("PINECONE_API_KEY")
        self.pinecone_client = Pinecone(api_key=api_key)
        
        index_name = os.getenv("PINECONE_INDEX_NAME", "community-vectors")
        
        # Create index if it doesn't exist
        if index_name not in self.pinecone_client.list_indexes().names():
            self.pinecone_client.create_index(
                name=index_name,
                dimension=1536,  # text-embedding-3-small dimension
                metric="cosine",
                spec=ServerlessSpec(cloud="aws", region="us-east-1")
            )
        
        self.pinecone_index = self.pinecone_client.Index(index_name)
    
    async def filter_communities_by_location(
        self, 
        city: str, 
        timezone: str, 
        limit: int = 1000
    ) -> List[Dict]:
        """
        Phase 1: SQL filtering by City + Timezone
        Reduces search space by ~95%
        """
        query = """
            SELECT 
                c.community_id,
                c.community_name,
                c.category,
                c.member_count,
                c.city,
                c.timezone,
                COALESCE(ca.message_count, 0) as recent_activity
            FROM communities c
            LEFT JOIN (
                SELECT 
                    community_id, 
                    COUNT(*) as message_count
                FROM community_activity
                WHERE created_at >= NOW() - INTERVAL '7 days'
                GROUP BY community_id
            ) ca ON c.community_id = ca.community_id
            WHERE 
                c.city = $1 
                AND c.timezone = $2
                AND c.is_active = true
            ORDER BY c.member_count DESC, ca.message_count DESC
            LIMIT $3;
        """
        
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(query, city, timezone, limit)
            return [dict(row) for row in rows]
    
    def vector_search(
        self, 
        query_vector: List[float], 
        community_ids: List[str], 
        top_k: int = 20
    ) -> List[Dict]:
        """
        Phase 2: Cosine similarity search on filtered subset
        Returns top_k matches with scores
        """
        # Filter parameter restricts search to pre-filtered community IDs
        results = self.pinecone_index.query(
            vector=query_vector,
            top_k=top_k,
            include_metadata=True,
            filter={"community_id": {"$in": community_ids}}
        )
        
        matches = []
        for match in results.matches:
            matches.append({
                "community_id": match.metadata.get("community_id"),
                "match_score": float(match.score),
                "metadata": match.metadata
            })
        
        return matches
    
    async def get_community_details(self, community_ids: List[str]) -> Dict[str, Dict]:
        """Fetch full community details from PostgreSQL"""
        query = """
            SELECT 
                community_id,
                community_name,
                category,
                member_count,
                description
            FROM communities
            WHERE community_id = ANY($1::text[]);
        """
        
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(query, community_ids)
            return {row['community_id']: dict(row) for row in rows}
    
    async def get_popular_communities(self, limit: int = 5) -> List[Dict]:
        """Fallback: Get general popular communities"""
        query = """
            SELECT 
                c.community_id,
                c.community_name,
                c.category,
                c.member_count,
                COALESCE(ca.message_count, 0) as recent_activity
            FROM communities c
            LEFT JOIN (
                SELECT 
                    community_id, 
                    COUNT(*) as message_count
                FROM community_activity
                WHERE created_at >= NOW() - INTERVAL '7 days'
                GROUP BY community_id
            ) ca ON c.community_id = ca.community_id
            WHERE c.is_active = true
            ORDER BY c.member_count DESC, ca.message_count DESC
            LIMIT $1;
        """
        
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(query, limit)
            return [dict(row) for row in rows]
    
    async def auto_join_community(self, user_id: str, community_id: str) -> bool:
        """Auto-join user to community (Soulmate tier)"""
        query = """
            INSERT INTO community_members (user_id, community_id, joined_at, auto_joined)
            VALUES ($1, $2, NOW(), true)
            ON CONFLICT (user_id, community_id) DO NOTHING
            RETURNING user_id;
        """
        
        async with self.pg_pool.acquire() as conn:
            result = await conn.fetchrow(query, user_id, community_id)
            return result is not None
    
    async def get_community_members_for_intro(
        self, 
        community_id: str, 
        limit: int = 5
    ) -> List[Dict]:
        """Get active members for AI intro @mention"""
        query = """
            SELECT 
                u.user_id,
                u.username,
                u.bio,
                COUNT(ca.message_id) as message_count
            FROM users u
            JOIN community_members cm ON u.user_id = cm.user_id
            LEFT JOIN community_activity ca ON ca.user_id = u.user_id 
                AND ca.community_id = $1
                AND ca.created_at >= NOW() - INTERVAL '7 days'
            WHERE cm.community_id = $1
            GROUP BY u.user_id, u.username, u.bio
            ORDER BY message_count DESC
            LIMIT $2;
        """
        
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(query, community_id, limit)
            return [dict(row) for row in rows]
    


    async def insert_karma_entry(
        self,
        user_id: str,
        action_type: str,
        point_delta: int,
        reference_id: str = None,
    ) -> str:
        """
        Append a row to karma_ledger.
        The DB trigger automatically updates users.karma_score.
        Returns the new ledger entry UUID.
        """
        query = """
            INSERT INTO karma_ledger (user_id, point_delta, action_type, reference_id)
            VALUES ($1, $2, $3::karma_action_type_enum, $4)
            RETURNING id;
        """
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(query, user_id, point_delta, action_type, reference_id)
            return str(row["id"])

    async def get_karma_score(self, user_id: str) -> Optional[int]:
        """Read the denormalized karma_score from users (O(1))."""
        query = "SELECT karma_score FROM users WHERE user_id = $1;"
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(query, user_id)
            return row["karma_score"] if row else None

    async def get_karma_ledger(
        self, user_id: str, limit: int = 20, offset: int = 0
    ) -> List[Dict]:
        """Return paginated ledger entries for a user, most recent first."""
        query = """
            SELECT id, action_type, point_delta, reference_id, created_at
              FROM karma_ledger
             WHERE user_id = $1
             ORDER BY created_at DESC
             LIMIT $2 OFFSET $3;
        """
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(query, user_id, limit, offset)
            return [dict(row) for row in rows]

    async def get_inbox_shield(self, user_id: str) -> int:
        """Return the user's inbox shield threshold (default 0)."""
        query = "SELECT COALESCE(inbox_shield_threshold, 0) AS t FROM users WHERE user_id = $1;"
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(query, user_id)
            return row["t"] if row else 0

    async def update_inbox_shield(self, user_id: str, threshold: int) -> None:
        """Set the user's inbox shield threshold."""
        query = "UPDATE users SET inbox_shield_threshold = $2 WHERE user_id = $1;"
        async with self.pg_pool.acquire() as conn:
            await conn.execute(query, user_id, threshold)

    async def check_message_eligibility(
        self, sender_id: str, target_id: str
    ) -> Dict:
        """
        Check outbound DM eligibility.
        Rules:
          1. Sender's karma ≥ target's karma  (outbound rule)
          2. Sender's karma ≥ target's inbox_shield_threshold
        """
        query = """
            SELECT user_id,
                   COALESCE(karma_score, 0) AS karma_score,
                   COALESCE(inbox_shield_threshold, 0) AS inbox_shield
              FROM users
             WHERE user_id = ANY($1::text[]);
        """
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(query, [sender_id, target_id])

        lookup = {row["user_id"]: dict(row) for row in rows}
        sender = lookup.get(sender_id, {"karma_score": 0, "inbox_shield": 0})
        target = lookup.get(target_id, {"karma_score": 0, "inbox_shield": 0})

        sender_score = sender["karma_score"]
        target_score = target["karma_score"]
        target_shield = target["inbox_shield"]


        if sender_score < target_score:
            return {
                "allowed": False,
                "reason": f"Your karma ({sender_score}) is below the recipient's karma ({target_score}). You can only message users with equal or lower karma.",
                "sender_score": sender_score,
                "target_score": target_score,
                "target_inbox_shield": target_shield,
            }


        if sender_score < target_shield:
            return {
                "allowed": False,
                "reason": f"The recipient's inbox shield requires a minimum karma of {target_shield}. Your karma is {sender_score}.",
                "sender_score": sender_score,
                "target_score": target_score,
                "target_inbox_shield": target_shield,
            }

        return {
            "allowed": True,
            "reason": None,
            "sender_score": sender_score,
            "target_score": target_score,
            "target_inbox_shield": target_shield,
        }

    async def close(self):
        """Close database connections"""
        if self.pg_pool:
            await self.pg_pool.close()


# Global instance
db_manager = DatabaseManager()
