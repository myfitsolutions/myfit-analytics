"""Real PostgreSQL outbox gate; never point TEST_POSTGRES_URL at production.

Prepare a fresh disposable database before running this module:
    python -m app.bootstrap_database
    python -m app.migrate
"""
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.models import AutomationsDelivery, AutomationsDeliveryAttempt, AutomationsIntegration, Studio
from app.services.automations import AutomationsClient, Fact, OutboxService, transition

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL disposable PostgreSQL database not configured")


class TestCredentials:
    def get_automations_bearer_token(self, integration):
        return "disposable-test-token"


@pytest.fixture(scope="module")
def pg_engine():
    from sqlalchemy import create_engine
    url = make_url(POSTGRES_URL)
    assert url.get_backend_name() == "postgresql"
    assert url.host in {"127.0.0.1", "localhost"}, "PostgreSQL gate requires a local disposable database"
    engine = create_engine(POSTGRES_URL, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture
def postgres_case(pg_engine):
    Session = sessionmaker(bind=pg_engine, expire_on_commit=False)
    studio_ids = []

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
            return studio.id, integration.id

    def fact_for(studio_id, logical_identity=None):
        identity = logical_identity or uuid.uuid4().hex
        return Fact(payload={
            "event_type":"member_activity_snapshot", "studio_id":f"target-{studio_id}",
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
    barrier = threading.Barrier(2)

    def enqueue_same_delivery(_):
        with Session() as db:
            integration = db.get(AutomationsIntegration, integration_a_id)
            barrier.wait()
            item, created = OutboxService().enqueue(db, integration, fact_a)
            return item.id, created

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue_same_delivery, range(2)))

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
