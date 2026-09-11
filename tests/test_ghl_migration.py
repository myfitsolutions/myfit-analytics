from contextlib import contextmanager
from types import SimpleNamespace

from app import migrate, migrate_ghl_integration


class RecordingConnection:
    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(str(statement))


class RecordingEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self):
        self.connection = RecordingConnection()

    @contextmanager
    def begin(self):
        yield self.connection


def test_ghl_migration_is_registered_and_idempotent(monkeypatch):
    engine = RecordingEngine()
    monkeypatch.setattr(migrate_ghl_integration, "engine", engine)
    migrate_ghl_integration.run_migration()
    migrate_ghl_integration.run_migration()
    assert len(engine.connection.statements) == len(migrate_ghl_integration.STATEMENTS) * 2
    assert "CREATE TABLE IF NOT EXISTS" in migrate_ghl_integration.STATEMENTS[0]
    assert migrate_ghl_integration.STATEMENTS[1] == "DROP INDEX IF EXISTS ix_ghl_integrations_studio_id"
    assert migrate_ghl_integration.STATEMENTS[2] == "DROP INDEX IF EXISTS ix_ghl_integrations_analytics_studio_id"
    create = migrate_ghl_integration.STATEMENTS[0]
    assert "analytics_studio_id" in create and "ON DELETE CASCADE" in create
    assert "token_env_var" in create and "location_env_var" in create
    assert "GHL_PRIVATE_INTEGRATION_TOKEN" in create and "GHL_LOCATION_ID" in create
    assert "token " not in create.lower() and "location_id " not in create.lower()


def test_ghl_migration_runs_in_the_ordered_migration_registry(monkeypatch):
    calls = []
    for name in vars(migrate):
        if name.startswith("migrate_"):
            monkeypatch.setattr(migrate, name, lambda name=name: calls.append(name))
    migrate.run_all_migrations()
    assert calls[-1] == "migrate_ghl_integration"
