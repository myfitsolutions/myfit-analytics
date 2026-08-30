from sqlalchemy import text

from app.database import engine


STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS member_milestone_status (
        id SERIAL PRIMARY KEY,
        studio_id INTEGER NOT NULL REFERENCES studios(id),
        member_id INTEGER NOT NULL REFERENCES members(id),
        milestone_type VARCHAR(30) NOT NULL DEFAULT 'attendance',
        milestone_value INTEGER NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'open',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        acknowledged_at TIMESTAMPTZ NULL,
        acknowledged_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
        CONSTRAINT ck_member_milestone_status CHECK (status IN ('open', 'celebrated', 'dismissed')),
        CONSTRAINT uq_member_milestone_identity UNIQUE (studio_id, member_id, milestone_type, milestone_value)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_member_milestone_studio_id ON member_milestone_status(studio_id)",
    "CREATE INDEX IF NOT EXISTS ix_member_milestone_member_id ON member_milestone_status(member_id)",
    "CREATE INDEX IF NOT EXISTS ix_member_milestone_studio_status ON member_milestone_status(studio_id, status)",
]


def run_migration():
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Attendance milestone migration requires PostgreSQL")
    with engine.begin() as connection:
        for statement in STATEMENTS:
            connection.execute(text(statement))
    print("Attendance milestone migration completed.")


if __name__ == "__main__":
    run_migration()
