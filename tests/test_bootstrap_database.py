import os
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text


# This test module must never inherit or connect to an externally configured database.
os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SESSION_COOKIE_SECURE"] = "false"

from app.database import Base  # noqa: E402
from app import models  # noqa: E402,F401
from app.bootstrap_database import (  # noqa: E402
    application_tables,
    verify_schema,
)


class BootstrapSchemaTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")

    def tearDown(self):
        self.engine.dispose()

    def test_empty_database_has_no_application_tables(self):
        self.assertEqual(application_tables(self.engine), set())

    def test_metadata_creates_all_model_tables_and_verifies_core_schema(self):
        Base.metadata.create_all(self.engine)
        self.assertEqual(application_tables(self.engine), set(Base.metadata.tables))
        with self.engine.connect() as connection:
            for table_name in Base.metadata.tables:
                count = connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar_one()
                self.assertEqual(count, 0)
        # SQLite cannot reflect expression-based indexes; PostgreSQL verification
        # checks those in addition to this portable core-schema test.
        with patch(
            "app.bootstrap_database.REQUIRED_UNIQUE_OBJECTS",
            {"action_status": "uq_action_status_studio_member_type"},
        ):
            verify_schema(self.engine)

    def test_unrelated_table_is_not_misidentified_as_myfit(self):
        with self.engine.begin() as connection:
            connection.execute(text("CREATE TABLE unrelated_table (id INTEGER PRIMARY KEY)"))
        self.assertEqual(application_tables(self.engine), set())


if __name__ == "__main__":
    unittest.main()
