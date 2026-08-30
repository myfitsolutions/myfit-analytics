import json
import os
import unittest
from unittest.mock import patch

from sqlalchemy.exc import ProgrammingError
from starlette.requests import Request


os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SESSION_COOKIE_SECURE"] = "false"

from app.main import production_safety_middleware  # noqa: E402


def request_with_id(request_id):
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/imports",
        "raw_path": b"/imports",
        "query_string": b"",
        "headers": [(b"x-request-id", request_id.encode())],
        "client": ("127.0.0.1", 1),
        "server": ("test", 443),
    })


class DriverDiagnostics:
    sqlstate = "42703"
    message_primary = 'column studio_data_sources.example does not exist'
    table_name = "studio_data_sources"
    column_name = "example"
    constraint_name = None


class DriverError(Exception):
    pgcode = "42703"
    diag = DriverDiagnostics()


class ProgrammingErrorLoggingTests(unittest.IsolatedAsyncioTestCase):
    request_id = "9e34a76c-74be-458f-a1a7-bdd4f23595af"

    async def test_programming_error_logs_structured_diagnostics_without_sensitive_context(self):
        error = ProgrammingError(
            "SELECT * FROM users WHERE password = %(password)s",
            {
                "password": "do-not-log-this-password",
                "database_url": "postgresql://user:do-not-log@database.example/app",
            },
            DriverError("driver detail"),
        )

        async def fail(_request):
            raise error

        with patch("builtins.print") as mocked_print:
            response = await production_safety_middleware(
                request_with_id(self.request_id), fail
            )

        log_line = mocked_print.call_args.args[0]
        self.assertIn("Unhandled ProgrammingError", log_line)
        self.assertIn(f"request_id={self.request_id}", log_line)
        diagnostics = json.loads(log_line.split("diagnostics=", 1)[1])
        self.assertEqual(diagnostics["sqlstate"], "42703")
        self.assertEqual(
            diagnostics["message_primary"],
            "column studio_data_sources.example does not exist",
        )
        self.assertEqual(diagnostics["table_name"], "studio_data_sources")
        self.assertEqual(diagnostics["column_name"], "example")
        self.assertNotIn("do-not-log", log_line)
        self.assertNotIn("postgresql://", log_line)
        self.assertNotIn("SELECT *", log_line)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            json.loads(response.body),
            {
                "detail": "An unexpected error occurred",
                "request_id": self.request_id,
            },
        )

    async def test_programming_error_without_driver_diagnostics_uses_safe_summary(self):
        error = ProgrammingError(
            "SELECT secret FROM private_table",
            {"token": "do-not-log-this-token"},
            RuntimeError("postgresql://user:password@database.example/app"),
        )

        async def fail(_request):
            raise error

        with patch("builtins.print") as mocked_print:
            await production_safety_middleware(request_with_id(self.request_id), fail)

        log_line = mocked_print.call_args.args[0]
        self.assertIn('diagnostics={"summary": "RuntimeError"}', log_line)
        self.assertNotIn("private_table", log_line)
        self.assertNotIn("do-not-log", log_line)
        self.assertNotIn("postgresql://", log_line)

    async def test_other_exception_logging_and_generic_response_are_unchanged(self):
        async def fail(_request):
            raise ValueError("sensitive request content")

        with patch("builtins.print") as mocked_print:
            response = await production_safety_middleware(
                request_with_id(self.request_id), fail
            )

        mocked_print.assert_called_once_with(
            f"Unhandled ValueError request_id={self.request_id}"
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            json.loads(response.body),
            {
                "detail": "An unexpected error occurred",
                "request_id": self.request_id,
            },
        )


if __name__ == "__main__":
    unittest.main()
