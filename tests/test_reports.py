import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from fastapi import HTTPException

os.environ["APP_ENV"]="development";os.environ["DATABASE_URL"]="sqlite:///:memory:";os.environ["SESSION_COOKIE_SECURE"]="false"
from app.database import Base
from app.main import build_report_health_summary_and_insights, get_reports_analytics, reports_date_bounds, reports_page
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
        summary=result["health_summary"]
        self.assertEqual(summary["churn_risk"]["value"],50)
        self.assertEqual(summary["churn_risk"]["supporting_text"],"2 of 4 members")
        self.assertEqual(summary["churn_risk"]["snapshot"],"Current retention snapshot")
        self.assertEqual(summary["attendance_rate"]["value"],attendance["attendance_rate"])
        self.assertEqual(summary["booking_growth"]["value"],attendance["booking_change_percentage"])
        self.assertEqual(summary["payment_failure_rate"]["value"],50)
        self.assertEqual(summary["revenue_per_active_member"]["value"],revenue["revenue_per_active_member"])
        self.assertEqual(summary["revenue_growth"]["value"],revenue["change_percentage"])

    def test_unavailable_datasets_are_distinct_from_valid_period_zeroes(self):
        empty=get_reports_analytics(1,"this_month",self.user,self.db)
        self.assertIsNone(empty["retention"]);self.assertIsNone(empty["attendance"]);self.assertIsNone(empty["member_activity"]);self.assertIsNone(empty["payments"]);self.assertIsNone(empty["revenue"])
        old=datetime.now(timezone.utc)-timedelta(days=400)
        member=Member(studio_id=1,first_name="Old",last_name="Data",email="old@example.test",status="active");self.db.add(member);self.db.flush()
        self.db.add_all([Booking(studio_id=1,member_id=member.id,class_name="Old",status="attended",booking_date=old),Payment(studio_id=1,member_id=member.id,status="paid",amount=100,payment_date=old),RevenueTransaction(studio_id=1,identity_key="old",analytics_date=old,transaction_kind="revenue",net_revenue=Decimal("1"),gross_revenue=Decimal("0"))]);self.db.commit()
        result=get_reports_analytics(1,"this_month",self.user,self.db)
        self.assertEqual(result["attendance"]["total_bookings"],0);self.assertIsNone(result["attendance"]["attendance_rate"])
        self.assertEqual(result["payments"]["total"],0);self.assertIsNone(result["payments"]["successful"]["percentage"])
        self.assertEqual(result["revenue"]["net_revenue"],0);self.assertEqual(result["revenue"]["transaction_count"],0)
        self.assertIsNone(result["health_summary"]["attendance_rate"]["value"])
        self.assertIsNone(result["health_summary"]["payment_failure_rate"]["value"])
        self.assertIsNone(result["health_summary"]["booking_growth"]["value"])
        self.assertIsNone(result["health_summary"]["revenue_growth"]["value"])
        self.assertIsNone(result["health_summary"]["revenue_per_active_member"]["value"])
        self.assertNotIn("Attendance",{item["category"] for item in result["insights"]})
        self.assertNotIn("Bookings",{item["category"] for item in result["insights"]})

    def test_missing_datasets_make_summary_unavailable_without_insights(self):
        result=get_reports_analytics(1,"this_month",self.user,self.db)
        self.assertTrue(all(not metric["available"] for metric in result["health_summary"].values()))
        self.assertEqual(result["health_summary"]["attendance_rate"]["supporting_text"],"Booking dataset unavailable")
        self.assertEqual(result["health_summary"]["payment_failure_rate"]["supporting_text"],"Payment dataset unavailable")
        self.assertEqual(result["health_summary"]["revenue_growth"]["supporting_text"],"Revenue dataset unavailable")
        self.assertEqual(result["insights"],[])

    def test_deterministic_insight_rules(self):
        self.studio.currency="PHP"
        retention={"snapshot":"Current snapshot","total_members_considered":10,"statuses":{
            "healthy":{"count":3},"watch":{"count":4},"at_risk":{"count":2},"critical":{"count":1}}}
        attendance={"attendance_rate":93.0,"booking_change_percentage":22.9,"has_records_in_period":True}
        activity={"members_with_no_attendance":2,"members_with_declining_attendance":1}
        payments={"failed":{"count":2,"percentage":4.65},"amount_needing_attention":Decimal("218"),"has_records_in_period":True}
        revenue={"change_percentage":4.4,"revenue_per_active_member":Decimal("116.72"),"has_records_in_period":True}
        summary,insights=build_report_health_summary_and_insights(self.studio,retention,attendance,activity,payments,revenue)
        self.assertEqual(summary["churn_risk"]["value"],30)
        self.assertEqual(summary["churn_risk"]["status"],"danger")
        self.assertEqual(summary["booking_growth"]["value"],22.9)
        self.assertEqual(summary["payment_failure_rate"]["value"],4.65)
        by_category={item["category"]:item for item in insights}
        self.assertIn("1 member is currently Critical",by_category["Retention"]["message"])
        self.assertIn("2 failed payments",by_category["Payments"]["message"])
        self.assertIn("PHP 218.00 currently requires attention",by_category["Payments"]["message"])
        self.assertEqual(by_category["Revenue"]["title"],"Revenue increased")
        self.assertIn("4.4%",by_category["Revenue"]["message"])
        self.assertEqual(by_category["Bookings"]["title"],"Bookings increased")
        self.assertEqual(by_category["Attendance"]["title"],"Attendance is strong")

        revenue["change_percentage"]=-8.25
        _,negative=build_report_health_summary_and_insights(self.studio,None,None,None,None,revenue)
        self.assertEqual(negative[0]["title"],"Revenue decreased")
        self.assertIn("8.2%",negative[0]["message"])

    def test_member_activity_insights_are_not_invented_when_data_is_missing(self):
        activity={"members_with_no_attendance":3,"members_with_declining_attendance":2}
        _,insights=build_report_health_summary_and_insights(self.studio,None,None,activity,None,None)
        self.assertEqual([item["title"] for item in insights],["Member activity needs attention"])
        self.assertIn("3 members have no recorded attended visits",insights[0]["message"])
        self.assertIn("2 active members have declining attendance",insights[0]["message"])

    def test_period_records_preserve_genuine_zero_metrics(self):
        now=datetime.now(timezone.utc)
        member=Member(studio_id=1,first_name="Zero",last_name="Metrics",email="zero@example.test",status="active")
        self.db.add(member);self.db.flush()
        self.db.add_all([
            Booking(studio_id=1,member_id=member.id,class_name="Cancelled",status="cancelled",booking_date=now),
            Payment(studio_id=1,member_id=member.id,status="paid",amount=100,payment_date=now),
            RevenueTransaction(studio_id=1,identity_key="zero-net",analytics_date=now,transaction_kind="revenue",net_revenue=Decimal("0"),gross_revenue=Decimal("0")),
        ]);self.db.commit()
        result=get_reports_analytics(1,"this_month",self.user,self.db)
        self.assertTrue(result["attendance"]["has_records_in_period"])
        self.assertEqual(result["health_summary"]["attendance_rate"]["value"],0)
        self.assertEqual(result["health_summary"]["payment_failure_rate"]["value"],0)
        self.assertEqual(result["health_summary"]["revenue_per_active_member"]["value"],0)
        attendance_insight=next(item for item in result["insights"] if item["category"]=="Attendance")
        self.assertEqual(attendance_insight["title"],"Attendance needs attention")

    def test_empty_current_period_does_not_claim_a_complete_hundred_percent_drop(self):
        _,_,previous_start,previous_end=reports_date_bounds(self.studio,"this_month")
        member=Member(studio_id=1,first_name="Previous",last_name="Only",email="previous@example.test",status="active")
        self.db.add(member);self.db.flush()
        self.db.add(Booking(studio_id=1,member_id=member.id,class_name="Previous",status="attended",booking_date=previous_start+timedelta(hours=1)))
        self.db.commit()
        result=get_reports_analytics(1,"this_month",self.user,self.db)
        self.assertGreater(result["attendance"]["previous_period_bookings"],0)
        self.assertIsNone(result["attendance"]["booking_change_percentage"])
        self.assertIsNone(result["health_summary"]["booking_growth"]["value"])
        self.assertFalse(any(item["category"]=="Bookings" for item in result["insights"]))

    def test_freshness_uses_latest_tenant_scoped_record_dates(self):
        current=datetime.now(timezone.utc)
        older=current-timedelta(days=3)
        other_member=Member(studio_id=2,first_name="Other",last_name="Fresh",email="fresh@other.test",status="active")
        member=Member(studio_id=1,first_name="Fresh",last_name="One",email="fresh@one.test",status="active")
        self.db.add_all([member,other_member]);self.db.flush()
        self.db.add_all([
            Booking(studio_id=1,member_id=member.id,class_name="Latest",status="attended",booking_date=older),
            Booking(studio_id=2,member_id=other_member.id,class_name="Other",status="attended",booking_date=current+timedelta(days=30)),
            Payment(studio_id=1,member_id=member.id,status="paid",amount=1,payment_date=older-timedelta(days=1)),
            RevenueTransaction(studio_id=1,identity_key="fresh",analytics_date=older-timedelta(days=2),transaction_kind="revenue",net_revenue=1,gross_revenue=1),
        ]);self.db.commit()
        result=get_reports_analytics(1,"this_month",self.user,self.db)
        freshness=result["data_freshness"]
        self.assertEqual(freshness["datasets"]["bookings"]["latest_record_date"],older.date())
        self.assertEqual(freshness["latest_data_date"],older.date())
        self.assertNotEqual(freshness["latest_data_date"],(current+timedelta(days=30)).date())

    def test_custom_range_validation_and_equal_duration_comparison(self):
        with self.assertRaises(HTTPException) as missing_start:
            reports_date_bounds(self.studio,"custom",None,date(2026,8,20))
        self.assertEqual(missing_start.exception.status_code,400)
        with self.assertRaises(HTTPException) as missing_end:
            reports_date_bounds(self.studio,"custom",date(2026,8,10),None)
        self.assertEqual(missing_end.exception.status_code,400)
        with self.assertRaises(HTTPException) as reversed_range:
            reports_date_bounds(self.studio,"custom",date(2026,8,20),date(2026,8,10))
        self.assertEqual(reversed_range.exception.status_code,400)
        start,end,previous_start,previous_end=reports_date_bounds(self.studio,"custom",date(2026,8,10),date(2026,8,20))
        self.assertEqual((end-start).days,11)
        self.assertEqual((previous_end-previous_start).days,11)
        self.assertEqual(previous_end,start)
        one_start,one_end,one_previous_start,one_previous_end=reports_date_bounds(self.studio,"custom",date(2026,8,10),date(2026,8,10))
        self.assertEqual(one_end-one_start,timedelta(days=1))
        self.assertEqual(one_previous_end-one_previous_start,timedelta(days=1))

    def test_custom_range_uses_studio_timezone_boundaries(self):
        self.studio.timezone="Asia/Singapore"
        start,end,_,_=reports_date_bounds(self.studio,"custom",date(2026,8,10),date(2026,8,10))
        self.assertEqual(start,datetime(2026,8,9,16,tzinfo=timezone.utc))
        self.assertEqual(end,datetime(2026,8,10,16,tzinfo=timezone.utc))

    def test_custom_range_calculates_all_period_datasets_and_freshness(self):
        member=Member(studio_id=1,first_name="Custom",last_name="Range",email="custom@example.test",status="active")
        other=Member(studio_id=2,first_name="Other",last_name="Range",email="range@other.test",status="active")
        self.db.add_all([member,other]);self.db.flush()
        current=datetime(2026,8,15,12,tzinfo=timezone.utc)
        previous=datetime(2026,8,4,12,tzinfo=timezone.utc)
        self.db.add_all([
            Booking(studio_id=1,member_id=member.id,class_name="Current",status="attended",booking_date=current),
            Booking(studio_id=1,member_id=member.id,class_name="Previous",status="attended",booking_date=previous),
            Booking(studio_id=2,member_id=other.id,class_name="Other",status="attended",booking_date=current),
            Payment(studio_id=1,member_id=member.id,status="paid",amount=100,payment_date=current),
            RevenueTransaction(studio_id=1,identity_key="custom-current",analytics_date=current,transaction_kind="revenue",net_revenue=Decimal("50"),gross_revenue=Decimal("50")),
            RevenueTransaction(studio_id=1,identity_key="custom-previous",analytics_date=previous,transaction_kind="revenue",net_revenue=Decimal("40"),gross_revenue=Decimal("40")),
            RevenueTransaction(studio_id=2,identity_key="custom-other",analytics_date=current,transaction_kind="revenue",net_revenue=Decimal("999"),gross_revenue=Decimal("999")),
        ]);self.db.commit()
        result=get_reports_analytics(1,"custom",self.user,self.db,start_date=date(2026,8,10),end_date=date(2026,8,20))
        self.assertEqual(result["selected_range"],"custom")
        self.assertEqual(result["attendance"]["total_bookings"],1)
        self.assertEqual(result["attendance"]["booking_change_percentage"],0)
        self.assertEqual(result["payments"]["total"],1)
        self.assertEqual(result["payments"]["failed"]["percentage"],0)
        self.assertEqual(result["revenue"]["net_revenue"],50)
        self.assertEqual(result["revenue"]["change_percentage"],25)
        self.assertEqual(result["data_freshness"]["selected_period_start"],date(2026,8,10))
        self.assertEqual(result["data_freshness"]["selected_period_end"],date(2026,8,20))
        self.assertEqual(result["data_freshness"]["datasets"]["bookings"]["latest_record_date"],date(2026,8,15))
        self.assertEqual(result["retention"]["snapshot"],"Current snapshot")

if __name__=="__main__": unittest.main()
