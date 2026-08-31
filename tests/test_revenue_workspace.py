import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

os.environ["APP_ENV"]="development"; os.environ["DATABASE_URL"]="sqlite:///:memory:"; os.environ["SESSION_COOKIE_SECURE"]="false"
from app.database import Base
from app.main import get_revenue_analytics, get_revenue_transactions, revenue_date_bounds, revenue_page
from app.models import ImportBatch, Member, RevenueTransaction, Studio, StudioDataSource, User


class RevenueWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine("sqlite:///:memory:"); Base.metadata.create_all(self.engine); self.db=sessionmaker(bind=self.engine)()
        now=datetime.now(timezone.utc)
        self.studio=Studio(id=1,name="One",timezone="Asia/Singapore",currency="SGD",onboarding_completed_at=now)
        self.other_studio=Studio(id=2,name="Two",timezone="UTC",onboarding_completed_at=now)
        self.user=User(id=1,studio_id=1,email="viewer@one.test",password_hash="x",role="staff")
        self.other_user=User(id=2,studio_id=2,email="viewer@two.test",password_hash="x",role="staff")
        self.db.add_all([self.studio,self.other_studio,self.user,self.other_user]);self.db.commit()
        self.source=StudioDataSource(studio_id=1,platform="hapana",display_name="Hapana",is_primary=True,is_active=True)
        self.db.add(self.source);self.db.commit()

    def tearDown(self): self.db.close();self.engine.dispose()

    def add_transaction(self,index,when,kind="revenue",member=None,studio_id=1):
        tx=RevenueTransaction(studio_id=studio_id,studio_data_source_id=self.source.id if studio_id==1 else None,member_id=member.id if member else None,identity_key=f"{studio_id}-{index}",analytics_date=when,customer_name=None if member else f"Guest {index}",customer_email=f"guest{index}@example.test",revenue_type="Membership" if index%2 else "POS",description=f"Product {index%3}",payment_method="Card" if index%2 else "Cash",source_status="Refund" if kind=="refund" else "Revenue",transaction_kind=kind,gross_revenue=Decimal("-12.00") if kind=="refund" else Decimal("12.00"),net_revenue=Decimal("-10.00") if kind=="refund" else Decimal("10.00"),discount=Decimal("1.00"))
        self.db.add(tx);return tx

    def test_page_loads_for_authenticated_studio_user(self):
        request=Request({"type":"http","method":"GET","path":"/revenue","headers":[],"session":{"user_id":1}})
        response=revenue_page(request,self.db)
        self.assertEqual(response.status_code,200);self.assertEqual(response.template.name,"revenue.html")
        self.assertTrue(Path("templates/revenue.html").exists())

    def test_no_revenue_is_unavailable_and_defaults_are_explicit(self):
        result=get_revenue_analytics(1,"this_month",None,None,self.user,self.db)
        self.assertFalse(result["available"]);self.assertEqual(result["selected_range"],"this_month");self.assertEqual(result["summary"]["transactions"],0);self.assertEqual(result["summary"]["average_net_transaction"],0);self.assertFalse(result["summary"]["gross_revenue_available"])

    def test_missing_gross_revenue_is_unavailable_not_zero(self):
        now=datetime.now(timezone.utc)
        self.db.add(RevenueTransaction(studio_id=1,studio_data_source_id=self.source.id,identity_key="net-only",analytics_date=now,transaction_kind="revenue",gross_revenue=Decimal("0"),net_revenue=Decimal("25.00")))
        self.db.commit()
        result=get_revenue_analytics(1,"last_7_days",None,None,self.user,self.db)
        self.assertEqual(result["summary"]["net_revenue"],Decimal("25.00"))
        self.assertIsNone(result["summary"]["gross_revenue"])
        self.assertFalse(result["summary"]["gross_revenue_available"])

    def test_ranges_custom_validation_and_studio_timezone(self):
        start,end=revenue_date_bounds(self.studio,"last_7_days");self.assertEqual((end-start).days,7)
        start30,end30=revenue_date_bounds(self.studio,"last_30_days");self.assertEqual((end30-start30).days,30)
        previous_start,previous_end=revenue_date_bounds(self.studio,"previous_month");self.assertEqual(previous_start.astimezone(__import__('zoneinfo').ZoneInfo("Asia/Singapore")).day,1);self.assertEqual(previous_end>previous_start,True)
        custom_start,custom_end=revenue_date_bounds(self.studio,"custom",date(2026,8,1),date(2026,8,2));self.assertEqual(custom_start.hour,16);self.assertEqual((custom_end-custom_start).days,2)
        with self.assertRaises(HTTPException): revenue_date_bounds(self.studio,"custom",date(2026,8,2),date(2026,8,1))

    def test_kpis_groups_refunds_freshness_and_tenant_isolation(self):
        now=datetime.now(timezone.utc);member=Member(studio_id=1,first_name="Linked",last_name="Member",email="linked@example.test",status="active");self.db.add(member);self.db.flush()
        self.add_transaction(1,now,member=member);self.add_transaction(2,now);self.add_transaction(3,now,"refund")
        self.add_transaction(1,now,studio_id=2)
        self.db.add(ImportBatch(studio_id=1,user_id=1,import_type="revenue",filename="revenue.csv",total_rows=3,imported_count=3,skipped_count=0,invalid_count=0,status="completed",studio_data_source_id=self.source.id));self.db.commit()
        result=get_revenue_analytics(1,"last_7_days",None,None,self.user,self.db);summary=result["summary"]
        self.assertTrue(result["available"]);self.assertEqual((summary["net_revenue"],summary["gross_revenue"],summary["transactions"]),(Decimal("10.00"),Decimal("12.00"),3))
        self.assertEqual((summary["refund_count"],summary["refund_value"],summary["discounts"]),(1,Decimal("10.00"),Decimal("3.00")));self.assertEqual(summary["average_net_transaction"],Decimal("10.00")/3)
        self.assertEqual(result["refund_summary"],{"count":1,"net_value":Decimal("10.00"),"gross_value":Decimal("12.00"),"gross_value_available":True});self.assertEqual(result["freshness"]["source"],"Hapana")
        self.assertEqual(sum(row["transactions"] for row in result["by_revenue_type"]),3);self.assertEqual(len(result["recent_refunds"]),1)

    def test_pagination_order_filters_search_and_customer_display(self):
        now=datetime.now(timezone.utc);member=Member(studio_id=1,first_name="Linked",last_name="Member",email="linked@example.test",status="active");self.db.add(member);self.db.flush()
        for index in range(26): self.add_transaction(index,now-timedelta(minutes=index),"refund" if index in (0,25) else "revenue",member if index==0 else None)
        self.add_transaction(999,now,studio_id=2);self.db.commit()
        first=get_revenue_transactions(1,"last_7_days",None,None,"all",None,1,25,self.user,self.db);second=get_revenue_transactions(1,"last_7_days",None,None,"all",None,2,25,self.user,self.db)
        self.assertEqual((len(first["items"]),len(second["items"]),first["total_items"],first["total_pages"]),(25,1,26,2));self.assertTrue(first["has_next"]);self.assertFalse(second["has_next"])
        self.assertFalse({item["id"] for item in first["items"]}&{item["id"] for item in second["items"]});self.assertGreater(first["items"][0]["date"],first["items"][-1]["date"])
        self.assertEqual(first["items"][0]["customer"],"Linked Member");self.assertEqual(first["items"][0]["member_id"],member.id);self.assertEqual(second["items"][0]["customer"],"Guest 25")
        revenue=get_revenue_transactions(1,"last_7_days",None,None,"revenue",None,1,25,self.user,self.db);refunds=get_revenue_transactions(1,"last_7_days",None,None,"refund",None,1,25,self.user,self.db)
        self.assertEqual((revenue["total_items"],refunds["total_items"]),(24,2));self.assertTrue(all(item["kind"]=="Refund" for item in refunds["items"]))
        searched=get_revenue_transactions(1,"last_7_days",None,None,"all","guest 25",1,25,self.user,self.db);self.assertEqual(searched["total_items"],1)


if __name__=="__main__": unittest.main()
