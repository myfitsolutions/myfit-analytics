import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SESSION_SECRET", "test-secret-at-least-32-characters")

from app.auth import hash_password  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import (  # noqa: E402
    CONTACT_SYNC_REQUEST_LIMIT,
    ImportRollbackRequest,
    _contact_sync_counts,
    _run_contact_sync,
    _seed_contact_sync_ledger,
    app,
    rollback_import_batch,
)
from app.models import (  # noqa: E402
    GhlContactSyncLedger, GhlIntegration, ImportBatch, Member, Studio, User,
)
from app.services.gohighlevel import GhlClient, build_contact_payload  # noqa: E402


class Credentials:
    def resolve(self, integration):
        from app.services.gohighlevel import GhlCredentials
        return GhlCredentials("private-token", "safe-location")


def configured_integration(studio_id=1):
    return GhlIntegration(
        analytics_studio_id=studio_id,
        token_env_var="TOKEN_REFERENCE",
        location_env_var="LOCATION_REFERENCE",
        integration_enabled=True,
        last_connection_test_status="connected",
    )


def test_upsert_uses_fixed_contract_and_allowlisted_payload_only():
    seen = {}
    def handler(request):
        seen["request"] = request
        return httpx.Response(200, json={"new": True, "contact": {"id": "safe-contact-id", "email": "ignored@example.test"}})
    contact = {
        "first_name": " First ", "last_name": " Last ", "email": "Person@Example.Test",
        "phone": "+1 (555) 555-1212", "status": "must-not-leak", "notes": "must-not-leak",
    }
    result = GhlClient(httpx.MockTransport(handler), Credentials()).upsert_contact(configured_integration(), contact)
    assert result == {"ok": True, "error": None, "ghl_contact_id": "safe-contact-id"}
    request = seen["request"]
    assert request.method == "POST" and request.url.path == "/contacts/upsert"
    assert request.headers["Version"] == "v3" and request.headers["Authorization"] == "Bearer private-token"
    payload = json.loads(request.content)
    assert payload == {
        "locationId": "safe-location", "createNewIfDuplicateAllowed": False,
        "firstName": "First", "lastName": "Last", "email": "person@example.test",
        "phone": "+15555551212",
    }
    assert "status" not in payload and "notes" not in payload


@pytest.mark.parametrize("status,created", [(200, True), (200, False), (201, True)])
def test_documented_update_and_create_success_shapes_are_accepted(status, created):
    contact_id = "seD4PfOuKoVMLkEZqohJ"
    transport = httpx.MockTransport(lambda request: httpx.Response(
        status,
        json={"new": created, "contact": {"id": contact_id}, "traceId": "not-retained"},
    ))
    assert GhlClient(transport, Credentials()).upsert_contact(
        configured_integration(), {"email": "person@example.test"}
    ) == {"ok": True, "error": None, "ghl_contact_id": contact_id}


def test_only_observed_201_create_and_documented_200_are_accepted():
    update_201 = httpx.MockTransport(lambda request: httpx.Response(
        201, json={"new": False, "contact": {"id": "seD4PfOuKoVMLkEZqohJ"}}
    ))
    arbitrary_202 = httpx.MockTransport(lambda request: httpx.Response(
        202, json={"new": True, "contact": {"id": "seD4PfOuKoVMLkEZqohJ"}}
    ))
    assert GhlClient(update_201, Credentials()).upsert_contact(
        configured_integration(), {"email": "person@example.test"}
    )["error"] == "upstream_status_unexpected"
    assert GhlClient(arbitrary_202, Credentials()).upsert_contact(
        configured_integration(), {"email": "person@example.test"}
    )["error"] == "upstream_status_unexpected"


@pytest.mark.parametrize("status,code", [
    (400, "contact_invalid"), (401, "authentication_failed"), (403, "access_forbidden"),
    (404, "location_not_found"), (409, "contact_conflict"), (422, "contact_invalid"),
    (429, "rate_limited"), (500, "ghl_unavailable"),
])
def test_upsert_http_failures_are_sanitized(status, code, caplog):
    caplog.set_level("DEBUG")
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text="upstream PII and secrets"))
    result = GhlClient(transport, Credentials()).upsert_contact(configured_integration(), {"email": "person@example.test"})
    assert result == {"ok": False, "error": code}
    assert "upstream PII" not in caplog.text and "person@example.test" not in caplog.text


def test_contact_sync_logs_redact_every_sensitive_value_and_preserve_unrelated_logs(caplog):
    caplog.set_level("DEBUG")
    logging.getLogger("unrelated.integration").warning("unrelated integration remains visible")
    marker = "UPSTREAM-RESPONSE-BODY-MARKER"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"new": True, "contact": {"id": "returned-contact-id"}, "marker": marker})
    )
    contact = {
        "first_name": "SensitiveFirst",
        "last_name": "SensitiveLast",
        "email": "sensitive@example.test",
        "phone": "+65 8123 4567",
    }
    result = GhlClient(transport, Credentials()).upsert_contact(configured_integration(), contact)
    assert result["ok"] is True
    for secret in (
        "private-token", "safe-location", "returned-contact-id", marker,
        "sensitive@example.test", "+6581234567", "SensitiveFirst", "SensitiveLast",
        '"locationId"', '"email"',
    ):
        assert secret not in caplog.text
    assert "unrelated integration remains visible" in caplog.text


def test_upsert_timeout_invalid_json_and_invalid_success_are_sanitized():
    def timeout(request):
        raise httpx.ReadTimeout("private PII", request=request)
    contact = {"email": "person@example.test"}
    assert GhlClient(httpx.MockTransport(timeout), Credentials()).upsert_contact(configured_integration(), contact)["error"] == "ghl_unavailable"
    invalid_json = httpx.MockTransport(lambda request: httpx.Response(200, text="private body"))
    assert GhlClient(invalid_json, Credentials()).upsert_contact(configured_integration(), contact)["error"] == "upstream_json_invalid"
    missing_contact = httpx.MockTransport(lambda request: httpx.Response(200, json={"new": True}))
    assert GhlClient(missing_contact, Credentials()).upsert_contact(configured_integration(), contact)["error"] == "upstream_contact_missing"
    missing_id = httpx.MockTransport(lambda request: httpx.Response(200, json={"new": True, "contact": {"email": "private@example.test"}}))
    assert GhlClient(missing_id, Credentials()).upsert_contact(configured_integration(), contact)["error"] == "upstream_contact_id_invalid"


@pytest.mark.parametrize("contact_id", [123, "", "bad/id", "bad id", "x" * 101])
def test_invalid_contact_identifiers_are_rejected_with_granular_code(contact_id):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"new": True, "contact": {"id": contact_id}}
    ))
    result = GhlClient(transport, Credentials()).upsert_contact(
        configured_integration(), {"email": "person@example.test"}
    )
    assert result == {"ok": False, "error": "upstream_contact_id_invalid"}


def test_payload_requires_usable_email_or_phone_and_never_copies_extra_fields():
    assert build_contact_payload({"email": "not-an-email", "phone": "12", "secret": "x"}, "safe-location") is None
    payload = build_contact_payload({"phone": "+65 8123 4567", "secret": "x"}, "safe-location")
    assert payload == {"locationId": "safe-location", "createNewIfDuplicateAllowed": False, "phone": "+6581234567"}


def workflow_database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Studio(id=1, name="One")); db.add(User(id=1, studio_id=1, email="owner@example.test", password_hash="x", role="owner")); db.flush()
    batch = ImportBatch(id=10, studio_id=1, user_id=1, import_type="members", filename="safe.csv", imported_count=3, status="completed")
    db.add(batch); db.add(configured_integration())
    db.add_all([
        Member(id=1, studio_id=1, import_batch_id=10, first_name="One", last_name="A", email="one@example.test"),
        Member(id=2, studio_id=1, import_batch_id=10, first_name="Two", last_name="B", email="two@example.test"),
        Member(id=3, studio_id=1, import_batch_id=10, first_name="Bad", last_name="C", email="invalid"),
    ])
    db.commit()
    return db


def test_workflow_partial_failure_idempotency_retry_and_no_open_http_transaction(monkeypatch):
    db = workflow_database(); outcomes = [True, False, True]; calls = []
    class Client:
        def upsert_contact(self, integration, payload):
            assert not db.in_transaction()
            calls.append(set(payload))
            ok = outcomes.pop(0)
            return {"ok": True, "error": None, "ghl_contact_id": f"contact-{len(calls)}"} if ok else {"ok": False, "error": "rate_limited"}
    monkeypatch.setattr("app.main.GhlClient", Client)
    _run_contact_sync(db, 1, 10, False)
    rows = db.query(GhlContactSyncLedger).order_by(GhlContactSyncLedger.local_member_id).all()
    assert [row.status for row in rows] == ["succeeded", "failed"]
    assert [row.attempt_count for row in rows] == [1, 1]
    assert all("email" in keys and "status" not in keys for keys in calls)
    _run_contact_sync(db, 1, 10, True)
    rows = db.query(GhlContactSyncLedger).order_by(GhlContactSyncLedger.local_member_id).all()
    assert [row.status for row in rows] == ["succeeded", "succeeded"]
    assert [row.attempt_count for row in rows] == [1, 2]
    assert len(calls) == 3


def test_sync_request_is_bounded_to_three_calls_and_leaves_remaining_pending(monkeypatch):
    db = workflow_database()
    batch = db.get(ImportBatch, 10)
    for member_id in range(4, 7):
        db.add(Member(id=member_id, studio_id=1, import_batch_id=10, first_name="More", last_name=str(member_id), email=f"more-{member_id}@example.test"))
    batch.imported_count = 6
    db.commit()
    calls = []
    class Client:
        def upsert_contact(self, integration, payload):
            calls.append(payload["email"])
            return {"ok": True, "error": None, "ghl_contact_id": f"contact-{len(calls)}"}
    monkeypatch.setattr("app.main.GhlClient", Client)
    _run_contact_sync(db, 1, 10, False)
    assert CONTACT_SYNC_REQUEST_LIMIT == 3
    assert len(calls) == 3
    assert db.query(GhlContactSyncLedger).filter_by(status="succeeded").count() == 3
    assert db.query(GhlContactSyncLedger).filter_by(status="pending").count() == 2


@pytest.mark.parametrize("stop_code", [
    "authentication_failed", "access_forbidden", "location_invalid",
    "location_not_found", "rate_limited",
])
def test_batch_wide_failure_stops_after_current_contact(monkeypatch, stop_code):
    db = workflow_database(); calls = []
    class Client:
        def upsert_contact(self, integration, payload):
            calls.append(payload)
            return {"ok": False, "error": stop_code}
    monkeypatch.setattr("app.main.GhlClient", Client)
    _run_contact_sync(db, 1, 10, False)
    rows = db.query(GhlContactSyncLedger).order_by(GhlContactSyncLedger.local_member_id).all()
    assert len(calls) == 1
    assert [row.status for row in rows] == ["failed", "pending"]
    assert [row.attempt_count for row in rows] == [1, 0]


def test_unexpected_client_exception_is_sanitized_and_remaining_work_is_recoverable(monkeypatch):
    db = workflow_database(); calls = []
    class BrokenClient:
        def upsert_contact(self, integration, payload):
            calls.append(payload)
            raise RuntimeError("secret exception payload private@example.test")
    monkeypatch.setattr("app.main.GhlClient", BrokenClient)
    _run_contact_sync(db, 1, 10, False)
    rows = db.query(GhlContactSyncLedger).order_by(GhlContactSyncLedger.local_member_id).all()
    assert len(calls) == 1
    assert [(row.status, row.safe_error_code, row.attempt_count) for row in rows] == [
        ("failed", "sync_interrupted", 1), ("pending", None, 0)
    ]
    class RecoveredClient:
        def upsert_contact(self, integration, payload):
            return {"ok": True, "error": None, "ghl_contact_id": f"recovered-{payload['email'][0]}"}
    monkeypatch.setattr("app.main.GhlClient", RecoveredClient)
    _run_contact_sync(db, 1, 10, False)
    assert {row.status for row in db.query(GhlContactSyncLedger).all()} == {"succeeded"}


def test_malformed_client_result_is_sanitized_without_leaving_a_claim(monkeypatch):
    db = workflow_database()
    class MalformedClient:
        def upsert_contact(self, integration, payload):
            return {"unexpected": "private upstream value"}
    monkeypatch.setattr("app.main.GhlClient", MalformedClient)
    _run_contact_sync(db, 1, 10, False)
    rows = db.query(GhlContactSyncLedger).order_by(GhlContactSyncLedger.local_member_id).all()
    assert [(row.status, row.safe_error_code, row.claim_token) for row in rows] == [
        ("failed", "sync_interrupted", None), ("pending", None, None)
    ]


def test_ambiguous_success_is_not_recorded_without_commit_and_retry_upserts_again(monkeypatch):
    db = workflow_database(); calls = []
    class Client:
        def upsert_contact(self, integration, payload):
            calls.append(payload["email"])
            return {"ok": True, "error": None, "ghl_contact_id": f"contact-{len(calls)}"}
    monkeypatch.setattr("app.main.GhlClient", Client)
    engine = db.get_bind(); fail_once = {"active": True}
    def fail_result_persistence(conn, cursor, statement, parameters, context, executemany):
        if fail_once["active"] and statement.startswith("UPDATE ghl_contact_sync_ledger") and "ghl_contact_id" in statement:
            fail_once["active"] = False
            raise SQLAlchemyError("simulated local persistence interruption")
    event.listen(engine, "before_cursor_execute", fail_result_persistence)
    try:
        _run_contact_sync(db, 1, 10, False)
    finally:
        event.remove(engine, "before_cursor_execute", fail_result_persistence)
    row = db.query(GhlContactSyncLedger).filter_by(local_member_id=1).one()
    assert row.status == "failed" and row.safe_error_code == "result_persistence_failed"
    assert row.ghl_contact_id is None and row.last_synced_at is None
    batch = db.get(ImportBatch, 10)
    members = [(member.id, {"email": member.email}) for member in db.query(Member).filter_by(import_batch_id=10).all() if "@" in member.email]
    assert _contact_sync_counts(db, 1, batch, members)["failure_categories"] == [
        {"label": "Result persistence failed", "count": 1}
    ]
    _run_contact_sync(db, 1, 10, True)
    row = db.query(GhlContactSyncLedger).filter_by(local_member_id=1).one()
    assert calls == ["one@example.test", "one@example.test"]
    assert row.status == "succeeded" and row.attempt_count == 2


def test_successful_sync_protects_member_rollback_but_unsynced_member_is_removed(monkeypatch):
    db = workflow_database(); user = db.get(User, 1)
    class Client:
        def upsert_contact(self, integration, payload):
            return {"ok": True, "error": None, "ghl_contact_id": "safe-contact"}
    monkeypatch.setattr("app.main.GhlClient", Client)
    # Make only member 1 eligible so member 2 remains genuinely unsynchronized.
    db.get(Member, 2).email = "invalid-two"
    db.commit()
    _run_contact_sync(db, 1, 10, False)
    result = rollback_import_batch(1, 10, ImportRollbackRequest(confirm=True), user, db)
    assert result["deleted"] == 2
    assert result["protected"] == 1
    assert result["status"] == "partially_rolled_back"
    assert "protected_records" not in result
    assert "One" not in str(result) and "one@example.test" not in str(result)
    assert db.get(Member, 1) is not None
    assert db.get(Member, 2) is None and db.get(Member, 3) is None
    assert db.query(GhlContactSyncLedger).filter_by(local_member_id=1, status="succeeded").count() == 1


def test_ledger_writer_rejects_cross_studio_integration_before_mutation():
    db = workflow_database()
    integration = db.query(GhlIntegration).one()
    integration.analytics_studio_id = 2
    batch = db.get(ImportBatch, 10)
    members = [(1, {"email": "one@example.test"})]
    with pytest.raises(Exception) as exc:
        _seed_contact_sync_ledger(db, integration, batch, members)
    assert getattr(exc.value, "status_code", None) == 409
    assert db.query(GhlContactSyncLedger).count() == 0


def test_ledger_writer_rejects_member_outside_the_studio_import_before_mutation():
    db = workflow_database()
    db.add(Studio(id=2, name="Two"))
    db.add(Member(id=20, studio_id=2, first_name="Other", last_name="Tenant", email="other@example.test"))
    db.commit()
    integration = db.query(GhlIntegration).one()
    batch = db.get(ImportBatch, 10)
    with pytest.raises(Exception) as exc:
        _seed_contact_sync_ledger(db, integration, batch, [(20, {"email": "other@example.test"})])
    assert getattr(exc.value, "status_code", None) == 409
    assert db.query(GhlContactSyncLedger).count() == 0


def test_stale_claim_is_recovered_but_fresh_claim_is_not(monkeypatch):
    db = workflow_database(); integration = db.query(GhlIntegration).one()
    for member_id, expiry in ((1, datetime.now(timezone.utc) - timedelta(minutes=1)), (2, datetime.now(timezone.utc) + timedelta(minutes=5))):
        db.add(GhlContactSyncLedger(analytics_studio_id=1, integration_id=integration.id, local_member_id=member_id,
                                    source_import_id=10, status="in_progress", claim_token=f"claim-{member_id}", claim_expires_at=expiry))
    db.commit(); calls = []
    class Client:
        def upsert_contact(self, integration, payload):
            calls.append(payload); return {"ok": True, "error": None, "ghl_contact_id": "recovered-id"}
    monkeypatch.setattr("app.main.GhlClient", Client)
    _run_contact_sync(db, 1, 10, True)
    assert len(calls) == 1
    assert db.query(GhlContactSyncLedger).filter_by(local_member_id=1).one().status == "succeeded"
    assert db.query(GhlContactSyncLedger).filter_by(local_member_id=2).one().status == "in_progress"


@pytest.fixture
def http_case(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine); Session = sessionmaker(bind=engine, expire_on_commit=False); password = "valid-test-password"
    with Session.begin() as db:
        db.add_all([Studio(id=1, name="One"), Studio(id=2, name="Two")])
        db.add_all([User(id=i, studio_id=1, email=f"{role}@one.test", password_hash=hash_password(password), role=role)
                    for i, role in enumerate(("owner", "manager", "staff"), 1)])
        batch = ImportBatch(id=10, studio_id=1, user_id=1, import_type="members", filename="safe-import.csv", imported_count=1, status="completed")
        db.add_all([
            batch,
            ImportBatch(id=11, studio_id=1, user_id=1, import_type="payments", filename="payments.csv", imported_count=1, status="completed"),
            ImportBatch(id=12, studio_id=1, user_id=1, import_type="members", filename="old-members.csv", imported_count=1, status="partially_rolled_back"),
        ])
        db.add(configured_integration())
        db.add(Member(id=1, studio_id=1, import_batch_id=10, first_name="Private", last_name="Person", email="private@example.test"))
    def override_db():
        with Session() as db: yield db
    calls = []
    class Client:
        def upsert_contact(self, integration, payload):
            calls.append(True); return {"ok": True, "error": None, "ghl_contact_id": "safe-id"}
    monkeypatch.setattr("app.main.GhlClient", Client); app.dependency_overrides[get_db] = override_db
    try: yield Session, password, calls
    finally: app.dependency_overrides.pop(get_db, None); engine.dispose()


def login(client, role, password):
    assert client.post("/login", data={"email": f"{role}@one.test", "password": password}, follow_redirects=False).status_code == 303


def preview(client):
    response = client.get("/studios/1/integrations/gohighlevel/imports/10/contacts")
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert response.status_code == 200 and match
    assert "private@example.test" not in response.text and "Private Person" not in response.text
    return match.group(1), response


@pytest.mark.parametrize("role", ["owner", "manager"])
def test_owner_manager_real_http_preview_and_sync(http_case, role):
    Session, password, calls = http_case
    with TestClient(app) as client:
        login(client, role, password); csrf, page = preview(client)
        assert "Eligible</dt><dd>1" in page.text and "Excluded</dt><dd>0" in page.text
        assert "Remaining: 1 pending, 0 failed, 0 in progress" in page.text
        assert "Up to 3 contacts are attempted per request" in page.text
        response = client.post("/studios/1/integrations/gohighlevel/imports/10/contacts/sync",
                               data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 303 and calls == [True]
    with Session() as db: assert db.query(GhlContactSyncLedger).count() == 1


def test_rejected_http_requests_have_zero_calls_and_mutations(http_case):
    Session, password, calls = http_case
    with TestClient(app) as client:
        unauthenticated = client.post("/studios/1/integrations/gohighlevel/imports/10/contacts/sync", data={"csrf_token": "x"})
    with TestClient(app) as client:
        login(client, "staff", password)
        staff = client.post("/studios/1/integrations/gohighlevel/imports/10/contacts/sync", data={"csrf_token": "x"})
    with TestClient(app) as client:
        login(client, "owner", password); csrf, _ = preview(client)
        cross = client.post("/studios/2/integrations/gohighlevel/imports/10/contacts/sync", data={"csrf_token": csrf})
        missing = client.post("/studios/1/integrations/gohighlevel/imports/10/contacts/sync", data={})
        invalid = client.post("/studios/1/integrations/gohighlevel/imports/10/contacts/sync", data={"csrf_token": "bad"})
    assert [unauthenticated.status_code, staff.status_code, cross.status_code, missing.status_code, invalid.status_code] == [401, 403, 403, 403, 403]
    assert calls == []
    with Session() as db: assert db.query(GhlContactSyncLedger).count() == 0


@pytest.mark.parametrize("batch_id", [11, 12, 999])
def test_non_member_incomplete_and_missing_imports_are_not_sync_eligible(http_case, batch_id):
    Session, password, calls = http_case
    with TestClient(app) as client:
        login(client, "owner", password)
        response = client.get(f"/studios/1/integrations/gohighlevel/imports/{batch_id}/contacts")
    assert response.status_code == 404 and calls == []
    with Session() as db:
        assert db.query(GhlContactSyncLedger).count() == 0


def seed_failed_ledger(Session):
    with Session.begin() as db:
        integration = db.query(GhlIntegration).filter_by(analytics_studio_id=1).one()
        db.add(GhlContactSyncLedger(
            analytics_studio_id=1,
            integration_id=integration.id,
            local_member_id=1,
            source_import_id=10,
            status="failed",
            safe_error_code="rate_limited",
            attempt_count=1,
        ))


@pytest.mark.parametrize("role", ["owner", "manager"])
def test_owner_manager_real_http_retry_with_valid_csrf(http_case, role):
    Session, password, calls = http_case
    seed_failed_ledger(Session)
    with TestClient(app) as client:
        login(client, role, password); csrf, _ = preview(client)
        response = client.post(
            "/studios/1/integrations/gohighlevel/imports/10/contacts/retry",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
    assert response.status_code == 303 and calls == [True]
    with Session() as db:
        row = db.query(GhlContactSyncLedger).one()
        assert row.status == "succeeded" and row.attempt_count == 2


def test_rejected_retry_requests_make_no_calls_or_ledger_mutations(http_case):
    Session, password, calls = http_case
    seed_failed_ledger(Session)
    with TestClient(app) as client:
        unauthenticated = client.post(
            "/studios/1/integrations/gohighlevel/imports/10/contacts/retry",
            data={"csrf_token": "x"},
        )
    with TestClient(app) as client:
        login(client, "staff", password)
        staff = client.post(
            "/studios/1/integrations/gohighlevel/imports/10/contacts/retry",
            data={"csrf_token": "x"},
        )
    with TestClient(app) as client:
        login(client, "owner", password); csrf, _ = preview(client)
        cross = client.post(
            "/studios/2/integrations/gohighlevel/imports/10/contacts/retry",
            data={"csrf_token": csrf},
        )
        missing = client.post(
            "/studios/1/integrations/gohighlevel/imports/10/contacts/retry", data={}
        )
        invalid = client.post(
            "/studios/1/integrations/gohighlevel/imports/10/contacts/retry",
            data={"csrf_token": "bad"},
        )
    assert [unauthenticated.status_code, staff.status_code, cross.status_code, missing.status_code, invalid.status_code] == [401, 403, 403, 403, 403]
    assert calls == []
    with Session() as db:
        row = db.query(GhlContactSyncLedger).one()
        assert (row.status, row.safe_error_code, row.attempt_count) == ("failed", "rate_limited", 1)
