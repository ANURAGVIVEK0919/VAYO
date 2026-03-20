BEGIN;

-- Table 1: Community Outings
CREATE TABLE IF NOT EXISTS community_outings (
    outing_id      TEXT PRIMARY KEY,
    community_id   TEXT NOT NULL REFERENCES communities(community_id),
    title          TEXT NOT NULL,
    created_by     TEXT NOT NULL REFERENCES users(user_id),
    outing_date    TIMESTAMPTZ DEFAULT NOW(),
    status         TEXT DEFAULT 'active',
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Table 2: Outing Members
CREATE TABLE IF NOT EXISTS outing_members (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    outing_id     TEXT NOT NULL REFERENCES community_outings(outing_id),
    user_id       TEXT NOT NULL REFERENCES users(user_id),
    joined_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(outing_id, user_id)
);

-- Table 3: Outing Expenses
CREATE TABLE IF NOT EXISTS outing_expenses (
    expense_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    outing_id     TEXT NOT NULL REFERENCES community_outings(outing_id),
    paid_by       TEXT NOT NULL REFERENCES users(user_id),
    amount        INTEGER NOT NULL,
    description   TEXT NOT NULL,
    split_type    TEXT DEFAULT 'equal',
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Table 4: Outing Settlements
CREATE TABLE IF NOT EXISTS outing_settlements (
    settlement_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    outing_id     TEXT NOT NULL REFERENCES community_outings(outing_id),
    from_user     TEXT NOT NULL REFERENCES users(user_id),
    to_user       TEXT NOT NULL REFERENCES users(user_id),
    amount        INTEGER NOT NULL,
    settled_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Table 5: Outing Expense Splits
-- status = pending  → user ne accept nahi kiya abhi
-- status = accepted → user ne accept kiya, balance mein count hoga
-- status = rejected → user ne reject kiya, balance mein count nahi hoga
CREATE TABLE IF NOT EXISTS outing_expense_splits (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    expense_id   UUID NOT NULL REFERENCES outing_expenses(expense_id),
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    amount       INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'accepted', 'rejected')),
    responded_at TIMESTAMPTZ,
    UNIQUE(expense_id, user_id)
);

-- Table 6: Settlement Confirmations
CREATE TABLE IF NOT EXISTS outing_settlement_confirmations (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    settlement_id  UUID NOT NULL REFERENCES outing_settlements(settlement_id),
    receiver_id    TEXT NOT NULL REFERENCES users(user_id),
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'confirmed', 'disputed')),
    dispute_reason TEXT,
    responded_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_outing_members_outing
ON outing_members(outing_id);

CREATE INDEX IF NOT EXISTS idx_outing_expenses_outing
ON outing_expenses(outing_id);

CREATE INDEX IF NOT EXISTS idx_outing_settlements_outing
ON outing_settlements(outing_id);

CREATE INDEX IF NOT EXISTS idx_expense_splits_expense
ON outing_expense_splits(expense_id);

CREATE INDEX IF NOT EXISTS idx_expense_splits_status
ON outing_expense_splits(status);

CREATE INDEX IF NOT EXISTS idx_confirmations_settlement
ON outing_settlement_confirmations(settlement_id);

CREATE INDEX IF NOT EXISTS idx_confirmations_receiver
ON outing_settlement_confirmations(receiver_id);

CREATE INDEX IF NOT EXISTS idx_confirmations_status
ON outing_settlement_confirmations(status);

COMMIT;