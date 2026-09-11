"""Shared test infrastructure; PostgreSQL access is local and disposable only."""
import os

import pytest
from sqlalchemy.engine import make_url


@pytest.fixture(scope="session")
def pg_engine():
    postgres_url = os.getenv("TEST_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("TEST_POSTGRES_URL disposable PostgreSQL database not configured")

    url = make_url(postgres_url)
    assert url.get_backend_name() == "postgresql"
    assert url.host in {"127.0.0.1", "localhost"}, (
        "PostgreSQL tests require a local disposable database"
    )

    from sqlalchemy import create_engine
    from app.database import Base
    from app import models  # noqa: F401 - register all application models
    from app import (
        migrate_automations_integration,
        migrate_automations_outbox,
        migrate_ghl_integration,
    )

    engine = create_engine(postgres_url, pool_pre_ping=True)
    migration_modules = (
        migrate_automations_integration,
        migrate_automations_outbox,
        migrate_ghl_integration,
    )
    original_engines = [module.engine for module in migration_modules]
    try:
        # TEST_POSTGRES_URL is explicitly disposable. Only known application
        # tables are removed; unrelated database objects are left untouched.
        Base.metadata.drop_all(bind=engine, checkfirst=True)
        Base.metadata.create_all(bind=engine)
        for module in migration_modules:
            module.engine = engine
            module.run_migration()
        yield engine
    finally:
        Base.metadata.drop_all(bind=engine, checkfirst=True)
        for module, original_engine in zip(migration_modules, original_engines):
            module.engine = original_engine
        engine.dispose()
