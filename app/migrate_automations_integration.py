from sqlalchemy import text
from app.database import engine

STATEMENTS = [
"""CREATE TABLE IF NOT EXISTS automations_integrations (id SERIAL PRIMARY KEY, analytics_studio_id INTEGER NOT NULL REFERENCES studios(id) ON DELETE CASCADE, automations_base_url VARCHAR(500) NOT NULL, automations_studio_id VARCHAR(36) NOT NULL, credential_env_var VARCHAR(100) NOT NULL DEFAULT 'MYFIT_AUTOMATIONS_API_KEY', integration_enabled BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), CONSTRAINT uq_automations_integrations_studio UNIQUE (analytics_studio_id))""",
"CREATE INDEX IF NOT EXISTS ix_automations_integrations_studio_id ON automations_integrations(analytics_studio_id)",
"""CREATE TABLE IF NOT EXISTS automations_deliveries (id SERIAL PRIMARY KEY, analytics_studio_id INTEGER NOT NULL REFERENCES studios(id) ON DELETE CASCADE, automations_studio_id VARCHAR(36) NOT NULL, event_type VARCHAR(50) NOT NULL, subject_id VARCHAR(200) NOT NULL, correlation_id VARCHAR(100) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, delivery_status VARCHAR(30) NOT NULL, http_status INTEGER NULL, evaluation_id VARCHAR(36) NULL, runs_created INTEGER NOT NULL DEFAULT 0, runs_reused INTEGER NOT NULL DEFAULT 0, safe_error_code VARCHAR(100) NULL, attempt_count INTEGER NOT NULL DEFAULT 1, last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), CONSTRAINT uq_automations_deliveries_request UNIQUE (analytics_studio_id,idempotency_key))""",
"CREATE INDEX IF NOT EXISTS ix_automations_deliveries_studio_id ON automations_deliveries(analytics_studio_id)",
"CREATE INDEX IF NOT EXISTS ix_automations_deliveries_studio_attempted ON automations_deliveries(analytics_studio_id,last_attempt_at)"
]

def run_migration():
    if engine.dialect.name != "postgresql": raise RuntimeError("Automations integration migration requires PostgreSQL")
    with engine.begin() as connection:
        for statement in STATEMENTS: connection.execute(text(statement))

if __name__ == "__main__": run_migration()
