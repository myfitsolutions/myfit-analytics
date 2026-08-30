import os
import unittest

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SESSION_COOKIE_SECURE"] = "false"

from app.database import Base
from app.models import Booking, ImportBatch, Member, Payment, RevenueTransaction, Studio, StudioDataSource, User
from app.platforms import PLATFORM_KEYS
from app.services.data_sources import get_dataset_availability, get_primary_data_source, set_primary_platform


class StudioDataSourceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add_all([Studio(id=1, name="One"), Studio(id=2, name="Two")])
        self.db.add(User(id=1, studio_id=1, email="owner@one.test", password_hash="x", role="owner"))
        self.db.commit()

    def tearDown(self):
        self.db.close(); self.engine.dispose()

    def test_supported_platforms_can_be_created_and_invalid_is_rejected(self):
        self.assertEqual(PLATFORM_KEYS, ("hapana", "bsport", "other"))
        for platform in PLATFORM_KEYS:
            source = set_primary_platform(self.db, 1, platform); self.db.commit()
            self.assertEqual(source.platform, platform)
        with self.assertRaises(ValueError): set_primary_platform(self.db, 1, "invalid")

    def test_switch_preserves_history_and_one_active_primary(self):
        hapana = set_primary_platform(self.db, 1, "hapana"); self.db.commit()
        batch = ImportBatch(studio_id=1, user_id=1, import_type="members", filename="members.csv", studio_data_source_id=hapana.id)
        self.db.add(batch); self.db.commit()
        bsport = set_primary_platform(self.db, 1, "bsport"); self.db.commit()
        self.db.refresh(hapana); self.db.refresh(batch)
        self.assertFalse(hapana.is_active); self.assertFalse(hapana.is_primary)
        self.assertEqual(batch.studio_data_source_id, hapana.id)
        self.assertEqual(get_primary_data_source(self.db, 1).id, bsport.id)
        self.assertEqual(self.db.query(StudioDataSource).filter_by(studio_id=1, is_primary=True, is_active=True).count(), 1)

    def test_database_rejects_two_active_primary_sources(self):
        self.db.add_all([
            StudioDataSource(studio_id=1, platform="hapana", display_name="Hapana", is_primary=True, is_active=True),
            StudioDataSource(studio_id=1, platform="bsport", display_name="bsport", is_primary=True, is_active=True),
        ])
        with self.assertRaises(IntegrityError): self.db.commit()
        self.db.rollback()

    def test_dataset_availability_uses_records_or_non_rolled_back_imports(self):
        self.assertEqual(get_dataset_availability(self.db, 1), {"members":False,"bookings":False,"payments":False,"revenue":False})
        self.db.add(Member(id=1, studio_id=1, first_name="A", last_name="One", email="a@one.test")); self.db.commit()
        self.assertTrue(get_dataset_availability(self.db, 1)["members"])
        self.db.add(ImportBatch(studio_id=1, user_id=1, import_type="bookings", filename="empty.csv", imported_count=0)); self.db.commit()
        availability = get_dataset_availability(self.db, 1)
        self.assertTrue(availability["bookings"])
        self.assertFalse(availability["payments"])
        self.db.add(Payment(studio_id=1, member_id=1, amount=1000, status="paid")); self.db.commit()
        availability = get_dataset_availability(self.db, 1)
        self.assertTrue(availability["payments"]); self.assertFalse(availability["revenue"])
        self.db.add(RevenueTransaction(studio_id=1, identity_key="one", analytics_date=__import__('datetime').datetime.now(__import__('datetime').timezone.utc), net_revenue=100, gross_revenue=100)); self.db.commit()
        self.assertTrue(get_dataset_availability(self.db, 1)["revenue"])
        self.assertEqual(get_dataset_availability(self.db, 2), {"members":False,"bookings":False,"payments":False,"revenue":False})


if __name__ == "__main__": unittest.main()
