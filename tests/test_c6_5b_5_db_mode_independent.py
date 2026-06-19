"""P1b C6-5b-5 — USDT/KRX DB-side mode-independence LOCK (behavior-change-0).

C6-5b 트랙 마지막 sub-step. write-mode gating topology의 비대칭을 잠근다:
- bank/investing DB는 orchestrator-top gate라 MODE-DEPENDENT (halt→skip+return 0 / atomic→v2 commit) — a2_2가 잠금.
- usdt/krx source DB writer(`insert_source_rate_if_changed` / `insert_source_rate_unconditional`)는 write-mode
  미참조 → 모든 모드에서 DB write 진행 = MODE-INDEPENDENT. atomic flip 시 usdt/krx Redis(`latest:source:*`)는
  A2-3/C6-5b-2로 BLOCKED + mirror(C6-5b-4) source skip이라, DB canonical이 Redis miss/evict 시 유일 fresh
  recovery rail (P1_COMMON_BASE_DESIGN §392 결정).

DRY: bool/dedup/timestamp contract는 test_crud_insert_source_rate_unconditional.py가 real-DB로 잠금 →
여기선 **patched-snapshot(HALT/ATOMIC) 차원만** 추가(row가 여전히 land) + function-scoped 구조 lock.
per-source 중복 금지 — 5 USDT WS writer + KrxDbWriter는 전부 공용 crud fn에 위임. snapshot 패치 idiom은
a2_2/a2_3의 `_snap`, AST `_syms`는 test_atomic_write_runtime 패턴 재사용.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, models
from app.atomic_write_control import WriterMode
from app.atomic_write_runtime import WriteModeSnapshot


def _snap(enforced: str) -> WriteModeSnapshot:
    """a2_2/a2_3 idiom — gate는 enforced_action만 읽음."""
    return WriteModeSnapshot(
        diagnostic_effective_mode=enforced,
        activation_latched=(enforced != WriterMode.LEGACY),
        enforced_action=enforced,
        mode_generation=0,
    )


class TestSourceDbWritesModeIndependent(unittest.TestCase):
    """usdt/krx source DB writer는 halt/atomic에서도 real-DB INSERT 수행 (mode-independent).

    각 mode마다 fresh in-memory SQLite — insert_source_rate_if_changed는 change-only라
    같은 rate 재삽입 시 dedup-skip되므로 격리 필수.
    """

    def _run_under_mode(self, mode, writer_call):
        engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            with patch("app.atomic_write_runtime.snapshot", return_value=_snap(mode)):
                result = writer_call(db)
            return result, db.query(models.SourceRate).all()
        finally:
            db.close()
            engine.dispose()

    def test_insert_source_rate_if_changed_inserts_under_all_modes(self):
        # CASE 1 — 5 USDT WS writer + KrxDbWriter routine이 위임하는 공용 unit.
        # legacy/halt/atomic 전수 — 같은 결과(row land) = mode-independence가 self-evident.
        # (legacy real-DB write contract 자체는 test_crud_insert_source_rate_unconditional가 잠금 —
        #  여기 legacy는 "mode와 무관" 명시용 1 iteration.)
        for mode in (WriterMode.LEGACY, WriterMode.HALT, WriterMode.ATOMIC):
            with self.subTest(mode=mode):
                ok, rows = self._run_under_mode(
                    mode, lambda db: crud.insert_source_rate_if_changed(db, "upbit", "usdt-krw", 1500.0)
                )
                self.assertTrue(ok, f"{mode}: DB write가 mode로 차단되면 안 됨")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].source, "upbit")
                self.assertEqual(rows[0].rate, 1500.0)

    def test_insert_source_rate_unconditional_inserts_under_all_modes(self):
        # CASE 2 — close finalizer DB path (KrxCloseWindowWriter). legacy/halt/atomic 전수.
        ts = datetime(2026, 6, 5, 6, 45, 0)  # UTC naive
        for mode in (WriterMode.LEGACY, WriterMode.HALT, WriterMode.ATOMIC):
            with self.subTest(mode=mode):
                ok, rows = self._run_under_mode(
                    mode,
                    lambda db: crud.insert_source_rate_unconditional(
                        db, "krx", "usd-krw-futures", 1490.6, timestamp=ts
                    ),
                )
                self.assertTrue(ok)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].source, "krx")
                self.assertEqual(rows[0].timestamp, ts)

    def test_bank_orchestrator_is_mode_dependent_contrast(self):
        # 대조 anchor: 동일 HALT에서 bank orchestrator는 staging 진입 안 함(DB write 0) — 비대칭 의도 확인.
        # (gate 통과/차단 자체는 a2_2 소유 — 여기선 DB-side 비대칭 대조만, _stage mock으로 staging-진입 차단 확인.)
        from unittest.mock import MagicMock
        db = MagicMock()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.HALT)), \
             patch("app.crud._stage_bank_rate_changes", side_effect=AssertionError("staging 진입됨")):
            result = crud.insert_bank_rates_into_db(db, {"usd-krw": 1300.0}, "kb")
        self.assertEqual(result, 0)          # halt → DB+Redis 동시 정지 (source와 비대칭)
        self.assertFalse(db.add.called)


class TestSourceDbWritersNoWriteModeSymbols(unittest.TestCase):
    """structural-ast NEGATIVE trip-wire — 두 source DB writer body에 write-mode 심볼 0.

    bank/investing(a2_2) + mirror(test_atomic_write_runtime)의 POSITIVE trip-wire와 대칭.
    **function-scoped** (crud.py module-top은 bank/inv용으로 WriterMode/atomic_write_runtime을 정당하게
    import해 :488/:675에서 씀 — module 전체 scan은 false-positive). 미래 PR이 DB-side gate를 몰래 추가하면
    이 lock이 잡고, §392 결정을 먼저 갱신하도록 강제한다.
    """

    _FORBIDDEN = {
        "snapshot", "enforced_action", "WriterMode", "atomic_write_runtime",
        "_record_write_mode_skip", "_record_write_mode_block",
    }

    @staticmethod
    def _syms(fn) -> set:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                out.add(node.id)
            elif isinstance(node, ast.Attribute):
                out.add(node.attr)
        return out

    def test_source_db_writers_contain_no_write_mode_symbols(self):
        for fn in (crud.insert_source_rate_if_changed, crud.insert_source_rate_unconditional):
            leaked = self._syms(fn) & self._FORBIDDEN
            self.assertEqual(
                leaked, set(),
                f"{fn.__name__} body에 write-mode 심볼 {leaked} — C6-5b-5는 usdt/krx DB-side를 "
                "mode-independent로 잠금. DB-side gate 추가 시 §392 결정을 먼저 갱신해야 함.",
            )


if __name__ == "__main__":
    unittest.main()
