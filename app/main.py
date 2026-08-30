import csv
import io
import json
import re
import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import case, func, text

from pydantic import BaseModel, Field, model_validator
from typing import Literal
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import engine, get_db
from app.config import settings
from app.models import (
    ActionHistory,
    ActionStatus,
    Base,
    Booking,
    FollowUp,
    ImportBatch,
    ImportMappingPreset,
    ImportSourceProfile,
    Member,
    MemberMilestoneStatus,
    Payment,
    RevenueTransaction,
    Studio
    ,StudioDataSource
    ,User
)

from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, Response
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from datetime import date, datetime, timezone, timedelta
from dateutil.relativedelta import relativedelta

from app.services.email_service import EmailServiceError, send_email
from app.services.attendance import ATTENDANCE_MILESTONES, get_attendance_aggregates, get_attendance_milestone, calculate_attendance_decline, format_ordinal
from app.services.data_sources import get_dataset_availability, get_primary_data_source, serialize_data_source, set_primary_platform
from app.platforms import PLATFORMS, get_import_profile
from app.services.revenue import normalize_revenue_row, parse_revenue_date
from app.auth import (
    get_current_user,
    normalize_email,
    hash_password,
    require_action_status_permission,
    require_booking_import,
    require_current_user,
    require_email_permission,
    require_member_import,
    require_import_history,
    require_payment_import,
    require_owner,
    require_settings_write,
    require_data_source_write,
    require_studio_user,
    verify_password
)

if not settings.is_production:
    Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="MyFit Analytics API",
    version="0.1.0"
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="myfit_session",
    max_age=60 * 60 * 12,
    same_site="lax",
    https_only=settings.session_cookie_secure
)

app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)

templates = Jinja2Templates(
    directory="templates"
)


@app.middleware("http")
async def production_safety_middleware(request: Request, call_next):
    supplied_id = request.headers.get("X-Request-ID", "")
    try:
        request_id = str(uuid.UUID(supplied_id)) if supplied_id else str(uuid.uuid4())
    except ValueError:
        request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    try:
        response = await call_next(request)
    except Exception as error:
        print(f"Unhandled {type(error).__name__} request_id={request_id}")
        response = JSONResponse(
            status_code=500,
            content={"detail": "An unexpected error occurred", "request_id": request_id}
        )
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.on_event("startup")
def verify_production_database():
    if settings.is_production:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            raise RuntimeError("Production database readiness check failed") from None

class StudioCreate(BaseModel):
    name: str
    timezone: str = "Asia/Manila"
    currency: str = "USD"

class MemberCreate(BaseModel):
    studio_id: int
    first_name: str
    last_name: str
    email: str
    status: str = "active"

class PaymentCreate(BaseModel):
    studio_id: int
    member_id: int
    amount: int
    status: str = "paid"

class BookingCreate(BaseModel):
    studio_id: int
    member_id: int
    class_name: str
    status: str = "booked"


EngagementActionType = Literal["retention", "payment", "attendance_decline", "attendance_milestone"]


class ActionMessageRequest(BaseModel):
    member_id: int = Field(gt=0)
    member_name: str = Field(min_length=1, max_length=100)
    action_type: EngagementActionType
    retention_status: Literal[
        "healthy",
        "watch",
        "at_risk",
        "critical"
    ] | None = None
    days_inactive: int | None = Field(default=None, ge=0, le=36500)
    failed_amount: float = Field(default=0, ge=0, le=100000000)
    priority: Literal["urgent", "high", "payment", "watch", "normal"] | None = None
    milestone_value: int | None = Field(default=None, ge=1, le=1000000)

    @model_validator(mode="after")
    def validate_action_context(self):
        self.member_name = self.member_name.strip()

        if not self.member_name:
            raise ValueError("member_name must not be blank")

        if self.action_type == "retention" and not self.retention_status:
            raise ValueError(
                "retention_status is required for retention messages"
            )

        if self.action_type == "attendance_milestone" and self.milestone_value not in ATTENDANCE_MILESTONES:
            raise ValueError("milestone_value must be a configured attendance milestone")

        return self


class ActionHistoryCreate(BaseModel):
    member_id: int = Field(gt=0)
    action_type: EngagementActionType
    event_type: Literal[
        "fallback_message_generated",
        "message_copied"
    ]
    priority: Literal["urgent", "high", "payment", "watch", "normal"] | None = None
    message_text: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def normalize_message_text(self):
        self.message_text = self.message_text.strip()

        if not self.message_text:
            raise ValueError("message_text must not be blank")

        return self


class ActionEmailRequest(BaseModel):
    member_id: int = Field(gt=0)
    action_type: EngagementActionType
    priority: Literal["urgent", "high", "payment", "watch", "normal"] | None = None
    subject: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def normalize_email_content(self):
        self.subject = self.subject.strip()
        self.message = self.message.strip()

        if not self.subject:
            raise ValueError("subject must not be blank")

        if "\r" in self.subject or "\n" in self.subject:
            raise ValueError("subject must be a single line")

        if not self.message:
            raise ValueError("message must not be blank")

        return self


class ActionStatusUpdate(BaseModel):
    member_id: int = Field(gt=0)
    action_type: Literal["retention", "payment", "attendance_decline"]
    status: Literal["open", "contacted", "resolved", "snoozed"]
    snooze_until: datetime | None = None
    priority: Literal["urgent", "high", "payment", "watch", "normal"] | None = None

    @model_validator(mode="after")
    def validate_snooze(self):
        if self.status != "snoozed":
            self.snooze_until = None
            return self

        if self.snooze_until is None:
            raise ValueError(
                "snooze_until is required when status is snoozed"
            )

        if self.snooze_until.tzinfo is None:
            raise ValueError("snooze_until must include a timezone")

        if self.snooze_until <= datetime.now(timezone.utc):
            raise ValueError("snooze_until must be in the future")

        return self


class FollowUpCreate(BaseModel):
    member_id: int = Field(gt=0)
    action_type: Literal["retention", "payment", "attendance_decline"]
    due_at: datetime
    note: str | None = Field(default=None, max_length=1000)


    @model_validator(mode="after")
    def validate_follow_up(self):
        if self.due_at.tzinfo is None:
            raise ValueError("due_at must include a timezone")

        if self.due_at <= datetime.now(timezone.utc):
            raise ValueError("due_at must be in the future")

        if self.note is not None:
            self.note = self.note.strip() or None

        return self


class MilestoneStatusUpdate(BaseModel):
    status: Literal["celebrated", "dismissed"]


class ImportRollbackRequest(BaseModel):
    confirm: bool


class ImportPresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    import_type: Literal["members", "bookings", "payments"]
    mapping: dict[str, str | None]

    @model_validator(mode="after")
    def validate_size(self):
        if not self.mapping or len(self.mapping) > 100:
            raise ValueError("Mapping must contain between 1 and 100 entries")
        if any(not source.strip() or len(source) > 255 for source in self.mapping):
            raise ValueError("Source column names must be 1 to 255 characters")
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Preset name is required")
        return self


class ImportPresetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    mapping: dict[str, str | None] | None = None

    @model_validator(mode="after")
    def validate_update(self):
        if self.name is None and self.mapping is None:
            raise ValueError("Name or mapping is required")
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("Preset name is required")
        if self.mapping is not None:
            if not self.mapping or len(self.mapping) > 100:
                raise ValueError("Mapping must contain between 1 and 100 entries")
            if any(not source.strip() or len(source) > 255 for source in self.mapping):
                raise ValueError("Source column names must be 1 to 255 characters")
        return self


class ImportSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def normalize_source(self):
        self.name = self.name.strip()
        if not self.name: raise ValueError("Source name is required")
        if self.description is not None: self.description = self.description.strip() or None
        return self


class ImportSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def normalize_source(self):
        if self.name is None and self.description is None: raise ValueError("Name or description is required")
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name: raise ValueError("Source name is required")
        if self.description is not None: self.description = self.description.strip() or None
        return self


class ImportSourcePresetAssignment(BaseModel):
    members_preset_id: int | None = Field(default=None, gt=0)
    bookings_preset_id: int | None = Field(default=None, gt=0)
    payments_preset_id: int | None = Field(default=None, gt=0)


class StudioSettingsUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    timezone: str = Field(min_length=1, max_length=100)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    retention_healthy_days: int = Field(ge=0, le=3650)
    retention_watch_days: int = Field(ge=1, le=3650)
    retention_at_risk_days: int = Field(ge=2, le=3650)
    default_follow_up_days: int = Field(ge=1, le=365)
    sender_name: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_settings(self):
        self.name = self.name.strip()
        self.timezone = self.timezone.strip()

        if not self.name:
            raise ValueError("Studio name must not be blank")

        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("Timezone must be a valid IANA timezone")

        if not (
            self.retention_healthy_days
            < self.retention_watch_days
            < self.retention_at_risk_days
        ):
            raise ValueError(
                "Retention thresholds must increase from healthy to watch "
                "to at risk"
            )

        if self.sender_name is not None:
            self.sender_name = self.sender_name.strip()

        return self


class OnboardingComplete(StudioSettingsUpdate):
    destination: Literal["dashboard", "imports"] = "dashboard"
    platform: Literal["hapana", "bsport", "other"]


class PrimaryPlatformUpdate(BaseModel):
    platform: Literal["hapana", "bsport", "other"]


class TeamMemberCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=10, max_length=128)
    role: Literal["manager", "staff"] = "staff"

    @model_validator(mode="after")
    def normalize_team_email(self):
        self.email = normalize_email(self.email)

        if "@" not in self.email:
            raise ValueError("Email must be valid")

        return self


class TeamMemberUpdate(BaseModel):
    role: Literal["owner", "manager", "staff"]
    is_active: bool

@app.post("/bookings")
def create_booking(
    booking: BookingCreate,
    current_user: User = Depends(require_current_user),
    db: Session = Depends(get_db)
):
    if booking.studio_id != current_user.studio_id:
        raise HTTPException(status_code=403, detail="Studio access forbidden")
    get_studio_member(db, booking.studio_id, booking.member_id)
    new_booking = Booking(
        studio_id=booking.studio_id,
        member_id=booking.member_id,
        class_name=booking.class_name,
        status=booking.status
    )

    db.add(new_booking)
    db.commit()
    db.refresh(new_booking)

    return new_booking

@app.post("/payments")
def create_payment(
    payment: PaymentCreate,
    current_user: User = Depends(require_current_user),
    db: Session = Depends(get_db)
):
    if payment.studio_id != current_user.studio_id:
        raise HTTPException(status_code=403, detail="Studio access forbidden")
    get_studio_member(db, payment.studio_id, payment.member_id)
    new_payment = Payment(
        studio_id=payment.studio_id,
        member_id=payment.member_id,
        amount=payment.amount,
        status=payment.status
    )

    db.add(new_payment)
    db.commit()
    db.refresh(new_payment)

    return new_payment

@app.post("/studios")
def create_studio(
    studio: StudioCreate,
    current_user: User = Depends(require_current_user),
    db: Session = Depends(get_db)
):
    new_studio = Studio(
        name=studio.name,
        timezone=studio.timezone,
        currency=studio.currency
    )

    db.add(new_studio)
    db.commit()
    db.refresh(new_studio)

    return new_studio

@app.get("/")
def root():
    return RedirectResponse("/dashboard", status_code=303)

@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/ready")
def readiness_check():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ready"}
    except SQLAlchemyError:
        return JSONResponse(status_code=503, content={"status": "not_ready"})

@app.get("/database-test")
def database_test(current_user: User = Depends(require_current_user)):
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1"))
        value = result.scalar()

    return {
        "database": "connected",
        "test": value
    }

@app.post("/members")
def create_member(
    member: MemberCreate,
    current_user: User = Depends(require_current_user),
    db: Session = Depends(get_db)
):
    if member.studio_id != current_user.studio_id:
        raise HTTPException(status_code=403, detail="Studio access forbidden")
    new_member = Member(
        studio_id=member.studio_id,
        first_name=member.first_name,
        last_name=member.last_name,
        email=member.email,
        status=member.status
    )

    db.add(new_member)
    db.commit()
    db.refresh(new_member)

    return new_member

@app.get("/studios/{studio_id}/members")
def get_studio_members(
    studio_id: int,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    members = (
        db.query(Member)
        .filter(Member.studio_id == studio_id)
        .all()
    )

    return members

@app.get("/studios/{studio_id}/analytics/overview")
def get_analytics_overview(
    studio_id: int,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    # -------------------------
    # MEMBER ANALYTICS
    # -------------------------

    total_members = (
        db.query(Member)
        .filter(Member.studio_id == studio_id)
        .count()
    )

    active_members = (
        db.query(Member)
        .filter(
            Member.studio_id == studio_id,
            Member.status == "active"
        )
        .count()
    )

    inactive_members = (
        db.query(Member)
        .filter(
            Member.studio_id == studio_id,
            Member.status == "inactive"
        )
        .count()
    )

    # -------------------------
    # PAYMENT ANALYTICS
    # -------------------------

    paid_payments = (
        db.query(Payment)
        .filter(
            Payment.studio_id == studio_id,
            Payment.status == "paid"
        )
        .all()
    )

    failed_payments = (
        db.query(Payment)
        .filter(
            Payment.studio_id == studio_id,
            Payment.status == "failed"
        )
        .count()
    )

    successful_payments = len(paid_payments)

    total_revenue_centavos = sum(
        payment.amount for payment in paid_payments
    )

    total_revenue = total_revenue_centavos / 100

    if active_members > 0:
        average_revenue_per_active_member = (
            total_revenue / active_members
        )
    else:
        average_revenue_per_active_member = 0

    # -------------------------
    # BOOKING ANALYTICS
    # -------------------------

    total_bookings = (
        db.query(Booking)
        .filter(Booking.studio_id == studio_id)
        .count()
    )

    attended_bookings = (
        db.query(Booking)
        .filter(
            Booking.studio_id == studio_id,
            Booking.status == "attended"
        )
        .count()
    )

    cancelled_bookings = (
        db.query(Booking)
        .filter(
            Booking.studio_id == studio_id,
            Booking.status == "cancelled"
        )
        .count()
    )

    no_show_bookings = (
        db.query(Booking)
        .filter(
            Booking.studio_id == studio_id,
            Booking.status == "no_show"
        )
        .count()
    )

    if total_bookings > 0:
        attendance_rate = (
            attended_bookings / total_bookings
        ) * 100
    else:
        attendance_rate = 0

    # -------------------------
    # RETURN ANALYTICS
    # -------------------------

    return {
        "studio_id": studio_id,
        "total_members": total_members,
        "active_members": active_members,
        "inactive_members": inactive_members,
        "total_revenue": total_revenue,
        "successful_payments": successful_payments,
        "failed_payments": failed_payments,
        "average_revenue_per_active_member": round(
            average_revenue_per_active_member,
            2
        ),
        "total_bookings": total_bookings,
        "attended_bookings": attended_bookings,
        "cancelled_bookings": cancelled_bookings,
        "no_show_bookings": no_show_bookings,
        "attendance_rate": round(attendance_rate, 2)
    }

@app.get("/dashboard")
def dashboard(
    request: Request,
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)

    if not current_user:
        return RedirectResponse("/login", status_code=303)

    studio = get_studio(db, current_user.studio_id)
    if studio.onboarding_completed_at is None:
        return RedirectResponse("/onboarding", status_code=303)
    has_data = any((
        db.query(Member.id).filter(Member.studio_id == studio.id).first(),
        db.query(Booking.id).filter(Booking.studio_id == studio.id).first(),
        db.query(Payment.id).filter(Payment.studio_id == studio.id).first()
    ))

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "studio_id": current_user.studio_id,
            "user_email": current_user.email,
            "user_role": current_user.role,
            "studio_name": studio.name
            ,"has_data": has_data
        }
    )


@app.get("/onboarding")
def onboarding_page(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user: return RedirectResponse("/login", status_code=303)
    studio = get_studio(db, current_user.studio_id)
    if studio.onboarding_completed_at is not None: return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="onboarding.html", context={
        "studio": serialize_studio_settings(studio), "user_role": current_user.role,
        "user_email": current_user.email, "is_owner": current_user.role == "owner",
        "platforms": PLATFORMS
    })


@app.get("/revenue")
def revenue_page(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user: return RedirectResponse("/login", status_code=303)
    studio = get_studio(db, current_user.studio_id)
    if studio.onboarding_completed_at is None: return RedirectResponse("/onboarding", status_code=303)
    return templates.TemplateResponse(request=request, name="revenue.html", context={
        "studio_id": studio.id, "studio_name": studio.name, "currency": studio.currency,
        "user_email": current_user.email, "user_role": current_user.role,
    })


@app.post("/onboarding/complete")
def complete_onboarding(
    onboarding: OnboardingComplete,
    current_user: User = Depends(require_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role != "owner": raise HTTPException(status_code=403, detail="Onboarding must be completed by an Owner")
    studio = get_studio(db, current_user.studio_id)
    if studio.onboarding_completed_at is not None: raise HTTPException(status_code=409, detail="Studio onboarding is already complete")
    studio.name = onboarding.name; studio.timezone = onboarding.timezone; studio.currency = onboarding.currency
    studio.retention_healthy_days = onboarding.retention_healthy_days; studio.retention_watch_days = onboarding.retention_watch_days
    studio.retention_at_risk_days = onboarding.retention_at_risk_days; studio.default_follow_up_days = onboarding.default_follow_up_days
    studio.sender_name = onboarding.sender_name; studio.onboarding_completed_at = datetime.now(timezone.utc)
    set_primary_platform(db, studio.id, onboarding.platform)
    try: db.commit()
    except SQLAlchemyError:
        db.rollback(); raise HTTPException(status_code=503, detail="Studio onboarding is temporarily unavailable")
    return {"success": True, "redirect_to": "/imports" if onboarding.destination == "imports" else "/dashboard"}


@app.get("/members")
def members_page(
    request: Request,
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)

    if not current_user:
        return RedirectResponse("/login", status_code=303)

    studio = get_studio(db, current_user.studio_id)
    return templates.TemplateResponse(
        request=request,
        name="members.html",
        context={
            "studio_id": current_user.studio_id,
            "studio_name": studio.name,
            "user_email": current_user.email,
            "user_role": current_user.role
        }
    )


@app.get("/members/import-template.csv")
def download_member_import_template(
    current_user: User = Depends(require_current_user)
):
    content = (
        "first_name,last_name,email,status,last_visit_at\r\n"
        "Jane,Doe,jane@example.com,active,2026-08-01T10:00:00Z\r\n"
    )
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                'attachment; filename="myfit-members-template.csv"'
            )
        }
    )


@app.get("/bookings/import-template.csv")
def download_booking_import_template(
    current_user: User = Depends(require_current_user)
):
    content = (
        "member_email,class_name,booking_date,status\r\n"
        "jane@example.com,Reformer Pilates,2026-08-18T10:00:00Z,attended\r\n"
        "jane@example.com,Yoga,2026-08-19T09:00:00Z,cancelled\r\n"
    )
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                'attachment; filename="myfit-bookings-template.csv"'
            )
        }
    )


@app.get("/payments/import-template.csv")
def download_payment_import_template(
    current_user: User = Depends(require_current_user)
):
    content = (
        "member_email,amount,payment_date,status\r\n"
        "jane@example.com,1500.00,2026-08-18T10:00:00Z,paid\r\n"
        "jane@example.com,1500.00,2026-08-19T10:00:00Z,failed\r\n"
    )
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                'attachment; filename="myfit-payments-template.csv"'
            )
        }
    )


@app.get("/members/{member_id}")
def member_detail_page(
    member_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)

    if not current_user:
        return RedirectResponse("/login", status_code=303)

    member = get_studio_member(db, current_user.studio_id, member_id)
    studio = get_studio(db, current_user.studio_id)
    return templates.TemplateResponse(
        request=request,
        name="member_detail.html",
        context={
            "studio_id": current_user.studio_id,
            "studio_name": studio.name,
            "member_id": member.id,
            "user_email": current_user.email,
            "user_role": current_user.role
        }
    )


MAX_MEMBER_CSV_BYTES = 5 * 1024 * 1024
MAX_MEMBER_CSV_ROWS = 10_000
MAX_IMPORT_ERRORS = 100
MEMBER_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SUPPORTED_BOOKING_STATUSES = {"booked", "attended", "cancelled", "no_show"}
SUPPORTED_PAYMENT_STATUSES = {"paid", "failed"}
MAX_PAYMENT_MINOR_UNITS = 2_147_483_647


def member_import_error(errors, row_number, email, reason):
    if len(errors) < MAX_IMPORT_ERRORS:
        errors.append({
            "row": row_number,
            "email": email[:320],
            "reason": reason
        })


def booking_identity(member_id, class_name, booking_date, status):
    if booking_date.tzinfo is None:
        booking_date = booking_date.replace(tzinfo=timezone.utc)
    normalized_date = booking_date.astimezone(timezone.utc)
    return (member_id, class_name, normalized_date, status)


def payment_identity(member_id, amount, payment_date, status):
    if payment_date.tzinfo is None:
        payment_date = payment_date.replace(tzinfo=timezone.utc)
    normalized_date = payment_date.astimezone(timezone.utc)
    return (member_id, amount, normalized_date, status)


def sanitize_import_filename(filename):
    label = (filename or "import.csv").replace("\\", "/").split("/")[-1]
    label = re.sub(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", "", label, flags=re.I)
    label = re.sub(r"[\x00-\x1f\x7f]", "", label).strip()
    return (label or "import.csv")[:255]


def create_import_batch(db, user, import_type, filename, total, imported, skipped):
    primary_source = get_primary_data_source(db, user.studio_id)
    batch = ImportBatch(
        studio_id=user.studio_id,
        user_id=user.id,
        import_type=import_type,
        filename=sanitize_import_filename(filename),
        total_rows=total,
        imported_count=imported,
        skipped_count=skipped,
        invalid_count=total - imported - skipped,
        status="completed",
        studio_data_source_id=primary_source.id if primary_source else None
    )
    db.add(batch)
    db.flush()
    return batch


@app.post("/studios/{studio_id}/members/import")
async def import_members_csv(
    studio_id: int,
    file: UploadFile = File(...),
    authorized_user: User = Depends(require_member_import),
    db: Session = Depends(get_db),
    dry_run: bool = False
):
    filename = (file.filename or "").strip()

    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Invalid CSV file")

    chunks = []
    total_bytes = 0

    try:
        while True:
            chunk = await file.read(64 * 1024)

            if not chunk:
                break

            total_bytes += len(chunk)

            if total_bytes > MAX_MEMBER_CSV_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="File exceeds 5 MB limit"
                )

            chunks.append(chunk)
    finally:
        await file.close()

    try:
        decoded = b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid UTF-8 CSV file")

    try:
        reader = csv.DictReader(io.StringIO(decoded, newline=""))
        original_headers = reader.fieldnames
    except csv.Error:
        raise HTTPException(status_code=400, detail="Invalid CSV file")

    if not original_headers:
        raise HTTPException(status_code=400, detail="Invalid CSV file")

    headers = [header.strip().lower() for header in original_headers]

    if len(headers) != len(set(headers)):
        raise HTTPException(status_code=400, detail="Duplicate CSV column")

    if "studio_id" in headers:
        raise HTTPException(
            status_code=400,
            detail="CSV must not contain a studio_id column"
        )

    for required in ("first_name", "last_name", "email"):
        if required not in headers:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required column: {required}"
            )

    allowed_headers = {
        "first_name",
        "last_name",
        "email",
        "status",
        "last_visit_at"
    }
    unknown_headers = sorted(set(headers) - allowed_headers)

    if unknown_headers:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported CSV column: {unknown_headers[0]}"
        )

    reader.fieldnames = headers
    parsed_rows = []

    try:
        for row_number, row in enumerate(reader, start=2):
            if row_number - 1 > MAX_MEMBER_CSV_ROWS:
                raise HTTPException(
                    status_code=413,
                    detail="CSV exceeds 10,000 row limit"
                )

            parsed_rows.append((row_number, row))
    except csv.Error:
        raise HTTPException(status_code=400, detail="Invalid CSV file")

    existing_emails = {
        normalize_email(email)
        for (email,) in (
            db.query(Member.email)
            .filter(Member.studio_id == studio_id)
            .all()
        )
    }
    seen_emails = set(existing_emails)
    new_members = []
    errors = []
    skipped_existing = 0

    for row_number, row in parsed_rows:
        if None in row:
            member_import_error(errors, row_number, "", "Invalid CSV row")
            continue

        values = {
            key: (value or "").strip()
            for key, value in row.items()
        }
        email = normalize_email(values.get("email", ""))
        first_name = values.get("first_name", "")
        last_name = values.get("last_name", "")
        status = values.get("status", "").lower() or "active"
        last_visit_value = values.get("last_visit_at", "")

        if any(len(value) > 1000 for value in values.values()):
            member_import_error(
                errors,
                row_number,
                email,
                "Cell value exceeds maximum length"
            )
            continue

        if not first_name:
            member_import_error(errors, row_number, email, "Missing first name")
            continue

        if not last_name:
            member_import_error(errors, row_number, email, "Missing last name")
            continue

        if not email:
            member_import_error(errors, row_number, email, "Missing email")
            continue

        if len(first_name) > 150 or len(last_name) > 150:
            member_import_error(
                errors,
                row_number,
                email,
                "Name exceeds maximum length"
            )
            continue

        if len(email) > 320 or not MEMBER_EMAIL_PATTERN.fullmatch(email):
            member_import_error(errors, row_number, email, "Invalid email")
            continue

        if status not in {"active", "inactive"}:
            member_import_error(errors, row_number, email, "Invalid status")
            continue

        last_visit_at = None

        if last_visit_value:
            try:
                last_visit_at = datetime.fromisoformat(
                    last_visit_value.replace("Z", "+00:00")
                )

                if last_visit_at.tzinfo is None:
                    raise ValueError
            except ValueError:
                member_import_error(
                    errors,
                    row_number,
                    email,
                    "Invalid last_visit_at"
                )
                continue

        if email in seen_emails:
            skipped_existing += 1
            continue

        seen_emails.add(email)
        new_members.append(Member(
            studio_id=authorized_user.studio_id,
            first_name=first_name,
            last_name=last_name,
            email=email,
            status=status,
            last_visit_at=last_visit_at
        ))

    batch = create_import_batch(
        db, authorized_user, "members", filename,
        len(parsed_rows), len(new_members), skipped_existing
    )
    for member in new_members:
        member.import_batch_id = batch.id
    db.add_all(new_members)

    try:
        if dry_run:
            db.flush()
            db.rollback()
        else:
            db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Member import is temporarily unavailable"
        )

    return {
        "batch_id": batch.id if not dry_run else None,
        "total_rows": len(parsed_rows),
        "imported": len(new_members),
        "skipped_existing": skipped_existing,
        "invalid": len(parsed_rows) - len(new_members) - skipped_existing,
        "errors": errors,
        "errors_truncated": (
            len(parsed_rows) - len(new_members) - skipped_existing
            > len(errors)
        )
    }


@app.post("/studios/{studio_id}/bookings/import")
async def import_bookings_csv(
    studio_id: int,
    file: UploadFile = File(...),
    authorized_user: User = Depends(require_booking_import),
    db: Session = Depends(get_db),
    dry_run: bool = False
):
    filename = (file.filename or "").strip()

    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Invalid CSV file")

    chunks = []
    total_bytes = 0

    try:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_MEMBER_CSV_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="File exceeds 5 MB limit"
                )
            chunks.append(chunk)
    finally:
        await file.close()

    try:
        decoded = b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid UTF-8 CSV file")

    try:
        reader = csv.DictReader(io.StringIO(decoded, newline=""))
        original_headers = reader.fieldnames
    except csv.Error:
        raise HTTPException(status_code=400, detail="Invalid CSV file")

    if not original_headers:
        raise HTTPException(status_code=400, detail="Invalid CSV file")

    headers = [header.strip().lower() for header in original_headers]
    if len(headers) != len(set(headers)):
        raise HTTPException(status_code=400, detail="Duplicate CSV column")
    if "studio_id" in headers:
        raise HTTPException(
            status_code=400,
            detail="CSV must not contain a studio_id column"
        )

    required_headers = {"member_email", "class_name", "booking_date", "status"}
    for required in sorted(required_headers):
        if required not in headers:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required column: {required}"
            )
    unknown_headers = sorted(set(headers) - required_headers)
    if unknown_headers:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported CSV column: {unknown_headers[0]}"
        )

    reader.fieldnames = headers
    parsed_rows = []
    try:
        for row_number, row in enumerate(reader, start=2):
            if row_number - 1 > MAX_MEMBER_CSV_ROWS:
                raise HTTPException(
                    status_code=413,
                    detail="CSV exceeds 10,000 row limit"
                )
            parsed_rows.append((row_number, row))
    except csv.Error:
        raise HTTPException(status_code=400, detail="Invalid CSV file")

    errors = []
    validated_rows = []
    candidate_emails = set()

    for row_number, row in parsed_rows:
        if None in row:
            member_import_error(errors, row_number, "", "Invalid CSV row")
            continue
        values = {key: (value or "").strip() for key, value in row.items()}
        email = normalize_email(values.get("member_email", ""))
        class_name = values.get("class_name", "")
        booking_date_value = values.get("booking_date", "")
        status = values.get("status", "").lower()

        if any(len(value) > 1000 for value in values.values()):
            member_import_error(
                errors, row_number, email, "Cell value exceeds maximum length"
            )
            continue
        if not email:
            member_import_error(errors, row_number, email, "Missing member email")
            continue
        if len(email) > 320 or not MEMBER_EMAIL_PATTERN.fullmatch(email):
            member_import_error(errors, row_number, email, "Invalid email")
            continue
        if not class_name:
            member_import_error(errors, row_number, email, "Missing class name")
            continue
        if len(class_name) > 255:
            member_import_error(errors, row_number, email, "Class name exceeds maximum length")
            continue
        if not booking_date_value:
            member_import_error(errors, row_number, email, "Missing booking date")
            continue
        try:
            booking_date = datetime.fromisoformat(
                booking_date_value.replace("Z", "+00:00")
            )
            if booking_date.tzinfo is None:
                raise ValueError
            booking_date = booking_date.astimezone(timezone.utc)
        except ValueError:
            member_import_error(
                errors, row_number, email, "Invalid timezone-aware booking date"
            )
            continue
        if status not in SUPPORTED_BOOKING_STATUSES:
            member_import_error(errors, row_number, email, "Invalid status")
            continue

        candidate_emails.add(email)
        validated_rows.append({
            "row": row_number,
            "email": email,
            "class_name": class_name,
            "booking_date": booking_date,
            "status": status
        })

    members = (
        db.query(Member)
        .filter(
            Member.studio_id == authorized_user.studio_id,
            func.lower(func.trim(Member.email)).in_(candidate_emails)
        )
        .all()
        if candidate_emails else []
    )
    member_by_email = {
        normalize_email(member.email): member.id for member in members
    }
    matched_rows = []
    for row in validated_rows:
        member_id = member_by_email.get(row["email"])
        if member_id is None:
            member_import_error(
                errors, row["row"], row["email"], "Member not found"
            )
            continue
        row["member_id"] = member_id
        matched_rows.append(row)

    existing_identities = set()
    if matched_rows:
        member_ids = {row["member_id"] for row in matched_rows}
        earliest = min(row["booking_date"] for row in matched_rows)
        latest = max(row["booking_date"] for row in matched_rows)
        existing_bookings = (
            db.query(Booking)
            .filter(
                Booking.studio_id == authorized_user.studio_id,
                Booking.member_id.in_(member_ids),
                Booking.booking_date >= earliest,
                Booking.booking_date <= latest
            )
            .all()
        )
        existing_identities = {
            booking_identity(
                booking.member_id,
                booking.class_name,
                booking.booking_date,
                booking.status
            )
            for booking in existing_bookings
        }

    seen_identities = set(existing_identities)
    new_bookings = []
    skipped_existing = 0
    for row in matched_rows:
        identity = booking_identity(
            row["member_id"],
            row["class_name"],
            row["booking_date"],
            row["status"]
        )
        if identity in seen_identities:
            skipped_existing += 1
            continue
        seen_identities.add(identity)
        new_bookings.append(Booking(
            studio_id=authorized_user.studio_id,
            member_id=row["member_id"],
            class_name=row["class_name"],
            booking_date=row["booking_date"],
            status=row["status"]
        ))

    batch = create_import_batch(
        db, authorized_user, "bookings", filename,
        len(parsed_rows), len(new_bookings), skipped_existing
    )
    for booking in new_bookings:
        booking.import_batch_id = batch.id
    db.add_all(new_bookings)
    try:
        if dry_run:
            db.flush()
            db.rollback()
        else:
            db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Booking import is temporarily unavailable"
        )

    invalid = len(parsed_rows) - len(new_bookings) - skipped_existing
    return {
        "batch_id": batch.id if not dry_run else None,
        "total_rows": len(parsed_rows),
        "imported": len(new_bookings),
        "skipped_existing": skipped_existing,
        "invalid": invalid,
        "errors": errors,
        "errors_truncated": invalid > len(errors)
    }


@app.post("/studios/{studio_id}/payments/import")
async def import_payments_csv(
    studio_id: int,
    file: UploadFile = File(...),
    authorized_user: User = Depends(require_payment_import),
    db: Session = Depends(get_db),
    dry_run: bool = False
):
    filename = (file.filename or "").strip()
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Invalid CSV file")

    chunks = []
    total_bytes = 0
    try:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_MEMBER_CSV_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="File exceeds 5 MB limit"
                )
            chunks.append(chunk)
    finally:
        await file.close()

    try:
        decoded = b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid UTF-8 CSV file")

    try:
        reader = csv.DictReader(io.StringIO(decoded, newline=""))
        original_headers = reader.fieldnames
    except csv.Error:
        raise HTTPException(status_code=400, detail="Invalid CSV file")

    if not original_headers:
        raise HTTPException(status_code=400, detail="Invalid CSV file")

    headers = [header.strip().lower() for header in original_headers]
    if len(headers) != len(set(headers)):
        raise HTTPException(status_code=400, detail="Duplicate CSV column")
    if "studio_id" in headers:
        raise HTTPException(
            status_code=400,
            detail="CSV must not contain a studio_id column"
        )

    required_headers = {"member_email", "amount", "payment_date", "status"}
    for required in sorted(required_headers):
        if required not in headers:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required column: {required}"
            )
    unknown_headers = sorted(set(headers) - required_headers)
    if unknown_headers:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported CSV column: {unknown_headers[0]}"
        )

    reader.fieldnames = headers
    parsed_rows = []
    try:
        for row_number, row in enumerate(reader, start=2):
            if row_number - 1 > MAX_MEMBER_CSV_ROWS:
                raise HTTPException(
                    status_code=413,
                    detail="CSV exceeds 10,000 row limit"
                )
            parsed_rows.append((row_number, row))
    except csv.Error:
        raise HTTPException(status_code=400, detail="Invalid CSV file")

    errors = []
    validated_rows = []
    candidate_emails = set()
    for row_number, row in parsed_rows:
        if None in row:
            member_import_error(errors, row_number, "", "Invalid CSV row")
            continue
        values = {key: (value or "").strip() for key, value in row.items()}
        email = normalize_email(values.get("member_email", ""))
        amount_value = values.get("amount", "")
        payment_date_value = values.get("payment_date", "")
        status = values.get("status", "").lower()

        if any(len(value) > 1000 for value in values.values()):
            member_import_error(
                errors, row_number, email, "Cell value exceeds maximum length"
            )
            continue
        if not email:
            member_import_error(errors, row_number, email, "Missing member email")
            continue
        if len(email) > 320 or not MEMBER_EMAIL_PATTERN.fullmatch(email):
            member_import_error(errors, row_number, email, "Invalid email")
            continue
        if not amount_value:
            member_import_error(errors, row_number, email, "Missing payment amount")
            continue
        try:
            amount_decimal = Decimal(amount_value)
            minor_units_decimal = amount_decimal * 100
            if (
                not amount_decimal.is_finite()
                or amount_decimal <= 0
                or minor_units_decimal != minor_units_decimal.to_integral_value()
                or minor_units_decimal > MAX_PAYMENT_MINOR_UNITS
            ):
                raise InvalidOperation
            amount = int(minor_units_decimal)
        except (InvalidOperation, ValueError):
            member_import_error(
                errors, row_number, email, "Invalid payment amount"
            )
            continue
        if not payment_date_value:
            member_import_error(errors, row_number, email, "Missing payment date")
            continue
        try:
            payment_date = datetime.fromisoformat(
                payment_date_value.replace("Z", "+00:00")
            )
            if payment_date.tzinfo is None:
                raise ValueError
            payment_date = payment_date.astimezone(timezone.utc)
        except ValueError:
            member_import_error(
                errors, row_number, email, "Invalid timezone-aware payment date"
            )
            continue
        if status not in SUPPORTED_PAYMENT_STATUSES:
            member_import_error(errors, row_number, email, "Invalid status")
            continue

        candidate_emails.add(email)
        validated_rows.append({
            "row": row_number,
            "email": email,
            "amount": amount,
            "payment_date": payment_date,
            "status": status
        })

    members = (
        db.query(Member)
        .filter(
            Member.studio_id == authorized_user.studio_id,
            func.lower(func.trim(Member.email)).in_(candidate_emails)
        )
        .all()
        if candidate_emails else []
    )
    member_by_email = {
        normalize_email(member.email): member.id for member in members
    }
    matched_rows = []
    for row in validated_rows:
        member_id = member_by_email.get(row["email"])
        if member_id is None:
            member_import_error(
                errors, row["row"], row["email"], "Member not found"
            )
            continue
        row["member_id"] = member_id
        matched_rows.append(row)

    existing_identities = set()
    if matched_rows:
        member_ids = {row["member_id"] for row in matched_rows}
        earliest = min(row["payment_date"] for row in matched_rows)
        latest = max(row["payment_date"] for row in matched_rows)
        existing_payments = (
            db.query(Payment)
            .filter(
                Payment.studio_id == authorized_user.studio_id,
                Payment.member_id.in_(member_ids),
                Payment.payment_date >= earliest,
                Payment.payment_date <= latest
            )
            .all()
        )
        existing_identities = {
            payment_identity(
                payment.member_id,
                payment.amount,
                payment.payment_date,
                payment.status
            )
            for payment in existing_payments
        }

    seen_identities = set(existing_identities)
    new_payments = []
    skipped_existing = 0
    for row in matched_rows:
        identity = payment_identity(
            row["member_id"],
            row["amount"],
            row["payment_date"],
            row["status"]
        )
        if identity in seen_identities:
            skipped_existing += 1
            continue
        seen_identities.add(identity)
        new_payments.append(Payment(
            studio_id=authorized_user.studio_id,
            member_id=row["member_id"],
            amount=row["amount"],
            payment_date=row["payment_date"],
            status=row["status"]
        ))

    batch = create_import_batch(
        db, authorized_user, "payments", filename,
        len(parsed_rows), len(new_payments), skipped_existing
    )
    for payment in new_payments:
        payment.import_batch_id = batch.id
    db.add_all(new_payments)
    try:
        if dry_run:
            db.flush()
            db.rollback()
        else:
            db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Payment import is temporarily unavailable"
        )

    invalid = len(parsed_rows) - len(new_payments) - skipped_existing
    return {
        "batch_id": batch.id if not dry_run else None,
        "total_rows": len(parsed_rows),
        "imported": len(new_payments),
        "skipped_existing": skipped_existing,
        "invalid": invalid,
        "errors": errors,
        "errors_truncated": invalid > len(errors)
    }


@app.post("/studios/{studio_id}/revenue/import")
async def import_revenue_csv(
    studio_id: int, file: UploadFile = File(...),
    authorized_user: User = Depends(require_import_history),
    db: Session = Depends(get_db), dry_run: bool = False
):
    source = get_primary_data_source(db, studio_id)
    if source is None or source.platform != "hapana":
        raise HTTPException(status_code=409, detail="Hapana must be the active primary platform for this profile")
    filename, headers, rows = await read_mapping_csv(file)
    required = set(IMPORT_FIELDS["revenue"]["required"])
    missing = required - set(headers)
    if missing: raise HTTPException(status_code=400, detail=f"Missing required column: {sorted(missing)[0]}")
    errors, validated, emails = [], [], set()
    for row_number, row in enumerate(rows, start=2):
        try:
            item = normalize_revenue_row(row)
            email = normalize_email(item.get("customer_email", ""))
            item["customer_email"] = email or None
            item["row"] = row_number
            if email: emails.add(email)
            validated.append(item)
        except ValueError as error:
            member_import_error(errors, row_number, row.get("customer_email", ""), str(error))
    members = db.query(Member).filter(
        Member.studio_id == studio_id,
        func.lower(func.trim(Member.email)).in_(emails)
    ).all() if emails else []
    members_by_email = {normalize_email(member.email): member.id for member in members}
    existing = {row[0] for row in db.query(RevenueTransaction.identity_key).filter(
        RevenueTransaction.studio_id == studio_id,
        RevenueTransaction.studio_data_source_id == source.id,
        RevenueTransaction.identity_key.in_({item["identity_key"] for item in validated})
    ).all()} if validated else set()
    seen, new_rows, duplicates = set(existing), [], 0
    for item in validated:
        if item["identity_key"] in seen:
            duplicates += 1; continue
        seen.add(item["identity_key"]); new_rows.append(item)
    batch = create_import_batch(db, authorized_user, "revenue", filename, len(rows), len(new_rows), duplicates)
    transactions = []
    allowed = {column.name for column in RevenueTransaction.__table__.columns} - {"id", "studio_id", "member_id", "studio_data_source_id", "import_batch_id", "created_at", "updated_at"}
    for item in new_rows:
        values = {key: value for key, value in item.items() if key in allowed}
        transactions.append(RevenueTransaction(
            studio_id=studio_id, member_id=members_by_email.get(item.get("customer_email")),
            studio_data_source_id=source.id, import_batch_id=batch.id, **values
        ))
    db.add_all(transactions)
    try:
        if dry_run: db.flush(); db.rollback()
        else: db.commit()
    except SQLAlchemyError:
        db.rollback(); raise HTTPException(status_code=503, detail="Revenue import is temporarily unavailable")
    gross = sum((item["gross_revenue"] for item in new_rows), Decimal("0.00"))
    net = sum((item["net_revenue"] for item in new_rows), Decimal("0.00"))
    refund_rows = [item for item in new_rows if item["transaction_kind"] == "refund"]
    return {
        "batch_id": batch.id if not dry_run else None, "profile": "hapana/revenue/v1",
        "total_rows": len(rows), "imported": len(new_rows), "skipped_existing": duplicates,
        "invalid": len(rows) - len(validated), "errors": errors,
        "revenue_rows": sum(item["transaction_kind"] == "revenue" for item in new_rows),
        "refund_rows": len(refund_rows), "gross_revenue": gross, "net_revenue": net,
        "refund_value": abs(sum((item["net_revenue"] for item in refund_rows), Decimal("0.00"))),
        "discounts": sum((item.get("discount") or Decimal("0.00") for item in new_rows), Decimal("0.00")),
    }


IMPORT_FIELDS = {
    "members": {
        "required": ["first_name", "last_name", "email"],
        "optional": ["status", "last_visit_at"]
    },
    "bookings": {
        "required": ["member_email", "class_name", "booking_date", "status"],
        "optional": []
    },
    "payments": {
        "required": ["member_email", "amount", "payment_date", "status"],
        "optional": []
    },
    "revenue": {
        "required": ["net_revenue"],
        "optional": ["customer_name", "customer_email", "payment_method", "revenue_type", "transaction_category", "description", "external_transaction_id", "processed_by", "sale_referred_by", "source_status", "invoice_date", "payment_date", "admin_fee", "dishonour_fee", "transaction_fee", "tax", "discount", "gross_revenue"]
    },
}

HEADER_ALIASES = {
    "members": {
        "first_name": ["first_name", "firstname", "first name", "given_name", "given name", "member_first_name", "member first name"],
        "last_name": ["last_name", "lastname", "last name", "surname", "family_name", "family name", "member_last_name"],
        "email": ["email", "email_address", "email address", "member_email", "member email", "e-mail"],
        "status": ["status", "member_status", "member status", "active_status"],
        "last_visit_at": ["last_visit_at", "last visit", "last_visit", "last attendance", "last_attendance"]
    },
    "bookings": {
        "member_email": ["member_email", "member email", "email", "email address", "email_address", "client_email", "client email"],
        "class_name": ["class_name", "class name", "class", "activity", "activity_name", "service", "session_name"],
        "booking_date": ["booking_date", "booking date", "date", "start_date", "start date", "class_date", "attendance_date"],
        "status": ["status", "booking_status", "booking status", "attendance_status"]
    },
    "payments": {
        "member_email": ["member_email", "member email", "email", "email address", "email_address", "client_email", "client email"],
        "amount": ["amount", "payment_amount", "payment amount", "total", "value", "paid_amount"],
        "payment_date": ["payment_date", "payment date", "date", "transaction_date", "transaction date", "created_at"],
        "status": ["status", "payment_status", "payment status", "transaction_status"]
    },
    "revenue": {
        field: [
            header for header, destination in {
                **get_import_profile("hapana", "revenue", "v1")["mapping"],
                **get_import_profile("hapana", "revenue", "v1").get("aliases", {}),
            }.items() if destination == field
        ]
        for field in set(get_import_profile("hapana", "revenue", "v1")["mapping"].values())
    },
}

STATUS_ALIASES = {
    "members": {"active": "active", "enabled": "active", "current": "active", "inactive": "inactive", "disabled": "inactive", "former": "inactive"},
    "bookings": {"booked": "booked", "reserved": "booked", "attended": "attended", "completed": "attended", "cancelled": "cancelled", "canceled": "cancelled", "no_show": "no_show", "no show": "no_show", "no-show": "no_show", "absent": "no_show"},
    "payments": {"paid": "paid", "successful": "paid", "succeeded": "paid", "completed": "paid", "failed": "failed", "declined": "failed"}
}


def normalize_header_name(value):
    normalized = re.sub(r"[\s\-]+", "_", value.strip().casefold())
    return re.sub(r"[^a-z0-9_]", "", normalized)


def normalize_csv_header(value):
    return (value or "").strip().lstrip("\ufeff").strip()


def suggested_mapping(import_type, headers):
    aliases = {
        normalize_header_name(alias): field
        for field, values in HEADER_ALIASES[import_type].items()
        for alias in values
    }
    suggestions = {}
    used = set()
    for header in headers:
        field = aliases.get(normalize_header_name(header))
        suggestions[header] = field if field and field not in used else None
        if suggestions[header]:
            used.add(suggestions[header])
    return suggestions


async def read_mapping_csv(file):
    filename = (file.filename or "").strip()
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Invalid CSV file")
    chunks, total_bytes = [], 0
    try:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_MEMBER_CSV_BYTES:
                raise HTTPException(status_code=413, detail="File exceeds 5 MB limit")
            chunks.append(chunk)
    finally:
        await file.close()
    try:
        decoded = b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid UTF-8 CSV file")
    try:
        reader = csv.DictReader(io.StringIO(decoded, newline=""))
        headers = reader.fieldnames
        if not headers:
            raise HTTPException(status_code=400, detail="CSV contains no header row")
        headers = [normalize_csv_header(header) for header in headers]
        if len(headers) != len(set(headers)):
            raise HTTPException(status_code=400, detail="Duplicate CSV column")
        reader.fieldnames = headers
        rows = []
        for number, row in enumerate(reader, start=1):
            if number > MAX_MEMBER_CSV_ROWS:
                raise HTTPException(status_code=413, detail="CSV exceeds 10,000 row limit")
            if None in row:
                raise HTTPException(status_code=400, detail="Invalid CSV row")
            rows.append({key: (value or "").strip() for key, value in row.items()})
    except csv.Error:
        raise HTTPException(status_code=400, detail="Invalid CSV file")
    return filename, headers, rows


def parse_mapping(import_type, mapping_json, headers):
    try:
        mapping = json.loads(mapping_json)
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid column mapping")
    if not isinstance(mapping, dict):
        raise HTTPException(status_code=400, detail="Invalid column mapping")
    allowed = set(IMPORT_FIELDS[import_type]["required"] + IMPORT_FIELDS[import_type]["optional"])
    selected = {}
    for source, destination in mapping.items():
        if normalize_header_name(source) == "studio_id":
            raise HTTPException(status_code=400, detail="studio_id cannot be mapped")
        if source not in headers:
            raise HTTPException(status_code=400, detail="Mapping references an unknown CSV column")
        if destination in (None, ""):
            continue
        if destination == "studio_id" or destination not in allowed:
            raise HTTPException(status_code=400, detail="Invalid destination field")
        if destination in selected.values():
            raise HTTPException(status_code=400, detail=f"Two columns are mapped to {destination}")
        selected[source] = destination
    for required in IMPORT_FIELDS[import_type]["required"]:
        if required not in selected.values():
            raise HTTPException(status_code=400, detail=f"{required.replace('_', ' ').title()} must be mapped before import")
    return selected


def normalize_mapped_date(value, studio, day_first=False):
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        if not day_first: return value
        try:
            parsed = parse_revenue_date(value, ZoneInfo(studio.timezone))
        except (ValueError, ZoneInfoNotFoundError):
            return value
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(studio.timezone))
        except ZoneInfoNotFoundError:
            return value
    return parsed.astimezone(timezone.utc).isoformat()


def transform_mapped_csv(import_type, headers, rows, mapping, studio):
    destinations = IMPORT_FIELDS[import_type]["required"] + IMPORT_FIELDS[import_type]["optional"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=destinations, extrasaction="ignore")
    writer.writeheader()
    transformed = []
    date_fields = {
        "members": ("last_visit_at",), "bookings": ("booking_date",),
        "payments": ("payment_date",), "revenue": ("invoice_date", "payment_date")
    }[import_type]
    for row in rows:
        item = {destination: row.get(source, "") for source, destination in mapping.items()}
        if "status" in item:
            key = item["status"].strip().casefold().replace("_", " ")
            item["status"] = STATUS_ALIASES[import_type].get(key, item["status"].strip().casefold())
        for date_field in date_fields:
            if date_field in item: item[date_field] = normalize_mapped_date(item[date_field], studio, import_type == "revenue")
        if import_type == "payments" and "amount" in item:
            amount = item["amount"]
            if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", amount):
                item["amount"] = amount.replace(",", "")
        writer.writerow(item)
        transformed.append(item)
    return output.getvalue().encode("utf-8"), transformed


async def run_mapped_import(import_type, filename, content, user, db, dry_run):
    upload = UploadFile(file=io.BytesIO(content), filename=sanitize_import_filename(filename))
    handler = {"members": import_members_csv, "bookings": import_bookings_csv, "payments": import_payments_csv, "revenue": import_revenue_csv}[import_type]
    return await handler(studio_id=user.studio_id, file=upload, authorized_user=user, db=db, dry_run=dry_run)


def validate_import_type(import_type):
    if import_type not in IMPORT_FIELDS:
        raise HTTPException(status_code=400, detail="Invalid import type")


def validate_preset_mapping(import_type, mapping):
    return parse_mapping(import_type, json.dumps(mapping), list(mapping.keys()))


def get_studio_preset(db, studio_id, preset_id):
    preset = db.query(ImportMappingPreset).filter(
        ImportMappingPreset.id == preset_id,
        ImportMappingPreset.studio_id == studio_id
    ).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Import preset not found")
    return preset


def serialize_preset(preset):
    try:
        mapping = json.loads(preset.mapping_json)
    except json.JSONDecodeError:
        mapping = {}
    return {
        "id": preset.id,
        "name": preset.name,
        "import_type": preset.import_type,
        "mapping": mapping,
        "created_at": preset.created_at,
        "updated_at": preset.updated_at,
        "last_used_at": preset.last_used_at
    }


def get_studio_source(db, studio_id, source_id):
    source = db.query(ImportSourceProfile).filter(
        ImportSourceProfile.id == source_id,
        ImportSourceProfile.studio_id == studio_id
    ).first()
    if not source: raise HTTPException(status_code=404, detail="Import source not found")
    return source


def serialize_source(source, preset_map):
    def summary(preset_id):
        preset = preset_map.get(preset_id)
        return {"id": preset.id, "name": preset.name, "import_type": preset.import_type} if preset else None
    return {
        "id": source.id, "name": source.name, "description": source.description,
        "created_at": source.created_at, "updated_at": source.updated_at, "last_used_at": source.last_used_at,
        "members_preset": summary(source.members_preset_id),
        "bookings_preset": summary(source.bookings_preset_id),
        "payments_preset": summary(source.payments_preset_id)
    }


def source_preset_map(db, sources):
    ids = {value for source in sources for value in (source.members_preset_id, source.bookings_preset_id, source.payments_preset_id) if value}
    return {preset.id: preset for preset in db.query(ImportMappingPreset).filter(ImportMappingPreset.id.in_(ids)).all()} if ids else {}


@app.get("/studios/{studio_id}/import-sources")
def list_import_sources(studio_id: int, authorized_user: User = Depends(require_import_history), db: Session = Depends(get_db)):
    sources = db.query(ImportSourceProfile).filter(ImportSourceProfile.studio_id == studio_id).order_by(ImportSourceProfile.name.asc()).all()
    preset_map = source_preset_map(db, sources)
    return {"studio_id": studio_id, "sources": [serialize_source(source, preset_map) for source in sources]}


@app.post("/studios/{studio_id}/import-sources")
def create_import_source(studio_id: int, request_data: ImportSourceCreate, authorized_user: User = Depends(require_import_history), db: Session = Depends(get_db)):
    duplicate = db.query(ImportSourceProfile.id).filter(ImportSourceProfile.studio_id == studio_id, func.lower(func.trim(ImportSourceProfile.name)) == request_data.name.casefold()).first()
    if duplicate: raise HTTPException(status_code=409, detail="A data source with this name already exists")
    source = ImportSourceProfile(studio_id=authorized_user.studio_id, name=request_data.name, description=request_data.description, created_by_user_id=authorized_user.id)
    db.add(source)
    try: db.commit(); db.refresh(source)
    except IntegrityError:
        db.rollback(); raise HTTPException(status_code=409, detail="A data source with this name already exists")
    return serialize_source(source, {})


@app.put("/studios/{studio_id}/import-sources/{source_id}")
def update_import_source(studio_id: int, source_id: int, request_data: ImportSourceUpdate, authorized_user: User = Depends(require_import_history), db: Session = Depends(get_db)):
    source = get_studio_source(db, studio_id, source_id)
    if request_data.name is not None: source.name = request_data.name
    if "description" in request_data.model_fields_set: source.description = request_data.description
    try: db.commit(); db.refresh(source)
    except IntegrityError:
        db.rollback(); raise HTTPException(status_code=409, detail="A data source with this name already exists")
    return serialize_source(source, source_preset_map(db, [source]))


@app.put("/studios/{studio_id}/import-sources/{source_id}/presets")
def assign_import_source_presets(studio_id: int, source_id: int, request_data: ImportSourcePresetAssignment, authorized_user: User = Depends(require_import_history), db: Session = Depends(get_db)):
    source = get_studio_source(db, studio_id, source_id)
    assignments = {"members": request_data.members_preset_id, "bookings": request_data.bookings_preset_id, "payments": request_data.payments_preset_id}
    preset_ids = {value for value in assignments.values() if value}
    presets = {preset.id: preset for preset in db.query(ImportMappingPreset).filter(ImportMappingPreset.studio_id == studio_id, ImportMappingPreset.id.in_(preset_ids)).all()} if preset_ids else {}
    if len(presets) != len(preset_ids): raise HTTPException(status_code=400, detail="Preset does not belong to this studio")
    for import_type, preset_id in assignments.items():
        if preset_id and presets[preset_id].import_type != import_type: raise HTTPException(status_code=400, detail=f"Preset type does not match {import_type} slot")
        setattr(source, f"{import_type}_preset_id", preset_id)
    db.commit(); db.refresh(source)
    return serialize_source(source, presets)


@app.delete("/studios/{studio_id}/import-sources/{source_id}")
def delete_import_source(studio_id: int, source_id: int, authorized_user: User = Depends(require_import_history), db: Session = Depends(get_db)):
    source = get_studio_source(db, studio_id, source_id); db.delete(source)
    try: db.commit()
    except SQLAlchemyError:
        db.rollback(); raise HTTPException(status_code=503, detail="Import source deletion is temporarily unavailable")
    return {"success": True, "source_id": source_id}


@app.get("/studios/{studio_id}/import-presets")
def list_import_presets(
    studio_id: int,
    import_type: Literal["members", "bookings", "payments"] | None = Query(default=None),
    authorized_user: User = Depends(require_import_history),
    db: Session = Depends(get_db)
):
    query = db.query(ImportMappingPreset).filter(ImportMappingPreset.studio_id == studio_id)
    if import_type:
        query = query.filter(ImportMappingPreset.import_type == import_type)
    presets = query.order_by(ImportMappingPreset.name.asc()).all()
    return {"studio_id": studio_id, "presets": [serialize_preset(preset) for preset in presets]}


@app.post("/studios/{studio_id}/import-presets")
def create_import_preset(
    studio_id: int,
    preset_request: ImportPresetCreate,
    authorized_user: User = Depends(require_import_history),
    db: Session = Depends(get_db)
):
    mapping = validate_preset_mapping(preset_request.import_type, preset_request.mapping)
    normalized_name = preset_request.name.casefold()
    existing = db.query(ImportMappingPreset.id).filter(
        ImportMappingPreset.studio_id == studio_id,
        func.lower(func.trim(ImportMappingPreset.name)) == normalized_name
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="A preset with this name already exists")
    preset = ImportMappingPreset(
        studio_id=authorized_user.studio_id,
        name=preset_request.name,
        import_type=preset_request.import_type,
        mapping_json=json.dumps(mapping, ensure_ascii=False, separators=(",", ":")),
        created_by_user_id=authorized_user.id
    )
    db.add(preset)
    try:
        db.commit(); db.refresh(preset)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A preset with this name already exists")
    return serialize_preset(preset)


@app.put("/studios/{studio_id}/import-presets/{preset_id}")
def update_import_preset(
    studio_id: int,
    preset_id: int,
    preset_request: ImportPresetUpdate,
    authorized_user: User = Depends(require_import_history),
    db: Session = Depends(get_db)
):
    preset = get_studio_preset(db, studio_id, preset_id)
    if preset_request.name is not None:
        preset.name = preset_request.name
    if preset_request.mapping is not None:
        mapping = validate_preset_mapping(preset.import_type, preset_request.mapping)
        preset.mapping_json = json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
    try:
        db.commit(); db.refresh(preset)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A preset with this name already exists")
    return serialize_preset(preset)


@app.delete("/studios/{studio_id}/import-presets/{preset_id}")
def delete_import_preset(
    studio_id: int,
    preset_id: int,
    authorized_user: User = Depends(require_import_history),
    db: Session = Depends(get_db)
):
    preset = get_studio_preset(db, studio_id, preset_id)
    sources = db.query(ImportSourceProfile).filter(
        ImportSourceProfile.studio_id == studio_id,
        (ImportSourceProfile.members_preset_id == preset.id) |
        (ImportSourceProfile.bookings_preset_id == preset.id) |
        (ImportSourceProfile.payments_preset_id == preset.id)
    ).all()
    for source in sources:
        for field in ("members_preset_id", "bookings_preset_id", "payments_preset_id"):
            if getattr(source, field) == preset.id: setattr(source, field, None)
    db.delete(preset)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=503, detail="Import preset deletion is temporarily unavailable")
    return {"success": True, "preset_id": preset_id}


@app.post("/studios/{studio_id}/imports/preview")
async def preview_mapped_import(
    studio_id: int,
    import_type: str = Form(...),
    file: UploadFile = File(...),
    authorized_user: User = Depends(require_import_history),
    db: Session = Depends(get_db)
):
    validate_import_type(import_type)
    profile_name = None
    if import_type == "revenue":
        source = get_primary_data_source(db, studio_id)
        if source is None or source.platform != "hapana": raise HTTPException(status_code=409, detail="Hapana must be the active primary platform for Revenue V1")
        profile_name = "hapana/revenue/v1"
    filename, headers, rows = await read_mapping_csv(file)
    return {
        "filename": sanitize_import_filename(filename),
        "import_type": import_type,
        "columns": headers,
        "preview_rows": rows[:10],
        "suggested_mapping": suggested_mapping(import_type, headers),
        "profile": profile_name,
        **IMPORT_FIELDS[import_type]
    }


@app.post("/studios/{studio_id}/imports/validate")
async def validate_mapped_import(
    studio_id: int,
    import_type: str = Form(...),
    mapping: str = Form(...),
    file: UploadFile = File(...),
    authorized_user: User = Depends(require_import_history),
    db: Session = Depends(get_db)
):
    validate_import_type(import_type)
    filename, headers, rows = await read_mapping_csv(file)
    selected = parse_mapping(import_type, mapping, headers)
    studio = get_studio(db, authorized_user.studio_id)
    content, transformed = transform_mapped_csv(import_type, headers, rows, selected, studio)
    result = await run_mapped_import(import_type, filename, content, authorized_user, db, True)
    return {**result, "valid": result["imported"], "preview_of_transformed_rows": transformed[:10]}


@app.post("/studios/{studio_id}/imports/execute")
async def execute_mapped_import(
    studio_id: int,
    import_type: str = Form(...),
    mapping: str = Form(...),
    preset_id: int | None = Form(default=None),
    source_profile_id: int | None = Form(default=None),
    file: UploadFile = File(...),
    authorized_user: User = Depends(require_import_history),
    db: Session = Depends(get_db)
):
    validate_import_type(import_type)
    preset = None
    source = None
    if source_profile_id is not None:
        source = get_studio_source(db, studio_id, source_profile_id)
    if preset_id is not None:
        preset = get_studio_preset(db, studio_id, preset_id)
        if preset.import_type != import_type:
            raise HTTPException(status_code=400, detail="Preset import type does not match")
        if source is not None:
            assigned_id = getattr(source, f"{import_type}_preset_id")
            if assigned_id != preset.id:
                raise HTTPException(status_code=400, detail="Preset is not assigned to this data source")
    filename, headers, rows = await read_mapping_csv(file)
    selected = parse_mapping(import_type, mapping, headers)
    studio = get_studio(db, authorized_user.studio_id)
    content, _ = transform_mapped_csv(import_type, headers, rows, selected, studio)
    result = await run_mapped_import(import_type, filename, content, authorized_user, db, False)
    if source is not None:
        batch = get_studio_import_batch(db, studio_id, result["batch_id"])
        batch.source_profile_id = source.id
        batch.source_name_snapshot = source.name
        source.last_used_at = datetime.now(timezone.utc)
    if preset is not None:
        preset.last_used_at = datetime.now(timezone.utc)
    if source is not None or preset is not None:
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise HTTPException(status_code=503, detail="Preset usage could not be recorded")
    return result


def get_studio_import_batch(db, studio_id, batch_id, lock=False):
    query = db.query(ImportBatch).filter(
        ImportBatch.id == batch_id,
        ImportBatch.studio_id == studio_id
    )
    if lock:
        query = query.with_for_update()
    batch = query.first()
    if not batch:
        raise HTTPException(status_code=404, detail="Import batch not found")
    return batch


def batch_record_query(db, batch):
    model = {"members": Member, "bookings": Booking, "payments": Payment, "revenue": RevenueTransaction}[batch.import_type]
    return db.query(model).filter(
        model.studio_id == batch.studio_id,
        model.import_batch_id == batch.id
    )


def protected_member_ids(db, studio_id, member_ids):
    if not member_ids:
        return set()
    protected = set()
    for model in (Booking, Payment, FollowUp, ActionHistory, ActionStatus, MemberMilestoneStatus):
        protected.update(
            row[0] for row in db.query(model.member_id).filter(
                model.studio_id == studio_id,
                model.member_id.in_(member_ids)
            ).distinct().all()
        )
    return protected


def serialize_import_batch(db, batch, users=None, data_sources=None):
    if users is None:
        user_ids = {batch.user_id, batch.rolled_back_by_user_id} - {None}
        users = {
            user.id: user.email
            for user in db.query(User).filter(User.id.in_(user_ids)).all()
        } if user_ids else {}
    if data_sources is None:
        data_sources = {
            source.id: source.display_name
            for source in db.query(StudioDataSource).filter(StudioDataSource.id == batch.studio_data_source_id, StudioDataSource.studio_id == batch.studio_id).all()
        } if batch.studio_data_source_id else {}
    return {
        "id": batch.id,
        "import_type": batch.import_type,
        "filename": batch.filename,
        "total_rows": batch.total_rows,
        "imported_count": batch.imported_count,
        "skipped_count": batch.skipped_count,
        "invalid_count": batch.invalid_count,
        "status": batch.status,
        "created_at": batch.created_at,
        "rolled_back_at": batch.rolled_back_at,
        "performed_by": users.get(batch.user_id, "Unknown user"),
        "rolled_back_by": users.get(batch.rolled_back_by_user_id) if batch.rolled_back_by_user_id else None,
        "source_name": batch.source_name_snapshot
        ,"studio_data_source_id": batch.studio_data_source_id
        ,"platform_source": data_sources.get(batch.studio_data_source_id)
    }


@app.get("/imports")
def imports_page(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse("/login", status_code=303)
    if current_user.role not in {"owner", "manager"}:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    studio = get_studio(db, current_user.studio_id)
    primary_source = get_primary_data_source(db, studio.id)
    return templates.TemplateResponse(
        request=request,
        name="imports.html",
        context={
            "studio_id": studio.id,
            "studio_name": studio.name,
            "user_email": current_user.email,
            "user_role": current_user.role,
            "primary_platform": primary_source.platform if primary_source else None
        }
    )


@app.get("/studios/{studio_id}/imports")
def list_import_batches(
    studio_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    authorized_user: User = Depends(require_import_history),
    db: Session = Depends(get_db)
):
    batches = db.query(ImportBatch).filter(
        ImportBatch.studio_id == studio_id
    ).order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc()).limit(limit).all()
    user_ids = {
        user_id for batch in batches
        for user_id in (batch.user_id, batch.rolled_back_by_user_id)
        if user_id is not None
    }
    users = {
        user.id: user.email
        for user in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}
    source_ids = {batch.studio_data_source_id for batch in batches if batch.studio_data_source_id}
    data_sources = {source.id: source.display_name for source in db.query(StudioDataSource).filter(StudioDataSource.studio_id == studio_id, StudioDataSource.id.in_(source_ids)).all()} if source_ids else {}
    return {"studio_id": studio_id, "imports": [serialize_import_batch(db, batch, users, data_sources) for batch in batches]}


@app.get("/studios/{studio_id}/imports/{batch_id}")
def get_import_batch_detail(
    studio_id: int,
    batch_id: int,
    authorized_user: User = Depends(require_import_history),
    db: Session = Depends(get_db)
):
    batch = get_studio_import_batch(db, studio_id, batch_id)
    remaining = batch_record_query(db, batch).count()
    protected = 0
    if batch.import_type == "members" and remaining:
        member_ids = [row[0] for row in batch_record_query(db, batch).with_entities(Member.id).all()]
        protected = len(protected_member_ids(db, studio_id, member_ids))
    return {
        **serialize_import_batch(db, batch),
        "records_remaining": remaining,
        "protected_count": protected,
        "rollback_eligible": batch.status != "rolled_back"
    }


@app.post("/studios/{studio_id}/imports/{batch_id}/rollback")
def rollback_import_batch(
    studio_id: int,
    batch_id: int,
    rollback_request: ImportRollbackRequest,
    authorized_user: User = Depends(require_import_history),
    db: Session = Depends(get_db)
):
    if not rollback_request.confirm:
        raise HTTPException(status_code=400, detail="Rollback confirmation required")
    try:
        batch = get_studio_import_batch(db, studio_id, batch_id, lock=True)
        if batch.status == "rolled_back":
            raise HTTPException(status_code=409, detail="Import is already rolled back")

        deleted = 0
        protected_records = []
        if batch.import_type == "members":
            members = batch_record_query(db, batch).all()
            member_ids = [member.id for member in members]
            protected_ids = protected_member_ids(db, studio_id, member_ids)
            safe_ids = [member.id for member in members if member.id not in protected_ids]
            if safe_ids:
                deleted = db.query(Member).filter(
                    Member.studio_id == studio_id,
                    Member.import_batch_id == batch.id,
                    Member.id.in_(safe_ids)
                ).delete(synchronize_session=False)
            for member in members:
                if member.id in protected_ids and len(protected_records) < 100:
                    protected_records.append({
                        "member_id": member.id,
                        "name": f"{member.first_name} {member.last_name}",
                        "reason": "Member has dependent records"
                    })
            protected = len(protected_ids)
        else:
            deleted = batch_record_query(db, batch).delete(synchronize_session=False)
            protected = 0

        batch.status = "partially_rolled_back" if protected else "rolled_back"
        batch.rolled_back_at = datetime.now(timezone.utc)
        batch.rolled_back_by_user_id = authorized_user.id
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=503, detail="Import rollback is temporarily unavailable")

    return {
        "success": True,
        "batch_id": batch.id,
        "import_type": batch.import_type,
        "deleted": deleted,
        "protected": protected,
        "status": batch.status,
        "protected_records": protected_records
    }


@app.get("/login")
def login_page(
    request: Request,
    db: Session = Depends(get_db)
):
    if get_current_user(request, db):
        return RedirectResponse("/dashboard", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None}
    )


@app.post("/login")
async def login(
    request: Request,
    db: Session = Depends(get_db)
):
    form = parse_qs((await request.body()).decode("utf-8"))
    email = normalize_email(form.get("email", [""])[0])
    password = form.get("password", [""])[0]
    user = db.query(User).filter(User.email == email).first()

    if (
        not user
        or not user.is_active
        or not verify_password(password, user.password_hash)
    ):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid email or password."},
            status_code=400
        )

    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def get_studio(db, studio_id):
    studio = db.query(Studio).filter(Studio.id == studio_id).first()

    if not studio:
        raise HTTPException(status_code=404, detail="Studio not found")

    return studio


def serialize_studio_settings(studio):
    return {
        "studio_id": studio.id,
        "name": studio.name,
        "timezone": studio.timezone,
        "currency": studio.currency,
        "retention_healthy_days": studio.retention_healthy_days,
        "retention_watch_days": studio.retention_watch_days,
        "retention_at_risk_days": studio.retention_at_risk_days,
        "default_follow_up_days": studio.default_follow_up_days,
        "sender_name": (
            studio.name
            if studio.sender_name is None
            else studio.sender_name
        )
    }


def build_data_source_status(db, studio_id):
    sources = db.query(StudioDataSource).filter(
        StudioDataSource.studio_id == studio_id,
        StudioDataSource.source_type == "management_platform"
    ).order_by(StudioDataSource.created_at.desc(), StudioDataSource.id.desc()).all()
    last_import_rows = db.query(
        ImportBatch.studio_data_source_id,
        func.max(ImportBatch.created_at).label("last_import_at")
    ).filter(
        ImportBatch.studio_id == studio_id,
        ImportBatch.studio_data_source_id.is_not(None)
    ).group_by(ImportBatch.studio_data_source_id).all()
    last_import_by_source = {row.studio_data_source_id: row.last_import_at for row in last_import_rows}
    serialized = []
    for source in sources:
        item = serialize_data_source(source)
        item["last_import_at"] = last_import_by_source.get(source.id)
        serialized.append(item)
    primary = next((item for item in serialized if item["is_primary"] and item["is_active"]), None)
    return {
        "studio_id": studio_id,
        "primary_source": primary,
        "sources": serialized,
        "availability": get_dataset_availability(db, studio_id),
        "platforms": [
            {"key": key, **definition}
            for key, definition in PLATFORMS.items()
        ],
    }


@app.get("/studios/{studio_id}/data-sources")
def get_studio_data_sources(
    studio_id: int,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    get_studio(db, studio_id)
    return build_data_source_status(db, studio_id)


@app.put("/studios/{studio_id}/data-sources/primary")
def update_primary_data_source(
    studio_id: int,
    selection: PrimaryPlatformUpdate,
    authorized_user: User = Depends(require_data_source_write),
    db: Session = Depends(get_db)
):
    get_studio(db, studio_id)
    try:
        source = set_primary_platform(db, studio_id, selection.platform)
        db.commit()
        db.refresh(source)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Primary platform could not be changed")
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=503, detail="Data sources are temporarily unavailable")
    return build_data_source_status(db, studio_id)


@app.get("/studios/{studio_id}/settings")
def get_studio_settings(
    studio_id: int,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    return serialize_studio_settings(get_studio(db, studio_id))


@app.put("/studios/{studio_id}/settings")
def update_studio_settings(
    studio_id: int,
    settings: StudioSettingsUpdate,
    authorized_user: User = Depends(require_settings_write),
    db: Session = Depends(get_db)
):
    studio = get_studio(db, studio_id)

    for field, value in settings.model_dump().items():
        setattr(studio, field, value)

    try:
        db.commit()
        db.refresh(studio)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Studio settings are temporarily unavailable"
        )

    return serialize_studio_settings(studio)


def serialize_team_member(user):
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at
    }


@app.get("/studios/{studio_id}/team")
def get_team(
    studio_id: int,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db)
):
    users = (
        db.query(User)
        .filter(User.studio_id == studio_id)
        .order_by(User.created_at.asc(), User.id.asc())
        .all()
    )

    return {
        "studio_id": studio_id,
        "team_count": len(users),
        "team": [serialize_team_member(user) for user in users]
    }


@app.post("/studios/{studio_id}/team")
def create_team_member(
    studio_id: int,
    team_request: TeamMemberCreate,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db)
):
    if db.query(User).filter(User.email == team_request.email).first():
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        studio_id=studio_id,
        email=team_request.email,
        password_hash=hash_password(team_request.password),
        role=team_request.role,
        is_active=True
    )
    db.add(user)

    try:
        db.commit()
        db.refresh(user)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Team member could not be created"
        )

    return serialize_team_member(user)


@app.put("/studios/{studio_id}/team/{user_id}")
def update_team_member(
    studio_id: int,
    user_id: int,
    team_request: TeamMemberUpdate,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db)
):
    target = (
        db.query(User)
        .filter(User.id == user_id, User.studio_id == studio_id)
        .first()
    )

    if not target:
        raise HTTPException(status_code=404, detail="Team member not found")

    removes_active_owner = (
        target.role == "owner"
        and target.is_active
        and (
            team_request.role != "owner"
            or not team_request.is_active
        )
    )

    if removes_active_owner:
        active_owner_count = (
            db.query(User)
            .filter(
                User.studio_id == studio_id,
                User.role == "owner",
                User.is_active.is_(True)
            )
            .count()
        )

        if active_owner_count <= 1:
            raise HTTPException(
                status_code=400,
                detail="Studio must have at least one active owner"
            )

    target.role = team_request.role
    target.is_active = team_request.is_active

    try:
        db.commit()
        db.refresh(target)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Team member could not be updated"
        )

    return serialize_team_member(target)


def get_studio_member(db, studio_id, member_id):
    member = (
        db.query(Member)
        .filter(
            Member.id == member_id,
            Member.studio_id == studio_id
        )
        .first()
    )

    if not member:
        raise HTTPException(
            status_code=404,
            detail="Member not found for this studio"
        )

    return member


def log_action_history(
    db,
    studio_id,
    member_id,
    action_type,
    event_type,
    priority=None,
    message_text=None
):
    history_entry = ActionHistory(
        studio_id=studio_id,
        member_id=member_id,
        action_type=action_type,
        event_type=event_type,
        priority=priority,
        message_text=message_text
    )

    db.add(history_entry)

    try:
        db.commit()
        db.refresh(history_entry)
    except SQLAlchemyError:
        db.rollback()
        raise

    return history_entry


def get_effective_action_status(status_record, now=None):
    if status_record is None:
        return "open", None

    if status_record.status != "snoozed":
        return status_record.status, None

    current_time = now or datetime.now(timezone.utc)
    snooze_until = status_record.snooze_until

    if snooze_until is None:
        return "open", None

    if snooze_until.tzinfo is None:
        snooze_until = snooze_until.replace(tzinfo=timezone.utc)

    if snooze_until <= current_time:
        return "open", None

    return "snoozed", snooze_until


def get_action_status_map(db, studio_id):
    records = (
        db.query(ActionStatus)
        .filter(ActionStatus.studio_id == studio_id)
        .all()
    )

    return {
        (record.member_id, record.action_type): record
        for record in records
    }


def upsert_action_status(
    db,
    studio_id,
    member_id,
    action_type,
    status,
    snooze_until=None,
    priority=None
):
    status_record = (
        db.query(ActionStatus)
        .filter(
            ActionStatus.studio_id == studio_id,
            ActionStatus.member_id == member_id,
            ActionStatus.action_type == action_type
        )
        .first()
    )

    if status_record is None:
        status_record = ActionStatus(
            studio_id=studio_id,
            member_id=member_id,
            action_type=action_type
        )
        db.add(status_record)

    changed = (
        status_record.status != status
        or status_record.snooze_until != snooze_until
    )

    if not changed:
        return status_record, False

    status_record.status = status
    status_record.snooze_until = (
        snooze_until
        if status == "snoozed"
        else None
    )

    event_type = {
        "open": "action_reopened",
        "contacted": "action_contacted",
        "resolved": "action_resolved",
        "snoozed": "action_snoozed"
    }[status]

    history_entry = ActionHistory(
        studio_id=studio_id,
        member_id=member_id,
        action_type=action_type,
        event_type=event_type,
        priority=priority,
        message_text=None
    )
    db.add(history_entry)

    try:
        db.commit()
        db.refresh(status_record)
    except SQLAlchemyError:
        db.rollback()
        raise

    return status_record, True


def build_action_message_prompt(message_request, studio_name, currency):
    first_name = message_request.member_name.split()[0]

    if message_request.action_type == "retention":
        inactivity_context = (
            f"approximately {message_request.days_inactive} days"
            if message_request.days_inactive is not None
            else "an unknown amount of time"
        )

        return (
            f"Write a concise retention message for {studio_name}, a "
            "boutique fitness "
            f"studio member named {first_name}. Their retention status is "
            f"{message_request.retention_status}, and it has been "
            f"{inactivity_context} since their last recorded visit. "
            "Address them by first name. Sound warm, professional, and not "
            "overly salesy. Gently acknowledge that it has been a while "
            "without sounding creepy or overly specific, invite them back, "
            "and offer help. Use 2 to 4 short sentences. Do not invent "
            "discounts, promotions, membership details, or personal staff "
            "observations. Return only the message text."
        )

    if message_request.action_type == "attendance_milestone":
        milestone_label = "first" if message_request.milestone_value == 1 else format_ordinal(message_request.milestone_value)
        return (
            f"Generate a short, warm congratulations message for {studio_name} member {first_name}, "
            f"who reached their {milestone_label} attended class. Address them by first name; "
            "be concise, celebratory, and not over-the-top. Do not invent rewards, discounts, promotions, or "
            "membership details. Use 2 to 4 short sentences and return only the message text."
        )

    if message_request.action_type == "attendance_decline":
        return (
            f"Generate a warm check-in message for {studio_name} member {first_name}, whose attendance frequency "
            "has declined. Do not say attendance is being monitored and do not mention percentages or use creepy "
            "surveillance language. Invite them back, offer help, stay concise, and do not invent promotions. "
            "Use 2 to 4 short sentences and return only the message text."
        )

    amount_context = (
        f" The failed amount is {currency} "
        f"{message_request.failed_amount:,.2f}."
        if message_request.failed_amount > 0
        else ""
    )

    return (
        f"Write a concise payment recovery message for {studio_name}, a "
        "boutique fitness "
        f"studio member named {first_name}.{amount_context} Address them by "
        "first name. Politely explain that there appears to be an issue "
        "with the recent payment, ask them to check or update their payment "
        "details, and offer help. Keep the tone friendly and professional, "
        "not threatening or aggressive. Do not invent a failure reason, "
        "discount, promotion, or membership detail. Use 2 to 4 short "
        "sentences and return only the message text."
    )


@app.post("/studios/{studio_id}/action-message")
def generate_action_message(
    studio_id: int,
    message_request: ActionMessageRequest,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    member = get_studio_member(
        db,
        studio_id,
        message_request.member_id
    )
    studio = get_studio(db, studio_id)
    trusted_request = message_request.model_copy(
        update={
            "member_name": f"{member.first_name} {member.last_name}"
        }
    )
    if trusted_request.action_type in {"attendance_milestone", "attendance_decline"}:
        attendance = get_attendance_aggregates(db, studio_id, datetime.now(timezone.utc)).get(member.id, {})
        if trusted_request.action_type == "attendance_milestone" and attendance.get("total_attended", 0) < trusted_request.milestone_value:
            raise HTTPException(status_code=409, detail="Attendance milestone has not been reached")
        if trusted_request.action_type == "attendance_decline" and not attendance.get("attendance_declining"):
            raise HTTPException(status_code=409, detail="Attendance decline is not currently active")
    api_key = settings.openai_api_key

    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="AI message generation is not configured"
        )

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            timeout=12.0,
            max_retries=1
        )
        response = client.responses.create(
            model="gpt-5-mini",
            instructions=(
                "You draft friendly, professional member messages for a "
                "boutique fitness studio. Follow the supplied constraints "
                "exactly and output only the finished message."
            ),
            input=build_action_message_prompt(
                trusted_request,
                studio.name,
                studio.currency
            ),
            reasoning={"effort": "minimal"},
            max_output_tokens=300
        )
        message = response.output_text.strip()

        if not message:
            raise ValueError("OpenAI returned an empty message")

        log_action_history(
            db=db,
            studio_id=studio_id,
            member_id=member.id,
            action_type=trusted_request.action_type,
            event_type="ai_message_generated",
            priority=trusted_request.priority,
            message_text=message
        )

        return {"message": message}
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="AI message generation is unavailable"
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="AI message generation is temporarily unavailable"
        )


@app.post("/studios/{studio_id}/action-history")
def create_action_history(
    studio_id: int,
    history_request: ActionHistoryCreate,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    get_studio_member(
        db,
        studio_id,
        history_request.member_id
    )

    try:
        history_entry = log_action_history(
            db=db,
            studio_id=studio_id,
            member_id=history_request.member_id,
            action_type=history_request.action_type,
            event_type=history_request.event_type,
            priority=history_request.priority,
            message_text=history_request.message_text
        )
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail="Action history is temporarily unavailable"
        )

    return {
        "id": history_entry.id,
        "event_type": history_entry.event_type,
        "created_at": history_entry.created_at
    }


@app.post("/studios/{studio_id}/send-action-email")
def send_action_email(
    studio_id: int,
    email_request: ActionEmailRequest,
    authorized_user: User = Depends(require_email_permission),
    db: Session = Depends(get_db)
):
    member = get_studio_member(
        db,
        studio_id,
        email_request.member_id
    )
    studio = get_studio(db, studio_id)

    try:
        send_email(
            to_email=member.email,
            subject=email_request.subject,
            body=email_request.message,
            sender_name=(
                studio.name
                if studio.sender_name is None
                else studio.sender_name
            )
        )
    except EmailServiceError as error:
        print(f"Email send failed: {type(error).__name__}: {error}")
        try:
            log_action_history(
                db=db,
                studio_id=studio_id,
                member_id=member.id,
                action_type=email_request.action_type,
                event_type="email_failed",
                priority=email_request.priority,
                message_text=email_request.message
            )
        except SQLAlchemyError:
            pass

        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": "Email could not be sent"
            }
        )

    try:
        log_action_history(
            db=db,
            studio_id=studio_id,
            member_id=member.id,
            action_type=email_request.action_type,
            event_type="email_sent",
            priority=email_request.priority,
            message_text=email_request.message
        )
    except SQLAlchemyError:
        # The email has already been sent. Return success so the manager
        # does not retry and accidentally send a duplicate.
        pass

    try:
        upsert_action_status(
            db=db,
            studio_id=studio_id,
            member_id=member.id,
            action_type=email_request.action_type,
            status="contacted",
            priority=email_request.priority
        )
    except SQLAlchemyError:
        # The email has already been sent. Do not encourage a duplicate send.
        pass

    return {
        "success": True,
        "message": "Email sent successfully"
    }


@app.post("/studios/{studio_id}/action-status")
def update_action_status(
    studio_id: int,
    status_request: ActionStatusUpdate,
    authorized_user: User = Depends(require_action_status_permission),
    db: Session = Depends(get_db)
):
    get_studio_member(
        db,
        studio_id,
        status_request.member_id
    )

    try:
        status_record, changed = upsert_action_status(
            db=db,
            studio_id=studio_id,
            member_id=status_request.member_id,
            action_type=status_request.action_type,
            status=status_request.status,
            snooze_until=status_request.snooze_until,
            priority=status_request.priority
        )
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail="Action status is temporarily unavailable"
        )

    effective_status, effective_snooze_until = (
        get_effective_action_status(status_record)
    )

    return {
        "member_id": status_record.member_id,
        "action_type": status_record.action_type,
        "status": effective_status,
        "snooze_until": effective_snooze_until,
        "changed": changed
    }


@app.get("/studios/{studio_id}/action-status")
def get_action_statuses(
    studio_id: int,
    member_id: int | None = Query(default=None, gt=0),
    action_type: Literal["retention", "payment"] | None = Query(
        default=None
    ),
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    if member_id is not None:
        members = [get_studio_member(db, studio_id, member_id)]
    else:
        members = (
            db.query(Member)
            .filter(Member.studio_id == studio_id)
            .all()
        )

    action_types = (
        [action_type]
        if action_type
        else ["retention", "payment"]
    )
    status_map = get_action_status_map(db, studio_id)
    statuses = []

    for member in members:
        for current_action_type in action_types:
            status_record = status_map.get(
                (member.id, current_action_type)
            )
            effective_status, snooze_until = (
                get_effective_action_status(status_record)
            )

            statuses.append({
                "member_id": member.id,
                "member_name": f"{member.first_name} {member.last_name}",
                "action_type": current_action_type,
                "status": effective_status,
                "snooze_until": snooze_until,
                "updated_at": (
                    status_record.updated_at
                    if status_record
                    else None
                )
            })

    return {
        "studio_id": studio_id,
        "status_count": len(statuses),
        "statuses": statuses
    }


def serialize_follow_up(follow_up, member, now=None):
    current_time = now or datetime.now(timezone.utc)
    due_at = follow_up.due_at

    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)

    return {
        "id": follow_up.id,
        "studio_id": follow_up.studio_id,
        "member_id": member.id,
        "member_name": f"{member.first_name} {member.last_name}",
        "email": member.email,
        "action_type": follow_up.action_type,
        "status": follow_up.status,
        "due_at": due_at,
        "is_due": (
            follow_up.status == "pending"
            and due_at <= current_time
        ),
        "note": follow_up.note,
        "created_at": follow_up.created_at,
        "updated_at": follow_up.updated_at,
        "completed_at": follow_up.completed_at
    }


def get_studio_follow_up(db, studio_id, follow_up_id):
    follow_up = (
        db.query(FollowUp)
        .filter(
            FollowUp.id == follow_up_id,
            FollowUp.studio_id == studio_id
        )
        .first()
    )

    if not follow_up:
        raise HTTPException(status_code=404, detail="Follow-up not found")

    return follow_up


@app.post("/studios/{studio_id}/follow-ups")
def create_follow_up(
    studio_id: int,
    follow_up_request: FollowUpCreate,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    member = get_studio_member(
        db,
        studio_id,
        follow_up_request.member_id
    )
    due_at = follow_up_request.due_at.astimezone(timezone.utc)
    existing = (
        db.query(FollowUp)
        .filter(
            FollowUp.studio_id == studio_id,
            FollowUp.member_id == member.id,
            FollowUp.action_type == follow_up_request.action_type,
            FollowUp.status == "pending",
            FollowUp.due_at == due_at
        )
        .first()
    )

    if existing:
        return serialize_follow_up(existing, member)

    follow_up = FollowUp(
        studio_id=studio_id,
        member_id=member.id,
        action_type=follow_up_request.action_type,
        status="pending",
        due_at=due_at,
        note=follow_up_request.note
    )
    history_entry = ActionHistory(
        studio_id=studio_id,
        member_id=member.id,
        action_type=follow_up_request.action_type,
        event_type="follow_up_scheduled",
        message_text=(
            f"Follow-up scheduled for {due_at.isoformat()}"
            + (
                f": {follow_up_request.note}"
                if follow_up_request.note
                else ""
            )
        )
    )
    db.add_all([follow_up, history_entry])

    try:
        db.commit()
        db.refresh(follow_up)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Follow-up is temporarily unavailable"
        )

    return serialize_follow_up(follow_up, member)


@app.get("/studios/{studio_id}/follow-ups")
def get_follow_ups(
    studio_id: int,
    status: Literal["pending", "completed", "cancelled"] | None = Query(
        default=None
    ),
    due_only: bool = Query(default=False),
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    now = datetime.now(timezone.utc)
    query = (
        db.query(FollowUp, Member)
        .join(Member, Member.id == FollowUp.member_id)
        .filter(
            FollowUp.studio_id == studio_id,
            Member.studio_id == studio_id
        )
    )

    if due_only:
        query = query.filter(
            FollowUp.status == "pending",
            FollowUp.due_at <= now
        )
    elif status:
        query = query.filter(FollowUp.status == status)

    rows = query.order_by(FollowUp.due_at.asc(), FollowUp.id.asc()).all()

    return {
        "studio_id": studio_id,
        "follow_up_count": len(rows),
        "follow_ups": [
            serialize_follow_up(follow_up, member, now)
            for follow_up, member in rows
        ]
    }


def transition_follow_up(db, studio_id, follow_up_id, target_status):
    follow_up = get_studio_follow_up(
        db,
        studio_id,
        follow_up_id
    )

    if follow_up.status == target_status:
        member = get_studio_member(
            db,
            studio_id,
            follow_up.member_id
        )
        return serialize_follow_up(follow_up, member)

    if follow_up.status != "pending":
        raise HTTPException(
            status_code=409,
            detail="Follow-up is no longer pending"
        )

    now = datetime.now(timezone.utc)
    follow_up.status = target_status
    follow_up.completed_at = (
        now if target_status == "completed" else None
    )
    history_entry = ActionHistory(
        studio_id=studio_id,
        member_id=follow_up.member_id,
        action_type=follow_up.action_type,
        event_type=f"follow_up_{target_status}",
        message_text=f"Follow-up {target_status}"
    )
    db.add(history_entry)

    try:
        db.commit()
        db.refresh(follow_up)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Follow-up is temporarily unavailable"
        )

    member = get_studio_member(
        db,
        studio_id,
        follow_up.member_id
    )
    return serialize_follow_up(follow_up, member)


@app.post("/studios/{studio_id}/follow-ups/{follow_up_id}/complete")
def complete_follow_up(
    studio_id: int,
    follow_up_id: int,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    return transition_follow_up(
        db,
        studio_id,
        follow_up_id,
        "completed"
    )


@app.post("/studios/{studio_id}/follow-ups/{follow_up_id}/cancel")
def cancel_follow_up(
    studio_id: int,
    follow_up_id: int,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    return transition_follow_up(
        db,
        studio_id,
        follow_up_id,
        "cancelled"
    )


@app.get("/studios/{studio_id}/action-history")
def get_action_history(
    studio_id: int,
    limit: int = Query(default=25, ge=1, le=100),
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    try:
        rows = (
            db.query(ActionHistory, Member)
            .join(Member, Member.id == ActionHistory.member_id)
            .filter(
                ActionHistory.studio_id == studio_id,
                Member.studio_id == studio_id
            )
            .order_by(
                ActionHistory.created_at.desc(),
                ActionHistory.id.desc()
            )
            .limit(limit)
            .all()
        )
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail="Action history is temporarily unavailable"
        )

    history = [
        {
            "id": entry.id,
            "member_id": entry.member_id,
            "member_name": f"{member.first_name} {member.last_name}",
            "action_type": entry.action_type,
            "event_type": entry.event_type,
            "priority": entry.priority,
            "message_text": entry.message_text,
            "milestone_value": (
                int(match.group(1))
                if entry.event_type in {"attendance_milestone_celebrated", "attendance_milestone_dismissed"}
                and entry.message_text
                and (match := re.search(r"Attendance milestone (\d+)", entry.message_text))
                else None
            ),
            "created_at": entry.created_at
        }
        for entry, member in rows
    ]

    return {
        "studio_id": studio_id,
        "history_count": len(history),
        "history": history
    }

@app.get("/studios/{studio_id}/analytics/monthly")
def get_monthly_analytics(
    studio_id: int,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    now = datetime.now(timezone.utc)

    current_month_start = datetime(
        now.year,
        now.month,
        1,
        tzinfo=timezone.utc
    )

    next_month_start = (
        current_month_start
        + relativedelta(months=1)
    )

    last_month_start = (
        current_month_start
        - relativedelta(months=1)
    )

    # CURRENT MONTH PAYMENTS
    current_payments = (
        db.query(Payment)
        .filter(
            Payment.studio_id == studio_id,
            Payment.status == "paid",
            Payment.payment_date >= current_month_start,
            Payment.payment_date < next_month_start
        )
        .all()
    )

    current_revenue_centavos = sum(
        payment.amount for payment in current_payments
    )

    current_revenue = current_revenue_centavos / 100

    # LAST MONTH PAYMENTS
    last_month_payments = (
        db.query(Payment)
        .filter(
            Payment.studio_id == studio_id,
            Payment.status == "paid",
            Payment.payment_date >= last_month_start,
            Payment.payment_date < current_month_start
        )
        .all()
    )

    last_month_revenue_centavos = sum(
        payment.amount for payment in last_month_payments
    )

    last_month_revenue = (
        last_month_revenue_centavos / 100
    )

    # REVENUE CHANGE
    if last_month_revenue > 0:
        revenue_change_percent = (
            (
                current_revenue
                - last_month_revenue
            )
            / last_month_revenue
        ) * 100
    else:
        revenue_change_percent = None

    # CURRENT MONTH BOOKINGS
    current_bookings = (
        db.query(Booking)
        .filter(
            Booking.studio_id == studio_id,
            Booking.booking_date >= current_month_start,
            Booking.booking_date < next_month_start
        )
        .all()
    )

    total_bookings = len(current_bookings)

    attended = sum(
        1 for booking in current_bookings
        if booking.status == "attended"
    )

    cancelled = sum(
        1 for booking in current_bookings
        if booking.status == "cancelled"
    )

    no_shows = sum(
        1 for booking in current_bookings
        if booking.status == "no_show"
    )

    if total_bookings > 0:
        attendance_rate = (
            attended / total_bookings
        ) * 100
    else:
        attendance_rate = 0

    return {
        "studio_id": studio_id,
        "year": now.year,
        "month": now.month,

        "current_month": {
            "revenue": current_revenue,
            "successful_payments": len(
                current_payments
            ),
            "total_bookings": total_bookings,
            "attended": attended,
            "cancelled": cancelled,
            "no_shows": no_shows,
            "attendance_rate": round(
                attendance_rate,
                2
            )
        },

        "last_month": {
            "revenue": last_month_revenue,
            "successful_payments": len(
                last_month_payments
            )
        },

        "revenue_change_percent": (
            round(revenue_change_percent, 2)
            if revenue_change_percent is not None
            else None
        )
    }

@app.get("/studios/{studio_id}/analytics/revenue-trend")
def get_revenue_trend(
    studio_id: int,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    now = datetime.now(timezone.utc)

    results = []

    for months_ago in range(5, -1, -1):

        month_start = (
            datetime(
                now.year,
                now.month,
                1,
                tzinfo=timezone.utc
            )
            - relativedelta(months=months_ago)
        )

        next_month_start = (
            month_start
            + relativedelta(months=1)
        )

        payments = (
            db.query(Payment)
            .filter(
                Payment.studio_id == studio_id,
                Payment.status == "paid",
                Payment.payment_date >= month_start,
                Payment.payment_date < next_month_start
            )
            .all()
        )

        revenue_centavos = sum(
            payment.amount for payment in payments
        )

        revenue = revenue_centavos / 100

        results.append({
            "month": month_start.strftime("%b %Y"),
            "revenue": revenue
        })

    return results


def revenue_date_bounds(studio, range_name, start_date=None, end_date=None):
    zone = ZoneInfo(studio.timezone)
    local_now = datetime.now(timezone.utc).astimezone(zone)
    today = local_now.date()
    if range_name == "last_7_days": start, end = today - timedelta(days=6), today
    elif range_name == "this_month": start, end = today.replace(day=1), today
    elif range_name == "previous_month":
        end = today.replace(day=1) - timedelta(days=1); start = end.replace(day=1)
    elif range_name == "custom":
        if not start_date or not end_date or start_date > end_date: raise HTTPException(status_code=400, detail="Valid custom dates are required")
        start, end = start_date, end_date
    else: start, end = today - timedelta(days=29), today
    start_at = datetime.combine(start, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    end_at = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    return start_at, end_at


@app.get("/studios/{studio_id}/analytics/revenue")
def get_revenue_analytics(
    studio_id: int, range: Literal["last_7_days", "last_30_days", "this_month", "previous_month", "custom"] = "last_30_days",
    start_date: date | None = None, end_date: date | None = None,
    authorized_user: User = Depends(require_studio_user), db: Session = Depends(get_db)
):
    studio = get_studio(db, studio_id); start_at, end_at = revenue_date_bounds(studio, range, start_date, end_date)
    base = [RevenueTransaction.studio_id == studio_id, RevenueTransaction.analytics_date >= start_at, RevenueTransaction.analytics_date < end_at]
    totals = db.query(
        func.coalesce(func.sum(RevenueTransaction.net_revenue), 0).label("net"),
        func.coalesce(func.sum(RevenueTransaction.gross_revenue), 0).label("gross"),
        func.count(RevenueTransaction.id).label("transactions"),
        func.coalesce(func.sum(case((RevenueTransaction.transaction_kind == "refund", -RevenueTransaction.net_revenue), else_=0)), 0).label("refund_value"),
        func.sum(case((RevenueTransaction.transaction_kind == "refund", 1), else_=0)).label("refund_count"),
        func.coalesce(func.sum(RevenueTransaction.discount), 0).label("discounts"),
    ).filter(*base).one()
    def grouped(column, limit=None):
        query = db.query(column.label("label"), func.count(RevenueTransaction.id).label("transactions"), func.sum(RevenueTransaction.gross_revenue).label("gross"), func.sum(RevenueTransaction.net_revenue).label("net")).filter(*base).group_by(column).order_by(func.sum(RevenueTransaction.net_revenue).desc())
        if limit: query = query.limit(limit)
        return [{"label": row.label or "Unspecified", "transactions": row.transactions, "gross_revenue": row.gross or 0, "net_revenue": row.net or 0} for row in query.all()]
    trend_date = func.date(func.timezone(studio.timezone, RevenueTransaction.analytics_date)) if db.bind.dialect.name == "postgresql" else func.date(RevenueTransaction.analytics_date)
    trend_rows = db.query(trend_date.label("date"), func.sum(RevenueTransaction.net_revenue).label("net")).filter(*base).group_by(trend_date).order_by(trend_date).all()
    refund_rows = db.query(RevenueTransaction, Member).outerjoin(
        Member, (Member.id == RevenueTransaction.member_id) & (Member.studio_id == studio_id)
    ).filter(*base, RevenueTransaction.transaction_kind == "refund").order_by(
        RevenueTransaction.analytics_date.desc(), RevenueTransaction.id.desc()
    ).limit(10).all()
    refund_gross = db.query(func.coalesce(func.sum(RevenueTransaction.gross_revenue), 0)).filter(
        *base, RevenueTransaction.transaction_kind == "refund"
    ).scalar()
    latest_batch = db.query(ImportBatch, StudioDataSource).outerjoin(
        StudioDataSource,
        (StudioDataSource.id == ImportBatch.studio_data_source_id) & (StudioDataSource.studio_id == studio_id)
    ).filter(
        ImportBatch.studio_id == studio_id, ImportBatch.import_type == "revenue",
        ImportBatch.status != "rolled_back"
    ).order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc()).first()
    net_total = totals.net or Decimal("0")
    def percentage(value):
        return (value or Decimal("0")) * Decimal("100") / net_total if net_total else Decimal("0")
    revenue_types = grouped(RevenueTransaction.revenue_type)
    payment_methods = grouped(RevenueTransaction.payment_method)
    for row in revenue_types: row["percentage"] = percentage(row["net_revenue"])
    for row in payment_methods: row["percentage"] = percentage(row["net_revenue"])
    return {
        "available": get_dataset_availability(db, studio_id)["revenue"],
        "selected_range": range, "range": {"start": start_at, "end": end_at},
        "summary": {"net_revenue": totals.net, "gross_revenue": totals.gross, "transactions": totals.transactions, "refund_value": totals.refund_value, "refund_count": totals.refund_count or 0, "discounts": totals.discounts, "average_net_transaction": totals.net / totals.transactions if totals.transactions else 0},
        "trend": [{"date": row.date, "net_revenue": row.net or 0} for row in trend_rows],
        "by_revenue_type": revenue_types, "top_products": grouped(RevenueTransaction.description, 10),
        "by_payment_method": payment_methods,
        "refund_summary": {"count": totals.refund_count or 0, "net_value": totals.refund_value, "gross_value": abs(refund_gross or Decimal("0"))},
        "recent_refunds": [serialize_revenue_transaction(transaction, member) for transaction, member in refund_rows],
        "freshness": {"last_imported_at": latest_batch[0].created_at if latest_batch else None, "source": latest_batch[1].display_name if latest_batch and latest_batch[1] else None},
    }


def serialize_revenue_transaction(transaction, member=None):
    customer = f"{member.first_name} {member.last_name}" if member else (transaction.customer_name or "Unknown customer")
    return {
        "id": transaction.id, "date": transaction.analytics_date, "customer": customer,
        "member_id": member.id if member else None, "revenue_type": transaction.revenue_type or "Uncategorized",
        "description": transaction.description or "—", "payment_method": transaction.payment_method or "Unknown",
        "kind": "Refund" if transaction.transaction_kind == "refund" else "Revenue",
        "gross_revenue": transaction.gross_revenue, "net_revenue": transaction.net_revenue,
    }


@app.get("/studios/{studio_id}/analytics/revenue/transactions")
def get_revenue_transactions(
    studio_id: int,
    range: Literal["last_7_days", "last_30_days", "this_month", "previous_month", "custom"] = "this_month",
    start_date: date | None = None, end_date: date | None = None,
    kind: Literal["all", "revenue", "refund"] = "all", search: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=25),
    authorized_user: User = Depends(require_studio_user), db: Session = Depends(get_db),
):
    studio = get_studio(db, studio_id); start_at, end_at = revenue_date_bounds(studio, range, start_date, end_date)
    filters = [RevenueTransaction.studio_id == studio_id, RevenueTransaction.analytics_date >= start_at, RevenueTransaction.analytics_date < end_at]
    if kind != "all": filters.append(RevenueTransaction.transaction_kind == kind)
    query = db.query(RevenueTransaction, Member).outerjoin(
        Member, (Member.id == RevenueTransaction.member_id) & (Member.studio_id == studio_id)
    ).filter(*filters)
    term = (search or "").strip()
    if term:
        pattern = f"%{term}%"
        query = query.filter(
            RevenueTransaction.customer_name.ilike(pattern) | RevenueTransaction.customer_email.ilike(pattern) |
            RevenueTransaction.description.ilike(pattern) | RevenueTransaction.external_transaction_id.ilike(pattern)
        )
    total_items = query.count(); total_pages = (total_items + page_size - 1) // page_size
    rows = query.order_by(RevenueTransaction.analytics_date.desc(), RevenueTransaction.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": [serialize_revenue_transaction(transaction, member) for transaction, member in rows], "page": page, "page_size": page_size, "total_items": total_items, "total_pages": total_pages, "has_previous": page > 1, "has_next": page < total_pages}

def calculate_retention_status(last_visit_at, now, studio):
    if last_visit_at is None:
        return "critical", None

    if last_visit_at.tzinfo is None:
        last_visit_at = last_visit_at.replace(tzinfo=timezone.utc)

    days_inactive = (now - last_visit_at).days

    if days_inactive <= studio.retention_healthy_days:
        status = "healthy"
    elif days_inactive <= studio.retention_watch_days:
        status = "watch"
    elif days_inactive <= studio.retention_at_risk_days:
        status = "at_risk"
    else:
        status = "critical"

    return status, days_inactive


def get_latest_attendance_by_member(db, studio_id):
    rows = (
        db.query(
            Booking.member_id,
            func.max(Booking.booking_date).label("last_visit_at")
        )
        .filter(
            Booking.studio_id == studio_id,
            Booking.status == "attended"
        )
        .group_by(Booking.member_id)
        .all()
    )

    return {
        row.member_id: row.last_visit_at
        for row in rows
    }


def ensure_current_milestone_records(db, studio_id, attendance_by_member):
    existing = {
        (record.member_id, record.milestone_value): record
        for record in db.query(MemberMilestoneStatus).filter(
            MemberMilestoneStatus.studio_id == studio_id,
            MemberMilestoneStatus.milestone_type == "attendance"
        ).all()
    }
    created = False
    for member_id, attendance in attendance_by_member.items():
        value = attendance.get("last_milestone")
        if value and (member_id, value) not in existing:
            record = MemberMilestoneStatus(
                studio_id=studio_id, member_id=member_id,
                milestone_type="attendance", milestone_value=value, status="open"
            )
            db.add(record)
            existing[(member_id, value)] = record
            created = True
    if created:
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        existing = {
            (record.member_id, record.milestone_value): record
            for record in db.query(MemberMilestoneStatus).filter(
                MemberMilestoneStatus.studio_id == studio_id,
                MemberMilestoneStatus.milestone_type == "attendance"
            ).all()
        }
    return existing


@app.post("/studios/{studio_id}/members/{member_id}/milestones/{milestone_value}/status")
def update_member_milestone_status(
    studio_id: int, member_id: int, milestone_value: int,
    status_request: MilestoneStatusUpdate,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    get_studio_member(db, studio_id, member_id)
    if milestone_value not in ATTENDANCE_MILESTONES:
        raise HTTPException(status_code=404, detail="Attendance milestone not found")
    total = get_attendance_aggregates(db, studio_id, datetime.now(timezone.utc)).get(member_id, {}).get("total_attended", 0)
    record = db.query(MemberMilestoneStatus).filter(
        MemberMilestoneStatus.studio_id == studio_id,
        MemberMilestoneStatus.member_id == member_id,
        MemberMilestoneStatus.milestone_type == "attendance",
        MemberMilestoneStatus.milestone_value == milestone_value
    ).first()
    if not record or total < milestone_value:
        raise HTTPException(status_code=409, detail="Attendance milestone is not currently eligible")
    record.status = status_request.status
    record.acknowledged_at = datetime.now(timezone.utc)
    record.acknowledged_by_user_id = authorized_user.id
    db.add(ActionHistory(
        studio_id=studio_id, member_id=member_id, action_type="attendance_milestone",
        event_type=f"attendance_milestone_{status_request.status}",
        message_text=f"Attendance milestone {milestone_value} {status_request.status}"
    ))
    try:
        db.commit()
        db.refresh(record)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=503, detail="Milestone status is temporarily unavailable")
    return {"member_id": member_id, "milestone_value": milestone_value, "status": record.status, "acknowledged_at": record.acknowledged_at}


def build_member_summaries(db, studio):
    members = (
        db.query(Member)
        .filter(Member.studio_id == studio.id)
        .order_by(Member.last_name.asc(), Member.first_name.asc())
        .all()
    )
    latest_attendance = get_latest_attendance_by_member(db, studio.id)
    failed_rows = (
        db.query(
            Payment.member_id,
            func.count(Payment.id).label("failed_count"),
            func.sum(Payment.amount).label("failed_amount")
        )
        .filter(
            Payment.studio_id == studio.id,
            Payment.status == "failed"
        )
        .group_by(Payment.member_id)
        .all()
    )
    failed_by_member = {
        row.member_id: {
            "count": row.failed_count,
            "amount": (row.failed_amount or 0) / 100
        }
        for row in failed_rows
    }
    now = datetime.now(timezone.utc)
    summaries = []

    for member in members:
        last_visit = latest_attendance.get(member.id)
        retention_status, days_inactive = calculate_retention_status(
            last_visit,
            now,
            studio
        )
        failed = failed_by_member.get(member.id, {"count": 0, "amount": 0})
        summaries.append({
            "id": member.id,
            "name": f"{member.first_name} {member.last_name}",
            "email": member.email,
            "retention_status": retention_status,
            "days_inactive": days_inactive,
            "last_visit_at": last_visit,
            "failed_payment_count": failed["count"],
            "failed_amount": failed["amount"]
        })

    return summaries


@app.get("/api/members")
def get_members_crm(
    current_user: User = Depends(require_current_user),
    db: Session = Depends(get_db)
):
    studio = get_studio(db, current_user.studio_id)
    members = build_member_summaries(db, studio)
    counts = {
        status: sum(
            member["retention_status"] == status
            for member in members
        )
        for status in ("healthy", "watch", "at_risk", "critical")
    }
    counts["payment_issue"] = sum(
        member["failed_payment_count"] > 0
        for member in members
    )

    return {
        "studio_id": studio.id,
        "currency": studio.currency,
        "member_count": len(members),
        "counts": counts,
        "members": members
    }


@app.get("/api/members/{member_id}")
def get_member_crm_detail(
    member_id: int,
    current_user: User = Depends(require_current_user),
    db: Session = Depends(get_db)
):
    studio = get_studio(db, current_user.studio_id)
    member = get_studio_member(db, studio.id, member_id)
    now = datetime.now(timezone.utc)
    attendance = get_attendance_aggregates(db, studio.id, now).get(
        member.id,
        {**get_attendance_milestone(0), **calculate_attendance_decline(0, 0), "last_visit_at": None}
    )
    milestone_records = ensure_current_milestone_records(db, studio.id, {member.id: attendance})
    current_milestone = milestone_records.get((member.id, attendance.get("last_milestone")))
    last_visit = (
        db.query(func.max(Booking.booking_date))
        .filter(
            Booking.studio_id == studio.id,
            Booking.member_id == member.id,
            Booking.status == "attended"
        )
        .scalar()
    )
    retention_status, days_inactive = calculate_retention_status(
        last_visit,
        now,
        studio
    )
    booking_summary_row = (
        db.query(
            func.count(Booking.id).label("total"),
            func.sum(case((Booking.status == "attended", 1), else_=0)).label("attended"),
            func.sum(case((Booking.status == "cancelled", 1), else_=0)).label("cancelled"),
            func.sum(case((Booking.status == "no_show", 1), else_=0)).label("no_shows")
        )
        .filter(
            Booking.studio_id == studio.id,
            Booking.member_id == member.id
        )
        .one()
    )
    recent_bookings = (
        db.query(Booking)
        .filter(
            Booking.studio_id == studio.id,
            Booking.member_id == member.id
        )
        .order_by(Booking.booking_date.desc(), Booking.id.desc())
        .limit(20)
        .all()
    )
    payment_summary_row = (
        db.query(
            func.sum(case((Payment.status == "paid", Payment.amount), else_=0)).label("total_paid"),
            func.sum(case((Payment.status == "failed", 1), else_=0)).label("failed_count"),
            func.sum(case((Payment.status == "failed", Payment.amount), else_=0)).label("failed_amount")
        )
        .filter(
            Payment.studio_id == studio.id,
            Payment.member_id == member.id
        )
        .one()
    )
    recent_payments = (
        db.query(Payment)
        .filter(
            Payment.studio_id == studio.id,
            Payment.member_id == member.id
        )
        .order_by(Payment.payment_date.desc(), Payment.id.desc())
        .limit(20)
        .all()
    )
    status_records = (
        db.query(ActionStatus)
        .filter(
            ActionStatus.studio_id == studio.id,
            ActionStatus.member_id == member.id
        )
        .all()
    )
    status_by_type = {
        record.action_type: get_effective_action_status(record, now)[0]
        for record in status_records
    }
    follow_ups = (
        db.query(FollowUp)
        .filter(
            FollowUp.studio_id == studio.id,
            FollowUp.member_id == member.id
        )
        .order_by(FollowUp.due_at.asc(), FollowUp.id.asc())
        .all()
    )
    history = (
        db.query(ActionHistory)
        .filter(
            ActionHistory.studio_id == studio.id,
            ActionHistory.member_id == member.id
        )
        .order_by(ActionHistory.created_at.desc(), ActionHistory.id.desc())
        .limit(25)
        .all()
    )

    return {
        "studio": {
            "id": studio.id,
            "name": studio.name,
            "currency": studio.currency,
            "timezone": studio.timezone,
            "default_follow_up_days": studio.default_follow_up_days
        },
        "member": {
            "id": member.id,
            "name": f"{member.first_name} {member.last_name}",
            "email": member.email
        },
        "retention": {
            "status": retention_status,
            "days_inactive": days_inactive,
            "last_visit_at": last_visit
        },
        "attendance": {
            **attendance,
            "average_visits_per_week": round((booking_summary_row.attended or 0) / max(1, ((now - (member.created_at.replace(tzinfo=timezone.utc) if member.created_at and member.created_at.tzinfo is None else member.created_at or now)).days / 7)), 2),
            "milestone_status": current_milestone.status if current_milestone else None,
            "milestone_acknowledged_at": current_milestone.acknowledged_at if current_milestone else None,
        },
        "booking_summary": {
            "total": booking_summary_row.total or 0,
            "attended": booking_summary_row.attended or 0,
            "cancelled": booking_summary_row.cancelled or 0,
            "no_shows": booking_summary_row.no_shows or 0
        },
        "recent_bookings": [
            {
                "id": booking.id,
                "class_name": booking.class_name,
                "status": booking.status,
                "booking_date": booking.booking_date
            }
            for booking in recent_bookings
        ],
        "payment_summary": {
            "total_paid": (payment_summary_row.total_paid or 0) / 100,
            "failed_count": payment_summary_row.failed_count or 0,
            "failed_amount": (payment_summary_row.failed_amount or 0) / 100
        },
        "recent_payments": [
            {
                "id": payment.id,
                "amount": payment.amount / 100,
                "status": payment.status,
                "payment_date": payment.payment_date
            }
            for payment in recent_payments
        ],
        "action_statuses": {
            **{"retention": status_by_type.get("retention", "open")},
            **(
                {"payment": status_by_type.get("payment", "open")}
                if (
                    (payment_summary_row.failed_count or 0) > 0
                    or "payment" in status_by_type
                )
                else {}
            )
        },
        "follow_ups": [
            serialize_follow_up(follow_up, member, now)
            for follow_up in follow_ups
        ],
        "action_history": [
            {
                "id": entry.id,
                "action_type": entry.action_type,
                "event_type": entry.event_type,
                "priority": entry.priority,
                "created_at": entry.created_at,
                "milestone_value": (
                    int(match.group(1))
                    if entry.event_type in {"attendance_milestone_celebrated", "attendance_milestone_dismissed"}
                    and entry.message_text
                    and (match := re.search(r"Attendance milestone (\d+)", entry.message_text))
                    else None
                )
            }
            for entry in history
        ]
    }


@app.get("/studios/{studio_id}/analytics/retention-health")
def get_retention_health(
    studio_id: int,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    studio = get_studio(db, studio_id)
    active_members = (
        db.query(Member)
        .filter(
            Member.studio_id == studio_id,
            Member.status == "active"
        )
        .all()
    )

    latest_attendance = get_latest_attendance_by_member(db, studio_id)
    now = datetime.now(timezone.utc)
    results = []

    for member in active_members:
        last_visit_at = latest_attendance.get(member.id)
        status, days_inactive = calculate_retention_status(
            last_visit_at,
            now,
            studio
        )

        results.append({
            "id": member.id,
            "name": f"{member.first_name} {member.last_name}",
            "email": member.email,
            "status": status,
            "days_inactive": days_inactive,
            "last_visit_at": last_visit_at
        })

    summary = {
        "healthy": 0,
        "watch": 0,
        "at_risk": 0,
        "critical": 0
    }

    for member in results:
        summary[member["status"]] += 1

    return {
        "studio_id": studio_id,
        "summary": summary,
        "members": results
    } 


@app.get("/studios/{studio_id}/analytics/action-center")
def get_action_center(
    studio_id: int,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    studio = get_studio(db, studio_id)
    members = (
        db.query(Member)
        .filter(Member.studio_id == studio_id)
        .all()
    )

    now = datetime.now(timezone.utc)
    attendance_by_member = get_attendance_aggregates(db, studio_id, now)
    milestone_records = ensure_current_milestone_records(db, studio_id, attendance_by_member)

    failed_payment_rows = (
        db.query(
            Payment.member_id,
            func.count(Payment.id).label("failed_payment_count"),
            func.sum(Payment.amount).label("failed_amount_centavos")
        )
        .filter(
            Payment.studio_id == studio_id,
            Payment.status == "failed"
        )
        .group_by(Payment.member_id)
        .all()
    )

    failed_payments = {
        row.member_id: {
            "count": row.failed_payment_count,
            "amount_centavos": row.failed_amount_centavos or 0
        }
        for row in failed_payment_rows
    }

    action_statuses = get_action_status_map(db, studio_id)
    actions = []

    for member in members:
        payment_data = failed_payments.get(member.id)
        has_failed_payment = payment_data is not None
        retention_status = None
        days_inactive = None

        if member.status == "active":
            retention_status, days_inactive = calculate_retention_status(
                attendance_by_member.get(member.id, {}).get("last_visit_at"),
                now,
                studio
            )

        if retention_status == "critical" and has_failed_payment:
            priority = "urgent"
            recommended_action = "Contact member and recover failed payment"
        elif retention_status == "critical":
            priority = "high"
            recommended_action = "Contact member"
        elif retention_status == "at_risk" and has_failed_payment:
            priority = "high"
            recommended_action = "Contact member and recover failed payment"
        elif has_failed_payment:
            priority = "payment"
            recommended_action = "Recover failed payment"
        elif retention_status == "watch":
            priority = "watch"
            recommended_action = "Contact member"
        else:
            priority = None
            recommended_action = None

        action_type = "payment" if priority == "payment" else "retention"
        action_status, _ = get_effective_action_status(action_statuses.get((member.id, action_type)), now)

        if priority and action_status not in {"resolved", "snoozed"}:
            actions.append({
            "action_id": f"{action_type}:{member.id}",
            "member_id": member.id,
            "member_name": f"{member.first_name} {member.last_name}",
            "email": member.email,
            "priority": priority,
            "action_type": action_type,
            "action_status": action_status,
            "retention_status": retention_status,
            "days_inactive": days_inactive,
            "failed_payment_count": (
                payment_data["count"]
                if payment_data
                else 0
            ),
            "failed_amount": (
                payment_data["amount_centavos"] / 100
                if payment_data
                else 0
            ),
                "recommended_action": recommended_action
            })

        attendance = attendance_by_member.get(member.id)
        if member.status == "active" and attendance and attendance["attendance_declining"] and retention_status != "critical":
            decline_status, _ = get_effective_action_status(action_statuses.get((member.id, "attendance_decline")), now)
            if decline_status not in {"resolved", "snoozed"}:
                actions.append({
                    "action_id": f"attendance_decline:{member.id}", "member_id": member.id,
                    "member_name": f"{member.first_name} {member.last_name}", "email": member.email,
                    "action_type": "attendance_decline", "group": "needs_attention", "priority": "normal",
                    "title": "Attendance Declining", "description": "Recent attendance is meaningfully below the previous baseline.",
                    "recommended_action": "Check in before this member becomes At Risk", "action_status": decline_status,
                    "retention_status": retention_status, "days_inactive": days_inactive,
                    "failed_payment_count": 0, "failed_amount": 0,
                    "baseline_visits_per_week": attendance["baseline_visits_per_week"],
                    "recent_visits_per_week": attendance["recent_visits_per_week"],
                    "attendance_change_percent": attendance["attendance_change_percent"],
                    "last_visit_at": attendance["last_visit_at"],
                })

        if attendance and attendance["last_milestone"]:
            value = attendance["last_milestone"]
            ordinal = format_ordinal(value)
            milestone = milestone_records.get((member.id, value))
            if milestone and milestone.status == "open" and attendance["total_attended"] >= value:
                actions.append({
                    "action_id": f"attendance_milestone:{member.id}:{value}", "member_id": member.id,
                    "member_name": f"{member.first_name} {member.last_name}", "email": member.email,
                    "action_type": "attendance_milestone", "group": "celebration", "priority": "normal",
                    "title": "First Class" if value == 1 else f"{ordinal} Class",
                    "description": "Completed their first class" if value == 1 else f"Reached {value} attended classes",
                    "recommended_action": "Celebrate the achievement", "action_status": "open",
                    "retention_status": retention_status, "days_inactive": days_inactive,
                    "failed_payment_count": 0, "failed_amount": 0,
                    "milestone_value": value, "milestone_ordinal": ordinal,
                    "total_attended": attendance["total_attended"], "last_visit_at": attendance["last_visit_at"],
                    "created_at": milestone.created_at,
                })

    due_follow_up_rows = (
        db.query(FollowUp, Member)
        .join(Member, Member.id == FollowUp.member_id)
        .filter(FollowUp.studio_id == studio_id, Member.studio_id == studio_id, FollowUp.status == "pending", FollowUp.due_at <= now)
        .order_by(FollowUp.due_at.asc(), FollowUp.id.asc()).all()
    )
    for follow_up, member in due_follow_up_rows:
        actions.append({
            "action_id": f"follow_up:{follow_up.id}", "member_id": member.id,
            "member_name": f"{member.first_name} {member.last_name}", "email": member.email,
            "action_type": "follow_up", "group": "needs_attention", "priority": "normal",
            "title": "Follow-Up Due", "description": follow_up.note or "A scheduled member follow-up is due.",
            "recommended_action": "Complete the scheduled follow-up", "action_status": "open",
            "retention_status": None, "days_inactive": None, "failed_payment_count": 0, "failed_amount": 0,
            "follow_up_id": follow_up.id, "due_at": follow_up.due_at,
        })

    for action in actions:
        if "group" not in action:
            action["group"] = "urgent" if action["priority"] == "urgent" or action["retention_status"] == "critical" else "needs_attention"
            action["title"] = "Failed Payment" if action["action_type"] == "payment" else "Retention Attention"
            action["description"] = action["recommended_action"]
    priority_order = {"urgent": 0, "high": 1, "payment": 2, "watch": 4, "normal": 5}
    type_order = {"retention": 0, "payment": 2, "follow_up": 3, "attendance_decline": 5, "attendance_milestone": 6}

    actions.sort(
        key=lambda action: (
            0 if action.get("priority") == "urgent" else type_order.get(action["action_type"], 9),
            priority_order.get(action["priority"], 9),
            -(action.get("attendance_change_percent") or 0),
            -(action.get("milestone_value") or 0),
            action.get("days_inactive") is not None,
            -(action["days_inactive"] or 0),
            action["member_name"].lower()
        )
    )

    due_follow_ups = len(due_follow_up_rows)
    summary = {
        "total_actions": len(actions),
        "urgent": sum(action["group"] == "urgent" for action in actions),
        "needs_attention": sum(action["group"] == "needs_attention" for action in actions),
        "celebrations": sum(action["group"] == "celebration" for action in actions),
        "due_follow_ups": due_follow_ups,
    }

    return {
        "studio_id": studio_id,
        "summary": summary,
        "action_count": len(actions),
        "urgent_count": summary["urgent"],
        "high_count": sum(action["priority"] == "high" for action in actions),
        "payment_count": sum(action["action_type"] == "payment" for action in actions),
        "watch_count": sum(action["priority"] == "watch" for action in actions),
        "actions": actions
    }

@app.get("/studios/{studio_id}/analytics/payment-recovery")
def get_payment_recovery(
    studio_id: int,
    authorized_user: User = Depends(require_studio_user),
    db: Session = Depends(get_db)
):
    failed_payments = (
        db.query(Payment)
        .filter(
            Payment.studio_id == studio_id,
            Payment.status == "failed"
        )
        .all()
    )

    results = []
    total_failed_centavos = 0

    for payment in failed_payments:

        member = (
            db.query(Member)
            .filter(
                Member.id == payment.member_id,
                Member.studio_id == studio_id
            )
            .first()
        )

        total_failed_centavos += payment.amount

        results.append({
            "payment_id": payment.id,
            "member_id": payment.member_id,
            "member_name": (
                f"{member.first_name} {member.last_name}"
                if member
                else "Unknown Member"
            ),
            "email": (
                member.email
                if member
                else None
            ),
            "amount": payment.amount / 100,
            "failed_at": payment.payment_date
        })

    return {
        "studio_id": studio_id,
        "failed_payment_count": len(results),
        "revenue_to_recover": total_failed_centavos / 100,
        "payments": results
    }  
