import asyncio
import csv
import io
import os
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import UploadFile


os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SESSION_COOKIE_SECURE"] = "false"

from app.database import Base  # noqa: E402
from app.main import (  # noqa: E402
    ActionStatusUpdate,
    FollowUpCreate,
    complete_follow_up,
    create_follow_up,
    get_action_center,
    get_action_history,
    get_analytics_overview,
    get_members_crm,
    get_monthly_analytics,
    get_payment_recovery,
    get_retention_health,
    get_revenue_analytics,
    get_revenue_transactions,
    read_mapping_csv,
    run_mapped_import,
    suggested_mapping,
    transform_mapped_csv,
    update_action_status,
)
from app.models import ImportBatch, Studio, StudioDataSource, User  # noqa: E402
from app.services.data_sources import get_data_trust_summary  # noqa: E402


def csv_bytes(headers, rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


class StudioIntelligenceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        now = datetime.now(timezone.utc)
        self.studio = Studio(
            id=1,
            name="Synthetic Hapana Studio",
            timezone="UTC",
            currency="USD",
            onboarding_completed_at=now,
        )
        self.user = User(
            id=1,
            studio_id=1,
            email="owner@synthetic.test",
            password_hash="x",
            role="owner",
        )
        self.source = StudioDataSource(
            id=1,
            studio_id=1,
            platform="hapana",
            display_name="Hapana",
            is_primary=True,
            is_active=True,
        )
        self.db.add_all([self.studio, self.user, self.source])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    async def mapped_import(self, import_type, headers, rows):
        upload = UploadFile(
            io.BytesIO(csv_bytes(headers, rows)),
            filename=f"synthetic-hapana-{import_type}.csv",
        )
        filename, detected, raw_rows = await read_mapping_csv(upload)
        suggestions = suggested_mapping(import_type, detected)
        mapping = {key: value for key, value in suggestions.items() if value}
        content, _ = transform_mapped_csv(
            import_type, detected, raw_rows, mapping, self.studio
        )
        return await run_mapped_import(
            import_type, filename, content, self.user, self.db, False
        )

    def test_synthetic_hapana_import_to_action_and_outcome_flow(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        current_month_date = now.replace(day=1, hour=0, minute=0, second=0)
        previous_month_date = current_month_date - timedelta(days=1)
        people = [
            ("Healthy", "Member", "healthy@example.test"),
            ("Declining", "Member", "declining@example.test"),
            ("Watch", "Member", "watch@example.test"),
            ("Risk", "Member", "risk@example.test"),
            ("Critical", "Member", "critical@example.test"),
            ("Payment", "Member", "payment@example.test"),
            ("New", "Member", "new@example.test"),
        ]
        member_rows = [
            {
                "First Name": first,
                "Last Name": last,
                "Email Address": email,
                "Member Status": "Current",
                "Export Note": "ignored",
            }
            for first, last, email in people
        ]
        members_result = asyncio.run(self.mapped_import(
            "members",
            ["First Name", "Last Name", "Email Address", "Member Status", "Export Note"],
            member_rows,
        ))
        self.assertEqual(members_result["imported"], 7)

        booking_rows = []
        def booking(email, days, status="Completed", class_name="Studio Class"):
            booking_rows.append({
                "Email Address": email,
                "Class": class_name,
                "Start Date": (now - timedelta(days=days)).isoformat(),
                "Attendance Status": status,
                "Instructor": "Ignored Coach",
            })
        booking("healthy@example.test", 2)
        for day in (35, 42, 49, 56, 63, 70, 77, 82):
            booking("declining@example.test", day)
        booking("declining@example.test", 2)
        booking("watch@example.test", 15)
        booking("risk@example.test", 25)
        booking("critical@example.test", 40)
        booking("payment@example.test", 3)
        booking("new@example.test", 1)
        booking("healthy@example.test", 1, "Cancelled", "Cancelled Class")
        booking("healthy@example.test", 1, "Reserved", "Future Class")
        booking("healthy@example.test", 1, "No Show", "Missed Class")
        bookings_result = asyncio.run(self.mapped_import(
            "bookings",
            ["Email Address", "Class", "Start Date", "Attendance Status", "Instructor"],
            booking_rows,
        ))
        self.assertEqual(bookings_result["imported"], len(booking_rows))

        payment_rows = [
            {"Email Address":"healthy@example.test","Payment Amount":"45.00","Transaction Date":(now-timedelta(days=5)).isoformat(),"Payment Status":"Successful","Reference":"one"},
            {"Email Address":"payment@example.test","Payment Amount":"50.00","Transaction Date":(now-timedelta(days=4)).isoformat(),"Payment Status":"Declined","Reference":"two"},
            {"Email Address":"payment@example.test","Payment Amount":"50.00","Transaction Date":(now-timedelta(days=2)).isoformat(),"Payment Status":"Successful","Reference":"three"},
            {"Email Address":"payment@example.test","Payment Amount":"50.00","Transaction Date":(now-timedelta(days=2)).isoformat(),"Payment Status":"Successful","Reference":"duplicate"},
            {"Email Address":"healthy@example.test","Payment Amount":"invalid","Transaction Date":now.isoformat(),"Payment Status":"Successful","Reference":"invalid"},
        ]
        payments_result = asyncio.run(self.mapped_import(
            "payments",
            ["Email Address", "Payment Amount", "Transaction Date", "Payment Status", "Reference"],
            payment_rows,
        ))
        self.assertEqual((payments_result["imported"], payments_result["skipped_existing"], payments_result["invalid"]), (3, 1, 1))

        revenue_rows = [
            {"Email":"healthy@example.test","Payment Status":"Revenue","Revenue Type":"Membership","Description":"Monthly Membership","Transaction Id":"REV-1","Payment Date":current_month_date.isoformat(),"Tax":"8.00","Discount":"5.00","Gross Revenue":"105.00","Net Revenue":"100.00","Unnamed: 20":"8%"},
            {"Email":"unknown@example.test","Payment Status":"Revenue","Revenue Type":"POS","Description":"Retail Bottle","Transaction Id":"REV-2","Payment Date":current_month_date.isoformat(),"Tax":"2.00","Discount":"0","Gross Revenue":"32.00","Net Revenue":"30.00","Unnamed: 20":"8%"},
            {"Email":"healthy@example.test","Payment Status":"Refund","Revenue Type":"Membership","Description":"Refund","Transaction Id":"REV-3","Payment Date":current_month_date.isoformat(),"Tax":"-1.60","Discount":"0","Gross Revenue":"-21.60","Net Revenue":"-20.00","Unnamed: 20":"8%"},
            {"Email":"risk@example.test","Payment Status":"Revenue","Revenue Type":"Membership","Description":"Previous Period","Transaction Id":"REV-4","Payment Date":previous_month_date.isoformat(),"Tax":"7.20","Discount":"0","Gross Revenue":"97.20","Net Revenue":"90.00","Unnamed: 20":"8%"},
            {"Email":"healthy@example.test","Payment Status":"Revenue","Revenue Type":"Membership","Description":"Monthly Membership","Transaction Id":"REV-1","Payment Date":current_month_date.isoformat(),"Tax":"8.00","Discount":"5.00","Gross Revenue":"105.00","Net Revenue":"100.00","Unnamed: 20":"8%"},
        ]
        revenue_result = asyncio.run(self.mapped_import(
            "revenue",
            ["Email", "Payment Status", "Revenue Type", "Description", "Transaction Id", "Payment Date", "Tax", "Discount", "Gross Revenue", "Net Revenue", "Unnamed: 20"],
            revenue_rows,
        ))
        self.assertEqual((revenue_result["imported"], revenue_result["skipped_existing"]), (4, 1))

        trust = get_data_trust_summary(self.db, 1)
        self.assertTrue(all(dataset["available"] for dataset in trust["datasets"].values()))
        self.assertEqual(self.db.query(ImportBatch).count(), 4)

        overview = get_analytics_overview(1, self.user, self.db)
        monthly = get_monthly_analytics(1, self.user, self.db)
        revenue = get_revenue_analytics(1, "this_month", None, None, self.user, self.db)
        transactions = get_revenue_transactions(1, "this_month", None, None, "all", None, 1, 25, self.user, self.db)
        self.assertEqual(overview["financial_revenue_source"], "revenue_transactions")
        self.assertEqual(monthly["current_month"]["revenue"], revenue["summary"]["net_revenue"])
        self.assertEqual(revenue["summary"]["net_revenue"], 110)
        self.assertTrue(any(item["member_id"] is None for item in transactions["items"]))
        self.assertTrue(any(item["member_id"] is not None for item in transactions["items"]))

        members = get_members_crm(self.user, self.db)
        retention = get_retention_health(1, self.user, self.db)
        self.assertEqual(members["member_count"], 7)
        self.assertEqual(retention["summary"]["watch"], 1)
        self.assertEqual(retention["summary"]["at_risk"], 1)
        self.assertEqual(retention["summary"]["critical"], 1)

        center = get_action_center(1, self.user, self.db)
        action_types = {action["action_type"] for action in center["actions"]}
        self.assertTrue({"retention", "payment", "attendance_decline", "attendance_milestone"}.issubset(action_types))

        payment_action = next(action for action in center["actions"] if action["action_type"] == "payment")
        recovery = get_payment_recovery(1, self.user, self.db)
        self.assertTrue(any(item["later_matching_payment"] for item in recovery["payments"]))
        update_action_status(
            1,
            ActionStatusUpdate(member_id=payment_action["member_id"], action_type="payment", status="resolved"),
            self.user,
            self.db,
        )
        reopened_center = get_action_center(1, self.user, self.db)
        self.assertFalse(any(action["action_type"] == "payment" and action["member_id"] == payment_action["member_id"] for action in reopened_center["actions"]))
        update_action_status(
            1,
            ActionStatusUpdate(member_id=payment_action["member_id"], action_type="payment", status="open"),
            self.user,
            self.db,
        )

        risk_member = next(member for member in members["members"] if member["email"] == "risk@example.test")
        follow_up = create_follow_up(
            1,
            FollowUpCreate(member_id=risk_member["id"], action_type="retention", due_at=now + timedelta(days=1), note="Synthetic check-in"),
            self.user,
            self.db,
        )
        completed = complete_follow_up(1, follow_up["id"], self.user, self.db)
        self.assertEqual(completed["status"], "completed")
        history = get_action_history(1, 100, self.user, self.db)
        events = {entry["event_type"] for entry in history["history"]}
        self.assertTrue({"action_resolved", "action_reopened", "follow_up_scheduled", "follow_up_completed"}.issubset(events))


if __name__ == "__main__":
    unittest.main()
