"""USDT/KRX Redis writer — mode-INDEPENDENT 계약 (구 P1b A2-3 write-mode gate 제거).

배경: FX atomic cutover(prod live)의 전역 writer mode가 USDT/KRX source Redis writer까지
BLOCK시켜 iOS 테스트 테더 탭이 freeze됐다. 수정 = USDT/KRX Redis-latest writer를
mode-independent로 (FX writer mode 미참조, 항상 v1 write). 근거: latest:source:*는 FX atomic
keyspace와 **disjoint** — atomic loader는 bank/investing만 read, atomic mirror는 source loop를
allowlist-skip이라 KRX/USDT key를 안 읽고 안 쓴다 → v1 SET이 §11 FX v2 invariant를 깰 경로 없음.
DB writer(insert_source_rate_*)도 이미 mode-independent라 Redis도 정렬.

이 파일이 잠그는 것 (구 gate-lock 테스트를 invert):
- 3 Redis setter(usdt / krx base 598 / krx tick 675)가 legacy·halt·atomic 모두 v1 SET 진행
  (BLOCKED/False 미반환) — TestUsdt/KrxTick/KrxBase ModeIndependent.
- KRX routine(KRX_REDIS_TICK_WRITE_ENABLED=false) _sync_db_write가 halt/atomic서도 Redis write
  진행 (구 krx_kis.py 4번째 gate 제거) — TestKrxRoutineFlagFalseModeIndependent.
- structural-AST NEGATIVE trip-wire: 4 writer body에 write-mode 심볼 0 — 미래 PR이 전역 FX mode
  gate를 몰래 재결합 못 하게 (DB-side TestSourceDbWritersNoWriteModeSymbols 대칭). 재결합 시
  P1_COMMON_BASE_DESIGN §394/§401-402 ledger를 먼저 갱신해야 함.
- BLOCKED enum / _record_write_mode_block helper는 dormant scaffolding으로 보존(미래 per-source
  cutover gate용) — polling helper의 BLOCKED 방어 분기도 보존(TestUsdtSourcesPollingDefensive).
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import unittest
from unittest.mock import MagicMock, patch

from app import latest_rates_cache as lrc
from app.atomic_write_control import WriterMode
from app.atomic_write_runtime import WriteModeSnapshot

_TS = "2026-06-17T10:00:00+09:00"
_ALL_MODES = (WriterMode.LEGACY, WriterMode.HALT, WriterMode.ATOMIC)


def _snap(enforced: str) -> WriteModeSnapshot:
    return WriteModeSnapshot(
        diagnostic_effective_mode=enforced,
        activation_latched=(enforced != WriterMode.LEGACY),
        enforced_action=enforced,
        mode_generation=0,
    )


def _fresh_client() -> MagicMock:
    """cold-start client — get()=None이라 coalesce/regression 오염 없이 항상 SET 경로."""
    client = MagicMock()
    client.get.return_value = None
    return client


class TestUsdtModeIndependent(unittest.TestCase):
    """USDT setter는 legacy·halt·atomic 모두 v1 SET 진행 (mode-independent)."""

    def test_all_modes_proceed_to_set(self):
        for mode in _ALL_MODES:
            with self.subTest(mode=mode):
                lrc._last_written_usdt_state.clear()  # state-pollution 가드 (codex)
                client = _fresh_client()
                with patch("app.atomic_write_runtime.snapshot", return_value=_snap(mode)), \
                     patch("app.latest_rates_cache._get_sync_client", return_value=client) as gsc:
                    outcome = lrc.set_latest_usdt_rate_from_sync_job("upbit", "usdt-krw", 1300.0, _TS)
                gsc.assert_called_once()  # gate 없음 — 모든 mode에서 Redis I/O 진행
                self.assertEqual(outcome, lrc.UsdtLatestWriteOutcome.SET)
                client.set.assert_called_once()
                # v1 schema (5-field usdt) — atomic v2 marker 미포함
                _, written = client.set.call_args[0]
                self.assertNotIn('"schema_version"', written)


class TestKrxTickModeIndependent(unittest.TestCase):
    """KRX tick-level setter는 legacy·halt·atomic 모두 v1 SET 진행."""

    def test_all_modes_proceed_to_set(self):
        for mode in _ALL_MODES:
            with self.subTest(mode=mode):
                lrc._last_written_krx_state.clear()
                client = _fresh_client()
                with patch("app.atomic_write_runtime.snapshot", return_value=_snap(mode)), \
                     patch("app.latest_rates_cache._get_sync_client", return_value=client) as gsc:
                    outcome = lrc.set_latest_krx_rate_from_sync_job_tick_level("usd-krw-futures", 1500.0, _TS)
                gsc.assert_called_once()
                self.assertEqual(outcome, lrc.KrxLatestWriteOutcome.SET)
                client.set.assert_called_once()
                _, written = client.set.call_args[0]
                self.assertNotIn('"schema_version"', written)


class TestKrxBaseSetterModeIndependent(unittest.TestCase):
    """KRX base setter(598, close finalizer/REST/routine 공유)는 legacy·halt·atomic 모두 진행."""

    def test_all_modes_proceed_to_set(self):
        for mode in _ALL_MODES:
            with self.subTest(mode=mode):
                client = MagicMock()  # base setter는 state/get 미사용 — 직접 set
                with patch("app.atomic_write_runtime.snapshot", return_value=_snap(mode)), \
                     patch("app.latest_rates_cache._get_sync_client", return_value=client) as gsc:
                    result = lrc.set_latest_krx_rate_from_sync_job("usd-krw-futures", 1500.0, _TS)
                gsc.assert_called_once()
                self.assertTrue(result)  # bool 계약 — 모든 mode에서 True
                client.set.assert_called_once()
                _, written = client.set.call_args[0]
                self.assertNotIn('"schema_version"', written)

    def test_client_none_returns_false_all_modes(self):
        # client init 실패만 False — mode와 무관.
        for mode in _ALL_MODES:
            with self.subTest(mode=mode):
                with patch("app.atomic_write_runtime.snapshot", return_value=_snap(mode)), \
                     patch("app.latest_rates_cache._get_sync_client", return_value=None):
                    result = lrc.set_latest_krx_rate_from_sync_job("usd-krw-futures", 1500.0, _TS)
                self.assertFalse(result)


class TestKrxRoutineFlagFalseModeIndependent(unittest.TestCase):
    """KRX_REDIS_TICK_WRITE_ENABLED=false(Stage E rollback) routine Redis도 mode-independent.

    구 krx_kis.py 4번째 gate(_sync_db_write flag=false 분기) 제거 — halt/atomic서도
    write_after_db_insert 진행. flag=true(prod) 분기는 tick-level handler가 담당(여기 무관).
    """

    _TICK = {"price": "1500.0", "source": "krx", "asset": "usd-krw-futures"}

    def _run(self, mode):
        from app.crawlers import krx_kis
        with patch("app.crawlers.krx_kis.config.KRX_REDIS_TICK_WRITE_ENABLED", False), \
             patch("app.atomic_write_runtime.snapshot", return_value=_snap(mode)), \
             patch("app.crud.insert_source_rate_if_changed", return_value=True), \
             patch.object(krx_kis.KrxRedisLatestWriter, "write_after_db_insert",
                          return_value=True) as wadi:
            result = krx_kis.KrxDbWriter._sync_db_write(self._TICK)
        return result, wadi

    def test_all_modes_proceed_to_routine_redis(self):
        for mode in _ALL_MODES:
            with self.subTest(mode=mode):
                result, wadi = self._run(mode)
                wadi.assert_called_once()  # 모든 mode에서 routine Redis 진행 (gate 제거)
                self.assertTrue(result)


class TestUsdtSourcesPollingDefensive(unittest.TestCase):
    """polling helper의 BLOCKED 방어 분기 보존 — setter가 더는 BLOCKED 미반환(dormant)이나,
    미래 per-source cutover gate가 BLOCKED를 도입하면 success 오인 방지 분기가 필요하므로 유지."""

    def test_blocked_returns_false_not_success(self):
        from app.crawlers import usdt_sources
        latest = {"rate": 1300.0, "timestamp": _TS}
        with patch("app.crawlers.usdt_sources.crud.get_latest_source_rate", return_value=latest), \
             patch("app.crawlers.usdt_sources.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
                   return_value=lrc.UsdtLatestWriteOutcome.BLOCKED):
            result = usdt_sources._mirror_changed_source_to_redis(MagicMock(), "upbit", "usdt-krw")
        self.assertFalse(result)  # BLOCKED(가정적) → not success


class TestUsdtKrxRedisWritersNoWriteModeSymbols(unittest.TestCase):
    """structural-AST NEGATIVE trip-wire — USDT/KRX Redis writer body에 write-mode 심볼 0.

    DB-side TestSourceDbWritersNoWriteModeSymbols(C6-5b-5)의 Redis-side 대칭. 미래 PR이 전역 FX
    write-mode gate를 이 writer들에 몰래 재결합하면 이 lock이 잡고, P1_COMMON_BASE_DESIGN
    §394/§401-402 ledger(usdt/krx Redis mode-independent)를 먼저 갱신하도록 강제한다.

    _FORBIDDEN은 DB-side set + 우회형 gate 방어용 확장(codex): is_initialized / atomic_write_control
    / WriteModeSnapshot 추가.
    """

    _FORBIDDEN = {
        "snapshot", "enforced_action", "WriterMode", "atomic_write_runtime",
        "_record_write_mode_skip", "_record_write_mode_block",
        "is_initialized", "atomic_write_control", "WriteModeSnapshot",
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

    def test_redis_writers_contain_no_write_mode_symbols(self):
        from app.crawlers.krx_kis import KrxDbWriter
        targets = [
            lrc.set_latest_usdt_rate_from_sync_job,
            lrc.set_latest_krx_rate_from_sync_job,
            lrc.set_latest_krx_rate_from_sync_job_tick_level,
            KrxDbWriter._sync_db_write,  # 구 4번째 gate 위치 — 재결합 방지
        ]
        for fn in targets:
            leaked = self._syms(fn) & self._FORBIDDEN
            self.assertEqual(
                leaked, set(),
                f"{fn.__qualname__} body에 write-mode 심볼 {leaked} — USDT/KRX Redis writer는 "
                "mode-independent로 잠금. 전역 FX mode gate 재결합 시 §394/§401-402 ledger를 먼저 갱신.",
            )


if __name__ == "__main__":
    unittest.main()
