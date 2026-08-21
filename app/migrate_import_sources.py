from sqlalchemy import text
from app.database import engine


STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS import_source_profiles (
        id SERIAL PRIMARY KEY,
        studio_id INTEGER NOT NULL REFERENCES studios(id),
        name VARCHAR(100) NOT NULL,
        description VARCHAR(500) NULL,
        created_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
        members_preset_id INTEGER NULL REFERENCES import_mapping_presets(id) ON DELETE SET NULL,
        bookings_preset_id INTEGER NULL REFERENCES import_mapping_presets(id) ON DELETE SET NULL,
        payments_preset_id INTEGER NULL REFERENCES import_mapping_presets(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_used_at TIMESTAMPTZ NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_import_source_profiles_studio_id ON import_source_profiles(studio_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_import_source_profiles_studio_normalized_name ON import_source_profiles(studio_id, lower(trim(name)))",
    "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS source_profile_id INTEGER NULL",
    "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS source_name_snapshot VARCHAR(100) NULL",
    "CREATE INDEX IF NOT EXISTS ix_import_batches_source_profile_id ON import_batches(source_profile_id)",
    """DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'import_batches'::regclass AND contype = 'f' AND pg_get_constraintdef(oid) LIKE 'FOREIGN KEY (source_profile_id) REFERENCES import_source_profiles(id)%') THEN ALTER TABLE import_batches ADD CONSTRAINT fk_import_batches_source_profile FOREIGN KEY (source_profile_id) REFERENCES import_source_profiles(id) ON DELETE SET NULL; END IF; END $$"""
]


def run_migration():
    if engine.dialect.name != "postgresql": raise RuntimeError("Import source migration requires PostgreSQL")
    with engine.begin() as connection:
        for statement in STATEMENTS: connection.execute(text(statement))
    print("Import source profile migration completed.")


if __name__ == "__main__": run_migration()
