from sqlalchemy import text

from app.database import engine


STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS ghl_integrations (
        id SERIAL PRIMARY KEY,
        analytics_studio_id INTEGER NOT NULL REFERENCES studios(id) ON DELETE CASCADE,
        token_env_var VARCHAR(100) NOT NULL DEFAULT 'GHL_PRIVATE_INTEGRATION_TOKEN',
        location_env_var VARCHAR(100) NOT NULL DEFAULT 'GHL_LOCATION_ID',
        integration_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        last_connection_test_at TIMESTAMPTZ NULL,
        last_connection_test_status VARCHAR(20) NULL,
        safe_error_code VARCHAR(100) NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_ghl_integrations_studio UNIQUE (analytics_studio_id)
    )""",
    "DROP INDEX IF EXISTS ix_ghl_integrations_studio_id",
    "DROP INDEX IF EXISTS ix_ghl_integrations_analytics_studio_id",
]


def run_migration():
    if engine.dialect.name != "postgresql":
        raise RuntimeError("GoHighLevel integration migration requires PostgreSQL")
    with engine.begin() as connection:
        for statement in STATEMENTS:
            connection.execute(text(statement))


if __name__ == "__main__":
    run_migration()
