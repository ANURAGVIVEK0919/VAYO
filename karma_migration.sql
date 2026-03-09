-- Karma Points System Migration
-- Run ONCE after user_preferences_migration.sql:
--   psql -d community_matching -f matching_system/karma_migration.sql

BEGIN;

CREATE TYPE karma_action_type_enum AS ENUM (
    'SIGNUP_EMAIL_VERIFY',
    'SIGNUP_PROFILE_PHOTO',
    'SIGNUP_VIBE_QUESTIONS',
    'SIGNUP_CLAIM_ID',
    'EVENT_RSVP',
    'GPS_CHECKIN',
    'EVENT_PHOTO_POST',
    'PEER_ENDORSEMENT',
    'HOST_EVENT',
    'NO_SHOW_PENALTY',
    'HOST_CANCEL_PENALTY',
    'NEGATIVE_REVIEW_PENALTY',
    'ADMIN_ADJUSTMENT'
);

CREATE TABLE IF NOT EXISTS karma_ledger (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT            NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    point_delta     INTEGER         NOT NULL,
    action_type     karma_action_type_enum NOT NULL,
    reference_id    TEXT,
    created_at      TIMESTAMPTZ     DEFAULT NOW()
);

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS karma_score             INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS inbox_shield_threshold   INTEGER DEFAULT 0;

CREATE OR REPLACE FUNCTION update_karma_score()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    UPDATE users
       SET karma_score = karma_score + NEW.point_delta
     WHERE user_id = NEW.user_id;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_karma_ledger_after_insert
AFTER INSERT ON karma_ledger
FOR EACH ROW EXECUTE FUNCTION update_karma_score();

CREATE INDEX IF NOT EXISTS idx_karma_ledger_user
    ON karma_ledger (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_users_karma_score
    ON users (karma_score DESC);

COMMIT;

-- Verify
SELECT column_name, data_type, udt_name, is_nullable
  FROM information_schema.columns
 WHERE table_name = 'karma_ledger'
 ORDER BY ordinal_position;

SELECT column_name, data_type, column_default
  FROM information_schema.columns
 WHERE table_name = 'users'
   AND column_name IN ('karma_score', 'inbox_shield_threshold');
