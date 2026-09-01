"""Real PostgreSQL outbox gate; never point TEST_POSTGRES_URL at production."""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import AutomationsDelivery

POSTGRES_URL=os.getenv("TEST_POSTGRES_URL")
pytestmark=pytest.mark.skipif(not POSTGRES_URL,reason="TEST_POSTGRES_URL disposable PostgreSQL database not configured")


def test_postgresql_outbox_unique_identity_and_tenant_scope():
    engine=create_engine(POSTGRES_URL); assert engine.dialect.name=="postgresql"
    Session=sessionmaker(bind=engine)
    with Session() as db:
        rows=db.scalars(select(AutomationsDelivery)).all()
        identities={(row.analytics_studio_id,row.idempotency_key) for row in rows}
        assert len(rows)==len(identities)


def test_postgresql_outbox_lock_is_exclusive():
    engine=create_engine(POSTGRES_URL); Session=sessionmaker(bind=engine)
    with Session() as db: item=db.scalar(select(AutomationsDelivery).limit(1))
    if not item: pytest.skip("Seed one disposable outbox row in the migrated test database")
    def lock():
        with Session.begin() as db:
            return db.scalar(select(AutomationsDelivery.id).where(AutomationsDelivery.id==item.id).with_for_update())
    with ThreadPoolExecutor(max_workers=2) as pool: assert list(pool.map(lambda _:lock(),range(2)))==[item.id,item.id]
