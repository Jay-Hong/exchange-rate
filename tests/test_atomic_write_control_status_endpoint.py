"""GET /admin/api/atomic-write-control-status endpoint 테스트 (PR D / P1b A1).

conftest.py harness (firebase stub + DATABASE_URL=file sqlite, collection 전 설정).
lifespan(scheduler) 미진입 (TestClient context manager 미사용).

검증: seeded 200 success / row 부재 200 error(row_missing) / read 예외 200 error(never-crash) /
SessionLocal() 예외 200 error(never-crash) / writer_enforced·enforced_action은 writer가 실제 gate하는
cached snapshot 기준 파생 (legacy→false, atomic/halt→true — A2 land 후 writer가 control consume).
"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

# conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 설정.
from app import models
from app.atomic_write_control import bootstrap_atomic_write_control
from app.database import SessionLocal, engine
from app.main import app, verify_admin

models.Base.metadata.create_all(engine)


class TestAtomicWriteControlStatusEndpoint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # admin auth 우회 (endpoint 로직만 검증)
        app.dependency_overrides[verify_admin] = lambda: "admin"
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(verify_admin, None)

    def _seed(self):
        db = SessionLocal()
        try:
            bootstrap_atomic_write_control(db)
        finally:
            db.close()

    def _clear(self):
        db = SessionLocal()
        try:
            db.query(models.AtomicWriteControl).delete()
            db.commit()
        finally:
            db.close()

    def test_control_table_excluded_from_create_all_helper(self):
        # migration-first 코드 보장 (#1): 운영 진입점 공유 create_all helper가 control table 제외
        # → import/script 시점 운영 PG로 신규 CHECK DDL emit 안 함 (A1 behavior-change-0).
        from app.database import CREATE_ALL_EXCLUDE_TABLES
        self.assertIn("atomic_write_control", CREATE_ALL_EXCLUDE_TABLES)

    def test_success_when_seeded(self):
        self._seed()
        r = self.client.get("/admin/api/atomic-write-control-status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "success")
        self.assertTrue(body["control_available"])
        self.assertEqual(body["effective_mode"], "legacy")
        self.assertEqual(body["control"]["requested_mode"], "legacy")
        self.assertIsNone(body["control"]["activated_at"])  # 활성화 전 None
        self.assertIsNotNone(body["preflight"])
        self.assertFalse(body["writer_enforced"])
        self.assertIsNone(body["control_read_error"])

    def test_row_missing_returns_200_error(self):
        self._clear()
        try:
            r = self.client.get("/admin/api/atomic-write-control-status")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["status"], "error")
            self.assertFalse(body["control_available"])
            self.assertEqual(body["control_read_error"]["reason"], "row_missing")
            self.assertFalse(body["writer_enforced"])
            # fail-closed 진단: row 없으면 effective_mode=halt + preflight failed 노출
            self.assertEqual(body["effective_mode"], "halt")
            self.assertIsNotNone(body["preflight"])
            self.assertFalse(body["preflight"]["passed"])
        finally:
            self._seed()  # 후속 테스트 위해 복구

    def test_never_crash_on_read_exception(self):
        self._seed()
        with patch("app.atomic_write_control.read_control_row", side_effect=RuntimeError("boom")):
            r = self.client.get("/admin/api/atomic-write-control-status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["control_read_error"]["reason"], "RuntimeError")
        self.assertFalse(body["writer_enforced"])
        self.assertEqual(body["effective_mode"], "halt")  # read 예외도 fail-closed halt

    def test_writer_enforced_derives_from_snapshot_atomic(self):
        # A2 land 후 writer는 control을 consume — snapshot enforced_action=atomic이면 writer_enforced=true
        # + enforced_action 노출 (control row read와 독립적인 cached snapshot 기준).
        from types import SimpleNamespace
        from app.atomic_write_control import WriterMode
        self._seed()
        with patch("app.atomic_write_runtime.snapshot",
                   return_value=SimpleNamespace(enforced_action=WriterMode.ATOMIC)):
            r = self.client.get("/admin/api/atomic-write-control-status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["writer_enforced"])
        self.assertEqual(body["enforced_action"], WriterMode.ATOMIC)

    def test_writer_enforced_false_when_snapshot_legacy(self):
        # legacy snapshot → pass-through → writer_enforced=false (현 운영 상태와 동일, 회귀 잠금).
        from types import SimpleNamespace
        from app.atomic_write_control import WriterMode
        self._seed()
        with patch("app.atomic_write_runtime.snapshot",
                   return_value=SimpleNamespace(enforced_action=WriterMode.LEGACY)):
            r = self.client.get("/admin/api/atomic-write-control-status")
        body = r.json()
        self.assertFalse(body["writer_enforced"])
        self.assertEqual(body["enforced_action"], WriterMode.LEGACY)

    def test_writer_enforced_false_when_snapshot_call_fails(self):
        # snapshot() 호출 자체가 raise해도 never-crash — enforced_action=None, writer_enforced=false.
        self._seed()
        with patch("app.atomic_write_runtime.snapshot", side_effect=RuntimeError("boom")):
            r = self.client.get("/admin/api/atomic-write-control-status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNone(body["enforced_action"])
        self.assertFalse(body["writer_enforced"])

    def test_never_crash_on_session_open_failure(self):
        # SessionLocal() 자체 예외도 never-crash (db open이 try 안 — finding #2 fix)
        with patch("app.main.SessionLocal", side_effect=RuntimeError("pool exhausted")):
            r = self.client.get("/admin/api/atomic-write-control-status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "error")
        self.assertFalse(body["control_available"])
        self.assertEqual(body["control_read_error"]["reason"], "RuntimeError")
        self.assertEqual(body["effective_mode"], "halt")  # session 실패도 fail-closed halt


if __name__ == "__main__":
    unittest.main()
