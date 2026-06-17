"""P1b A2-2 — bank/investing writer write-mode gate + refresh wiring 테스트.

conftest.py가 firebase stub + DATABASE_URL=sqlite 처리 (collection 전).
mock atomic_write_runtime.snapshot로 enforced_action별 writer 동작 검증:
- legacy → gate 통과(_stage 호출, 기존 흐름) / halt·atomic → gate 차단(_stage 미호출 + return 0).
- _stage_*를 side_effect=AssertionError로 patch해 staging 미호출 직접 검증 (codex).
- refresh wrapper no-throw / config validation(subprocess) / runner·scheduler wiring static.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import crud
from app.atomic_write_control import WriterMode
from app.atomic_write_runtime import WriteModeSnapshot


def _snap(enforced: str) -> WriteModeSnapshot:
    """gate는 enforced_action만 읽음 — 나머지 필드는 cosmetic."""
    return WriteModeSnapshot(
        diagnostic_effective_mode=enforced,
        activation_latched=(enforced != WriterMode.LEGACY),
        enforced_action=enforced,
        mode_generation=0,
    )


class TestBankGate(unittest.TestCase):

    def test_legacy_proceeds_to_staging(self):
        db = MagicMock()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)), \
             patch("app.crud._stage_bank_rate_changes", return_value=[]) as stage:
            result = crud.insert_bank_rates_into_db(db, {"usd-krw": 1300.0}, "kb")
        stage.assert_called_once()          # gate 통과 → staging 호출(기존 흐름 진입)
        self.assertEqual(result, 0)         # 빈 changes → count 0
        self.assertFalse(db.commit.called)

    def test_halt_skips_staging(self):
        db = MagicMock()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.HALT)), \
             patch("app.crud._stage_bank_rate_changes", side_effect=AssertionError("staging 호출됨")):
            result = crud.insert_bank_rates_into_db(db, {"usd-krw": 1300.0}, "kb")
        self.assertEqual(result, 0)         # halt → write 0
        self.assertFalse(db.add.called)
        self.assertFalse(db.commit.called)

    def test_atomic_skips_staging(self):
        db = MagicMock()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.ATOMIC)), \
             patch("app.crud._stage_bank_rate_changes", side_effect=AssertionError("staging 호출됨")):
            result = crud.insert_bank_rates_into_db(db, {"usd-krw": 1300.0}, "kb")
        self.assertEqual(result, 0)         # atomic(A3 전 fail-closed) → write 0, legacy fallback 아님
        self.assertFalse(db.commit.called)

    def test_skip_counter_increments(self):
        crud._write_mode_skip_counts.clear()
        db = MagicMock()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.HALT)), \
             patch("app.crud._stage_bank_rate_changes", side_effect=AssertionError):
            crud.insert_bank_rates_into_db(db, {"usd-krw": 1300.0}, "kb")
        self.assertEqual(crud._write_mode_skip_counts.get(("kb", WriterMode.HALT)), 1)


class TestInvestingGate(unittest.TestCase):

    def test_legacy_proceeds_to_staging(self):
        db = MagicMock()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)), \
             patch("app.crud._stage_investing_rate_changes", return_value=[]) as stage:
            result = crud.insert_investing_rates_into_db(db, {"usd-krw": 1300.0})
        stage.assert_called_once()
        self.assertEqual(result, 0)

    def test_halt_skips_staging(self):
        db = MagicMock()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.HALT)), \
             patch("app.crud._stage_investing_rate_changes", side_effect=AssertionError):
            result = crud.insert_investing_rates_into_db(db, {"usd-krw": 1300.0})
        self.assertEqual(result, 0)
        self.assertFalse(db.commit.called)

    def test_atomic_skips_staging(self):
        db = MagicMock()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.ATOMIC)), \
             patch("app.crud._stage_investing_rate_changes", side_effect=AssertionError):
            result = crud.insert_investing_rates_into_db(db, {"usd-krw": 1300.0})
        self.assertEqual(result, 0)


class TestRefreshWrapper(unittest.TestCase):

    def test_no_throw_on_session_failure(self):
        from app import atomic_write_refresh
        with patch("app.database.SessionLocal", side_effect=RuntimeError("boom")):
            atomic_write_refresh.refresh_write_mode_cache()   # 예외 전파 0

    def test_calls_refresh_from_db_and_closes(self):
        from app import atomic_write_refresh
        with patch("app.database.SessionLocal") as sl, \
             patch("app.atomic_write_runtime.refresh_from_db") as rfd:
            atomic_write_refresh.refresh_write_mode_cache()
            rfd.assert_called_once()
            sl.return_value.close.assert_called_once()


class TestConfigValidation(unittest.TestCase):

    def test_poll_interval_zero_rejected(self):
        # config validation은 import 시 — subprocess로 격리 (reload 오염 회피)
        env = dict(os.environ, ATOMIC_MODE_POLL_INTERVAL_SECONDS="0")
        proc = subprocess.run(
            [sys.executable, "-c", "import app.config"],
            env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ATOMIC_MODE_POLL_INTERVAL_SECONDS", proc.stderr)


class TestWiringStatic(unittest.TestCase):

    def _src(self, *parts: str) -> str:
        return (Path(__file__).resolve().parent.parent.joinpath(*parts)).read_text(encoding="utf-8")

    def test_runner_refreshes_before_crawler_dispatch(self):
        # subprocess refresh가 crawler dispatch(CRAWLER_MAP.get) *전*에 호출돼야 (mode 읽고 크롤).
        src = self._src("app", "crawlers", "runner.py")
        self.assertIn("refresh_write_mode_cache", src)
        self.assertIn("CRAWLER_MAP.get", src)
        # rindex = 마지막 occurrence(= 호출부), import가 아니라 실제 call이 dispatch 전인지.
        self.assertLess(
            src.rindex("refresh_write_mode_cache()"), src.index("CRAWLER_MAP.get"),
            "subprocess refresh() 호출이 crawler dispatch 전에 있어야 함",
        )

    def test_scheduler_wires_refresh_and_poll(self):
        # substring이 아니라 AST로 start_scheduler 내 실제 call/job 등록 검증 (codex Low).
        import ast
        tree = ast.parse(self._src("app", "scheduler.py"))
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "start_scheduler"),
            None,
        )
        self.assertIsNotNone(fn, "start_scheduler 함수 없음")
        calls_refresh = False
        registers_poll = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "refresh_write_mode_cache":
                    calls_refresh = True  # startup 1회 호출
                for kw in node.keywords:
                    if kw.arg == "id" and isinstance(kw.value, ast.Constant) and kw.value.value == "atomic_write_mode_poll":
                        registers_poll = True  # poll job 등록
        self.assertTrue(calls_refresh, "start_scheduler가 refresh_write_mode_cache() startup 호출 안 함")
        self.assertTrue(registers_poll, "start_scheduler가 atomic_write_mode_poll job 등록 안 함")


if __name__ == "__main__":
    unittest.main()
