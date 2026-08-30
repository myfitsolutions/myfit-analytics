import csv
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

os.environ["APP_ENV"]="development"; os.environ["DATABASE_URL"]="sqlite:///:memory:"; os.environ["SESSION_COOKIE_SECURE"]="false"
from app.database import Base
from app.models import ImportBatch, Member, RevenueTransaction, Studio, StudioDataSource, User
from app.platforms import get_import_profile
from app.services.revenue import normalize_revenue_row, parse_money, parse_revenue_date, revenue_identity
from app.main import normalize_csv_header, suggested_mapping


class RevenueTransactionTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine("sqlite:///:memory:"); Base.metadata.create_all(self.engine); self.db=sessionmaker(bind=self.engine)()
        self.db.add(Studio(id=1,name="One")); self.db.add(User(id=1,studio_id=1,email="o@one.test",password_hash="x",role="owner")); self.db.commit()
        self.source=StudioDataSource(studio_id=1,platform="hapana",display_name="Hapana",is_primary=True,is_active=True); self.db.add(self.source); self.db.commit()

    def tearDown(self): self.db.close(); self.engine.dispose()

    def test_profile_is_centralized_and_versioned(self):
        profile=get_import_profile("hapana","revenue","v1")
        self.assertEqual(profile["version"],1); self.assertEqual(profile["mapping"]["Full Name"],"customer_name"); self.assertEqual(profile["mapping"]["Email"],"customer_email"); self.assertEqual(profile["mapping"]["Transaction Category"],"transaction_category"); self.assertEqual(profile["required"],["Net Revenue"])

    def test_money_dates_refund_and_fingerprint(self):
        row=normalize_revenue_row({"external_transaction_id":"T-1","net_revenue":"136.70","gross_revenue":"150.00","discount":"13.30","payment_date":"2026-08-28T12:00:00+08:00","source_status":"Refund"})
        self.assertIsInstance(row["net_revenue"],Decimal); self.assertEqual(row["net_revenue"],Decimal("-136.70")); self.assertEqual(row["gross_revenue"],Decimal("-150.00")); self.assertEqual(row["transaction_kind"],"refund"); self.assertEqual(row["identity_key"],"tx:t-1")
        fallback={"customer_email":"a@example.com","payment_date":"2026-08-28","description":"Membership","net_revenue":"10","source_status":"Revenue"}
        self.assertEqual(revenue_identity(fallback),revenue_identity(dict(reversed(list(fallback.items())))))

    def test_actual_money_and_day_first_date_formats(self):
        self.assertEqual(parse_money(" $916.51 "), Decimal("916.51"))
        self.assertEqual(parse_money("($20.00)"), Decimal("-20.00"))
        self.assertEqual(parse_money("-$1,234.50"), Decimal("-1234.50"))
        self.assertEqual(parse_revenue_date("8/10/2024").date().isoformat(), "2024-10-08")
        self.assertEqual(parse_revenue_date("30/09/2024").date().isoformat(), "2024-09-30")
        self.assertEqual(parse_revenue_date("8/10/2024 14:19").hour, 14)
        self.assertEqual(parse_revenue_date("21/09/2024 00:00:00").second, 0)

    def test_real_format_fixture_maps_and_normalizes(self):
        fixture=Path(__file__).parent / "fixtures" / "hapana_revenue_v1.csv"
        with fixture.open(encoding="utf-8", newline="") as stream:
            reader=csv.DictReader(stream); headers=[normalize_csv_header(value) for value in reader.fieldnames]; reader.fieldnames=headers; rows=list(reader)
        mapping=suggested_mapping("revenue",headers)
        self.assertEqual(len(rows),3); self.assertEqual(headers[0],"Full Name"); self.assertIsNone(mapping["Unnamed: 19"]); self.assertIsNone(mapping["Unnamed: 20"])
        self.assertEqual(rows[0]["Unnamed: 19"],"3.715596"); self.assertEqual(rows[0]["Unnamed: 20"],"8%")
        self.assertEqual(mapping["Full Name"],"customer_name"); self.assertEqual(mapping["Admin Fee"],"admin_fee"); self.assertEqual(mapping["Transaction Category"],"transaction_category")
        canonical=[{destination: row[source] for source,destination in mapping.items() if destination} for row in rows]
        parsed=[normalize_revenue_row(row) for row in canonical]
        self.assertEqual(parsed[0]["transaction_fee"],Decimal("0.50")); self.assertEqual(parsed[1]["transaction_category"],"Apparel Apparel")
        self.assertEqual(parsed[1]["gross_revenue"],Decimal("-20.00")); self.assertEqual(parsed[1]["net_revenue"],Decimal("-18.35")); self.assertEqual(parsed[1]["tax"],Decimal("-1.65"))
        self.assertTrue(parsed[1]["identity_key"].startswith("fp:")); self.assertEqual(parsed[1]["identity_key"],normalize_revenue_row(canonical[1])["identity_key"])

    def test_known_685_row_export_aggregate_regression(self):
        rows=[]
        for index in range(685):
            is_refund=index >= 681
            rows.append({
                "customer_email":f"member{index}@example.test", "payment_date":"8/10/2024 14:19",
                "source_status":"Refund" if is_refund else "Revenue", "revenue_type":"Membership",
                "transaction_category":"Apparel" if index == 0 else "", "description":f"Sanitized transaction {index}",
                "external_transaction_id":f"TX-{index + 1}" if index < 502 else "",
                "gross_revenue":"$0.00", "net_revenue":"$0.00", "tax":"$0.00", "discount":"$0.00",
            })
        rows[0].update({"gross_revenue":"$51,528.65","net_revenue":"$47,277.82","tax":"$4,238.53","discount":"$84.35"})
        for offset, row in enumerate(rows[-4:]):
            row["gross_revenue"]="($37.25)"; row["net_revenue"]="($34.18)" if offset < 2 else "($34.17)"
        parsed=[normalize_revenue_row(row) for row in rows]
        self.assertEqual(len(parsed),685)
        self.assertEqual(sum(row["transaction_kind"] == "revenue" for row in parsed),681); self.assertEqual(sum(row["transaction_kind"] == "refund" for row in parsed),4)
        self.assertEqual(sum((row["gross_revenue"] for row in parsed),Decimal("0")),Decimal("51379.65")); self.assertEqual(sum((row["net_revenue"] for row in parsed),Decimal("0")),Decimal("47141.12"))
        refunds=[row for row in parsed if row["transaction_kind"] == "refund"]
        self.assertEqual(sum((row["gross_revenue"] for row in refunds),Decimal("0")),Decimal("-149.00")); self.assertEqual(sum((row["net_revenue"] for row in refunds),Decimal("0")),Decimal("-136.70"))
        self.assertEqual(sum((row["tax"] or Decimal("0") for row in parsed),Decimal("0")),Decimal("4238.53")); self.assertEqual(sum((row["discount"] or Decimal("0") for row in parsed),Decimal("0")),Decimal("84.35"))
        self.assertEqual(sum(bool(row.get("external_transaction_id")) for row in parsed),502); self.assertEqual(sum(not row.get("external_transaction_id") for row in parsed),183)

    def test_nullable_member_and_source_batch_attribution(self):
        batch=ImportBatch(studio_id=1,user_id=1,import_type="revenue",filename="revenue.csv",studio_data_source_id=self.source.id); self.db.add(batch); self.db.flush()
        tx=RevenueTransaction(studio_id=1,member_id=None,studio_data_source_id=self.source.id,import_batch_id=batch.id,identity_key="tx:unknown",analytics_date=datetime.now(timezone.utc),net_revenue=Decimal("20.00"),gross_revenue=Decimal("20.00"),transaction_kind="revenue")
        self.db.add(tx); self.db.commit(); self.assertIsNone(tx.member_id); self.assertEqual(tx.studio_data_source_id,self.source.id); self.assertEqual(tx.import_batch_id,batch.id)

    def test_invalid_money_and_date_are_rejected(self):
        with self.assertRaisesRegex(ValueError,"monetary"): normalize_revenue_row({"net_revenue":"oops","payment_date":"2026-08-28T00:00:00Z"})
        with self.assertRaisesRegex(ValueError,"date"): normalize_revenue_row({"net_revenue":"1.00","payment_date":"not-a-date"})

    def test_duplicate_identity_is_source_scoped(self):
        now=datetime.now(timezone.utc)
        self.db.add(RevenueTransaction(studio_id=1,studio_data_source_id=self.source.id,identity_key="tx:dup",analytics_date=now,net_revenue=1,gross_revenue=1,transaction_kind="revenue"));self.db.commit()
        self.db.add(RevenueTransaction(studio_id=1,studio_data_source_id=self.source.id,identity_key="tx:dup",analytics_date=now,net_revenue=1,gross_revenue=1,transaction_kind="revenue"))
        with self.assertRaises(IntegrityError): self.db.commit()
        self.db.rollback()

    def test_normalized_analytics_totals_and_dynamic_groups(self):
        now=datetime.now(timezone.utc)
        rows=[
            RevenueTransaction(studio_id=1,studio_data_source_id=self.source.id,identity_key="a",analytics_date=now,net_revenue=Decimal("100"),gross_revenue=Decimal("120"),discount=Decimal("20"),transaction_kind="revenue",revenue_type="Membership",description="Monthly",payment_method="Card"),
            RevenueTransaction(studio_id=1,studio_data_source_id=self.source.id,identity_key="b",analytics_date=now,net_revenue=Decimal("50"),gross_revenue=Decimal("50"),discount=Decimal("0"),transaction_kind="revenue",revenue_type="POS",description="Water",payment_method="Cash"),
            RevenueTransaction(studio_id=1,studio_data_source_id=self.source.id,identity_key="c",analytics_date=now,net_revenue=Decimal("-25"),gross_revenue=Decimal("-30"),discount=Decimal("0"),transaction_kind="refund",revenue_type="Membership",description="Monthly",payment_method="Card"),
        ];self.db.add_all(rows);self.db.commit()
        totals=self.db.query(func.sum(RevenueTransaction.net_revenue),func.sum(RevenueTransaction.gross_revenue),func.sum(RevenueTransaction.discount)).filter(RevenueTransaction.studio_id==1).one()
        self.assertEqual(totals,(Decimal("125.00"),Decimal("140.00"),Decimal("20.00")))
        grouped=dict(self.db.query(RevenueTransaction.revenue_type,func.sum(RevenueTransaction.net_revenue)).filter(RevenueTransaction.studio_id==1).group_by(RevenueTransaction.revenue_type).all())
        self.assertEqual(grouped,{"Membership":Decimal("75.00"),"POS":Decimal("50.00")})

    def test_unknown_type_optional_fields_and_extra_columns_are_safe(self):
        row=normalize_revenue_row({"net_revenue":"10","invoice_date":"2026-08-28T00:00:00Z","revenue_type":"Future Category","extra_export_column":"ignored"})
        self.assertEqual(row["revenue_type"],"Future Category");self.assertIsNone(row["payment_date"]);self.assertEqual(row["transaction_kind"],"revenue")

    def test_real_fields_are_persisted_as_decimals(self):
        row=normalize_revenue_row({"net_revenue":"$10.00","invoice_date":"8/10/2024","transaction_category":"Apparel","admin_fee":"$1.00","dishonour_fee":"($2.00)","transaction_fee":"$0.30"})
        tx=RevenueTransaction(studio_id=1,studio_data_source_id=self.source.id,identity_key=row["identity_key"],analytics_date=row["analytics_date"],net_revenue=row["net_revenue"],gross_revenue=row["gross_revenue"],transaction_kind=row["transaction_kind"],transaction_category=row["transaction_category"],admin_fee=row["admin_fee"],dishonour_fee=row["dishonour_fee"],transaction_fee=row["transaction_fee"])
        self.db.add(tx);self.db.commit();self.db.refresh(tx)
        self.assertEqual((tx.transaction_category,tx.admin_fee,tx.dishonour_fee,tx.transaction_fee),("Apparel",Decimal("1.00"),Decimal("-2.00"),Decimal("0.30")))


if __name__=="__main__": unittest.main()
