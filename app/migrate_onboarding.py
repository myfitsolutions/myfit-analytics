from sqlalchemy import text
from app.database import engine


def run_migration():
    if engine.dialect.name != "postgresql": raise RuntimeError("Onboarding migration requires PostgreSQL")
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE studios ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMPTZ NULL"))
        connection.execute(text("UPDATE studios SET onboarding_completed_at = COALESCE(created_at, NOW()) WHERE id = 1 AND onboarding_completed_at IS NULL"))
    print("Onboarding migration completed; Studio 1 preserved and marked complete.")


if __name__ == "__main__": run_migration()
