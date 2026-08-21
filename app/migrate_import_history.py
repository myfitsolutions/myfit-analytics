from sqlalchemy import text

from app.database import engine


MIGRATION_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS import_batches (
        id SERIAL PRIMARY KEY,
        studio_id INTEGER NOT NULL REFERENCES studios(id),
        user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
        import_type VARCHAR(20) NOT NULL CHECK (import_type IN ('members', 'bookings', 'payments')),
        filename VARCHAR(255) NOT NULL,
        total_rows INTEGER NOT NULL DEFAULT 0,
        imported_count INTEGER NOT NULL DEFAULT 0,
        skipped_count INTEGER NOT NULL DEFAULT 0,
        invalid_count INTEGER NOT NULL DEFAULT 0,
        status VARCHAR(30) NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'partially_rolled_back', 'rolled_back')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        rolled_back_at TIMESTAMPTZ NULL,
        rolled_back_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL
    )""",
    "ALTER TABLE members ADD COLUMN IF NOT EXISTS import_batch_id INTEGER NULL",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS import_batch_id INTEGER NULL",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS import_batch_id INTEGER NULL",
    "CREATE INDEX IF NOT EXISTS ix_import_batches_studio_id ON import_batches(studio_id)",
    "CREATE INDEX IF NOT EXISTS ix_import_batches_user_id ON import_batches(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_import_batches_studio_created_at ON import_batches(studio_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_members_import_batch_id ON members(import_batch_id)",
    "CREATE INDEX IF NOT EXISTS ix_bookings_import_batch_id ON bookings(import_batch_id)",
    "CREATE INDEX IF NOT EXISTS ix_payments_import_batch_id ON payments(import_batch_id)",
    """DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'members'::regclass AND contype = 'f' AND pg_get_constraintdef(oid) LIKE 'FOREIGN KEY (import_batch_id) REFERENCES import_batches(id)%') THEN ALTER TABLE members ADD CONSTRAINT fk_members_import_batch FOREIGN KEY (import_batch_id) REFERENCES import_batches(id); END IF; END $$""",
    """DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'bookings'::regclass AND contype = 'f' AND pg_get_constraintdef(oid) LIKE 'FOREIGN KEY (import_batch_id) REFERENCES import_batches(id)%') THEN ALTER TABLE bookings ADD CONSTRAINT fk_bookings_import_batch FOREIGN KEY (import_batch_id) REFERENCES import_batches(id); END IF; END $$""",
    """DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'payments'::regclass AND contype = 'f' AND pg_get_constraintdef(oid) LIKE 'FOREIGN KEY (import_batch_id) REFERENCES import_batches(id)%') THEN ALTER TABLE payments ADD CONSTRAINT fk_payments_import_batch FOREIGN KEY (import_batch_id) REFERENCES import_batches(id); END IF; END $$"""
]


def run_migration():
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Import history migration requires PostgreSQL")
    with engine.begin() as connection:
        for statement in MIGRATION_STATEMENTS:
            connection.execute(text(statement))
    print("Import history migration completed.")


if __name__ == "__main__":
    run_migration()
