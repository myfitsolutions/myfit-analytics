from sqlalchemy import text

from app.database import engine


def run_migration():
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
            "role VARCHAR(20) NOT NULL DEFAULT 'staff'"
        ))
        connection.execute(text(
            "WITH first_user AS ("
            "SELECT id FROM users WHERE studio_id = 1 "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
            ") UPDATE users SET role = 'owner' "
            "WHERE id IN (SELECT id FROM first_user) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM users WHERE studio_id = 1 AND role = 'owner'"
            ")"
        ))

    print("Team role migration completed.")


if __name__ == "__main__":
    run_migration()
