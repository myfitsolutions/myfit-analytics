import os
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

os.environ["APP_ENV"]="development";os.environ["DATABASE_URL"]="sqlite:///:memory:";os.environ["SESSION_COOKIE_SECURE"]="false"
from app.database import Base
from app.main import app, dashboard, serialize_studio_settings
from app.models import Studio, User


ROOT=Path(__file__).resolve().parents[1]


class SettingsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine("sqlite:///:memory:");Base.metadata.create_all(self.engine);self.db=sessionmaker(bind=self.engine)()
        self.studio=Studio(id=1,name="MyFit Demo Studio",timezone="Asia/Manila",currency="PHP",sender_name="MyFit Demo Studio",retention_healthy_days=13,retention_watch_days=20,retention_at_risk_days=30,default_follow_up_days=3,onboarding_completed_at=datetime.now(timezone.utc))
        self.users={role:User(id=index,studio_id=1,email=f"{role}@example.test",password_hash="x",role=role) for index,role in enumerate(("owner","manager","staff"),1)}
        self.db.add(self.studio);self.db.add_all(self.users.values());self.db.commit()

    def tearDown(self): self.db.close();self.engine.dispose()

    def render_dashboard(self,role):
        request=Request({"type":"http","method":"GET","path":"/dashboard","headers":[],"session":{"user_id":self.users[role].id}})
        return dashboard(request,self.db).body.decode("utf-8")

    def test_owner_render_and_runtime_contract_are_editable_and_populated(self):
        html=self.render_dashboard("owner")
        self.assertIn('const CURRENT_USER_ROLE = "owner";',html)
        self.assertIn('const canEditSettings = CURRENT_USER_ROLE === "owner";',html)
        self.assertIn('field.readOnly = !canEditSettings;',html)
        self.assertIn('saveSettingsButton.hidden = !canEditSettings;',html)
        self.assertIn('id="save-settings"',html)
        for field_id in ("setting-name","setting-timezone","setting-currency","setting-sender-name","setting-healthy","setting-watch","setting-at-risk","setting-follow-up"):
            tag=html[html.index(f'id="{field_id}"')-20:html.index(f'id="{field_id}"')+140]
            self.assertNotIn(" disabled",tag);self.assertNotIn(" readonly",tag)
        values=serialize_studio_settings(self.studio)
        self.assertEqual(values,{"studio_id":1,"name":"MyFit Demo Studio","timezone":"Asia/Manila","currency":"PHP","retention_healthy_days":13,"retention_watch_days":20,"retention_at_risk_days":30,"default_follow_up_days":3,"sender_name":"MyFit Demo Studio"})
        self.assertIn('field.value = settings[key] ?? "";',html)

    def test_manager_and_staff_runtime_contract_remains_non_editable(self):
        manager=self.render_dashboard("manager");staff=self.render_dashboard("staff")
        self.assertIn('const CURRENT_USER_ROLE = "manager";',manager);self.assertIn('const CURRENT_USER_ROLE = "staff";',staff)
        self.assertIn('field.readOnly = !canEditSettings;',manager)
        self.assertIn('saveSettingsButton.hidden = !canEditSettings;',manager)
        self.assertNotIn('id="open-settings"',staff)

    def test_runtime_serves_final_theme_contract_after_legacy_rules(self):
        response=TestClient(app).get("/static/style.css?v=settings-theme-v3")
        self.assertEqual(response.status_code,200)
        css=response.text;final_start=css.index("/* Final Studio Settings theme contract.")
        self.assertGreater(final_start,css.rindex("input:disabled,select:disabled"))
        contract=css[final_start:]
        self.assertIn("color:var(--text)",contract);self.assertIn("-webkit-text-fill-color:var(--text); opacity:1",contract)
        self.assertIn("color:var(--text-secondary)",contract);self.assertIn("color:var(--text-muted)",contract)
        self.assertNotIn("#111827",contract);self.assertNotIn("#374151",contract)


if __name__=="__main__": unittest.main()
