from datetime import timedelta, timezone

from sqlalchemy import case, func

from app.models import Booking


ATTENDANCE_MILESTONES = (1, 5, 10, 25, 50, 100, 250, 500)
ATTENDANCE_DECLINE_THRESHOLD = 40.0
BASELINE_WEEKS = 8
RECENT_WEEKS = 4


def format_ordinal(value):
    number = int(value)
    last_two = abs(number) % 100
    if 11 <= last_two <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(abs(number) % 10, "th")
    return f"{number}{suffix}"


def get_attendance_milestone(total_attended):
    total = max(0, int(total_attended or 0))
    last = max((value for value in ATTENDANCE_MILESTONES if value <= total), default=None)
    next_value = next((value for value in ATTENDANCE_MILESTONES if value > total), None)
    return {
        "total_attended": total,
        "last_milestone": last,
        "next_milestone": next_value,
        "visits_until_next_milestone": next_value - total if next_value else None,
        "milestone_reached": total if total in ATTENDANCE_MILESTONES else None,
    }


def calculate_attendance_decline(baseline_visits, recent_visits):
    baseline = int(baseline_visits or 0)
    recent = int(recent_visits or 0)
    baseline_rate = baseline / BASELINE_WEEKS
    recent_rate = recent / RECENT_WEEKS
    eligible = baseline >= 4 and baseline_rate >= 0.5
    change = (
        max(0.0, (baseline_rate - recent_rate) / baseline_rate * 100)
        if eligible and baseline_rate
        else None
    )
    return {
        "attendance_declining": bool(eligible and change >= ATTENDANCE_DECLINE_THRESHOLD),
        "has_enough_history": eligible,
        "baseline_visits_per_week": round(baseline_rate, 2) if eligible else None,
        "recent_visits_per_week": round(recent_rate, 2) if eligible else None,
        "attendance_change_percent": round(change, 1) if eligible else None,
    }


def get_attendance_aggregates(db, studio_id, now):
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    recent_start = now - timedelta(weeks=RECENT_WEEKS)
    baseline_start = recent_start - timedelta(weeks=BASELINE_WEEKS)
    rows = (
        db.query(
            Booking.member_id,
            func.count(Booking.id).label("total_attended"),
            func.max(Booking.booking_date).label("last_visit_at"),
            func.sum(case((Booking.booking_date >= recent_start, 1), else_=0)).label("recent_visits"),
            func.sum(case((Booking.booking_date >= baseline_start, case((Booking.booking_date < recent_start, 1), else_=0)), else_=0)).label("baseline_visits"),
        )
        .filter(Booking.studio_id == studio_id, Booking.status == "attended")
        .group_by(Booking.member_id)
        .all()
    )
    return {
        row.member_id: {
            **get_attendance_milestone(row.total_attended),
            **calculate_attendance_decline(row.baseline_visits, row.recent_visits),
            "last_visit_at": row.last_visit_at,
        }
        for row in rows
    }
