"""Real PostgreSQL outbox gate; never point TEST_POSTGRES_URL at production.

The shared pg_engine fixture creates and removes the application schema.
"""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, delete, inspect, select, text, update
from sqlalchemy.orm import sessionmaker

from app.models import AutomationsDelivery, AutomationsDeliveryAttempt, AutomationsIntegration, Studio
from app.services.automations import AutomationsClient, Fact, OutboxService, transition

class TestCredentials:
    def get_automations_bearer_token(self, integration):
        return "disposable-test-token"

@pytest.fixture
def postgres_case(pg_engine, monkeypatch):
    monkeypatch.setenv("MYFIT_AUTOMATIONS_ALLOWED_ORIGINS","https://automations.invalid")
    Session = sessionmaker(bind=pg_engine, expire_on_commit=False)
    studio_ids = []
    targets = {}

    def create_mapping(label):
        unique = uuid.uuid4().hex
        with Session.begin() as db:
            studio = Studio(name=f"pg-outbox-{label}-{unique}", timezone="UTC", currency="USD")
            db.add(studio); db.flush()
            integration = AutomationsIntegration(
                analytics_studio_id=studio.id,
                automations_base_url="https://automations.invalid",
                automations_studio_id=str(uuid.uuid4()),
                credential_env_var=f"DISPOSABLE_{unique}",
                integration_enabled=True,
            )
            db.add(integration); db.flush(); studio_ids.append(studio.id)
            targets[studio.id] = integration.automations_studio_id
            return studio.id, integration.id

    def fact_for(studio_id, logical_identity=None):
        identity = logical_identity or uuid.uuid4().hex
        return Fact(payload={
            "event_type":"member_activity_snapshot", "studio_id":targets[studio_id],
            "subject_type":"member", "subject_id":identity,
            "occurred_at":datetime.now(timezone.utc).isoformat(), "source":"postgresql_gate",
            "payload":{"member_active":True},
        }, idempotency_key=f"pg-gate-{identity}")

    yield Session, create_mapping, fact_for
    with Session.begin() as db:
        db.execute(delete(Studio).where(Studio.id.in_(studio_ids)))


def test_postgresql_outbox_unique_identity_and_tenant_scope(postgres_case):
    Session, create_mapping, fact_for = postgres_case
    studio_a, integration_a_id = create_mapping("identity-a")
    studio_b, integration_b_id = create_mapping("identity-b")
    logical_identity = uuid.uuid4().hex
    fact_a = fact_for(studio_a, logical_identity)
    barrier = threading.Barrier(2, timeout=5)

    def enqueue_same_delivery(_):
        with Session() as db:
            integration = db.get(AutomationsIntegration, integration_a_id)
            barrier.wait()
            item, created = OutboxService().enqueue(db, integration, fact_a)
            return item.id, created

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue_same_delivery, range(2), timeout=15))

    with Session() as db:
        rows_a = db.scalars(select(AutomationsDelivery).where(
            AutomationsDelivery.analytics_studio_id == studio_a,
            AutomationsDelivery.idempotency_key == fact_a.idempotency_key,
        )).all()
        assert len(rows_a) == 1
        assert len({item_id for item_id, _ in results}) == 1
        assert sum(created for _, created in results) == 1

        integration_b = db.get(AutomationsIntegration, integration_b_id)
        item_b, created_b = OutboxService().enqueue(db, integration_b, fact_for(studio_b, logical_identity))
        assert created_b and item_b.analytics_studio_id == studio_b and item_b.id != rows_a[0].id
        assert db.scalar(select(AutomationsDelivery).where(
            AutomationsDelivery.id == item_b.id,
            AutomationsDelivery.analytics_studio_id == studio_a,
        )) is None
        mutation = db.execute(update(AutomationsDelivery).where(
            AutomationsDelivery.id == item_b.id,
            AutomationsDelivery.analytics_studio_id == studio_a,
        ).values(delivery_status="failed"))
        assert mutation.rowcount == 0
        db.rollback()


def test_postgresql_outbox_lock_is_exclusive(postgres_case):
    Session, create_mapping, fact_for = postgres_case
    studio_id, integration_id = create_mapping("locking")
    with Session() as db:
        integration = db.get(AutomationsIntegration, integration_id)
        item, _ = OutboxService().enqueue(db, integration, fact_for(studio_id)); item_id = item.id
    first, second = Session(), Session()
    try:
        claimed = first.scalar(select(AutomationsDelivery).where(
            AutomationsDelivery.id == item_id,
            AutomationsDelivery.analytics_studio_id == studio_id,
            AutomationsDelivery.delivery_status == "pending",
        ).with_for_update())
        transition(claimed, "delivering"); first.flush()
        competing_claim = second.scalar(select(AutomationsDelivery).where(
            AutomationsDelivery.id == item_id,
            AutomationsDelivery.analytics_studio_id == studio_id,
            AutomationsDelivery.delivery_status == "pending",
        ).with_for_update(skip_locked=True))
        assert competing_claim is None
        second.rollback(); first.commit()
        persisted = second.get(AutomationsDelivery, item_id)
        assert persisted.delivery_status == "delivering"
        with pytest.raises(ValueError, match="Illegal outbox transition"):
            transition(persisted, "delivering")
    finally:
        first.close(); second.close()


def test_postgresql_retry_preserves_identity_metadata_and_attempt_history(postgres_case):
    Session, create_mapping, fact_for = postgres_case
    studio_id, integration_id = create_mapping("retry")
    correlation_id, evaluation_id = f"corr-{uuid.uuid4().hex}", str(uuid.uuid4())
    with Session() as db:
        integration = db.get(AutomationsIntegration, integration_id)
        item, _ = OutboxService().enqueue(db, integration, fact_for(studio_id))
        item.correlation_id = correlation_id; item.delivery_status = "failed"; item.attempt_count = 1
        item.last_attempt_at = datetime.now(timezone.utc); item.safe_error_code = "automations_unavailable"
        db.add(AutomationsDeliveryAttempt(delivery_id=item.id, attempt_number=1,
            correlation_id=correlation_id, result="failed", safe_error_code="automations_unavailable"))
        db.commit(); original_id, original_key = item.id, item.idempotency_key

        retried = OutboxService().retry(db, item)
        assert (retried.id, retried.idempotency_key, retried.delivery_status, retried.attempt_count) == (original_id, original_key, "pending", 1)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "evaluation_id":evaluation_id, "runs_created":1, "runs_reused":0,
        }))
        delivered = OutboxService(AutomationsClient(transport, TestCredentials())).deliver(db, integration, retried)
        assert delivered.id == original_id and delivered.idempotency_key == original_key
        assert delivered.correlation_id == correlation_id and delivered.evaluation_id == evaluation_id
        assert delivered.delivery_status == "delivered" and delivered.attempt_count == 2
        assert delivered.last_attempt_at is not None and delivered.safe_error_code is None

        attempts = db.scalars(select(AutomationsDeliveryAttempt).where(
            AutomationsDeliveryAttempt.delivery_id == original_id,
        ).order_by(AutomationsDeliveryAttempt.attempt_number)).all()
        assert [attempt.attempt_number for attempt in attempts] == [1, 2]
        assert [attempt.result for attempt in attempts] == ["failed", "delivered"]
        assert all(attempt.correlation_id == correlation_id for attempt in attempts)
        assert db.scalar(select(AutomationsDelivery.analytics_studio_id).where(
            AutomationsDelivery.id == attempts[1].delivery_id,
        )) == studio_id
        rows = db.scalars(select(AutomationsDelivery).where(
            AutomationsDelivery.analytics_studio_id == studio_id,
            AutomationsDelivery.idempotency_key == original_key,
        )).all()
        assert [row.id for row in rows] == [original_id]


def test_postgresql_claim_migration_twice_and_stale_recovery(postgres_case, pg_engine):
    from app import migrate_automations_outbox
    Session, create_mapping, fact_for = postgres_case
    original = migrate_automations_outbox.engine
    try:
        migrate_automations_outbox.engine = pg_engine
        migrate_automations_outbox.run_migration()
        migrate_automations_outbox.run_migration()
    finally:
        migrate_automations_outbox.engine = original
    columns = {column["name"] for column in inspect(pg_engine).get_columns("automations_deliveries")}
    assert {"claim_token", "claim_expires_at"} <= columns
    studio_id, integration_id = create_mapping("stale-claim")
    with Session() as db:
        integration = db.get(AutomationsIntegration, integration_id)
        item, _ = OutboxService().enqueue(db, integration, fact_for(studio_id))
        item.delivery_status = "delivering"
        item.claim_token = str(uuid.uuid4())
        item.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        calls = []
        def receiver(request):
            calls.append(request.headers["idempotency-key"])
            return httpx.Response(200, json={"evaluation_id":"e1","runs_created":1,"runs_reused":0})
        service = OutboxService(AutomationsClient(httpx.MockTransport(receiver), TestCredentials()))
        result = service.deliver(db, integration, item)
        assert result.delivery_status == "delivered"
        assert calls == [item.idempotency_key]
        assert result.claim_token is None and result.claim_expires_at is None


def test_postgresql_two_workers_send_once_per_row(postgres_case):
    Session, create_mapping, fact_for = postgres_case
    studio_id, integration_id = create_mapping("two-workers")
    with Session() as db:
        integration = db.get(AutomationsIntegration, integration_id)
        for _ in range(3):
            OutboxService().enqueue(db, integration, fact_for(studio_id))
    lock = threading.Lock()
    calls = []
    barrier = threading.Barrier(2, timeout=5)
    def receiver(request):
        with lock:
            calls.append(request.headers["idempotency-key"])
        return httpx.Response(200, json={"evaluation_id":"e1","runs_created":1,"runs_reused":0})
    def worker(_):
        with Session() as db:
            integration = db.get(AutomationsIntegration, integration_id)
            barrier.wait()
            return OutboxService(AutomationsClient(httpx.MockTransport(receiver), TestCredentials())).deliver_pending(db, integration)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, number) for number in range(2)]
        for future in futures:
            future.result(timeout=15)
    with Session() as db:
        rows = db.scalars(select(AutomationsDelivery).where(
            AutomationsDelivery.analytics_studio_id == studio_id)).all()
        assert len(rows) == 3
        assert all(row.delivery_status == "delivered" for row in rows)
        assert len(calls) == len(set(calls)) == 3
        assert db.query(AutomationsDeliveryAttempt).filter(
            AutomationsDeliveryAttempt.delivery_id.in_([row.id for row in rows])).count() == 3


def test_postgresql_upgrade_recovers_old_delivering_row_without_new_columns(pg_engine):
    """Exercise a pre-hardening table, not Base.metadata.create_all's new schema."""
    from app import migrate_automations_outbox
    schema=f"test_automations_upgrade_{uuid.uuid4().hex}"
    with pg_engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    upgrade_engine=create_engine(pg_engine.url,connect_args={
        "options":f"-c search_path={schema} -c statement_timeout=10000 -c lock_timeout=5000"})
    original=migrate_automations_outbox.engine
    try:
        with upgrade_engine.begin() as connection:
            connection.execute(text("""CREATE TABLE automations_integrations (
                id INTEGER PRIMARY KEY, analytics_studio_id INTEGER NOT NULL,
                automations_base_url VARCHAR(500) NOT NULL,
                automations_studio_id VARCHAR(36) NOT NULL,
                credential_env_var VARCHAR(100) NOT NULL,
                integration_enabled BOOLEAN NOT NULL)"""))
            connection.execute(text("""CREATE TABLE automations_deliveries (
                id INTEGER PRIMARY KEY, analytics_studio_id INTEGER NOT NULL,
                integration_id INTEGER REFERENCES automations_integrations(id) ON DELETE CASCADE,
                automations_studio_id VARCHAR(36) NOT NULL,
                event_type VARCHAR(50) NOT NULL, subject_type VARCHAR(20),
                subject_id VARCHAR(200) NOT NULL, correlation_id VARCHAR(100) NOT NULL,
                idempotency_key VARCHAR(128) NOT NULL, payload_fingerprint VARCHAR(64),
                normalized_payload TEXT, delivery_status VARCHAR(30) NOT NULL,
                http_status INTEGER, evaluation_id VARCHAR(36), runs_created INTEGER NOT NULL DEFAULT 0,
                runs_reused INTEGER NOT NULL DEFAULT 0, safe_error_code VARCHAR(100),
                attempt_count INTEGER NOT NULL DEFAULT 0, last_attempt_at TIMESTAMPTZ,
                delivered_at TIMESTAMPTZ, next_retry_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""))
            connection.execute(text("""INSERT INTO automations_integrations
                (id,analytics_studio_id,automations_base_url,automations_studio_id,credential_env_var,integration_enabled)
                VALUES (1,1,'https://automations.invalid','target','DISPOSABLE_TEST_KEY',TRUE)"""))
            connection.execute(text("""INSERT INTO automations_deliveries
                (id,analytics_studio_id,integration_id,automations_studio_id,event_type,subject_type,
                 subject_id,correlation_id,idempotency_key,normalized_payload,delivery_status,attempt_count)
                VALUES (1,1,1,'target','member_activity_snapshot','member','1','corr','key',
                '{"event_type":"member_activity_snapshot"}','delivering',1)"""))
            connection.execute(text("""INSERT INTO automations_deliveries
                (id,analytics_studio_id,integration_id,automations_studio_id,event_type,subject_type,
                 subject_id,correlation_id,idempotency_key,normalized_payload,delivery_status,attempt_count)
                VALUES (2,1,1,'target','member_activity_snapshot','member','2','corr2','key2',
                '{"event_type":"member_activity_snapshot"}','pending',5)"""))
        migrate_automations_outbox.engine=upgrade_engine
        migrate_automations_outbox.run_migration()
        migrate_automations_outbox.run_migration()
        reflected=inspect(upgrade_engine)
        delivery_columns={column["name"] for column in reflected.get_columns("automations_deliveries")}
        integration_columns={column["name"] for column in reflected.get_columns("automations_integrations")}
        assert {"claim_token","claim_expires_at"}<=delivery_columns
        assert "target_revision" in integration_columns
        with upgrade_engine.connect() as connection:
            row=connection.execute(text("""SELECT delivery_status,safe_error_code,
                claim_token,claim_expires_at,next_retry_at,attempt_count
                FROM automations_deliveries WHERE id=1""")).one()
            assert row==( "failed","delivery_interrupted",None,None,None,1)
            terminal=connection.execute(text("""SELECT delivery_status,safe_error_code,attempt_count
                FROM automations_deliveries WHERE id=2""")).one()
            assert terminal==("failed","attempt_exhausted",5)
            assert connection.execute(text("SELECT target_revision FROM automations_integrations WHERE id=1")).scalar_one()==1
    finally:
        migrate_automations_outbox.engine=original
        upgrade_engine.dispose()
        with pg_engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))


def test_postgresql_slow_response_cannot_be_reclaimed_during_live_lease(postgres_case):
    Session, create_mapping, fact_for = postgres_case
    studio_id, integration_id = create_mapping("lease-boundary")
    with Session() as db:
        integration = db.get(AutomationsIntegration, integration_id)
        item, _ = OutboxService().enqueue(db, integration, fact_for(studio_id))
        item_id = item.id
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = []
    def receiver(request):
        with lock:
            calls.append(request.headers["idempotency-key"])
        entered.set()
        assert release.wait(timeout=3)
        return httpx.Response(200, json={"evaluation_id":"e1","runs_created":1,"runs_reused":0})
    def worker():
        with Session() as db:
            integration = db.get(AutomationsIntegration, integration_id)
            return OutboxService(AutomationsClient(httpx.MockTransport(receiver), TestCredentials())).deliver_pending(db, integration)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker)
        try:
            assert entered.wait(timeout=3)
            with Session() as db:
                claimed = db.get(AutomationsDelivery, item_id)
                assert claimed.delivery_status == "delivering"
                assert claimed.claim_expires_at > datetime.now(timezone.utc) + timedelta(seconds=30)
            second = pool.submit(worker)
            assert second.result(timeout=3) == []
        finally:
            release.set()
        assert len(first.result(timeout=5)) == 1
    assert len(calls) == 1


def test_postgresql_target_cancellation_fences_inflight_result(postgres_case):
    Session, create_mapping, fact_for = postgres_case
    studio_id, integration_id = create_mapping("target-cancellation")
    with Session() as db:
        integration = db.get(AutomationsIntegration, integration_id)
        item, _ = OutboxService().enqueue(db, integration, fact_for(studio_id))
        item_id = item.id
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def receiver(request):
        calls.append(request.headers["idempotency-key"])
        entered.set()
        assert release.wait(timeout=3)
        return httpx.Response(200, json={"evaluation_id":"e1","runs_created":1,"runs_reused":0})
    def worker():
        with Session() as db:
            integration = db.get(AutomationsIntegration, integration_id)
            item = db.get(AutomationsDelivery, item_id)
            return OutboxService(AutomationsClient(httpx.MockTransport(receiver), TestCredentials())).deliver(db, integration, item)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert entered.wait(timeout=3)
            with Session.begin() as db:
                row = db.get(AutomationsDelivery, item_id, with_for_update=True)
                row.delivery_status = "cancelled"
                row.safe_error_code = "target_changed"
                row.claim_token = None
                row.claim_expires_at = None
                integration = db.get(AutomationsIntegration, integration_id, with_for_update=True)
                integration.automations_studio_id = str(uuid.uuid4())
                integration.target_revision += 1
        finally:
            release.set()
        assert future.result(timeout=5) is None
    with Session() as db:
        row = db.get(AutomationsDelivery, item_id)
        assert row.delivery_status == "cancelled"
        assert row.safe_error_code == "target_changed"
        assert row.delivered_at is None
        assert db.query(AutomationsDeliveryAttempt).filter_by(delivery_id=item_id).count() == 0
    assert len(calls) == 1
