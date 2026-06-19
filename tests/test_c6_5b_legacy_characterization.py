"""P1b C6-5b-1 — legacy characterization lock for the C6-5b writer surface (additive, prod code 무변경).

C6-5b가 곧 건드릴 live writer들의 **legacy 관측 계약**을 변경 前에 박제 — 이후 high-risk 변경
(C6-5b-2 KRX :597 gate / C6-5b-3 bank·investing direct atomicize / C6-5b-4 mirror atomicize)이 legacy를
조용히 회귀시키지 못하게 하는 안전망. **순수 additive**: production code 무변경, mirror AST trip-wire
(test_atomic_write_runtime.py)도 무변경 — trip-wire 교체는 mirror가 실제 write-mode-aware해지는 C6-5b-4에서.

기존 lock과 DRY (중복 작성 안 함):
- USDT/KRX tick gate(legacy passes / halt·atomic BLOCKED + Redis I/O 0) + KRX :597 ungated-on-halt +
  BLOCKED counter + polling BLOCKED≠success → tests/test_atomic_write_a2_3.py
- bank/investing write-mode gate(legacy passes / halt·atomic _stage 미호출 + return 0) → tests/test_atomic_write_a2_2.py
- bank/investing serial order(commit→Redis→alerts) + payload shape + commit-fail isolation → tests/test_source_direct_write.py
- mirror 무조건 SET(값 복구, DB-sourced) + topic trigger 미발사 → tests/test_pr_d_set_failure_characterization.py
- mirror **source-skip** (USDT+KRX 전부 skip, source_skipped==3 / attempted==0 / topic-only key 미SET) →
  tests/test_krx_broadcast_filter.py (all-skip 케이스). 본 파일은 standalone 중복 안 하고, invariant 테스트에
  skip을 섞어 '실패+skip 혼재서도 invariant 유지'만 C6-local로 확인.

이 파일이 추가로 잠그는 GAP (위가 미커버, C6-5b 변경 타깃의 잔여 legacy 계약):
1. mirror **invariant under failure** — attempted_total == loaded_total + failed를 **실제 SET 실패 + skip 혼재**
   상황에서 (기존 broadcast_filter는 all-skip 0==0 trivial만 검증) / latest:index는 failed==0 일 때만 갱신
   (failed>0 시 LATEST_INDEX_KEY 미SET) / DXY는 rates invariant와 별도 카테고리. C6-5b-4 mirror atomicize가
   legacy 경로에서 보존해야 할 구조.
2. KRX direct setter set_latest_krx_rate_from_sync_job(:597) **legacy 계약** — client None→False / 실 client→
   True + v1 serialize_value(rate/timestamp/mirrored_at 정확 shape) 1회 set / 예외→False. C6-5b-2가 halt·atomic만
   gate하고 legacy는 이 계약을 유지.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from app import latest_rates_cache as lrc
from app.atomic_write_control import WriterMode
from app.atomic_write_runtime import WriteModeSnapshot

_TS = "2026-06-17T10:00:00+09:00"


def _snap(enforced: str) -> WriteModeSnapshot:
    """a2_3 패턴 — enforced_action별 WriteModeSnapshot. (legacy 경로 명시 + C6-5b-4 mirror gate 후 future-proof.)"""
    return WriteModeSnapshot(
        diagnostic_effective_mode=enforced,
        activation_latched=(enforced != WriterMode.LEGACY),
        enforced_action=enforced,
        mode_generation=0,
    )


class TestMirrorLegacyInvariant(unittest.IsolatedAsyncioTestCase):
    """GAP #1 — mirror invariant (pr_d의 무조건 SET/no-trigger 너머). C6-5b-4가 legacy서 보존해야 할 구조.

    snapshot=legacy 명시 — 현재 mirror는 write-mode 무관(ungated)이라 no-op이나, C6-5b-4가 mode-aware해진 뒤에도
    이 테스트가 'legacy mode invariant'를 특정하도록 future-proof.
    """

    def _mock_crud(self, mock_crud, *, banks=None, investing=None, sources=None, dxy=None):
        mock_crud.SUPPORTED_CURRENCY_PAIRS = ["usd-krw"]
        mock_crud.select_a_latest_investing_rate_from_db.return_value = investing
        mock_crud.select_latest_bank_rates_from_db.return_value = banks or []
        mock_crud.get_source_rates_as_legacy_format.return_value = sources or []
        mock_crud.get_latest_dxy_rate.return_value = dxy

    async def test_attempted_equals_loaded_plus_failed_with_skip(self):
        # 실패+skip 혼재: investing(1) + bank(2, hana SET 실패) + USDT source(skip) →
        # attempted=3(USDT 미포함), loaded=2, failed=1, source_skipped=1, invariant 유지.
        # (기존 test_krx_broadcast_filter는 all-skip 0==0 trivial만 검증 → 실패 동반 invariant가 C6-local gap.)
        banks = [
            {"bank": "kb", "currency": "usd-krw", "rate": 1375.0, "timestamp": _TS},
            {"bank": "hana", "currency": "usd-krw", "rate": 1376.0, "timestamp": _TS},
        ]
        investing = {"currency": "usd-krw", "rate": 1374.0, "timestamp": _TS}
        sources = [{"bank": "upbit", "currency": "usdt-krw", "rate": 1300.0, "timestamp": _TS}]  # topic-only → skip

        async def fake_set(key, value):
            return "hana" not in key  # hana만 실패

        with patch("app.latest_rates_cache.crud") as mc, \
             patch("app.latest_rates_cache._set_latest", side_effect=fake_set), \
             patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)):
            self._mock_crud(mc, banks=banks, investing=investing, sources=sources)
            stats = await lrc._mirror_all_latest(MagicMock())

        self.assertEqual(stats["attempted_total"], 3)        # USDT skip은 attempted 前 단계
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["source_skipped"], 1)         # USDT skip 반영
        self.assertEqual(stats["source"], 0)                 # topic-only 적재 0
        # loaded_total = bank + investing + source (data key 합)
        self.assertEqual(stats["loaded_total"], stats["bank"] + stats["investing"] + stats["source"])
        # 핵심 invariant — 실패+skip 혼재서도 유지
        self.assertEqual(stats["attempted_total"], stats["loaded_total"] + stats["failed"])

    async def test_index_updated_only_when_no_failure(self):
        banks = [{"bank": "kb", "currency": "usd-krw", "rate": 1375.0, "timestamp": _TS}]

        # (a) 전부 성공 → index_updated True
        async def all_ok(key, value):
            return True

        with patch("app.latest_rates_cache.crud") as mc, \
             patch("app.latest_rates_cache._set_latest", side_effect=all_ok), \
             patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)):
            self._mock_crud(mc, banks=banks)
            stats_ok = await lrc._mirror_all_latest(MagicMock())
        self.assertTrue(stats_ok["index_updated"])

        # (b) data SET 실패 → index_updated False + LATEST_INDEX_KEY SET 미시도 (이전 index 보존)
        captured = []

        async def fail_all(key, value):
            captured.append(key)
            return False

        with patch("app.latest_rates_cache.crud") as mc, \
             patch("app.latest_rates_cache._set_latest", side_effect=fail_all), \
             patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)):
            self._mock_crud(mc, banks=banks)
            stats_fail = await lrc._mirror_all_latest(MagicMock())
        self.assertFalse(stats_fail["index_updated"])
        self.assertNotIn(lrc.LATEST_INDEX_KEY, captured)  # failed>0 → index 갱신 차단

    async def test_dxy_separate_category(self):
        # DXY는 별도 카테고리 — dxy_* 카운터에만 반영, rates invariant(attempted/loaded_total)엔 미포함.
        dxy = {"rate": 103.5, "timestamp": _TS, "source": "investing"}

        async def fake_set(key, value):
            return True

        with patch("app.latest_rates_cache.crud") as mc, \
             patch("app.latest_rates_cache._set_latest", side_effect=fake_set), \
             patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)):
            self._mock_crud(mc, dxy=dxy)
            stats = await lrc._mirror_all_latest(MagicMock())

        self.assertEqual(stats["dxy_attempted"], 1)
        self.assertEqual(stats["dxy_loaded"], 1)
        self.assertEqual(stats["attempted_total"], 0)   # DXY는 rates invariant 밖
        self.assertEqual(stats["loaded_total"], 0)


class TestKrxDirectSetterLegacyContract(unittest.TestCase):
    """GAP #2 — set_latest_krx_rate_from_sync_job(:597) legacy 계약 (a2_3의 None→False ungated-on-halt 너머).

    이 setter는 현재 ungated(close finalizer/REST 공유라 A2-3 미gate). C6-5b-2가 halt·atomic을 gate해도 **legacy
    경로는 이 계약을 유지**해야 한다 → snapshot=legacy로 명시. v1 serialize_value write를 잠가, 향후 v2 retrofit/
    gating이 의도적·가시적 변경이 되게 한다.
    """

    def test_client_none_returns_false(self):
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)), \
             patch("app.latest_rates_cache._get_sync_client", return_value=None):
            self.assertFalse(lrc.set_latest_krx_rate_from_sync_job("usd-krw-futures", 1500.0, _TS))

    def test_real_client_writes_v1_and_returns_true(self):
        fake_client = MagicMock()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)), \
             patch("app.latest_rates_cache._get_sync_client", return_value=fake_client):
            result = lrc.set_latest_krx_rate_from_sync_job("usd-krw-futures", 1500.0, _TS)
        self.assertTrue(result)
        fake_client.set.assert_called_once()
        key, value = fake_client.set.call_args[0]
        self.assertEqual(key, lrc.latest_key_source("krx", "usd-krw-futures"))
        parsed = json.loads(value)
        # v1 serialize_value 정확 shape (rate/timestamp/mirrored_at) — schema_version 부재 + v2 retrofit/gating은
        # 의도적·가시적 변경이 되도록 잠금. 느슨한 'no schema_version'만으론 malformed v1-ish도 통과(codex).
        self.assertEqual(set(parsed.keys()), {"rate", "timestamp", "mirrored_at"})
        self.assertEqual(parsed["rate"], 1500.0)
        self.assertEqual(parsed["timestamp"], _TS)
        mirrored_at = datetime.fromisoformat(parsed["mirrored_at"])  # tz-aware ISO여야 (.isoformat() 산출)
        self.assertIsNotNone(mirrored_at.tzinfo)

    def test_set_exception_returns_false(self):
        fake_client = MagicMock()
        fake_client.set.side_effect = RuntimeError("redis down")
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)), \
             patch("app.latest_rates_cache._get_sync_client", return_value=fake_client):
            self.assertFalse(lrc.set_latest_krx_rate_from_sync_job("usd-krw-futures", 1500.0, _TS))


if __name__ == "__main__":
    unittest.main()
