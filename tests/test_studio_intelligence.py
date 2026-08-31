import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SESSION_COOKIE_SECURE"] = "false"

from app.database import Base  # noqa: E402
from app.main import (  # noqa: E402
    ActionStatusUpdate,
    get_analytics_overview,
    get_member_crm_detail,
    get_monthly_analytics,
    get_payment_recovery,
    get_revenue_analytics,
    update_action_status,
)
from app.models import (  # noqa: E402
    ImportBatch,
    Member,
    Payment,
    RevenueTransaction,
    Studio,
    StudioDataSource,
    User,
)
from app.services.data_sources import get_data_trust_summary  # noqa: E402


class StudioIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        now = datetime.now(timezone.utc)
        self.studio = Studio(
            id=1,
            name="One",
            timezone="UTC",
            currency="USD",
            onboarding_completed_at=now,
        )
        self.user = User(
            id=1,
            studio_id=1,
            email="owner@one.test",
            password_hash="x",
            role="owner",
        )
        self.member = Member(
            id=1,
            studio_id=1,
            first_name="Payment",
            last_name="Member",
            email="payment@example.test",
            status="active",
        )
        self.source = StudioDataSource(
            id=1,
            studio_id=1,
            platform="hapana",
            display_name="Hapana",
            is_primary=True,
            is_active=True,
        )
        self.db.add_all([self.studio, self.user, self.member, self.source])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_revenue(self, identity, amount, when):
        self.db.add(RevenueTransaction(
            studio_id=1,
            studio_data_source_id=1,
            identity_key=identity,
            analytics_date=when,
            transaction_kind="refund" if amount < 0 else "revenue",
            gross_revenue=amount,
            net_revenue=amount,
        ))

    def test_dashboard_and_revenue_workspace_use_same_authoritative_transactions(self):
        now = datetime.now(timezone.utc)
        self.db.add(Payment(
            studio_id=1,
            member_id=1,
            amount=999900,
            status="paid",
            payment_date=now,
        ))
        self.add_revenue("sale", Decimal("100.00"), now)
        self.add_revenue("refund", Decimal("-20.00"), now)
        self.db.commit()

        monthly = get_monthly_analytics(1, self.user, self.db)
        overview = get_analytics_overview(1, self.user, self.db)
        workspace = get_revenue_analytics(
            1, "this_month", None, None, self.user, self.db
        )

        self.assertEqual(monthly["financial_revenue_source"], "revenue_transactions")
        self.assertEqual(monthly["current_month"]["revenue"], Decimal("80.00"))
        self.assertEqual(workspace["summary"]["net_revenue"], Decimal("80.00"))
        self.assertEqual(overview["total_revenue"], Decimal("80.00"))
        self.assertEqual(overview["average_revenue_per_active_member"], Decimal("80.00"))

    def test_legacy_payment_fallback_is_explicit_and_empty_is_unavailable(self):
        now = datetime.now(timezone.utc)
        self.db.add(Payment(
            studio_id=1,
            member_id=1,
            amount=5000,
            status="paid",
            payment_date=now,
        ))
        self.db.commit()
        fallback = get_monthly_analytics(1, self.user, self.db)
        self.assertEqual(fallback["financial_revenue_source"], "legacy_payments")
        self.assertEqual(fallback["current_month"]["revenue"], Decimal("50"))

        empty_studio = Studio(id=2, name="Empty", timezone="UTC")
        empty_user = User(id=2, studio_id=2, email="owner@empty.test", password_hash="x", role="owner")
        self.db.add_all([empty_studio, empty_user])
        self.db.commit()
        empty = get_monthly_analytics(2, empty_user, self.db)
        self.assertFalse(empty["financial_revenue_available"])
        self.assertIsNone(empty["current_month"]["revenue"])

    def test_data_trust_uses_current_records_and_ignores_rolled_back_freshness(self):
        now = datetime.now(timezone.utc)
        completed = ImportBatch(
            studio_id=1,
            user_id=1,
            import_type="members",
            filename="members.csv",
            imported_count=1,
            status="completed",
            studio_data_source_id=1,
            created_at=now - timedelta(days=2),
        )
        rolled_back = ImportBatch(
            studio_id=1,
            user_id=1,
            import_type="members",
            filename="rolled-back.csv",
            imported_count=10,
            status="rolled_back",
            studio_data_source_id=1,
            created_at=now,
        )
        empty_booking = ImportBatch(
            studio_id=1,
            user_id=1,
            import_type="bookings",
            filename="empty.csv",
            imported_count=0,
            status="completed",
            studio_data_source_id=1,
        )
        self.db.add_all([completed, rolled_back, empty_booking])
        self.db.commit()

        trust = get_data_trust_summary(self.db, 1)
        self.assertTrue(trust["datasets"]["members"]["available"])
        self.assertEqual(trust["datasets"]["members"]["record_count"], 1)
        self.assertEqual(trust["datasets"]["members"]["filename"], "members.csv")
        self.assertFalse(trust["datasets"]["bookings"]["available"])
        self.assertIsNone(trust["datasets"]["bookings"]["last_imported_at"])

    def test_payment_workflow_open_contacted_resolved_reopened_and_evidence(self):
        now = datetime.now(timezone.utc)
        self.db.add_all([
            Payment(studio_id=1, member_id=1, amount=5000, status="failed", payment_date=now - timedelta(days=3)),
            Payment(studio_id=1, member_id=1, amount=5000, status="paid", payment_date=now - timedelta(days=1)),
        ])
        self.db.commit()

        opened = get_payment_recovery(1, self.user, self.db)
        self.assertEqual(opened["needs_attention_count"], 1)
        self.assertTrue(opened["payments"][0]["later_matching_payment"])
        self.assertFalse(opened["payments"][0]["recovery_confirmed"])

        for status in ("contacted", "resolved", "open"):
            update_action_status(
                1,
                ActionStatusUpdate(member_id=1, action_type="payment", status=status),
                self.user,
                self.db,
            )
            result = get_payment_recovery(1, self.user, self.db)
            self.assertEqual(result["payments"][0]["workflow_status"], status)

        update_action_status(
            1,
            ActionStatusUpdate(member_id=1, action_type="payment", status="resolved"),
            self.user,
            self.db,
        )
        resolved = get_payment_recovery(1, self.user, self.db)
        detail = get_member_crm_detail(1, self.user, self.db)
        self.assertEqual(resolved["needs_attention_count"], 0)
        self.assertEqual(resolved["resolved_count"], 1)
        self.assertEqual(detail["payment_summary"]["workflow_status"], "resolved")
        self.assertTrue(detail["payment_summary"]["later_matching_payment"])
        self.assertFalse(detail["payment_summary"]["recovery_confirmed"])

    def test_modified_intelligence_queries_are_studio_scoped(self):
        now = datetime.now(timezone.utc)
        other_studio = Studio(id=2, name="Other", timezone="UTC")
        other_member = Member(
            id=2,
            studio_id=2,
            first_name="Other",
            last_name="Member",
            email="other@example.test",
            status="active",
        )
        self.db.add_all([
            other_studio,
            other_member,
            RevenueTransaction(
                studio_id=2,
                identity_key="other-revenue",
                analytics_date=now,
                transaction_kind="revenue",
                gross_revenue=Decimal("900.00"),
                net_revenue=Decimal("900.00"),
            ),
            Payment(
                studio_id=2,
                member_id=2,
                amount=7000,
                status="failed",
                payment_date=now,
            ),
        ])
        self.add_revenue("own-revenue", Decimal("25.00"), now)
        self.db.commit()

        overview = get_analytics_overview(1, self.user, self.db)
        trust = get_data_trust_summary(self.db, 1)
        recovery = get_payment_recovery(1, self.user, self.db)

        self.assertEqual(overview["total_revenue"], Decimal("25.00"))
        self.assertEqual(trust["datasets"]["revenue"]["record_count"], 1)
        self.assertEqual(trust["datasets"]["members"]["record_count"], 1)
        self.assertEqual(recovery["failed_payment_count"], 0)


if __name__ == "__main__":
    unittest.main()
