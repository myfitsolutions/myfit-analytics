from datetime import datetime, timezone

from sqlalchemy import exists, func

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
    members, bookings, payments, revenue_rows = db.query(
        exists().where(Member.studio_id == studio_id),
        exists().where(Booking.studio_id == studio_id),
        exists().where(Payment.studio_id == studio_id),
        exists().where(RevenueTransaction.studio_id == studio_id),
    ).one()
    return {
        "members": bool(members),
        "bookings": bool(bookings),
        "payments": bool(payments),
        "revenue": bool(revenue_rows),
    }


def get_data_trust_summary(db, studio_id):
    models = {
        "members": Member,
        "bookings": Booking,
        "payments": Payment,
        "revenue": RevenueTransaction,
    }
    counts = {
        name: db.query(func.count(model.id)).filter(model.studio_id == studio_id).scalar()
        for name, model in models.items()
    }
    batches = {}
    for import_type in models:
        batch = (
            db.query(ImportBatch, StudioDataSource)
            .outerjoin(
                StudioDataSource,
                (StudioDataSource.id == ImportBatch.studio_data_source_id)
                & (StudioDataSource.studio_id == studio_id),
            )
            .filter(
                ImportBatch.studio_id == studio_id,
                ImportBatch.import_type == import_type,
                ImportBatch.status == "completed",
                ImportBatch.imported_count > 0,
            )
            .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
            .first()
        )
        batches[import_type] = batch
    primary = get_primary_data_source(db, studio_id)
    return {
        "platform": primary.platform if primary else None,
        "platform_name": primary.display_name if primary else None,
        "datasets": {
            name: {
                "available": bool(counts[name]),
                "record_count": counts[name] or 0,
                "last_imported_at": batches[name][0].created_at if batches[name] else None,
                "source": (
                    batches[name][1].display_name
                    if batches[name] and batches[name][1]
                    else None
                ),
                "filename": batches[name][0].filename if batches[name] else None,
            }
            for name in models
        },
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
