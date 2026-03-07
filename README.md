# AI-Powered Community Matching System v2.0

Intelligent onboarding solution with **<2 second** community matching using hybrid algorithms, a **reputation-based karma system**, and **real-time WebSocket** delivery.

## Architecture

### Tech Stack
- **API**: Python FastAPI (async, Pydantic validation)
- **Task Queue**: Celery + Redis broker
- **Database**: PostgreSQL 16 (user data) + Pinecone (vectors)
- **AI/ML**: OpenAI GPT-4o-mini + text-embedding-3-small
- **Real-time**: WebSocket (Socket.io) + Redis Pub/Sub
- **Auth**: Clerk JWT (RS256, JWKS auto-rotation)

### Matching Pipeline (5 Phases)

```mermaid
graph LR
    A[User Profile] --> B[Sanitization<br/>LLM PII Removal]
    B --> C[Vectorization<br/>1536D Embedding]
    C --> D[Hybrid Matching<br/>SQL + Vector Search]
    D --> E[Decision Engine<br/>Thresholds]
    E --> F{Match Tier?}
    F -->|>0.87| G[Soulmate<br/>Auto-join + AI Intro]
    F -->|0.55-0.87| H[Explorer<br/>Show 3-5 Options]
    F -->|<0.55| I[Fallback<br/>Popular + Update Request]
```

---

## Karma Points System

A reputation-based progression model that governs credibility, access, and interaction quality. Karma is non-consumable, earned through real activity, and required for feature access.

### Tier Architecture

| Level | Tier | Karma Range | Key Unlocks |
|-------|------|-------------|-------------|
| 1 | Beginner | 100 - 299 | DM peers, RSVP to events, Claim Vayo ID |
| 2 | Pathfinder | 300 - 499 | Private group chats |
| 3 | Explorer | 500 - 999 | Host public events, Advanced networking tools |
| 4 | Conqueror | 1000+ | Premium UI (glowing avatar), Custom Vayo ID colors, Algo boost |

### Phase 1 -- Onboarding Boost (0 to 100 Points)

Completing full onboarding yields exactly 100 karma points, placing the user into Beginner tier immediately.

| Action | Points | Frequency |
|--------|--------|-----------|
| Verify email or phone | +20 | Once |
| Upload profile picture | +30 | Once |
| Complete 3 Core Vibe questions | +30 | Once |
| Claim unique Vayo ID | +20 | Once |

### Phase 2 -- Engagement Loop (100 to 500+ Points)

Post-onboarding karma is tied to real-world event participation. Growth intentionally tapers at higher stages.

| Action | Points | Cap |
|--------|--------|-----|
| RSVP to a public event | +10 | Max 3 RSVPs/day |
| GPS check-in at event | +25 | Max 1/event |
| Post photo/update to event feed | +15 | Max 2/event |
| Verified Vibe peer endorsement | +20 | No daily cap |
| Host a community event | TBD | Per event |

### Communication Rules

- **Outbound Rule** -- A user can only initiate DMs with accounts holding equal or lower karma. Prevents cold-messaging by zero-point accounts.
- **Inbox Shield** -- Users set a minimum karma threshold for inbound DMs, giving granular control over who can contact them.
- **Chat Tier Range** -- Group chat participation limited to same tier or +/-1 tier level.

### Karma API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/karma/award` | Append a karma ledger entry, returns updated profile |
| `GET` | `/api/v1/users/{user_id}/karma` | Get score, tier, and optional paginated ledger history |
| `PATCH` | `/api/v1/users/{user_id}/inbox-shield` | Set inbox shield threshold (self-only) |
| `GET` | `/api/v1/users/{user_id}/karma/can-message/{target_user_id}` | Check outbound DM eligibility |

### Karma Data Architecture

The system uses an **append-only `karma_ledger`** table. Every point transaction is an immutable row providing a full audit trail. A PostgreSQL trigger keeps a denormalized `karma_score` column on the `users` table in sync for O(1) read performance.

```sql
-- Ledger structure (simplified)
CREATE TABLE karma_ledger (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    point_delta INTEGER NOT NULL,        -- +20 or -10
    action_type karma_action_type_enum,  -- SIGNUP_EMAIL_VERIFY, EVENT_RSVP, etc.
    reference_id TEXT,                   -- optional event_id or endorsement_id
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Quick Start

### 1. Installation

```bash
# Clone repository
git clone <repo_url>
cd matching_system

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys
```

### 2. Database Setup

```bash
# Create database
psql -c "CREATE DATABASE community_matching;"

# Run base schema
psql -d community_matching -f matching_system/setup_database.sql

# Run Clerk integration migration
psql -d community_matching -f matching_system/clerk_migration.sql

# Run user preferences migration
psql -d community_matching -f matching_system/user_preferences_migration.sql

# Run karma points migration
psql -d community_matching -f matching_system/karma_migration.sql
```

### 3. Start Services

```bash
# Terminal 1: Start FastAPI
uvicorn matching_system.api:app --reload --port 8000

# Terminal 2: Start Celery Worker
celery -A matching_system.celery_tasks worker --loglevel=info --concurrency=4

# Terminal 3: Start Redis (if not running)
redis-server
```

### 4. Test API

**Option 1: Interactive WebSocket Demo (Recommended)**
```
Open in browser: http://localhost:8000/static/websocket_demo.html
```
- Real-time WebSocket connection
- Live match result updates

**Option 2: REST API + Polling**

```bash
# POST request to initiate matching
curl -X POST http://localhost:8000/api/v1/match \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "bio": "Software engineer passionate about AI and machine learning. I code daily in Python.",
    "interest_tags": ["Programming", "AI", "Tech"],
    "city": "San Francisco",
    "timezone": "America/Los_Angeles"
  }'

# Response (immediate <50ms)
{
  "task_id": "abc-123-def",
  "status": "processing",
  "estimated_time_ms": 2000,
  "websocket_channel": "match_updates_user_123"
}

# Poll for results
curl http://localhost:8000/api/v1/match/abc-123-def
```

**Option 3: Karma System Tests**

```bash
# Unit tests (no server required)
python -m matching_system.test_karma

# Integration tests (requires running server + database)
python -m matching_system.test_karma --live
```

---

## Hybrid Matching Algorithm

### Phase A: Location Filter (SQL)
```python
# Reduces search space by ~95%
WHERE city = $1 AND timezone = $2
```

### Phase B: Vector Search (Pinecone)
```python
# Cosine similarity on filtered subset
query(vector=user_embedding, top_k=20, filter={"community_id": {"$in": filtered_ids}})
```

### Phase C: Diversity Injection
```python
# If top 3 matches are same category, inject 1 diverse match at position 2
if all_same_category(top_3):
    inject_diverse_match()
```

## Decision Engine Thresholds

| Tier | Threshold | Action |
|------|-----------|--------|
| **Soulmate** | >0.87 | Auto-join community + AI-generated intro with @mention |
| **Explorer** | 0.55-0.87 | Show 3-5 match options with scores |
| **Fallback** | <0.55 | Show popular communities + request profile update |

## AI Introduction Generator

Triggered on **Soulmate** matches:

1. Fetch user bio + community description
2. Retrieve top 5 active members (7-day activity)
3. Generate friendly intro (max 3 sentences) with GPT-4o-mini
4. Run toxicity check (block if score >0.75)
5. Post to community channel with @mention

## Caching Strategy

| Layer | Data | TTL | Storage |
|-------|------|-----|---------|
| L1 (Browser) | Static assets | 24h | LocalStorage |
| L3 (Redis) | User vectors | 7 days | Redis pickle |
| L3 (Redis) | Group vectors | 24h | Redis pickle |
| L4 (PostgreSQL) | Query results | 15min | Redis JSON |

## Performance Targets

- Task ID response: **<50ms**
- Total matching: **<2000ms**
- Vector embedding: **~300ms**
- Pinecone search: **~100ms**
- SQL filter: **~50ms**

## Configuration

Edit `.env` file:

```bash
# Required
OPENAI_API_KEY=sk-...
PINECONE_API_KEY=...
POSTGRES_PASSWORD=...

# Optional (defaults provided)
REDIS_HOST=localhost
API_PORT=8000
```

## Code Structure

```
matching_system/
├── api.py                    # FastAPI endpoints + router registration
├── karma.py                  # Karma points API routes
├── karma_models.py           # Karma enums, tier logic, Pydantic schemas
├── karma_migration.sql       # Karma ledger table + trigger migration
├── websocket_server.py       # WebSocket + Redis Pub/Sub
├── celery_tasks.py           # process_match_task (main logic)
├── models.py                 # Matching system Pydantic schemas
├── database.py               # PostgreSQL + Pinecone + Karma DB methods
├── ai_services.py            # OpenAI integration
├── cache.py                  # Redis caching
├── dependencies.py           # Clerk JWT auth dependency
├── preferences.py            # User onboarding preferences
├── webhooks.py               # Clerk webhook handlers
├── test_karma.py             # Karma unit + integration tests
├── test_example.py           # Matching system tests
├── test_websocket_client.py  # WebSocket tests
├── setup_database.sql        # Base schema
├── clerk_migration.sql       # Clerk auth migration
├── user_preferences_migration.sql  # Preferences migration
├── static/
│   └── websocket_demo.html   # Interactive demo
├── requirements.txt          # Dependencies
├── WEBSOCKET_GUIDE.md        # WebSocket documentation
└── .env.example              # Config template
```

## Database Migrations (Run Order)

Migrations must be run in order. Each is idempotent and wrapped in a transaction.

| Order | File | What It Does |
|-------|------|--------------|
| 1 | `setup_database.sql` | Base tables: users, communities, community_members, community_activity |
| 2 | `clerk_migration.sql` | Add Clerk auth columns (email, first_name, last_name, etc.) to users |
| 3 | `user_preferences_migration.sql` | user_preferences table with ENUM types for onboarding |
| 4 | `karma_migration.sql` | karma_ledger table, action ENUM, trigger for denormalized score, inbox shield |

## Troubleshooting

### Task timeout
- Increase `task_time_limit` in `celery_tasks.py`
- Check Pinecone index performance

### Low match scores
- Review embedding quality
- Check if location filtering is too restrictive
- Verify user profile has sufficient content

### AI intro blocked
- Review toxicity threshold (currently 0.75)
- Check OpenAI moderation API response

### Karma not updating
- Verify the migration was run: `\d karma_ledger` in psql
- Check that the trigger exists: `SELECT tgname FROM pg_trigger WHERE tgrelid = 'karma_ledger'::regclass;`
- Ensure the user exists in the `users` table before awarding karma

## License

MIT License
