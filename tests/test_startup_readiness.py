import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

os.environ["APP_ENV"]="production";os.environ["DATABASE_URL"]="sqlite:///:memory:";os.environ["SESSION_SECRET"]="offline-test-secret-at-least-32-characters";os.environ["SESSION_COOKIE_SECURE"]="true"
from app import main
from app.database import DATABASE_CONNECT_TIMEOUT_SECONDS, database_engine_options


ROOT=Path(__file__).resolve().parents[1]


class StartupReadinessTests(unittest.TestCase):
    def test_web_import_never_connects_or_creates_schema(self):
        code="""
import socket
from sqlalchemy.sql.schema import MetaData
socket.socket.connect=lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('network connection during import'))
MetaData.create_all=lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('schema creation during import'))
import app.main
print('IMPORT OK')
"""
        environment=os.environ.copy();environment.update({"APP_ENV":"production","DATABASE_URL":"postgresql://test:test@127.0.0.1:1/test","SESSION_SECRET":"offline-test-secret-at-least-32-characters","SESSION_COOKIE_SECURE":"true"})
        result=subprocess.run([sys.executable,"-c",code],cwd=ROOT,env=environment,text=True,capture_output=True,timeout=15,check=False)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),"IMPORT OK")

    def test_startup_and_health_do_not_require_database(self):
        unavailable=OperationalError("connection",{},Exception("postgresql://user:secret@example.test/db"))
        with patch.object(main.engine,"connect",side_effect=unavailable) as connect:
            with TestClient(main.app) as client:
                response=client.get("/health")
        self.assertEqual(response.status_code,200);self.assertEqual(response.json(),{"status":"ok"});connect.assert_not_called()

    def test_ready_returns_503_without_leaking_connection_details(self):
        secret="postgresql://private-user:private-password@example.test/private-db"
        with patch.object(main.engine,"connect",side_effect=OperationalError("connection",{},Exception(secret))):
            with TestClient(main.app) as client: response=client.get("/ready")
        self.assertEqual(response.status_code,503);self.assertEqual(response.json(),{"status":"not_ready"})
        self.assertNotIn("private-user",response.text);self.assertNotIn("private-password",response.text);self.assertNotIn("private-db",response.text)

    def test_ready_returns_200_when_database_is_available(self):
        connection=MagicMock();context=MagicMock();context.__enter__.return_value=connection
        with patch.object(main.engine,"connect",return_value=context):
            with TestClient(main.app) as client: response=client.get("/ready")
        self.assertEqual(response.status_code,200);self.assertEqual(response.json(),{"status":"ready"});connection.execute.assert_called_once()

    def test_postgresql_timeout_is_bounded_and_sqlite_has_no_postgres_args(self):
        postgres=database_engine_options("postgresql://test:test@example.test/database")
        sqlite=database_engine_options("sqlite:///:memory:")
        self.assertEqual(postgres["connect_args"]["connect_timeout"],DATABASE_CONNECT_TIMEOUT_SECONDS)
        self.assertGreaterEqual(DATABASE_CONNECT_TIMEOUT_SECONDS,5);self.assertLessEqual(DATABASE_CONNECT_TIMEOUT_SECONDS,10)
        self.assertTrue(postgres["pool_pre_ping"]);self.assertEqual(sqlite,{"pool_pre_ping":True})


if __name__=="__main__": unittest.main()
