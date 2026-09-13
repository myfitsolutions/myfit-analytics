"""Default-off GHL parking contract; all HTTP tests use an isolated SQLite DB."""
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SESSION_SECRET", "test-secret-at-least-32-characters")

from app.auth import hash_password  # noqa: E402
from app.config import ghl_feature_enabled  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app, _is_ghl_route  # noqa: E402
from app.models import GhlContactSyncLedger, GhlIntegration, ImportBatch, Member, Studio, User  # noqa: E402


@pytest.mark.parametrize("value", [None, "", "false", "FALSE", "0", "no", "off", "maybe", "enabled", "true-ish"])
def test_missing_false_and_malformed_flag_stay_off(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("GHL_FEATURE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("GHL_FEATURE_ENABLED", value)
    assert ghl_feature_enabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", " True ", "1", "yes", "YES", "on", "ON"])
def test_only_recognized_true_values_enable_ghl(monkeypatch, value):
    monkeypatch.setenv("GHL_FEATURE_ENABLED", value)
    assert ghl_feature_enabled() is True


@pytest.fixture
def parked_case(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    password = "valid-test-password"
    with Session.begin() as db:
        db.add(Studio(id=1, name="Studio"))
        db.add(User(id=1, studio_id=1, email="owner@example.test", password_hash=hash_password(password), role="owner"))
        db.add(ImportBatch(id=10, studio_id=1, user_id=1, import_type="members", filename="test.csv", imported_count=1, status="completed"))
        db.add(Member(id=1, studio_id=1, import_batch_id=10, first_name="Test", last_name="Member", email="member@example.test"))
        db.add(GhlIntegration(id=1, analytics_studio_id=1, token_env_var="UNUSED_TOKEN", location_env_var="UNUSED_LOCATION", integration_enabled=True, last_connection_test_status="connected"))
        db.add(GhlContactSyncLedger(id=1, analytics_studio_id=1, integration_id=1, local_member_id=1, source_import_id=10, status="failed", safe_error_code="rate_limited", attempt_count=1))

    def override_db():
        with Session() as db:
            yield db

    calls = []
    class NoNetworkClient:
        def test_connection(self, *args, **kwargs):
            calls.append("test")
            raise AssertionError("GHL call must not occur")
        def upsert_contact(self, *args, **kwargs):
            calls.append("upsert")
            raise AssertionError("GHL call must not occur")

    monkeypatch.setattr("app.main.GhlClient", NoNetworkClient)
    app.dependency_overrides[get_db] = override_db
    try:
        yield Session, password, calls
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def login(client, password):
    response = client.post("/login", data={"email": "owner@example.test", "password": password}, follow_redirects=False)
    assert response.status_code == 303


GHL_ROUTES = (
    ("get", "/integrations/gohighlevel"),
    ("post", "/studios/1/integrations/gohighlevel/test"),
    ("get", "/studios/1/integrations/gohighlevel/imports/10/contacts"),
    ("post", "/studios/1/integrations/gohighlevel/imports/10/contacts/sync"),
    ("post", "/studios/1/integrations/gohighlevel/imports/10/contacts/retry"),
)


def test_gate_covers_every_registered_ghl_route():
    registered = {
        (method.lower(), route.path)
        for route in app.routes
        for method in getattr(route, "methods", ())
        if "gohighlevel" in route.path
    }
    assert len(registered) == len(GHL_ROUTES)
    assert all(_is_ghl_route(path) for _, path in registered)


@pytest.mark.parametrize("flag", [None, "false", "malformed"])
def test_parked_routes_ui_and_history_remain_dormant(parked_case, monkeypatch, flag):
    Session, password, calls = parked_case
    if flag is None:
        monkeypatch.delenv("GHL_FEATURE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("GHL_FEATURE_ENABLED", flag)
    with TestClient(app) as client:
        login(client, password)
        imports = client.get("/imports")
        assert imports.status_code == 200
        assert "/integrations/gohighlevel" not in imports.text
        assert "GHL_FEATURE_ENABLED" not in imports.text
        detail = client.get("/studios/1/imports/10")
        assert detail.status_code == 200
        assert detail.json()["ghl_contact_sync_eligible"] is False

        def forbidden_db():
            raise AssertionError("Parked GHL routes must not open a database session")
            yield

        app.dependency_overrides[get_db] = forbidden_db
        for method, path in GHL_ROUTES:
            response = getattr(client, method)(path, data={"csrf_token": "not-needed"}) if method == "post" else client.get(path)
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}
            assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert calls == []
    with Session() as db:
        integration = db.get(GhlIntegration, 1)
        ledger = db.get(GhlContactSyncLedger, 1)
        assert integration.integration_enabled is True
        assert integration.last_connection_test_status == "connected"
        assert (ledger.status, ledger.safe_error_code, ledger.attempt_count) == ("failed", "rate_limited", 1)
        assert db.query(GhlContactSyncLedger).count() == 1


def test_enabled_controls_and_existing_workspace_remain_visible(parked_case, monkeypatch):
    _, password, calls = parked_case
    monkeypatch.setenv("GHL_FEATURE_ENABLED", "true")
    with TestClient(app) as client:
        login(client, password)
        imports = client.get("/imports")
        assert imports.status_code == 200
        assert 'href="/integrations/gohighlevel"' in imports.text
        detail = client.get("/studios/1/imports/10")
        assert detail.json()["ghl_contact_sync_eligible"] is True
        workspace = client.get("/integrations/gohighlevel")
        assert workspace.status_code == 200
        preview = client.get("/studios/1/integrations/gohighlevel/imports/10/contacts")
        assert preview.status_code == 200
    assert calls == []


def test_application_startup_makes_no_ghl_request_while_parked(parked_case, monkeypatch):
    _, _, calls = parked_case
    monkeypatch.delenv("GHL_FEATURE_ENABLED", raising=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert calls == []
