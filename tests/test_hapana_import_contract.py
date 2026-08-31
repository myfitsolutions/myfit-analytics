import asyncio
import io
import os
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import UploadFile


os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SESSION_COOKIE_SECURE"] = "false"

from app.database import Base  # noqa: E402
from app.main import (  # noqa: E402
    read_mapping_csv,
    run_mapped_import,
    suggested_mapping,
    transform_mapped_csv,
)
from app.models import Booking, Member, Payment, Studio, StudioDataSource, User  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures"


class HapanaImportContractTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.studio = Studio(id=1, name="Synthetic Hapana", timezone="Asia/Singapore")
        self.user = User(
            id=1,
            studio_id=1,
            email="owner@example.test",
            password_hash="x",
            role="owner",
        )
        self.source = StudioDataSource(
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

    async def import_fixture(self, import_type):
        path = FIXTURES / f"hapana_{import_type}_v1.csv"
        upload = UploadFile(io.BytesIO(path.read_bytes()), filename=path.name)
        filename, headers, rows = await read_mapping_csv(upload)
        mapping = suggested_mapping(import_type, headers)
        selected = {source: destination for source, destination in mapping.items() if destination}
        content, _ = transform_mapped_csv(
            import_type, headers, rows, selected, self.studio
        )
        result = await run_mapped_import(
            import_type, filename, content, self.user, self.db, False
        )
        return result, mapping

    def test_hapana_member_headers_duplicates_invalid_rows_and_ignored_columns(self):
        result, mapping = asyncio.run(self.import_fixture("members"))
        self.assertEqual(
            (result["total_rows"], result["imported"], result["skipped_existing"], result["invalid"]),
            (5, 3, 1, 1),
        )
        self.assertIsNone(mapping["Membership Type"])
        self.assertIsNone(mapping["Unnamed: 8"])
        self.assertEqual(self.db.query(Member).count(), 3)
        inactive = self.db.query(Member).filter_by(email="inez.inactive@example.test").one()
        self.assertEqual(inactive.status, "inactive")

    def test_hapana_booking_attendance_matching_duplicates_and_statuses(self):
        asyncio.run(self.import_fixture("members"))
        result, mapping = asyncio.run(self.import_fixture("bookings"))
        self.assertEqual(
            (result["total_rows"], result["imported"], result["skipped_existing"], result["invalid"]),
            (7, 4, 1, 2),
        )
        self.assertIsNone(mapping["Location"])
        self.assertIsNone(mapping["Instructor"])
        statuses = sorted(row[0] for row in self.db.query(Booking.status).all())
        self.assertEqual(statuses, ["attended", "booked", "cancelled", "no_show"])
        hana = self.db.query(Member).filter_by(email="hana.healthy@example.test").one()
        self.assertEqual(
            self.db.query(Booking).filter_by(member_id=hana.id, status="attended").count(),
            1,
        )

    def test_hapana_payments_amounts_matching_duplicates_and_later_success(self):
        asyncio.run(self.import_fixture("members"))
        result, mapping = asyncio.run(self.import_fixture("payments"))
        self.assertEqual(
            (result["total_rows"], result["imported"], result["skipped_existing"], result["invalid"]),
            (6, 3, 1, 2),
        )
        self.assertIsNone(mapping["Transaction Id"])
        self.assertIsNone(mapping["Notes"])
        felix = self.db.query(Member).filter_by(email="felix.finance@example.test").one()
        payments = self.db.query(Payment).filter_by(member_id=felix.id).order_by(Payment.payment_date).all()
        self.assertEqual([(payment.amount, payment.status) for payment in payments], [(5000, "failed"), (5000, "paid")])


if __name__ == "__main__":
    unittest.main()
