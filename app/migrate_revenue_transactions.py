from sqlalchemy import text
from app.database import engine

STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS revenue_transactions (
        id SERIAL PRIMARY KEY, studio_id INTEGER NOT NULL REFERENCES studios(id), member_id INTEGER NULL REFERENCES members(id) ON DELETE SET NULL,
        studio_data_source_id INTEGER NULL REFERENCES studio_data_sources(id) ON DELETE SET NULL, import_batch_id INTEGER NULL REFERENCES import_batches(id),
        identity_key VARCHAR(128) NOT NULL, external_transaction_id VARCHAR(255) NULL, customer_name VARCHAR(255) NULL, customer_email VARCHAR(320) NULL,
        invoice_date TIMESTAMPTZ NULL, payment_date TIMESTAMPTZ NULL, analytics_date TIMESTAMPTZ NOT NULL, payment_method VARCHAR(100) NULL,
        source_status VARCHAR(100) NULL, transaction_kind VARCHAR(20) NOT NULL DEFAULT 'other', revenue_type VARCHAR(100) NULL,
        transaction_category VARCHAR(255) NULL, description VARCHAR(500) NULL,
        gross_revenue NUMERIC(14,2) NOT NULL DEFAULT 0, net_revenue NUMERIC(14,2) NOT NULL, tax NUMERIC(14,2) NULL, discount NUMERIC(14,2) NULL,
        admin_fee NUMERIC(14,2) NULL, dishonour_fee NUMERIC(14,2) NULL, transaction_fee NUMERIC(14,2) NULL,
        processed_by VARCHAR(255) NULL, sale_referred_by VARCHAR(255) NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT ck_revenue_transactions_kind CHECK (transaction_kind IN ('revenue','refund','other')),
        CONSTRAINT uq_revenue_transactions_source_identity UNIQUE (studio_id, studio_data_source_id, identity_key)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_revenue_transactions_studio_id ON revenue_transactions(studio_id)",
    "CREATE INDEX IF NOT EXISTS ix_revenue_transactions_member_id ON revenue_transactions(member_id)",
    "CREATE INDEX IF NOT EXISTS ix_revenue_transactions_source_id ON revenue_transactions(studio_data_source_id)",
    "CREATE INDEX IF NOT EXISTS ix_revenue_transactions_import_batch_id ON revenue_transactions(import_batch_id)",
    "CREATE INDEX IF NOT EXISTS ix_revenue_transactions_studio_date ON revenue_transactions(studio_id, analytics_date)",
    "ALTER TABLE revenue_transactions ADD COLUMN IF NOT EXISTS transaction_category VARCHAR(255) NULL",
    "ALTER TABLE revenue_transactions ADD COLUMN IF NOT EXISTS admin_fee NUMERIC(14,2) NULL",
    "ALTER TABLE revenue_transactions ADD COLUMN IF NOT EXISTS dishonour_fee NUMERIC(14,2) NULL",
    "ALTER TABLE revenue_transactions ADD COLUMN IF NOT EXISTS transaction_fee NUMERIC(14,2) NULL",
    "ALTER TABLE import_batches DROP CONSTRAINT IF EXISTS import_batches_import_type_check",
    "ALTER TABLE import_batches DROP CONSTRAINT IF EXISTS ck_import_batches_type",
    "ALTER TABLE import_batches ADD CONSTRAINT ck_import_batches_type CHECK (import_type IN ('members','bookings','payments','revenue'))",
]

def run_migration():
    if engine.dialect.name != "postgresql": raise RuntimeError("Revenue transaction migration requires PostgreSQL")
    with engine.begin() as connection:
        for statement in STATEMENTS: connection.execute(text(statement))
    print("Revenue transaction migration completed.")

if __name__ == "__main__": run_migration()
