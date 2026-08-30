import os
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SESSION_COOKIE_SECURE"] = "false"

from app.database import Base
from app.models import Booking, Member, MemberMilestoneStatus, Studio
from app.services.attendance import calculate_attendance_decline, format_ordinal, get_attendance_aggregates, get_attendance_milestone


class AttendanceIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add_all([Studio(id=1, name="One"), Studio(id=2, name="Two")])
        self.db.add_all([
            Member(id=1, studio_id=1, first_name="A", last_name="One", email="a@one.test"),
            Member(id=2, studio_id=2, first_name="B", last_name="Two", email="b@two.test"),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_milestone_examples_and_boundaries(self):
        self.assertEqual(get_attendance_milestone(48)["next_milestone"], 50)
        self.assertEqual(get_attendance_milestone(48)["visits_until_next_milestone"], 2)
        fifty = get_attendance_milestone(50)
        self.assertEqual(fifty["milestone_reached"], 50)
        self.assertEqual(fifty["last_milestone"], 50)
        self.assertEqual(fifty["next_milestone"], 100)
        for value in (1, 5, 10):
            self.assertEqual(get_attendance_milestone(value)["milestone_reached"], value)

    def test_ordinal_formatting(self):
        expected = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", 10: "10th", 11: "11th", 12: "12th", 13: "13th", 21: "21st", 22: "22nd", 23: "23rd", 25: "25th", 50: "50th", 100: "100th", 250: "250th", 500: "500th"}
        self.assertEqual({value: format_ordinal(value) for value in expected}, expected)

    def test_decline_is_conservative(self):
        self.assertFalse(calculate_attendance_decline(3, 0)["attendance_declining"])
        self.assertFalse(calculate_attendance_decline(8, 4)["attendance_declining"])
        self.assertTrue(calculate_attendance_decline(16, 2)["attendance_declining"])

    def test_only_attended_and_requested_studio_are_aggregated(self):
        now = datetime.now(timezone.utc)
        rows = []
        for index in range(5):
            rows.append(Booking(studio_id=1, member_id=1, class_name="Class", status="attended", booking_date=now - timedelta(days=index)))
        for status in ("cancelled", "no_show", "booked"):
            rows.append(Booking(studio_id=1, member_id=1, class_name="Class", status=status, booking_date=now))
        rows.append(Booking(studio_id=2, member_id=2, class_name="Class", status="attended", booking_date=now))
        self.db.add_all(rows)
        self.db.commit()
        result = get_attendance_aggregates(self.db, 1, now)
        self.assertEqual(result[1]["total_attended"], 5)
        self.assertNotIn(2, result)

    def test_acknowledged_milestone_identity_cannot_be_recreated(self):
        self.db.add(MemberMilestoneStatus(studio_id=1, member_id=1, milestone_type="attendance", milestone_value=1, status="celebrated"))
        self.db.commit()
        self.db.add(MemberMilestoneStatus(studio_id=1, member_id=1, milestone_type="attendance", milestone_value=1, status="open"))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()
        record = self.db.query(MemberMilestoneStatus).filter_by(studio_id=1, member_id=1, milestone_type="attendance", milestone_value=1).one()
        self.assertEqual(record.status, "celebrated")


if __name__ == "__main__":
    unittest.main()
