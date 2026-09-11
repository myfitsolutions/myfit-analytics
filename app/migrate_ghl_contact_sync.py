from sqlalchemy import text

from app.database import engine


STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS ghl_contact_sync_ledger (
        id SERIAL PRIMARY KEY,
        analytics_studio_id INTEGER NOT NULL REFERENCES studios(id) ON DELETE CASCADE,
        integration_id INTEGER NOT NULL REFERENCES ghl_integrations(id) ON DELETE CASCADE,
        local_member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
        source_import_id INTEGER NULL REFERENCES import_batches(id) ON DELETE SET NULL,
        ghl_contact_id VARCHAR(100) NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        safe_error_code VARCHAR(100) NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        claim_token VARCHAR(36) NULL,
        claim_expires_at TIMESTAMPTZ NULL,
        last_attempted_at TIMESTAMPTZ NULL,
        last_synced_at TIMESTAMPTZ NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_ghl_contact_sync_studio_member UNIQUE (analytics_studio_id, local_member_id),
        CONSTRAINT ck_ghl_contact_sync_status CHECK (status IN ('pending', 'in_progress', 'succeeded', 'failed'))
    )""",
    "CREATE INDEX IF NOT EXISTS ix_ghl_contact_sync_studio_import_status ON ghl_contact_sync_ledger(analytics_studio_id, source_import_id, status)",
]


def run_migration():
    if engine.dialect.name != "postgresql":
        raise RuntimeError("GoHighLevel contact sync migration requires PostgreSQL")
    with engine.begin() as connection:
        for statement in STATEMENTS:
            connection.execute(text(statement))


if __name__ == "__main__":
    run_migration()
