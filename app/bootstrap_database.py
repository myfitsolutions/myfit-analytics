"""Explicit, non-destructive bootstrap for a brand-new MyFit PostgreSQL database."""

from sqlalchemy import inspect, text

from app.database import Base, engine
from app import models  # noqa: F401 - registers every model with Base.metadata
from app.migrate import run_all_migrations


MODEL_TABLES = frozenset(Base.metadata.tables)

REQUIRED_FOREIGN_KEYS = {
    "users": {(("studio_id",), "studios", ("id",))},
    "import_batches": {
        (("studio_id",), "studios", ("id",)),
        (("user_id",), "users", ("id",)),
        (("rolled_back_by_user_id",), "users", ("id",)),
        (("source_profile_id",), "import_source_profiles", ("id",)),
    },
    "import_mapping_presets": {
        (("studio_id",), "studios", ("id",)),
        (("created_by_user_id",), "users", ("id",)),
    },
    "import_source_profiles": {
        (("studio_id",), "studios", ("id",)),
        (("created_by_user_id",), "users", ("id",)),
        (("members_preset_id",), "import_mapping_presets", ("id",)),
        (("bookings_preset_id",), "import_mapping_presets", ("id",)),
        (("payments_preset_id",), "import_mapping_presets", ("id",)),
    },
    "members": {(("import_batch_id",), "import_batches", ("id",))},
    "bookings": {(("import_batch_id",), "import_batches", ("id",))},
    "payments": {(("import_batch_id",), "import_batches", ("id",))},
}

REQUIRED_UNIQUE_OBJECTS = {
    "action_status": "uq_action_status_studio_member_type",
    "import_mapping_presets": "uq_import_mapping_presets_studio_normalized_name",
    "import_source_profiles": "uq_import_source_profiles_studio_normalized_name",
    "members": "uq_members_studio_normalized_email",
}


class SchemaVerificationError(RuntimeError):
    pass


def application_tables(db_engine):
    """Return MyFit model tables present in the database's default schema."""
    return MODEL_TABLES.intersection(inspect(db_engine).get_table_names())


def _foreign_key_signatures(db_inspector, table_name):
    return {
        (
            tuple(item.get("constrained_columns") or ()),
            item.get("referred_table"),
            tuple(item.get("referred_columns") or ()),
        )
        for item in db_inspector.get_foreign_keys(table_name)
    }


def verify_schema(db_engine):
    """Inspect tables, model columns, key relationships, and important constraints."""
    db_inspector = inspect(db_engine)
    actual_tables = set(db_inspector.get_table_names())
    missing_tables = MODEL_TABLES - actual_tables
    problems = []
    if missing_tables:
        problems.append("missing application tables: " + ", ".join(sorted(missing_tables)))

    for table_name, model_table in Base.metadata.tables.items():
        if table_name not in actual_tables:
            continue
        actual_columns = {column["name"]: column for column in db_inspector.get_columns(table_name)}
        missing_columns = set(model_table.columns.keys()) - set(actual_columns)
        if missing_columns:
            problems.append(f"{table_name} missing columns: {', '.join(sorted(missing_columns))}")
        for column in model_table.columns:
            actual = actual_columns.get(column.name)
            if actual is not None and not column.nullable and actual.get("nullable", True):
                problems.append(f"{table_name}.{column.name} must be NOT NULL")

    for table_name, required in REQUIRED_FOREIGN_KEYS.items():
        if table_name in actual_tables:
            missing = required - _foreign_key_signatures(db_inspector, table_name)
            if missing:
                problems.append(f"{table_name} is missing {len(missing)} required foreign key(s)")

    for table_name, object_name in REQUIRED_UNIQUE_OBJECTS.items():
        if table_name not in actual_tables:
            continue
        unique_names = {
            item.get("name") for item in db_inspector.get_unique_constraints(table_name)
        }
        unique_names.update(
            item.get("name") for item in db_inspector.get_indexes(table_name) if item.get("unique")
        )
        if object_name not in unique_names:
            problems.append(f"{table_name} missing unique constraint/index {object_name}")

    if "import_batches" in actual_tables:
        check_names = {item.get("name") for item in db_inspector.get_check_constraints("import_batches")}
        for check_name in ("ck_import_batches_type", "ck_import_batches_status"):
            if check_name not in check_names:
                problems.append(f"import_batches missing check constraint {check_name}")

    if problems:
        raise SchemaVerificationError("; ".join(problems))


def bootstrap_database(db_engine=engine, migration_runner=run_all_migrations):
    print("Checking database...")
    if db_engine.dialect.name != "postgresql":
        raise RuntimeError("Bootstrap requires PostgreSQL")
    with db_engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    all_tables = set(inspect(db_engine).get_table_names())
    existing = MODEL_TABLES.intersection(all_tables)
    if existing:
        if existing == MODEL_TABLES:
            print("MyFit Analytics application tables already exist.")
            print("Database is already initialized; bootstrap is unnecessary.")
            return False
        raise RuntimeError(
            "A partial MyFit Analytics schema already exists; bootstrap stopped without changes"
        )
    if all_tables:
        raise RuntimeError(
            "The target schema is not empty; bootstrap stopped without changes"
        )

    print("Database is empty.")
    print("Creating MyFit Analytics baseline schema...")
    Base.metadata.create_all(bind=db_engine)
    print("Running migrations...")
    migration_runner()
    print("Verifying schema...")
    verify_schema(db_engine)
    print("MyFit Analytics database initialized successfully.")
    return True


def main():
    try:
        bootstrap_database()
    except Exception as exc:
        print(f"Database bootstrap failed ({exc.__class__.__name__}).")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
