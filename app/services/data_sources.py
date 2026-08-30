from datetime import datetime, timezone

from sqlalchemy import exists

from app.models import Booking, ImportBatch, Member, Payment, RevenueTransaction, StudioDataSource
from app.platforms import get_platform


def get_primary_data_source(db, studio_id):
    return db.query(StudioDataSource).filter(
        StudioDataSource.studio_id == studio_id,
        StudioDataSource.source_type == "management_platform",
        StudioDataSource.is_primary.is_(True),
        StudioDataSource.is_active.is_(True),
    ).first()


def set_primary_platform(db, studio_id, platform):
    definition = get_platform(platform)
    if definition is None:
        raise ValueError("Unsupported studio platform")
    current = db.query(StudioDataSource).filter(
        StudioDataSource.studio_id == studio_id,
        StudioDataSource.source_type == "management_platform",
        StudioDataSource.is_primary.is_(True),
        StudioDataSource.is_active.is_(True),
    ).all()
    for source in current:
        source.is_primary = False
        source.is_active = False
    source = db.query(StudioDataSource).filter(
        StudioDataSource.studio_id == studio_id,
        StudioDataSource.source_type == "management_platform",
        StudioDataSource.platform == platform,
    ).order_by(StudioDataSource.id.desc()).first()
    if source is None:
        source = StudioDataSource(
            studio_id=studio_id,
            source_type="management_platform",
            platform=platform,
            display_name=definition["name"],
        )
        db.add(source)
    source.display_name = definition["name"]
    source.is_primary = True
    source.is_active = True
    source.updated_at = datetime.now(timezone.utc)
    db.flush()
    return source


def get_dataset_availability(db, studio_id):
    members, bookings, payments, revenue_rows, member_import, booking_import, payment_import, revenue_import = db.query(
        exists().where(Member.studio_id == studio_id),
        exists().where(Booking.studio_id == studio_id),
        exists().where(Payment.studio_id == studio_id),
        exists().where(RevenueTransaction.studio_id == studio_id),
        exists().where(ImportBatch.studio_id == studio_id, ImportBatch.import_type == "members", ImportBatch.status != "rolled_back"),
        exists().where(ImportBatch.studio_id == studio_id, ImportBatch.import_type == "bookings", ImportBatch.status != "rolled_back"),
        exists().where(ImportBatch.studio_id == studio_id, ImportBatch.import_type == "payments", ImportBatch.status != "rolled_back"),
        exists().where(ImportBatch.studio_id == studio_id, ImportBatch.import_type == "revenue", ImportBatch.status != "rolled_back"),
    ).one()
    return {
        "members": bool(members or member_import),
        "bookings": bool(bookings or booking_import),
        "payments": bool(payments or payment_import),
        "revenue": bool(revenue_rows or revenue_import),
    }


def serialize_data_source(source):
    if source is None:
        return None
    last_import = getattr(source, "last_import_at", None)
    return {
        "id": source.id,
        "platform": source.platform,
        "display_name": source.display_name,
        "source_type": source.source_type,
        "is_primary": source.is_primary,
        "is_active": source.is_active,
        "last_import_at": last_import,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }
