from sqlalchemy import text
from app.database import engine

STATEMENTS = [
"ALTER TABLE automations_deliveries ADD COLUMN IF NOT EXISTS integration_id INTEGER NULL REFERENCES automations_integrations(id) ON DELETE CASCADE",
"ALTER TABLE automations_deliveries ADD COLUMN IF NOT EXISTS subject_type VARCHAR(20) NULL",
"ALTER TABLE automations_deliveries ADD COLUMN IF NOT EXISTS payload_fingerprint VARCHAR(64) NULL",
"ALTER TABLE automations_deliveries ADD COLUMN IF NOT EXISTS normalized_payload TEXT NULL",
"ALTER TABLE automations_deliveries ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ NULL",
"ALTER TABLE automations_deliveries ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ NULL",
"ALTER TABLE automations_deliveries ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
"ALTER TABLE automations_deliveries ALTER COLUMN last_attempt_at DROP NOT NULL",
"ALTER TABLE automations_deliveries ALTER COLUMN attempt_count SET DEFAULT 0",
"UPDATE automations_deliveries SET delivery_status='delivered', delivered_at=COALESCE(delivered_at,last_attempt_at), updated_at=NOW() WHERE delivery_status='accepted'",
"UPDATE automations_deliveries SET delivery_status='failed_legacy', safe_error_code=COALESCE(safe_error_code,'legacy_payload_unavailable'), updated_at=NOW() WHERE normalized_payload IS NULL AND delivery_status IN ('attempting','failed')",
"CREATE INDEX IF NOT EXISTS ix_automations_deliveries_integration_id ON automations_deliveries(integration_id)",
"""CREATE TABLE IF NOT EXISTS automations_delivery_attempts (id SERIAL PRIMARY KEY, delivery_id INTEGER NOT NULL REFERENCES automations_deliveries(id) ON DELETE CASCADE, attempt_number INTEGER NOT NULL, correlation_id VARCHAR(100) NOT NULL, result VARCHAR(30) NOT NULL, http_status INTEGER NULL, safe_error_code VARCHAR(100) NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
"CREATE INDEX IF NOT EXISTS ix_automations_delivery_attempts_delivery_id ON automations_delivery_attempts(delivery_id)",
"CREATE INDEX IF NOT EXISTS ix_automations_delivery_attempts_delivery_created ON automations_delivery_attempts(delivery_id,created_at)"
]

def run_migration():
    if engine.dialect.name != "postgresql": raise RuntimeError("Automations outbox migration requires PostgreSQL")
    with engine.begin() as connection:
        for statement in STATEMENTS: connection.execute(text(statement))

if __name__ == "__main__": run_migration()
