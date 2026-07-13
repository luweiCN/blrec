ALTER TABLE bili_accounts ADD COLUMN avatar_url TEXT NOT NULL DEFAULT '';

ALTER TABLE bili_accounts
    ADD COLUMN credential_expires_at INTEGER NOT NULL DEFAULT 0
    CHECK (credential_expires_at >= 0);
