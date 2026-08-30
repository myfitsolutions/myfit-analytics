from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    ForeignKey,
    text
)
from sqlalchemy.sql import func

from app.database import Base


class Studio(Base):
    __tablename__ = "studios"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    timezone = Column(String, default="Asia/Manila")
    currency = Column(String, default="USD")
    retention_healthy_days = Column(Integer, nullable=False, default=13)
    retention_watch_days = Column(Integer, nullable=False, default=20)
    retention_at_risk_days = Column(Integer, nullable=False, default=30)
    default_follow_up_days = Column(Integer, nullable=False, default=3)
    sender_name = Column(String(100), nullable=True)
    onboarding_completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now()
    )


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(
        Integer,
        ForeignKey("studios.id"),
        nullable=False,
        index=True
    )
    email = Column(String(320), nullable=False, unique=True, index=True)
    password_hash = Column(String(500), nullable=False)
    role = Column(String(20), nullable=False, default="staff")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class StudioDataSource(Base):
    __tablename__ = "studio_data_sources"
    __table_args__ = (
        CheckConstraint("platform IN ('hapana', 'bsport', 'other')", name="ck_studio_data_sources_platform"),
        CheckConstraint("source_type IN ('management_platform')", name="ck_studio_data_sources_type"),
        Index("ix_studio_data_sources_studio_active", "studio_id", "is_active"),
        Index(
            "uq_studio_data_sources_active_primary_management",
            "studio_id", unique=True,
            postgresql_where=text("source_type = 'management_platform' AND is_primary AND is_active"),
            sqlite_where=text("source_type = 'management_platform' AND is_primary = 1 AND is_active = 1"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(Integer, ForeignKey("studios.id"), nullable=False, index=True)
    source_type = Column(String(30), nullable=False, default="management_platform")
    platform = Column(String(30), nullable=False)
    display_name = Column(String(100), nullable=False)
    is_primary = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        CheckConstraint(
            "import_type IN ('members', 'bookings', 'payments', 'revenue')",
            name="ck_import_batches_type"
        ),
        CheckConstraint(
            "status IN ('completed', 'partially_rolled_back', 'rolled_back')",
            name="ck_import_batches_status"
        ),
        Index("ix_import_batches_studio_created_at", "studio_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(Integer, ForeignKey("studios.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    import_type = Column(String(20), nullable=False)
    filename = Column(String(255), nullable=False)
    total_rows = Column(Integer, nullable=False, default=0)
    imported_count = Column(Integer, nullable=False, default=0)
    skipped_count = Column(Integer, nullable=False, default=0)
    invalid_count = Column(Integer, nullable=False, default=0)
    status = Column(String(30), nullable=False, default="completed")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    rolled_back_at = Column(DateTime(timezone=True), nullable=True)
    rolled_back_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    source_profile_id = Column(Integer, ForeignKey("import_source_profiles.id", ondelete="SET NULL"), nullable=True, index=True)
    source_name_snapshot = Column(String(100), nullable=True)
    studio_data_source_id = Column(Integer, ForeignKey("studio_data_sources.id", ondelete="SET NULL"), nullable=True, index=True)


class ImportMappingPreset(Base):
    __tablename__ = "import_mapping_presets"

    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(Integer, ForeignKey("studios.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    import_type = Column(String(20), nullable=False)
    mapping_json = Column(Text, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)


Index(
    "uq_import_mapping_presets_studio_normalized_name",
    ImportMappingPreset.studio_id,
    func.lower(func.trim(ImportMappingPreset.name)),
    unique=True
)


class ImportSourceProfile(Base):
    __tablename__ = "import_source_profiles"

    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(Integer, ForeignKey("studios.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    members_preset_id = Column(Integer, ForeignKey("import_mapping_presets.id", ondelete="SET NULL"), nullable=True)
    bookings_preset_id = Column(Integer, ForeignKey("import_mapping_presets.id", ondelete="SET NULL"), nullable=True)
    payments_preset_id = Column(Integer, ForeignKey("import_mapping_presets.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)


Index(
    "uq_import_source_profiles_studio_normalized_name",
    ImportSourceProfile.studio_id,
    func.lower(func.trim(ImportSourceProfile.name)),
    unique=True
)


class Member(Base):
    __tablename__ = "members"

    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(Integer, nullable=False, index=True)
    import_batch_id = Column(Integer, ForeignKey("import_batches.id"), nullable=True, index=True)

    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    email = Column(String, nullable=False)

    status = Column(
        String,
        default="active"
    )

    last_visit_at = Column(
        DateTime(timezone=True),
        nullable=True
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )


member_studio_email_unique_index = Index(
    "uq_members_studio_normalized_email",
    Member.studio_id,
    func.lower(func.trim(Member.email)),
    unique=True
)

class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(Integer, nullable=False, index=True)
    member_id = Column(Integer, nullable=False, index=True)
    import_batch_id = Column(Integer, ForeignKey("import_batches.id"), nullable=True, index=True)

    amount = Column(Integer, nullable=False)

    status = Column(
        String,
        default="paid"
    )

    payment_date = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(Integer, nullable=False, index=True)
    member_id = Column(Integer, nullable=False, index=True)
    import_batch_id = Column(Integer, ForeignKey("import_batches.id"), nullable=True, index=True)

    class_name = Column(String, nullable=False)

    status = Column(
        String,
        default="booked"
    )

    booking_date = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )


class RevenueTransaction(Base):
    __tablename__ = "revenue_transactions"
    __table_args__ = (
        UniqueConstraint("studio_id", "studio_data_source_id", "identity_key", name="uq_revenue_transactions_source_identity"),
        CheckConstraint("transaction_kind IN ('revenue', 'refund', 'other')", name="ck_revenue_transactions_kind"),
        Index("ix_revenue_transactions_studio_date", "studio_id", "analytics_date"),
    )
    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(Integer, ForeignKey("studios.id"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("members.id", ondelete="SET NULL"), nullable=True, index=True)
    studio_data_source_id = Column(Integer, ForeignKey("studio_data_sources.id", ondelete="SET NULL"), nullable=True, index=True)
    import_batch_id = Column(Integer, ForeignKey("import_batches.id"), nullable=True, index=True)
    identity_key = Column(String(128), nullable=False)
    external_transaction_id = Column(String(255), nullable=True)
    customer_name = Column(String(255), nullable=True)
    customer_email = Column(String(320), nullable=True)
    invoice_date = Column(DateTime(timezone=True), nullable=True)
    payment_date = Column(DateTime(timezone=True), nullable=True)
    analytics_date = Column(DateTime(timezone=True), nullable=False)
    payment_method = Column(String(100), nullable=True)
    source_status = Column(String(100), nullable=True)
    transaction_kind = Column(String(20), nullable=False, default="other")
    revenue_type = Column(String(100), nullable=True)
    transaction_category = Column(String(255), nullable=True)
    description = Column(String(500), nullable=True)
    gross_revenue = Column(Numeric(14, 2), nullable=False, default=0)
    net_revenue = Column(Numeric(14, 2), nullable=False)
    tax = Column(Numeric(14, 2), nullable=True)
    discount = Column(Numeric(14, 2), nullable=True)
    admin_fee = Column(Numeric(14, 2), nullable=True)
    dishonour_fee = Column(Numeric(14, 2), nullable=True)
    transaction_fee = Column(Numeric(14, 2), nullable=True)
    processed_by = Column(String(255), nullable=True)
    sale_referred_by = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ActionHistory(Base):
    __tablename__ = "action_history"
    __table_args__ = (
        Index(
            "ix_action_history_studio_created_at",
            "studio_id",
            "created_at"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(Integer, nullable=False)
    member_id = Column(Integer, nullable=False, index=True)
    action_type = Column(String(20), nullable=False)
    event_type = Column(String(40), nullable=False)
    priority = Column(String(20), nullable=True)
    message_text = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )


class ActionStatus(Base):
    __tablename__ = "action_status"
    __table_args__ = (
        UniqueConstraint(
            "studio_id",
            "member_id",
            "action_type",
            name="uq_action_status_studio_member_type"
        ),
        Index(
            "ix_action_status_studio_status",
            "studio_id",
            "status"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(Integer, nullable=False)
    member_id = Column(Integer, nullable=False, index=True)
    action_type = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="open")
    snooze_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now()
    )


class MemberMilestoneStatus(Base):
    __tablename__ = "member_milestone_status"
    __table_args__ = (
        UniqueConstraint("studio_id", "member_id", "milestone_type", "milestone_value", name="uq_member_milestone_identity"),
        CheckConstraint("status IN ('open', 'celebrated', 'dismissed')", name="ck_member_milestone_status"),
        Index("ix_member_milestone_studio_status", "studio_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(Integer, ForeignKey("studios.id"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False, index=True)
    milestone_type = Column(String(30), nullable=False, default="attendance")
    milestone_value = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="open")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class FollowUp(Base):
    __tablename__ = "follow_ups"
    __table_args__ = (
        Index("ix_follow_ups_studio_status", "studio_id", "status"),
        Index("ix_follow_ups_studio_due_at", "studio_id", "due_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    studio_id = Column(Integer, nullable=False, index=True)
    member_id = Column(Integer, nullable=False, index=True)
    action_type = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    due_at = Column(DateTime(timezone=True), nullable=False, index=True)
    note = Column(String(1000), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now()
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)
