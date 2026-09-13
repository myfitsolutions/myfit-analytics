"""Security and identity contracts for the operator-controlled Automations boundary."""
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SESSION_SECRET", "test-secret-at-least-32-characters")

from app.auth import hash_password  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AutomationsDelivery, AutomationsIntegration, Member, Payment, Studio, User  # noqa: E402
from app.services.automations import (AutomationsClient, member_fact, payment_fact,
    validated_automations_origin, allowed_automations_origins, Fact, OutboxService)  # noqa: E402
from app.services import automations as automation_module  # noqa: E402


def test_member_identity_is_stable_until_authoritative_state_changes():
    at=datetime(2026,9,1,tzinfo=timezone.utc)
    member=Member(id=7,studio_id=1,status="active",last_visit_at=at-timedelta(days=4))
    first=member_fact(member,"target",at,reactivation=False)
    later=member_fact(member,"target",at+timedelta(hours=3),reactivation=False)
    assert first.idempotency_key==later.idempotency_key
    assert first.payload["evaluation_at"]!=later.payload["evaluation_at"]
    member.last_visit_at+=timedelta(days=1)
    assert member_fact(member,"target",at,reactivation=False).idempotency_key!=first.idempotency_key
    payment=Payment(id=2,studio_id=1,member_id=7,status="failed",payment_date=at)
    assert payment_fact(payment,"target",at).idempotency_key==payment_fact(payment,"target",at+timedelta(days=1)).idempotency_key


def test_equivalent_offsets_and_naive_utc_produce_identical_identities():
    utc=datetime(2026,3,8,7,30,tzinfo=timezone.utc)
    local=utc.astimezone(timezone(timedelta(hours=-4)))
    naive=utc.replace(tzinfo=None)
    keys=[]; payment_keys=[]
    for instant in (utc,local,naive):
        member=Member(id=7,studio_id=1,status="active",last_visit_at=instant)
        payment=Payment(id=8,studio_id=1,member_id=7,status="failed",payment_date=instant)
        keys.append(member_fact(member,"target",utc,reactivation=False,
            target_origin="https://automations.test").idempotency_key)
        payment_keys.append(payment_fact(payment,"target",utc,
            target_origin="https://automations.test").idempotency_key)
    assert len(set(keys))==len(set(payment_keys))==1
    later=Member(id=7,studio_id=1,status="active",last_visit_at=utc+timedelta(seconds=1))
    assert member_fact(later,"target",utc,reactivation=False,
        target_origin="https://automations.test").idempotency_key!=keys[0]
    later_payment=Payment(id=8,studio_id=1,member_id=7,status="failed",payment_date=utc+timedelta(seconds=1))
    assert payment_fact(later_payment,"target",utc,
        target_origin="https://automations.test").idempotency_key!=payment_keys[0]


@pytest.mark.parametrize("bad", ["http://automations.test","https://127.0.0.1",
    "https://[::1]","https://localhost","https://automations.test:8443",
    "https://user:pass@automations.test","https://automations.test/path",
    "https://automations.test?","https://automations.test#",
    "https://automations.test\\@evil.test","https://automations.test.",
    "https://automations.test%2f.evil.test","https://automations.test.local",
    "https://evil.test"])
def test_origin_allowlist_fails_closed(monkeypatch,bad):
    monkeypatch.setenv("MYFIT_AUTOMATIONS_ALLOWED_ORIGINS","https://automations.test")
    assert validated_automations_origin(bad) is None


@pytest.mark.parametrize("raw", ["", "https://automations.test,not-a-url",
    "http://automations.test", "https://automations.test/"])
def test_malformed_allowlist_fails_closed(monkeypatch,raw):
    monkeypatch.setenv("MYFIT_AUTOMATIONS_ALLOWED_ORIGINS",raw)
    assert not allowed_automations_origins()


def test_redirect_and_untrusted_receiver_response_are_not_followed_or_persisted(monkeypatch):
    monkeypatch.setenv("MYFIT_AUTOMATIONS_ALLOWED_ORIGINS","https://automations.test")
    monkeypatch.setenv("MYFIT_AUTOMATIONS_TEST_KEY","secret-marker")
    integration=AutomationsIntegration(analytics_studio_id=1,automations_base_url="https://automations.test",
        automations_studio_id="target",credential_env_var="MYFIT_AUTOMATIONS_TEST_KEY",integration_enabled=True)
    seen=[]
    def redirect(request):
        seen.append(str(request.url))
        return httpx.Response(302,headers={"Location":"https://evil.test/steal"},
            text="private response marker")
    result=AutomationsClient(httpx.MockTransport(redirect)).send(integration,{"event_type":"test"},"key","corr")
    assert seen==["https://automations.test/internal/v1/studios/target/events"]
    assert result["error"]=="automations_status_unexpected"
    assert "private response marker" not in str(result) and "secret-marker" not in str(result)
    invalid=AutomationsClient(httpx.MockTransport(lambda request:httpx.Response(200,json={
        "evaluation_id":"private response marker","runs_created":"1"}))).send(
            integration,{"event_type":"test"},"key","corr")
    assert invalid["error"]=="automations_response_invalid"
    assert "private response marker" not in str(invalid)


def test_receiver_errors_do_not_log_or_return_secrets(monkeypatch,caplog):
    monkeypatch.setenv("MYFIT_AUTOMATIONS_ALLOWED_ORIGINS","https://automations.test")
    monkeypatch.setenv("MYFIT_AUTOMATIONS_TEST_KEY","bearer-private-marker")
    integration=AutomationsIntegration(analytics_studio_id=1,automations_base_url="https://automations.test",
        automations_studio_id="target",credential_env_var="MYFIT_AUTOMATIONS_TEST_KEY",integration_enabled=True)
    payload={"member":"NamePrivate","email":"email-private@example.test",
        "phone":"phone-private-marker"}
    with caplog.at_level("INFO"):
        result=AutomationsClient(httpx.MockTransport(lambda request:httpx.Response(
            403,text="response-body-private-marker"))).send(integration,payload,"key","corr")
    combined=str(result)+caplog.text
    for marker in ("bearer-private-marker","NamePrivate","email-private@example.test",
                   "phone-private-marker","response-body-private-marker"):
        assert marker not in combined


def test_oversized_and_wrongly_typed_acknowledgements_are_rejected(monkeypatch):
    monkeypatch.setenv("MYFIT_AUTOMATIONS_ALLOWED_ORIGINS","https://automations.test")
    monkeypatch.setenv("MYFIT_AUTOMATIONS_TEST_KEY","test-secret")
    integration=AutomationsIntegration(analytics_studio_id=1,automations_base_url="https://automations.test",
        automations_studio_id="target",credential_env_var="MYFIT_AUTOMATIONS_TEST_KEY",integration_enabled=True)
    for response in (httpx.Response(200,content=b"x"*9000),
                     httpx.Response(200,json={"evaluation_id":"e1","runs_created":True}),
                     httpx.Response(200,json=["e1"])):
        result=AutomationsClient(httpx.MockTransport(lambda request:response)).send(
            integration,{"event_type":"test"},"key","corr")
        assert result["ok"] is False
        assert result["error"]=="automations_response_invalid"


def test_http_deadline_and_claim_lease_have_bounded_margin(monkeypatch):
    assert automation_module.CLAIM_SECONDS > (
        automation_module.HTTP_TOTAL_BUDGET_SECONDS+
        automation_module.HTTP_IO_TIMEOUT_SECONDS+
        automation_module.PERSISTENCE_MARGIN_SECONDS)
    monkeypatch.setenv("MYFIT_AUTOMATIONS_ALLOWED_ORIGINS","https://automations.test")
    monkeypatch.setenv("MYFIT_AUTOMATIONS_TEST_KEY","test-secret")
    monkeypatch.setattr(automation_module,"HTTP_TOTAL_BUDGET_SECONDS",0.05)
    integration=AutomationsIntegration(analytics_studio_id=1,automations_base_url="https://automations.test",
        automations_studio_id="target",credential_env_var="MYFIT_AUTOMATIONS_TEST_KEY",integration_enabled=True)
    def slow_headers(request):
        time.sleep(0.08)
        return httpx.Response(200,json={"evaluation_id":"e1"})
    started=time.monotonic()
    result=AutomationsClient(httpx.MockTransport(slow_headers)).send(integration,{"event_type":"test"},"key","corr")
    assert time.monotonic()-started<1
    assert result["error"]=="automations_unavailable"
    async def dripped_headers(request):
        await anyio.sleep(0.5)
        return httpx.Response(200,json={"evaluation_id":"e1"})
    started=time.monotonic()
    cancelled=AutomationsClient(httpx.MockTransport(dripped_headers)).send(
        integration,{"event_type":"test"},"key","corr")
    assert time.monotonic()-started<0.4
    assert cancelled["error"]=="automations_unavailable"


@pytest.fixture
def http_case(monkeypatch):
    monkeypatch.setenv("MYFIT_AUTOMATIONS_ALLOWED_ORIGINS","https://automations.test,https://automations-two.test")
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    with Session.begin() as db:
        db.add_all([Studio(id=1,name="One"),Studio(id=2,name="Two")])
        for uid,studio,role in [(1,1,"owner"),(2,1,"manager"),(3,1,"staff"),(4,2,"owner")]:
            db.add(User(id=uid,studio_id=studio,email=f"{role}{uid}@example.test",
                password_hash=hash_password("valid-test-password"),role=role))
        db.add(AutomationsIntegration(id=1,analytics_studio_id=1,
            automations_base_url="https://automations.test",automations_studio_id="target",
            credential_env_var="MYFIT_AUTOMATIONS_TEST_KEY",integration_enabled=True))
    def override_db():
        with Session() as db: yield db
    app.dependency_overrides[get_db]=override_db
    calls=[]
    class NoNetwork:
        def connection(self,*args,**kwargs):
            calls.append("connection"); return {"ok":True}
        def send(self,*args,**kwargs):
            calls.append("send"); return {"ok":True,"evaluation_id":"e1","runs_created":0,"runs_reused":0}
    monkeypatch.setattr("app.main.AutomationsClient",NoNetwork)
    try: yield Session,calls
    finally:
        app.dependency_overrides.pop(get_db,None)
        engine.dispose()


def _login(client,uid):
    role={1:"owner",2:"manager",3:"staff",4:"owner"}[uid]
    response=client.post("/login",data={"email":f"{role}{uid}@example.test",
        "password":"valid-test-password"},follow_redirects=False)
    assert response.status_code==303


def _csrf(client):
    from html.parser import HTMLParser
    class TokenParser(HTMLParser):
        token=None
        def handle_starttag(self,tag,attrs):
            values=dict(attrs)
            if tag=="input" and values.get("name")=="csrf_token": self.token=values.get("value")
    response=client.get("/integrations/myfit-automations")
    parser=TokenParser();parser.feed(response.text)
    return parser.token


POST_PATHS=("/studios/1/integrations/myfit-automations",
    "/studios/1/integrations/myfit-automations/test",
    "/studios/1/integrations/myfit-automations/sync/all",
    "/studios/1/integrations/myfit-automations/deliver-pending",
    "/studios/1/integrations/myfit-automations/outbox/999/retry",
    "/studios/1/integrations/myfit-automations/retry-failures")


@pytest.mark.parametrize("path",POST_PATHS)
def test_http_rejections_make_no_mutation_or_call(http_case,path):
    Session,calls=http_case
    with TestClient(app) as client:
        assert client.post(path,data={}).status_code in {401,303}
        _login(client,3)
        assert client.post(path,data={}).status_code==403
        _login(client,1)
        assert client.post(path,data={}).status_code==403
        assert client.post(path,data={"csrf_token":"invalid"}).status_code==403
        valid=_csrf(client)
        cross=path.replace("/studios/1/","/studios/2/")
        assert client.post(cross,data={"csrf_token":valid}).status_code==403
    assert calls==[]
    with Session() as db:
        integration=db.get(AutomationsIntegration,1)
        assert integration.automations_base_url=="https://automations.test"
        assert db.query(AutomationsDelivery).count()==0


@pytest.mark.parametrize("path",POST_PATHS)
def test_missing_csrf_prevents_database_dependency_creation(http_case,path):
    _,calls=http_case
    with TestClient(app) as client:
        _login(client,1)
        _csrf(client)
        def forbidden_db():
            raise AssertionError("Automations CSRF must run before DB dependency")
            yield
        app.dependency_overrides[get_db]=forbidden_db
        response=client.post(path,data={})
        assert response.status_code==403
    assert calls==[]


@pytest.mark.parametrize("uid",[1,2])
def test_owner_manager_can_use_valid_csrf_for_connection_test(http_case,uid):
    _,calls=http_case
    with TestClient(app) as client:
        _login(client,uid)
        token=_csrf(client)
        assert token
        response=client.post("/studios/1/integrations/myfit-automations/test",
            data={"csrf_token":token})
        assert response.status_code==200
    assert calls==["connection"]


def test_application_startup_makes_no_automations_call(http_case):
    _,calls=http_case
    with TestClient(app) as client:
        assert client.get("/health").status_code==200
    assert calls==[]


def test_owner_save_is_allowed_only_with_valid_csrf_and_allowlisted_origin(http_case):
    Session,calls=http_case
    with TestClient(app) as client:
        _login(client,1)
        token=_csrf(client)
        valid={"csrf_token":token,"base_url":"https://automations.test",
            "automations_studio_id":"new-target",
            "credential_env_var":"MYFIT_AUTOMATIONS_TEST_KEY","enabled":"true"}
        rejected=client.post("/studios/1/integrations/myfit-automations",
            data={**valid,"base_url":"https://127.0.0.1"})
        assert rejected.status_code==400
        saved=client.post("/studios/1/integrations/myfit-automations",
            data=valid,follow_redirects=False)
        assert saved.status_code==303
    with Session() as db:
        integration=db.get(AutomationsIntegration,1)
        assert integration.automations_base_url=="https://automations.test"
        assert integration.automations_studio_id=="new-target"
    assert calls==[]


def test_manager_cannot_save_mapping_even_with_valid_csrf(http_case):
    Session,calls=http_case
    with TestClient(app) as client:
        _login(client,2)
        token=_csrf(client)
        response=client.post("/studios/1/integrations/myfit-automations",
            data={"csrf_token":token,"base_url":"https://automations.test",
                  "automations_studio_id":"different",
                  "credential_env_var":"MYFIT_AUTOMATIONS_TEST_KEY"})
        assert response.status_code==403
    with Session() as db:
        assert db.get(AutomationsIntegration,1).automations_studio_id=="target"
    assert calls==[]


def test_target_change_cancels_old_rows_and_fences_stale_result(http_case):
    Session,calls=http_case
    at=datetime(2026,9,1,tzinfo=timezone.utc)
    members=[Member(id=number,studio_id=1,status="active",last_visit_at=at)
        for number in range(1,5)]
    with Session() as db:
        integration=db.get(AutomationsIntegration,1)
        old_facts=[member_fact(member,"target",at,reactivation=False,
            target_origin="https://automations.test",target_revision=1) for member in members]
        rows=[OutboxService().enqueue(db,integration,fact)[0] for fact in old_facts]
        rows[1].delivery_status="failed"
        rows[2].delivery_status="delivering"
        rows[2].claim_token="old-claim-token"
        rows[2].claim_expires_at=at+timedelta(days=1)
        rows[3].delivery_status="delivered"
        db.commit()
        old_id=integration.id; stale_row_id=rows[2].id
    with TestClient(app) as client:
        _login(client,1)
        token=_csrf(client)
        configuration={"csrf_token":token,"base_url":"https://automations-two.test",
            "automations_studio_id":"target-two",
            "credential_env_var":"MYFIT_AUTOMATIONS_TEST_KEY","enabled":"true"}
        assert client.post("/studios/1/integrations/myfit-automations",
            data=configuration,follow_redirects=False).status_code==303
        workspace=client.get("/integrations/myfit-automations")
        assert "Legacy/cancelled: 3" in workspace.text
        assert "Pending: 0" in workspace.text
        assert "old-claim-token" not in workspace.text
        assert client.post("/studios/1/integrations/myfit-automations",
            data=configuration,follow_redirects=False).status_code==303
    with Session() as db:
        integration=db.get(AutomationsIntegration,1)
        assert integration.target_revision==2
        old_rows=db.query(AutomationsDelivery).filter(
            AutomationsDelivery.id.in_([row.id for row in rows])).order_by(AutomationsDelivery.id).all()
        assert [row.delivery_status for row in old_rows]==["cancelled","cancelled","cancelled","delivered"]
        assert all(row.safe_error_code=="target_changed" for row in old_rows[:3])
        assert all(row.claim_token is None and row.claim_expires_at is None for row in old_rows[:3])
        old_mapping=SimpleNamespace(id=old_id,analytics_studio_id=1)
        stale=OutboxService()._persist(db,old_mapping,stale_row_id,"old-claim-token",
            {"ok":True,"evaluation_id":"e1","runs_created":1,"runs_reused":0})
        assert stale is None
        assert db.get(AutomationsDelivery,stale_row_id).delivery_status=="cancelled"
        new_fact=member_fact(members[0],"target-two",at,reactivation=False,
            target_origin="https://automations-two.test",target_revision=integration.target_revision)
        assert new_fact.idempotency_key!=old_facts[0].idempotency_key
        new_row,created=OutboxService().enqueue(db,integration,new_fact)
        assert created and new_row.delivery_status=="pending"
        again,created_again=OutboxService().enqueue(db,integration,new_fact)
        assert not created_again and again.id==new_row.id
    with TestClient(app) as client:
        _login(client,1)
        token=_csrf(client)
        back={"csrf_token":token,"base_url":"https://automations.test",
            "automations_studio_id":"target",
            "credential_env_var":"MYFIT_AUTOMATIONS_TEST_KEY","enabled":"true"}
        assert client.post("/studios/1/integrations/myfit-automations",
            data=back,follow_redirects=False).status_code==303
    with Session() as db:
        integration=db.get(AutomationsIntegration,1)
        assert integration.target_revision==3
        assert db.get(AutomationsDelivery,new_row.id).delivery_status=="cancelled"
        back_fact=member_fact(members[0],"target",at,reactivation=False,
            target_origin="https://automations.test",target_revision=3)
        assert back_fact.idempotency_key not in {old_facts[0].idempotency_key,new_fact.idempotency_key}
        _,created=OutboxService().enqueue(db,integration,back_fact)
        assert created
    assert calls==[]


def test_origin_only_change_cancels_pending_rows(http_case):
    Session,_=http_case
    _queued_rows(Session,1)
    with TestClient(app) as client:
        _login(client,1)
        token=_csrf(client)
        response=client.post("/studios/1/integrations/myfit-automations",data={
            "csrf_token":token,"base_url":"https://automations-two.test",
            "automations_studio_id":"target",
            "credential_env_var":"MYFIT_AUTOMATIONS_TEST_KEY","enabled":"true"},follow_redirects=False)
        assert response.status_code==303
    with Session() as db:
        assert db.get(AutomationsIntegration,1).target_revision==2
        row=db.query(AutomationsDelivery).one()
        assert row.delivery_status=="cancelled" and row.safe_error_code=="target_changed"


def test_operational_counts_distinguish_retryable_exhausted_and_legacy(http_case,monkeypatch):
    Session,calls=http_case
    _queued_rows(Session,3)
    with Session.begin() as db:
        rows=db.query(AutomationsDelivery).order_by(AutomationsDelivery.id).all()
        rows[0].delivery_status="failed"; rows[0].attempt_count=1
        rows[1].delivery_status="failed"; rows[1].attempt_count=5
        rows[2].delivery_status="failed_legacy"; rows[2].attempt_count=1
    monkeypatch.delenv("MYFIT_AUTOMATIONS_TEST_KEY",raising=False)
    with TestClient(app) as client:
        _login(client,1)
        token=_csrf(client)
        response=client.get("/integrations/myfit-automations")
        assert "Retryable failed: 1" in response.text
        assert "Attempt-exhausted: 1" in response.text
        assert "Legacy/cancelled: 1" in response.text
        retry=client.post("/studios/1/integrations/myfit-automations/retry-failures",
            data={"csrf_token":token})
        assert retry.status_code==200 and retry.json()["attempted"]==1
    with Session() as db:
        rows=db.query(AutomationsDelivery).order_by(AutomationsDelivery.id).all()
        assert rows[0].attempt_count==2
        assert rows[1].attempt_count==5 and rows[2].delivery_status=="failed_legacy"
    assert calls==[]


def _queued_rows(Session,count):
    with Session() as db:
        integration=db.get(AutomationsIntegration,1)
        for number in range(count):
            payload={"event_type":"member_activity_snapshot","studio_id":"target",
                "subject_type":"member","subject_id":str(number),"payload":{"member_active":True}}
            OutboxService().enqueue(db,integration,Fact(payload,f"test-key-{number}"))


def test_manual_batch_is_bounded_and_stops_on_rate_limit(http_case,monkeypatch):
    Session,_=http_case
    _queued_rows(Session,5)
    monkeypatch.setenv("MYFIT_AUTOMATIONS_TEST_KEY","test-secret")
    calls=[]
    def rate_limited(request):
        calls.append(request.headers["idempotency-key"])
        return httpx.Response(429,json={"error":"private upstream marker"})
    with Session() as db:
        integration=db.get(AutomationsIntegration,1)
        results=OutboxService(AutomationsClient(httpx.MockTransport(rate_limited))).deliver_pending(db,integration)
        assert len(results)==1 and results[0].safe_error_code=="automations_rate_limited"
        assert db.query(AutomationsDelivery).filter_by(delivery_status="pending").count()==4
    assert len(calls)==1


def test_manual_batch_never_makes_more_than_three_calls(http_case,monkeypatch):
    Session,_=http_case
    _queued_rows(Session,5)
    monkeypatch.setenv("MYFIT_AUTOMATIONS_TEST_KEY","test-secret")
    calls=[]
    def receiver(request):
        calls.append(request.headers["idempotency-key"])
        return httpx.Response(200,json={"evaluation_id":"e1","runs_created":1,"runs_reused":0})
    with Session() as db:
        integration=db.get(AutomationsIntegration,1)
        results=OutboxService(AutomationsClient(httpx.MockTransport(receiver))).deliver_pending(db,integration)
        assert len(results)==3
        assert db.query(AutomationsDelivery).filter_by(delivery_status="pending").count()==2
    assert len(calls)==3


def test_outbox_writer_rejects_mismatched_fact_and_delivery_studio(http_case):
    Session,_=http_case
    with Session() as db:
        integration=db.get(AutomationsIntegration,1)
        bad=Fact({"event_type":"member_activity_snapshot","studio_id":"other-target",
            "subject_type":"member","subject_id":"1"},"bad-key")
        with pytest.raises(ValueError,match="Fact studio mismatch"):
            OutboxService().enqueue(db,integration,bad)
        assert db.query(AutomationsDelivery).count()==0
    _queued_rows(Session,1)
    with Session() as db:
        item=db.query(AutomationsDelivery).one()
        integration=db.get(AutomationsIntegration,1)
        integration.analytics_studio_id=2
        with pytest.raises(ValueError,match="Delivery studio mismatch"):
            OutboxService().deliver(db,integration,item)


def test_unexpected_client_exception_is_fixed_failure_and_stale_claim_recovers(http_case):
    Session,_=http_case
    _queued_rows(Session,1)
    class BrokenClient:
        def send(self,*args,**kwargs): raise RuntimeError("private payload marker")
    with Session() as db:
        integration=db.get(AutomationsIntegration,1)
        item=db.query(AutomationsDelivery).one()
        result=OutboxService(BrokenClient()).deliver(db,integration,item)
        assert result.safe_error_code=="automations_client_error"
        assert "private payload marker" not in str(result.__dict__)
        key=result.idempotency_key
        OutboxService().retry(db,result)
        assert result.idempotency_key==key


def test_failed_result_persistence_never_marks_delivered(http_case,monkeypatch):
    Session,_=http_case
    _queued_rows(Session,1)
    class AckClient:
        def send(self,*args,**kwargs):
            return {"ok":True,"evaluation_id":"e1","runs_created":1,"runs_reused":0}
    with Session() as db:
        integration=db.get(AutomationsIntegration,1)
        item=db.query(AutomationsDelivery).one()
        original_commit=db.commit
        def fail_result_commit():
            if any(row.delivery_status=="delivered" for row in db.dirty):
                raise RuntimeError("private persistence marker")
            return original_commit()
        monkeypatch.setattr(db,"commit",fail_result_commit)
        assert OutboxService(AckClient()).deliver(db,integration,item) is None
        persisted=db.get(AutomationsDelivery,item.id)
        assert persisted.delivery_status=="delivering"
        assert persisted.attempt_count==0 and persisted.delivered_at is None
        assert persisted.idempotency_key==item.idempotency_key
