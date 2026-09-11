import os
import logging
import re

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SESSION_SECRET", "test-secret-at-least-32-characters")

from app.auth import hash_password  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import (  # noqa: E402
    _get_or_create_ghl_integration,
    app,
    gohighlevel_workspace,
)
from app.models import GhlIntegration, Studio, User  # noqa: E402
from app.services.gohighlevel import (  # noqa: E402
    GHL_API_BASE_URL,
    EnvironmentCredentialProvider,
    GhlClient,
)


def database():
    db_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(db_engine)
    db = sessionmaker(bind=db_engine)()
    db.add_all([
        Studio(id=1, name="One"), Studio(id=2, name="Two"),
        User(id=1, studio_id=1, email="owner@one.test", password_hash="x", role="owner"),
        User(id=2, studio_id=1, email="manager@one.test", password_hash="x", role="manager"),
        User(id=3, studio_id=1, email="staff@one.test", password_hash="x", role="staff"),
    ])
    db.commit()
    return db


@pytest.fixture
def http_case(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    password = "valid-test-password"
    with Session.begin() as db:
        db.add_all([
            Studio(id=1, name="One"), Studio(id=2, name="Two"),
            User(id=1, studio_id=1, email="owner@one.test", password_hash=hash_password(password), role="owner"),
            User(id=2, studio_id=1, email="manager@one.test", password_hash=hash_password(password), role="manager"),
            User(id=3, studio_id=1, email="staff@one.test", password_hash=hash_password(password), role="staff"),
        ])

    def override_db():
        with Session() as db:
            yield db

    calls = []

    class SuccessfulClient:
        def test_connection(self, item):
            calls.append(item.analytics_studio_id)
            return {"ok": True, "error": None}

    monkeypatch.setattr("app.main.GhlClient", SuccessfulClient)
    app.dependency_overrides[get_db] = override_db
    try:
        yield Session, password, calls
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def login(client, email, password):
    response = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert response.status_code == 303


def csrf_from_workspace(client):
    response = client.get("/integrations/gohighlevel")
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1), response


def integration():
    return GhlIntegration(
        analytics_studio_id=1,
        token_env_var="TEST_GHL_TOKEN",
        location_env_var="TEST_GHL_LOCATION",
        integration_enabled=True,
    )


def test_missing_token_and_location_are_safe(monkeypatch):
    item = integration()
    monkeypatch.delenv("TEST_GHL_TOKEN", raising=False)
    monkeypatch.delenv("TEST_GHL_LOCATION", raising=False)
    assert GhlClient().test_connection(item) == {"ok": False, "error": "token_not_configured"}
    monkeypatch.setenv("TEST_GHL_TOKEN", "private-token")
    assert GhlClient().test_connection(item) == {"ok": False, "error": "location_not_configured"}


def test_success_is_get_only_with_fixed_host_headers_and_bounded_timeout(monkeypatch):
    monkeypatch.setenv("TEST_GHL_TOKEN", "private-token")
    monkeypatch.setenv("TEST_GHL_LOCATION", "private-location")
    seen = {}

    def handler(request):
        seen["request"] = request
        return httpx.Response(200, json={"location": {"name": "PII intentionally ignored"}})

    assert GhlClient(httpx.MockTransport(handler)).test_connection(integration()) == {"ok": True, "error": None}
    request = seen["request"]
    assert request.method == "GET"
    assert str(request.url).startswith(GHL_API_BASE_URL)
    assert request.headers["Authorization"] == "Bearer private-token"
    assert request.headers["Version"] == "v3"
    assert request.content == b""


def test_http_failures_timeout_and_invalid_json_are_sanitized(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    monkeypatch.setenv("TEST_GHL_TOKEN", "private-token")
    monkeypatch.setenv("TEST_GHL_LOCATION", "private-location")
    expected = {401: "authentication_failed", 403: "access_forbidden", 404: "location_not_found", 429: "rate_limited"}
    for status, code in expected.items():
        client = GhlClient(httpx.MockTransport(lambda request, status=status: httpx.Response(status, text="secret response")))
        assert client.test_connection(integration()) == {"ok": False, "error": code}
    invalid = GhlClient(httpx.MockTransport(lambda request: httpx.Response(200, text="private invalid body")))
    assert invalid.test_connection(integration()) == {"ok": False, "error": "invalid_response"}

    def timeout(request):
        raise httpx.ReadTimeout("private-token private-location", request=request)

    assert GhlClient(httpx.MockTransport(timeout)).test_connection(integration()) == {"ok": False, "error": "ghl_unavailable"}
    assert "private-token" not in caplog.text
    assert "private-location" not in caplog.text
    assert "secret response" not in caplog.text


@pytest.mark.parametrize("location_id", [
    "../contacts", "..", "abc/def", r"abc\def", "abc%2Fdef", "abc?x=1",
    "abc#fragment", "white space", "\tcontrol", "https://example.test/x", "a" * 101,
])
def test_malicious_location_ids_are_rejected_without_requests_or_logs(monkeypatch, caplog, location_id):
    monkeypatch.setenv("TEST_GHL_TOKEN", "private-token")
    monkeypatch.setenv("TEST_GHL_LOCATION", location_id)
    requests = []
    caplog.set_level("DEBUG")
    result = GhlClient(httpx.MockTransport(lambda request: requests.append(request))).test_connection(integration())
    assert result == {"ok": False, "error": "location_invalid"}
    assert requests == []
    assert location_id not in caplog.text


def test_ghl_location_is_redacted_without_suppressing_unrelated_http_logs(monkeypatch, caplog):
    monkeypatch.setenv("TEST_GHL_TOKEN", "private-token")
    monkeypatch.setenv("TEST_GHL_LOCATION", "private-location")
    caplog.set_level("INFO", logger="httpx")
    logging.getLogger("httpx").info("unrelated HTTP integration remains visible")
    result = GhlClient(httpx.MockTransport(lambda request: httpx.Response(200, json={}))).test_connection(integration())
    assert result["ok"] is True
    assert "unrelated HTTP integration remains visible" in caplog.text
    assert "private-location" not in caplog.text
    assert "/locations/[redacted]" in caplog.text


def test_environment_provider_returns_values_without_persisting_them(monkeypatch):
    monkeypatch.setenv("TEST_GHL_TOKEN", "private-token")
    monkeypatch.setenv("TEST_GHL_LOCATION", "private-location")
    item = integration()
    credentials = EnvironmentCredentialProvider().resolve(item)
    assert credentials.token == "private-token" and credentials.location_id == "private-location"
    assert "private-token" not in repr(item.__dict__)
    assert "private-location" not in repr(item.__dict__)


def test_conflict_safe_create_reuses_tenant_row_without_overwriting_configuration():
    db = database()
    first = _get_or_create_ghl_integration(db, 1)
    first.token_env_var = "TENANT_TOKEN_REFERENCE"
    db.commit()
    second = _get_or_create_ghl_integration(db, 1)
    db.commit()
    assert second.id == first.id
    assert second.token_env_var == "TENANT_TOKEN_REFERENCE"
    assert db.query(GhlIntegration).filter_by(analytics_studio_id=1).count() == 1


def test_workspace_is_tenant_scoped_and_template_redacts_secrets(monkeypatch):
    db = database()
    db.add_all([
        integration(),
        GhlIntegration(analytics_studio_id=2, token_env_var="OTHER_TOKEN", location_env_var="OTHER_LOCATION", integration_enabled=True,
                       last_connection_test_status="failed", safe_error_code="authentication_failed"),
    ])
    db.commit()
    monkeypatch.setenv("TEST_GHL_TOKEN", "private-token")
    monkeypatch.setenv("TEST_GHL_LOCATION", "private-location")
    request = Request({"type": "http", "method": "GET", "path": "/integrations/gohighlevel", "headers": [], "session": {"user_id": 1}})
    response = gohighlevel_workspace(request, db)
    body = response.body.decode()
    assert "Token configured</dt><dd>Yes" in body
    assert "Location configured</dt><dd>Yes" in body
    for forbidden in ("owner@one.test", "private-token", "private-location", "TEST_GHL_TOKEN", "TEST_GHL_LOCATION", "OTHER_TOKEN", "OTHER_LOCATION"):
        assert forbidden not in body


def test_unauthenticated_requests_are_rejected_without_ghl_call(http_case):
    _, _, calls = http_case
    with TestClient(app) as client:
        assert client.get("/integrations/gohighlevel", follow_redirects=False).status_code == 303
        assert client.post("/studios/1/integrations/gohighlevel/test", data={"csrf_token": "x"}).status_code == 401
    assert calls == []


@pytest.mark.parametrize("role", ["owner", "manager"])
def test_owner_and_manager_with_valid_csrf_can_test_over_real_http(http_case, role):
    Session, password, calls = http_case
    with TestClient(app) as client:
        login(client, f"{role}@one.test", password)
        csrf_token, page = csrf_from_workspace(client)
        assert f"{role}@one.test" not in page.text
        response = client.post(
            "/studios/1/integrations/gohighlevel/test",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
    assert response.status_code == 303 and calls == [1]
    with Session() as db:
        item = db.query(GhlIntegration).filter_by(analytics_studio_id=1).one()
        assert item.last_connection_test_status == "connected" and item.safe_error_code is None
        assert item.token_env_var == "GHL_PRIVATE_INTEGRATION_TOKEN"
        assert item.location_env_var == "GHL_LOCATION_ID"
        assert not hasattr(item, "token") and not hasattr(item, "location_id")


def test_staff_and_cross_tenant_http_requests_are_forbidden_before_ghl(http_case):
    _, password, calls = http_case
    with TestClient(app) as staff_client:
        login(staff_client, "staff@one.test", password)
        staff = staff_client.post("/studios/1/integrations/gohighlevel/test", data={"csrf_token": "not-relevant"})
    with TestClient(app) as owner_client:
        login(owner_client, "owner@one.test", password)
        csrf_token, _ = csrf_from_workspace(owner_client)
        cross_tenant = owner_client.post("/studios/2/integrations/gohighlevel/test", data={"csrf_token": csrf_token})
    assert staff.status_code == 403 and cross_tenant.status_code == 403
    assert calls == []


@pytest.mark.parametrize("form", [{}, {"csrf_token": "invalid-token"}])
def test_missing_or_invalid_csrf_is_forbidden_before_ghl(http_case, form):
    _, password, calls = http_case
    with TestClient(app) as client:
        login(client, "owner@one.test", password)
        csrf_from_workspace(client)
        response = client.post("/studios/1/integrations/gohighlevel/test", data=form)
    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid CSRF token"}
    assert calls == []


def test_application_startup_does_not_call_ghl(monkeypatch):
    calls = []
    monkeypatch.setattr(GhlClient, "test_connection", lambda *args, **kwargs: calls.append(True))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert calls == []
