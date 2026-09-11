"""Real PostgreSQL GHL schema and concurrency gate; never use a production URL."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, inspect
from sqlalchemy.orm import sessionmaker

from app import migrate_ghl_contact_sync, migrate_ghl_integration
from app.main import _claim_contact_sync, _get_or_create_ghl_integration, _run_contact_sync
from app.models import GhlContactSyncLedger, GhlIntegration, ImportBatch, Member, Studio, User
from datetime import datetime, timezone


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

    barrier = threading.Barrier(2, timeout=5)

    def create(_):
        with Session() as db:
            barrier.wait()
            item = _get_or_create_ghl_integration(db, studio_id)
            db.commit()
            return item.id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            identifiers = list(pool.map(create, range(2), timeout=15))
        with Session() as db:
            rows = db.query(GhlIntegration).filter_by(analytics_studio_id=studio_id).all()
            assert len(rows) == 1
            assert identifiers == [rows[0].id, rows[0].id]
    finally:
        with Session.begin() as db:
            db.execute(delete(Studio).where(Studio.id == studio_id))


def test_real_postgresql_contact_ledger_migration_and_exclusive_claim(pg_engine, monkeypatch):
    monkeypatch.setattr(migrate_ghl_contact_sync, "engine", pg_engine)
    migrate_ghl_contact_sync.run_migration()
    migrate_ghl_contact_sync.run_migration()
    inspector = inspect(pg_engine)
    assert "ghl_contact_sync_ledger" in inspector.get_table_names()
    assert any(
        item["name"] == "uq_ghl_contact_sync_studio_member"
        and item["column_names"] == ["analytics_studio_id", "local_member_id"]
        for item in inspector.get_unique_constraints("ghl_contact_sync_ledger")
    )
    assert any(
        item["name"] == "ix_ghl_contact_sync_studio_import_status"
        and item["column_names"] == ["analytics_studio_id", "source_import_id", "status"]
        for item in inspector.get_indexes("ghl_contact_sync_ledger")
    )
    foreign_keys = inspector.get_foreign_keys("ghl_contact_sync_ledger")
    assert {
        (tuple(item["constrained_columns"]), item["referred_table"], item.get("options", {}).get("ondelete"))
        for item in foreign_keys
    } >= {
        (("analytics_studio_id",), "studios", "CASCADE"),
        (("integration_id",), "ghl_integrations", "CASCADE"),
        (("local_member_id",), "members", "CASCADE"),
        (("source_import_id",), "import_batches", "SET NULL"),
    }

    Session = sessionmaker(bind=pg_engine, expire_on_commit=False)
    unique = uuid.uuid4().hex
    with Session.begin() as db:
        studio = Studio(name=f"pg-ghl-ledger-{unique}", timezone="UTC", currency="USD")
        db.add(studio); db.flush(); studio_id = studio.id
        user = User(studio_id=studio_id, email=f"{unique}@example.test", password_hash="x", role="owner")
        db.add(user); db.flush()
        batch = ImportBatch(studio_id=studio_id, user_id=user.id, import_type="members", filename="safe.csv", imported_count=1, status="completed")
        db.add(batch); db.flush()
        member = Member(studio_id=studio_id, import_batch_id=batch.id, first_name="A", last_name="B", email=f"a-{unique}@example.test")
        db.add(member); db.flush()
        integration = GhlIntegration(analytics_studio_id=studio_id, token_env_var="TEST", location_env_var="TEST", integration_enabled=True)
        db.add(integration); db.flush()
        ledger = GhlContactSyncLedger(analytics_studio_id=studio_id, integration_id=integration.id, local_member_id=member.id, source_import_id=batch.id)
        db.add(ledger); db.flush(); ledger_id = ledger.id

    barrier = threading.Barrier(2, timeout=5)
    def claim(_):
        with Session() as db:
            barrier.wait()
            return _claim_contact_sync(db, studio_id, ledger_id, False, datetime.now(timezone.utc))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, range(2), timeout=15))
        assert sum(value is not None for value in claims) == 1
    finally:
        with Session.begin() as db:
            db.execute(delete(Member).where(Member.studio_id == studio_id))
            db.execute(delete(ImportBatch).where(ImportBatch.studio_id == studio_id))
            db.execute(delete(User).where(User.studio_id == studio_id))
            db.execute(delete(Studio).where(Studio.id == studio_id))


def test_real_postgresql_two_complete_syncs_upsert_each_member_once(pg_engine, monkeypatch):
    Session = sessionmaker(bind=pg_engine, expire_on_commit=False)
    unique = uuid.uuid4().hex
    with Session.begin() as db:
        studio = Studio(name=f"pg-ghl-complete-{unique}", timezone="UTC", currency="USD")
        db.add(studio); db.flush(); studio_id = studio.id
        user = User(studio_id=studio_id, email=f"{unique}@example.test", password_hash="x", role="owner")
        db.add(user); db.flush()
        batch = ImportBatch(studio_id=studio_id, user_id=user.id, import_type="members", filename="safe.csv", imported_count=3, status="completed")
        db.add(batch); db.flush(); batch_id = batch.id
        db.add_all([
            Member(studio_id=studio_id, import_batch_id=batch_id, first_name="Member", last_name=str(index), email=f"member-{index}-{unique}@example.test")
            for index in range(3)
        ])
        db.add(GhlIntegration(
            analytics_studio_id=studio_id, token_env_var="TEST", location_env_var="TEST",
            integration_enabled=True, last_connection_test_status="connected",
        ))

    start = threading.Barrier(2, timeout=5)
    first_http = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    calls = {}
    threads_at_http = set()

    class Client:
        def upsert_contact(self, integration, payload):
            thread_id = threading.get_ident()
            with lock:
                first_for_thread = thread_id not in threads_at_http
                threads_at_http.add(thread_id)
                calls[payload["email"]] = calls.get(payload["email"], 0) + 1
            if first_for_thread:
                first_http.wait()
            return {"ok": True, "error": None, "ghl_contact_id": f"contact-{len(calls)}"}

    monkeypatch.setattr("app.main.GhlClient", Client)

    def synchronize(_):
        with Session() as db:
            start.wait()
            _run_contact_sync(db, studio_id, batch_id, False)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(synchronize, range(2), timeout=20))
        assert len(calls) == 3
        assert set(calls.values()) == {1}
        with Session() as db:
            rows = db.query(GhlContactSyncLedger).filter_by(analytics_studio_id=studio_id).all()
            assert len(rows) == 3
            assert all(row.status == "succeeded" and row.attempt_count == 1 for row in rows)
    finally:
        with Session.begin() as db:
            db.execute(delete(Member).where(Member.studio_id == studio_id))
            db.execute(delete(ImportBatch).where(ImportBatch.studio_id == studio_id))
            db.execute(delete(User).where(User.studio_id == studio_id))
            db.execute(delete(Studio).where(Studio.id == studio_id))
