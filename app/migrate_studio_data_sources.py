from sqlalchemy import text

from app.database import engine


STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS studio_data_sources (
        id SERIAL PRIMARY KEY,
        studio_id INTEGER NOT NULL REFERENCES studios(id),
        source_type VARCHAR(30) NOT NULL DEFAULT 'management_platform',
        platform VARCHAR(30) NOT NULL,
        display_name VARCHAR(100) NOT NULL,
        is_primary BOOLEAN NOT NULL DEFAULT FALSE,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT ck_studio_data_sources_platform CHECK (platform IN ('hapana', 'bsport', 'other')),
        CONSTRAINT ck_studio_data_sources_type CHECK (source_type IN ('management_platform'))
    )""",
    "CREATE INDEX IF NOT EXISTS ix_studio_data_sources_studio_id ON studio_data_sources(studio_id)",
    "CREATE INDEX IF NOT EXISTS ix_studio_data_sources_studio_active ON studio_data_sources(studio_id, is_active)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_studio_data_sources_active_primary_management ON studio_data_sources(studio_id) WHERE source_type = 'management_platform' AND is_primary AND is_active",
    "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS studio_data_source_id INTEGER NULL",
    "CREATE INDEX IF NOT EXISTS ix_import_batches_studio_data_source_id ON import_batches(studio_data_source_id)",
    """DO $$ BEGIN IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'import_batches'::regclass
        AND contype = 'f' AND pg_get_constraintdef(oid) LIKE 'FOREIGN KEY (studio_data_source_id) REFERENCES studio_data_sources(id)%'
    ) THEN ALTER TABLE import_batches ADD CONSTRAINT fk_import_batches_studio_data_source
        FOREIGN KEY (studio_data_source_id) REFERENCES studio_data_sources(id) ON DELETE SET NULL;
    END IF; END $$""",
]


def run_migration():
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Studio data source migration requires PostgreSQL")
    with engine.begin() as connection:
        for statement in STATEMENTS:
            connection.execute(text(statement))
    print("Studio data source migration completed.")


if __name__ == "__main__":
    run_migration()
