import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

os.environ["APP_ENV"]="development";os.environ["DATABASE_URL"]="sqlite:///:memory:";os.environ["SESSION_COOKIE_SECURE"]="false"
from app.database import Base
from app.main import get_reports_analytics, reports_page
from app.models import Booking, Member, Payment, RevenueTransaction, Studio, User


class ReportsWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine("sqlite:///:memory:");Base.metadata.create_all(self.engine);self.db=sessionmaker(bind=self.engine)()
        now=datetime.now(timezone.utc)
        self.studio=Studio(id=1,name="Reports Studio",timezone="UTC",currency="USD",onboarding_completed_at=now)
        self.other=Studio(id=2,name="Other Studio",timezone="UTC",currency="USD",onboarding_completed_at=now)
        self.user=User(id=1,studio_id=1,email="reporter@example.test",password_hash="x",role="staff")
        self.db.add_all([self.studio,self.other,self.user]);self.db.commit()

    def tearDown(self): self.db.close();self.engine.dispose()

    def test_authenticated_reports_page(self):
        request=Request({"type":"http","method":"GET","path":"/reports","headers":[],"session":{"user_id":1}})
        response=reports_page(request,self.db)
        self.assertEqual(response.status_code,200);self.assertEqual(response.template.name,"reports.html")
        self.assertTrue(Path("templates/reports.html").exists())

    def test_unauthenticated_reports_page_redirects(self):
        request=Request({"type":"http","method":"GET","path":"/reports","headers":[],"session":{}})
        response=reports_page(request,self.db)
        self.assertEqual(response.status_code,303);self.assertEqual(response.headers["location"],"/login")

    def test_percentages_revenue_authority_and_tenant_isolation(self):
        now=datetime.now(timezone.utc)
        members=[]
        for index in range(4):
            member=Member(studio_id=1,first_name=f"Member{index}",last_name="One",email=f"m{index}@one.test",status="active")
            self.db.add(member);self.db.flush();members.append(member)
        inactive=Member(studio_id=1,first_name="Inactive",last_name="One",email="inactive@one.test",status="inactive")
        other_member=Member(studio_id=2,first_name="Other",last_name="Studio",email="other@two.test",status="active")
        self.db.add_all([inactive,other_member]);self.db.flush()
        for member,days in zip(members,(1,15,25,40)):
            self.db.add(Booking(studio_id=1,member_id=member.id,class_name="Class",status="attended",booking_date=now-timedelta(days=days)))
        for status in ("attended","cancelled","no_show","booked"):
            self.db.add(Booking(studio_id=1,member_id=members[0].id,class_name="Period",status=status,booking_date=now-timedelta(hours=1)))
        self.db.add(Booking(studio_id=2,member_id=other_member.id,class_name="Other",status="attended",booking_date=now))
        self.db.add_all([Payment(studio_id=1,member_id=members[0].id,status="paid",amount=1000,payment_date=now),Payment(studio_id=1,member_id=members[1].id,status="failed",amount=500,payment_date=now),Payment(studio_id=2,member_id=other_member.id,status="failed",amount=9999,payment_date=now)])
        self.db.add_all([RevenueTransaction(studio_id=1,identity_key="one",analytics_date=now,transaction_kind="revenue",net_revenue=Decimal("100"),gross_revenue=Decimal("120"),revenue_type="Membership",payment_method="Card"),RevenueTransaction(studio_id=1,identity_key="refund",analytics_date=now,transaction_kind="refund",net_revenue=Decimal("-10"),gross_revenue=Decimal("-12"),revenue_type="Membership",payment_method="Card"),RevenueTransaction(studio_id=2,identity_key="other",analytics_date=now,transaction_kind="revenue",net_revenue=Decimal("999"),gross_revenue=Decimal("999"))]);self.db.commit()
        result=get_reports_analytics(1,"last_3_months",self.user,self.db)
        self.assertEqual(result["retention"]["total_members_considered"],4)
        self.assertEqual({key:value["count"] for key,value in result["retention"]["statuses"].items()},{"healthy":1,"watch":1,"at_risk":1,"critical":1})
        self.assertTrue(all(value["percentage"]==25 for value in result["retention"]["statuses"].values()))
        attendance=result["attendance"];self.assertEqual(attendance["total_bookings"],8);self.assertEqual(attendance["attended"]["count"],5);self.assertEqual(attendance["attendance_rate"],62.5)
        self.assertEqual(result["payments"]["successful"]["percentage"],50);self.assertEqual(result["payments"]["failed"]["percentage"],50);self.assertEqual(result["payments"]["amount_needing_attention"],5)
        revenue=result["revenue"];self.assertEqual(revenue["source"],"RevenueTransaction");self.assertEqual(revenue["net_revenue"],Decimal("90"));self.assertEqual(revenue["transaction_count"],2);self.assertEqual(revenue["refund_count"],1);self.assertIsNone(revenue["gross_revenue"])

    def test_unavailable_datasets_are_distinct_from_valid_period_zeroes(self):
        empty=get_reports_analytics(1,"this_month",self.user,self.db)
        self.assertIsNone(empty["retention"]);self.assertIsNone(empty["attendance"]);self.assertIsNone(empty["member_activity"]);self.assertIsNone(empty["payments"]);self.assertIsNone(empty["revenue"])
        old=datetime.now(timezone.utc)-timedelta(days=400)
        member=Member(studio_id=1,first_name="Old",last_name="Data",email="old@example.test",status="active");self.db.add(member);self.db.flush()
        self.db.add_all([Booking(studio_id=1,member_id=member.id,class_name="Old",status="attended",booking_date=old),Payment(studio_id=1,member_id=member.id,status="paid",amount=100,payment_date=old),RevenueTransaction(studio_id=1,identity_key="old",analytics_date=old,transaction_kind="revenue",net_revenue=Decimal("1"),gross_revenue=Decimal("0"))]);self.db.commit()
        result=get_reports_analytics(1,"this_month",self.user,self.db)
        self.assertEqual(result["attendance"]["total_bookings"],0);self.assertEqual(result["attendance"]["attendance_rate"],0)
        self.assertEqual(result["payments"]["total"],0);self.assertEqual(result["payments"]["successful"]["percentage"],0)
        self.assertEqual(result["revenue"]["net_revenue"],0);self.assertEqual(result["revenue"]["transaction_count"],0)

if __name__=="__main__": unittest.main()
