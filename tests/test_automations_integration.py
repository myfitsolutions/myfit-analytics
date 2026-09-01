import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV","development")
os.environ.setdefault("DATABASE_URL","sqlite:///:memory:")
os.environ.setdefault("SESSION_SECRET","test-secret-at-least-32-characters")

from app.database import Base
from app.models import AutomationsDelivery, AutomationsDeliveryAttempt, AutomationsIntegration, Member, Payment, Studio
from app.services.automations import (AutomationsClient, EnvironmentCredentialProvider, OutboxService,
    member_fact, payment_fact, transition)

UTC=timezone.utc


def database():
    engine=create_engine("sqlite:///:memory:"); Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_fact_adapters_emit_authoritative_facts_and_skip_insufficient_data():
    now=datetime(2026,9,1,12,tzinfo=UTC)
    active=Member(id=10,studio_id=1,first_name="A",last_name="B",email="a@test",status="active",last_visit_at=now-timedelta(days=40))
    inactive=Member(id=11,studio_id=1,first_name="I",last_name="B",email="i@test",status="inactive",last_visit_at=now-timedelta(days=60))
    missing=Member(id=12,studio_id=1,first_name="M",last_name="B",email="m@test",status="active",last_visit_at=None)
    retention=member_fact(active,"automation-studio",now,reactivation=False)
    reactivation=member_fact(inactive,"automation-studio",now,reactivation=True)
    assert retention.payload["source"]==reactivation.payload["source"]=="myfit_analytics"
    assert retention.payload["payload"]["member_active"] is True
    assert reactivation.payload["payload"]["member_active"] is False
    assert member_fact(missing,"automation-studio",now,reactivation=False) is None
    failed=Payment(id=8,studio_id=1,member_id=10,amount=100,status="failed",payment_date=now-timedelta(days=5))
    paid=Payment(id=9,studio_id=1,member_id=10,amount=100,status="paid",payment_date=now)
    assert payment_fact(failed,"automation-studio",now).payload["payload"]["resolved"] is False
    assert payment_fact(paid,"automation-studio",now) is None


def test_delivery_headers_idempotency_timeout_observability_and_no_secret_persistence(monkeypatch):
    db=database(); studio=Studio(id=1,name="Studio"); db.add(studio); db.commit()
    integration=AutomationsIntegration(analytics_studio_id=1,automations_base_url="https://automations.test",
        automations_studio_id="target-studio",credential_env_var="TEST_AUTOMATIONS_KEY",integration_enabled=True)
    db.add(integration); db.commit(); monkeypatch.setenv("TEST_AUTOMATIONS_KEY","top-secret-token")
    now=datetime(2026,9,1,12,tzinfo=UTC)
    member=Member(id=10,studio_id=1,first_name="A",last_name="B",email="a@test",status="active",last_visit_at=now-timedelta(days=40))
    fact=member_fact(member,"target-studio",now,reactivation=False); seen={}
    def handler(request):
        seen["request"]=request
        return httpx.Response(200,json={"correlation_id":"corr-returned","evaluation_id":"evaluation-1","runs_created":1,"runs_reused":0})
    service=OutboxService(AutomationsClient(httpx.MockTransport(handler)))
    delivery,created=service.enqueue(db,integration,fact); delivery.correlation_id="corr-sent"; db.commit()
    duplicate,duplicate_created=service.enqueue(db,integration,fact)
    assert created and not duplicate_created and duplicate.id==delivery.id and delivery.delivery_status=="pending"
    delivery=service.deliver(db,integration,delivery)
    request=seen["request"]
    assert request.url.path=="/internal/v1/studios/target-studio/events"
    assert request.headers["authorization"]=="Bearer top-secret-token"
    assert request.headers["x-myfit-event-version"]=="1" and request.headers["x-correlation-id"]=="corr-sent"
    assert request.headers["idempotency-key"]==fact.idempotency_key
    assert delivery.delivery_status=="delivered" and delivery.evaluation_id=="evaluation-1" and delivery.runs_created==1
    assert "top-secret-token" not in repr(delivery.__dict__)
    assert db.query(AutomationsDeliveryAttempt).filter_by(delivery_id=delivery.id).count()==1


def test_failure_manual_retry_same_identity_and_correlation(monkeypatch):
    db=database(); db.add(Studio(id=1,name="Studio")); db.commit()
    integration=AutomationsIntegration(analytics_studio_id=1,automations_base_url="https://automations.test",
        automations_studio_id="target",credential_env_var="KEY_A",integration_enabled=True); db.add(integration); db.commit()
    monkeypatch.setenv("KEY_A","secret-a"); now=datetime(2026,9,1,12,tzinfo=UTC)
    fact=member_fact(Member(id=1,studio_id=1,status="active",last_visit_at=now-timedelta(days=40)),"target",now,reactivation=False)
    def unavailable(request): raise httpx.ConnectError("raw private failure",request=request)
    failed_service=OutboxService(AutomationsClient(httpx.MockTransport(unavailable)))
    item,_=failed_service.enqueue(db,integration,fact); correlation=item.correlation_id
    item=failed_service.deliver(db,integration,item)
    assert item.delivery_status=="failed" and item.safe_error_code=="automations_unavailable" and item.attempt_count==1
    failed_service.retry(db,item); assert item.delivery_status=="pending"
    ok=OutboxService(AutomationsClient(httpx.MockTransport(lambda request:httpx.Response(200,json={"evaluation_id":"e1"}))))
    item=ok.deliver(db,integration,item)
    assert item.delivery_status=="delivered" and item.attempt_count==2 and item.correlation_id==correlation
    assert item.idempotency_key==fact.idempotency_key and db.query(AutomationsDelivery).count()==1
    assert db.query(AutomationsDeliveryAttempt).filter_by(delivery_id=item.id).count()==2


def test_state_machine_tenant_and_per_studio_credentials(monkeypatch):
    item=AutomationsDelivery(delivery_status="pending"); transition(item,"delivering"); transition(item,"failed")
    transition(item,"pending")
    try: transition(item,"delivered"); assert False
    except ValueError: pass
    first=AutomationsIntegration(analytics_studio_id=1,credential_env_var="STUDIO_A")
    second=AutomationsIntegration(analytics_studio_id=2,credential_env_var="STUDIO_B")
    monkeypatch.setenv("STUDIO_A","alpha"); monkeypatch.setenv("STUDIO_B","beta")
    provider=EnvironmentCredentialProvider()
    assert provider.get_automations_bearer_token(first)=="alpha"
    assert provider.get_automations_bearer_token(second)=="beta"


def test_disabled_missing_credential_and_unavailable_are_safe(monkeypatch):
    integration=AutomationsIntegration(analytics_studio_id=1,automations_base_url="https://automations.test",
        automations_studio_id="target",credential_env_var="ABSENT_KEY",integration_enabled=False)
    client=AutomationsClient()
    assert client.connection(integration)["error"]=="integration_disabled"
    integration.integration_enabled=True; monkeypatch.delenv("ABSENT_KEY",raising=False)
    assert client.connection(integration)["error"]=="credential_not_configured"
    monkeypatch.setenv("ABSENT_KEY","secret")
    def unavailable(request): raise httpx.ConnectError("offline",request=request)
    assert AutomationsClient(httpx.MockTransport(unavailable)).connection(integration)["error"]=="automations_unavailable"
