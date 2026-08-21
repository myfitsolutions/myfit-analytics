from sqlalchemy import text

from app.database import engine


STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS import_mapping_presets (
        id SERIAL PRIMARY KEY,
        studio_id INTEGER NOT NULL REFERENCES studios(id),
        name VARCHAR(100) NOT NULL,
        import_type VARCHAR(20) NOT NULL CHECK (import_type IN ('members', 'bookings', 'payments')),
        mapping_json TEXT NOT NULL,
        created_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_used_at TIMESTAMPTZ NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_import_mapping_presets_studio_id ON import_mapping_presets(studio_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_import_mapping_presets_studio_normalized_name ON import_mapping_presets(studio_id, lower(trim(name)))"
]


def run_migration():
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Import preset migration requires PostgreSQL")
    with engine.begin() as connection:
        for statement in STATEMENTS:
            connection.execute(text(statement))
    print("Import mapping preset migration completed.")


if __name__ == "__main__":
    run_migration()
