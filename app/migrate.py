from app.migrate_team_roles import run_migration as migrate_team_roles
from app.migrate_import_history import run_migration as migrate_import_history
from app.migrate_import_presets import run_migration as migrate_import_presets
from app.migrate_import_sources import run_migration as migrate_import_sources
from app.migrate_onboarding import run_migration as migrate_onboarding


def run_all_migrations():
    for name, migration in (
        ("team roles", migrate_team_roles),
        ("import history", migrate_import_history),
        ("import presets", migrate_import_presets),
        ("import sources", migrate_import_sources),
        ("onboarding", migrate_onboarding)
    ):
        print(f"Running {name} migration...")
        migration()
    print("All migrations completed.")


def main():
    run_all_migrations()


if __name__ == "__main__":
    main()
