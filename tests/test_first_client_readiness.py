import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SESSION_COOKIE_SECURE"] = "false"

from app.database import Base
from app.main import build_data_source_status, build_import_readiness, imports_page
from app.models import Booking, Member, Payment, RevenueTransaction, Studio, User


ROOT = Path(__file__).resolve().parents[1]


def trust_for(*available):
    return {"datasets": {
        name: {
            "available": name in available,
            "record_count": 10 if name in available else 0,
            "latest_record_date": datetime(2026, 8, 31, tzinfo=timezone.utc) if name in available and name != "members" else None,
            "last_imported_at": datetime(2026, 9, 1, tzinfo=timezone.utc) if name in available else None,
            "source": "Hapana" if name in available else None,
        }
        for name in ("members", "bookings", "payments", "revenue")
    }}


class FirstClientReadinessTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        now = datetime.now(timezone.utc)
        self.studios = [
            Studio(id=1, name="New Studio", timezone="UTC", currency="USD", onboarding_completed_at=now),
            Studio(id=2, name="Other Studio", timezone="UTC", currency="USD", onboarding_completed_at=now),
        ]
        self.users = {
            role: User(id=index, studio_id=1, email=f"{role}@example.test", password_hash="x", role=role)
            for index, role in enumerate(("owner", "manager", "staff"), 1)
        }
        self.db.add_all(self.studios + list(self.users.values()))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_next_step_progression_for_every_first_client_state(self):
        hapana = {"platform": "hapana"}
        cases = [
            ((), "import_members"),
            (("members",), "import_bookings"),
            (("members", "bookings"), "import_payments"),
            (("members", "bookings", "payments"), "import_revenue"),
            (("members", "bookings", "payments", "revenue"), "review_dashboard"),
        ]
        for available, expected in cases:
            with self.subTest(available=available):
                readiness = build_import_readiness(trust_for(*available), hapana)
                self.assertEqual(readiness["next_step"]["type"], expected)
                self.assertEqual([item["position"] for item in readiness["datasets"]], [1, 2, 3, 4])
                self.assertEqual([item["name"] for item in readiness["datasets"]], ["members", "bookings", "payments", "revenue"])

    def test_revenue_is_optional_without_hapana_and_status_metadata_stays_neutral(self):
        readiness = build_import_readiness(trust_for("members", "bookings", "payments"), None)
        self.assertEqual(readiness["next_step"]["type"], "review_dashboard")
        self.assertIn("optional", readiness["next_step"]["description"])
        bookings = readiness["datasets"][1]
        self.assertEqual(bookings["status"], "imported")
        self.assertEqual(bookings["record_count"], 10)
        self.assertIsNotNone(bookings["latest_record_date"])
        self.assertNotIn("stale", str(readiness).lower())

    def test_readiness_counts_and_business_dates_are_tenant_scoped(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        own = Member(studio_id=1, first_name="Own", last_name="Member", email="own@example.test", status="active")
        other = Member(studio_id=2, first_name="Other", last_name="Member", email="other@example.test", status="active")
        self.db.add_all([own, other]); self.db.flush()
        self.db.add_all([
            Booking(studio_id=1, member_id=own.id, class_name="Own", status="attended", booking_date=now - timedelta(days=2)),
            Booking(studio_id=2, member_id=other.id, class_name="Other", status="attended", booking_date=now + timedelta(days=20)),
            Payment(studio_id=1, member_id=own.id, amount=100, status="paid", payment_date=now - timedelta(days=3)),
            RevenueTransaction(studio_id=2, identity_key="other", analytics_date=now, transaction_kind="revenue", net_revenue=999, gross_revenue=999),
        ]); self.db.commit()
        status = build_data_source_status(self.db, 1)
        datasets = {item["name"]: item for item in status["import_readiness"]["datasets"]}
        self.assertEqual(datasets["members"]["record_count"], 1)
        self.assertEqual(datasets["bookings"]["record_count"], 1)
        self.assertEqual(datasets["bookings"]["latest_record_date"].date(), (now - timedelta(days=2)).date())
        self.assertEqual(datasets["revenue"]["status"], "not_imported")

    def test_imports_page_permissions_remain_owner_manager_only(self):
        for role in ("owner", "manager"):
            request = Request({"type": "http", "method": "GET", "path": "/imports", "headers": [], "session": {"user_id": self.users[role].id}})
            self.assertEqual(imports_page(request, self.db).status_code, 200)
        request = Request({"type": "http", "method": "GET", "path": "/imports", "headers": [], "session": {"user_id": self.users["staff"].id}})
        with self.assertRaises(HTTPException) as denied:
            imports_page(request, self.db)
        self.assertEqual(denied.exception.status_code, 403)

    def test_first_client_ui_post_import_empty_and_responsive_contracts(self):
        template = (ROOT / "templates/imports.html").read_text(encoding="utf-8")
        script = (ROOT / "static/import_history.js").read_text(encoding="utf-8")
        members = (ROOT / "static/member_crm.js").read_text(encoding="utf-8")
        dashboard = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
        css = (ROOT / "static/style.css").read_text(encoding="utf-8")
        positions = [template.index(f'id="import-{name}"') for name in ("members", "bookings", "payments", "revenue")]
        self.assertEqual(positions, sorted(positions))
        for phrase in ("Data Readiness", "Members → Bookings → Payments → Revenue", "Next step"):
            self.assertIn(phrase, template + script)
        self.assertIn("Data through", script)
        self.assertIn("Imported ${formatDate", script)
        self.assertIn("What’s next", script)
        self.assertIn('location.hash.startsWith("#import-")', script)
        self.assertIn("No member data yet", members)
        self.assertIn("payments for payment health, and revenue for financial reporting", dashboard)
        for token in ("var(--surface-elevated)", "var(--text-secondary)", "var(--success)", "var(--purple)"):
            self.assertIn(token, css)
        self.assertIn(".import-readiness-grid { grid-template-columns:1fr; }", css)
        self.assertIn(".import-next-step { align-items:stretch; flex-direction:column; }", css)


if __name__ == "__main__":
    unittest.main()
