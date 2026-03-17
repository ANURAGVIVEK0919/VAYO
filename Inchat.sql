-- ============================================================
-- MIGRATION 7 — Connections system (Karma Connect)
-- ============================================================

CREATE TABLE IF NOT EXISTS follow_requests (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sender_id    TEXT NOT NULL REFERENCES users(user_id),
    receiver_id  TEXT NOT NULL REFERENCES users(user_id),
    status       TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'declined')),
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (sender_id, receiver_id)
);

CREATE TABLE IF NOT EXISTS connections (
    user_id_1    TEXT NOT NULL REFERENCES users(user_id),
    user_id_2    TEXT NOT NULL REFERENCES users(user_id),
    connected_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id_1, user_id_2)
);

CREATE TABLE IF NOT EXISTS blocked_users (
    blocker_id  TEXT NOT NULL REFERENCES users(user_id),
    blocked_id  TEXT NOT NULL REFERENCES users(user_id),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (blocker_id, blocked_id)
);

CREATE TABLE IF NOT EXISTS reported_users (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reporter_id  TEXT NOT NULL REFERENCES users(user_id),
    reported_id  TEXT NOT NULL REFERENCES users(user_id),
    reason       TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS muted_users (
    muter_id    TEXT NOT NULL REFERENCES users(user_id),
    muted_id    TEXT NOT NULL REFERENCES users(user_id),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (muter_id, muted_id)
);

CREATE TABLE IF NOT EXISTS shared_details (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shared_by    TEXT NOT NULL REFERENCES users(user_id),
    shared_with  TEXT NOT NULL REFERENCES users(user_id),
    detail_type  TEXT NOT NULL CHECK (detail_type IN ('instagram', 'linkedin', 'twitter')),
    detail_value TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (shared_by, shared_with, detail_type)
);


-- ============================================================
-- MIGRATION 8 — Privacy + status columns on users
-- ============================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_visibility  TEXT DEFAULT 'public' CHECK (profile_visibility IN ('public', 'connections', 'hidden'));
ALTER TABLE users ADD COLUMN IF NOT EXISTS show_karma_score    BOOLEAN DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS show_last_seen      BOOLEAN DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen           TIMESTAMPTZ;


-- ============================================================
-- MIGRATION 9 — Discovery columns on users
-- ============================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS region    TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS latitude  FLOAT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS longitude FLOAT;


-- ============================================================
-- MIGRATION 10 — Chat messages table
-- ============================================================

CREATE TABLE IF NOT EXISTS messages (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sender_id    TEXT NOT NULL REFERENCES users(user_id),
    receiver_id  TEXT NOT NULL REFERENCES users(user_id),
    content      TEXT NOT NULL,
    is_read      BOOLEAN DEFAULT FALSE,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_sender   ON messages (sender_id,   created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_receiver ON messages (receiver_id, created_at DESC);
