"""Real PostgreSQL GHL schema and concurrency gate; never use a production URL."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, inspect
from sqlalchemy.orm import sessionmaker

from app import migrate_ghl_integration
from app.main import _get_or_create_ghl_integration
from app.models import GhlIntegration, Studio


def test_real_postgresql_migration_twice_and_reflected_contract(pg_engine, monkeypatch):
    monkeypatch.setattr(migrate_ghl_integration, "engine", pg_engine)
    migrate_ghl_integration.run_migration()
    migrate_ghl_integration.run_migration()

    inspector = inspect(pg_engine)
    assert "ghl_integrations" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("ghl_integrations")}
    assert columns == {
        "id", "analytics_studio_id", "token_env_var", "location_env_var",
        "integration_enabled", "last_connection_test_at", "last_connection_test_status",
        "safe_error_code", "created_at", "updated_at",
    }
    foreign_keys = inspector.get_foreign_keys("ghl_integrations")
    assert any(
        item["constrained_columns"] == ["analytics_studio_id"]
        and item["referred_table"] == "studios"
        and item["referred_columns"] == ["id"]
        and item.get("options", {}).get("ondelete") == "CASCADE"
        for item in foreign_keys
    )
    uniques = inspector.get_unique_constraints("ghl_integrations")
    assert any(
        item["name"] == "uq_ghl_integrations_studio"
        and item["column_names"] == ["analytics_studio_id"]
        for item in uniques
    )
    duplicate_indexes = [
        item for item in inspector.get_indexes("ghl_integrations")
        if item["column_names"] == ["analytics_studio_id"]
        and not item.get("unique")
    ]
    assert duplicate_indexes == []


def test_real_postgresql_concurrent_first_create_returns_one_tenant_row(pg_engine):
    Session = sessionmaker(bind=pg_engine, expire_on_commit=False)
    unique = uuid.uuid4().hex
    with Session.begin() as db:
        studio = Studio(name=f"pg-ghl-{unique}", timezone="UTC", currency="USD")
        db.add(studio)
        db.flush()
        studio_id = studio.id

    barrier = threading.Barrier(2)

    def create(_):
        with Session() as db:
            barrier.wait()
            item = _get_or_create_ghl_integration(db, studio_id)
            db.commit()
            return item.id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            identifiers = list(pool.map(create, range(2)))
        with Session() as db:
            rows = db.query(GhlIntegration).filter_by(analytics_studio_id=studio_id).all()
            assert len(rows) == 1
            assert identifiers == [rows[0].id, rows[0].id]
    finally:
        with Session.begin() as db:
            db.execute(delete(Studio).where(Studio.id == studio_id))
