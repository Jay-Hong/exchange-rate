"""PR D precondition — SET-failure 복구 경로 characterization (Step 1).

PR D는 [main.py](../app/main.py) broadcast의 legacy fx hook
(`safe_publish_all_fx_snapshots`)을 제거해 `fx:*`를 source-trigger-only로 만든다.
그 전에 **현재 SET 실패가 어떻게 복구되는지**를 박제해, PR D가 이 동작을 조용히
회귀시키지 않게 한다. C1의 SET-only trigger gating 때문에 direct SET이 실패하면:

  (a) pure miss   — 실패 변경은 trigger 자체가 안 남 → 다음 변경까지 미발행
  (b) partial-stale aggregate — fx:<asset>은 9은행+investing 집계라, 한 source SET
      실패 + 다른 source 성공 시 성공 trigger가 옛 값 섞인 snapshot 발행

현재 이 공백은 **mirror(값 복구) + broadcast diff + legacy fx hook** 3단으로 메워진다.
복구 latency는 hard SLA가 아니라 nominal worst-case ≈ mirror 최대 ~3s + 다음
broadcast 주기 최대 ~10s (scheduler 지연/skip 제외).

이 파일이 잠그는 것 (Codex 합의 시나리오 #3/#4/#5):
  #3 mirror가 DB 최신값을 Redis로 무조건 SET (DB-sourced) — 이것이 실패한 direct
     SET을 **값** 차원에서 복구하는 절반 (SET-실패 절반은 #1 기존 테스트)
  #4 mirror 실행 중 topic trigger는 발생하지 않음 (발행은 legacy hook 의존) —
     positive control(실제 mirror 동작) + trigger/publisher 진입점 다중 감시
  #5 (b) — 실제 helper가 fresh mirrored_at + 옛 rate를 None 아닌 채 반환(#5a) →
     builder가 DB fallback 없이 그 옛 값을 aggregate에 사용(#5b)

기존 계약 테스트가 나머지를 커버 (중복 작성 안 함):
  #1 SET 실패분 trigger 제외 → tests/test_bank_investing_topic_emission.py
     (test_bank_exception_excluded / test_emission_set_only_alert_all)
  #2 payload 변경 시 legacy fx hook 호출 → tests/test_broadcast_rates_once_wiring.py
     + tests/test_fx_topic_publisher.py (safe_publish_all_fx_snapshots)

근거 코드: latest_rates_cache._mirror_all_latest (무조건 SET, trigger 없음) /
get_latest_bank_rate_from_sync_job (is_stale=time-only, content-staleness 미감지) /
fx_topic_payload.load_and_build_fx_topic_payload:211-222 (redis_entry is not None →
DB 비교 없이 사용).
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.latest_rates_cache import (
    _KST,
    _mirror_all_latest,
    get_latest_bank_rate_from_sync_job,
    serialize_value,
)
from app.fx_topic_payload import load_and_build_fx_topic_payload


class TestMirrorWritesDbLatestWithoutTrigger(unittest.IsolatedAsyncioTestCase):
    """#3/#4 — mirror는 DB 최신값을 무조건 Redis로 쓰고(값 복구) trigger는 발사 안 함.

    mirror가 DB-sourced + 무조건 SET이라, 직전 direct SET이 실패해 Redis에 옛 값이
    남아도 다음 mirror cycle이 DB 최신값으로 덮어쓴다(= **값** 복구의 절반; SET-실패
    precondition은 #1 기존 테스트가 잠금). 단 mirror는 topic trigger를 발사하지
    않으므로, PR D가 legacy hook을 제거하면 **발행** 복구가 사라진다 → 복구 trigger를
    별도 설계해야 함 (§6.6.2 ②; mirror는 무조건 SET이라 naive mirror-trigger는
    storm → change-detection/pending-repair 필요).
    """

    def _mock_crud(self, mock_crud):
        """bank loop가 usd-krw 1회 돌도록 + 나머지 read 무력화."""
        mock_crud.SUPPORTED_CURRENCY_PAIRS = ["usd-krw"]
        mock_crud.select_a_latest_investing_rate_from_db.return_value = None
        mock_crud.select_latest_bank_rates_from_db.return_value = [
            {"bank": "kb", "currency": "usd-krw", "rate": 1375.0,
             "timestamp": "2026-06-15T15:00:00+09:00"},  # DB 최신값
        ]
        mock_crud.get_source_rates_as_legacy_format.return_value = []
        mock_crud.get_latest_dxy_rate.return_value = None

    async def test_mirror_writes_db_latest_value_to_redis(self):
        """#3 — mirror가 DB 최신값(1375.0)을 latest:bank:kb:usd-krw로 SET (DB-sourced)."""
        captured = {}

        async def fake_set_latest(key, value):
            captured[key] = value
            return True

        with patch("app.latest_rates_cache.crud") as mock_crud, \
             patch("app.latest_rates_cache._set_latest", side_effect=fake_set_latest):
            self._mock_crud(mock_crud)
            stats = await _mirror_all_latest(MagicMock())

        self.assertEqual(stats["bank"], 1)  # positive control — mirror가 실제 처리
        key = "latest:bank:kb:usd-krw"
        self.assertIn(key, captured)
        parsed = json.loads(captured[key])
        self.assertEqual(parsed["rate"], 1375.0)  # DB 최신값으로 Redis 복구

    async def test_mirror_does_not_emit_topic_trigger(self):
        """#4 — mirror는 trigger/publisher 진입점을 호출하지 않음 (positive control 포함).

        bridge 퍼널 + 직접 FX trigger + publisher 2종(legacy hook
        safe_publish_all_fx_snapshots / C1 direct flush safe_publish_fx_snapshot)을
        모두 감시 — "mirror는 topic trigger를 발사하지 않는다" 주장 범위와 일치.
        """
        async def fake_set_latest(key, value):
            return True

        with patch("app.latest_rates_cache.crud") as mock_crud, \
             patch("app.latest_rates_cache._set_latest", side_effect=fake_set_latest), \
             patch("app.topic_trigger_bridge.schedule_on_loop") as mock_bridge, \
             patch("app.fx_topic_trigger.request_fx_topic_trigger") as mock_fx_trigger, \
             patch("app.fx_topic_publisher.safe_publish_all_fx_snapshots",
                   new_callable=AsyncMock) as mock_fx_hook, \
             patch("app.fx_topic_publisher.safe_publish_fx_snapshot",
                   new_callable=AsyncMock) as mock_fx_direct:
            self._mock_crud(mock_crud)
            stats = await _mirror_all_latest(MagicMock())

        # positive control: mirror가 실제로 bank를 처리했는데도 trigger 0
        self.assertEqual(stats["bank"], 1)
        mock_bridge.assert_not_called()
        mock_fx_trigger.assert_not_called()
        mock_fx_hook.assert_not_called()       # legacy hook (safe_publish_all_fx_snapshots)
        mock_fx_direct.assert_not_called()     # C1 direct flush (safe_publish_fx_snapshot)


class TestFreshButOutdatedRedisStaleAggregate(unittest.TestCase):
    """#5 ((b)) — fresh-but-outdated Redis 값이 DB fallback 없이 aggregate에 섞인다.

    direct SET 실패 → Redis에 옛 값 잔존. mirrored_at이 is_stale 임계 이내(fresh)면
    helper는 None이 아니라 **옛 값**을 반환(#5a, content-staleness 미감지) →
    builder가 DB 비교 없이 그 값을 사용(#5b) → 다른 source 성공 trigger 발행 시
    옛 값 섞인 stale aggregate.
    """

    def test_real_helper_returns_fresh_mirrored_at_but_outdated_rate(self):
        """#5a (핵심) — 실제 get_latest_bank_rate_from_sync_job가 fresh mirrored_at +
        옛 rate를 None 아닌 채 반환 (is_stale은 시간만 보고 내용은 못 봄).

        helper를 mock하지 않고 실제 실행 — Redis client.get만 mock해 'mirrored_at은
        지금(fresh)인데 rate는 옛 1370.0'인 값을 주입.
        """
        fresh_mirrored_at = datetime.now(_KST)  # is_stale=False (방금 mirror된 것처럼)
        raw = serialize_value(1370.0, "2026-06-15T14:59:50+09:00", fresh_mirrored_at)
        fake_client = MagicMock()
        fake_client.get.return_value = raw.encode()  # 실제 sync client는 bytes 반환

        with patch("app.latest_rates_cache._get_sync_client", return_value=fake_client):
            result = get_latest_bank_rate_from_sync_job("kb", "usd-krw")

        # 핵심: fresh mirrored_at라 is_stale=False → None 아님 + 옛 rate 그대로 반환
        self.assertIsNotNone(result)
        self.assertEqual(result["rate"], 1370.0)

    def test_builder_uses_fresh_but_outdated_redis_without_db_fallback(self):
        """#5b — builder가 helper의 fresh-but-outdated 값을 DB 비교 없이 aggregate에 사용.

        kb는 옛 Redis 값(1370.0), DB엔 최신(1375.0). fallback이 일어났다면 1375.0이
        쓰여야 하지만 helper가 None이 아니라 안 쓰임 → stale aggregate.
        """
        def fake_bank_redis(bank, asset):
            if bank == "kb":
                return {"source": "kb", "asset": asset, "rate": 1370.0,
                        "timestamp": "2026-06-15T14:59:50+09:00"}  # 옛 값 — None 아님
            return {"source": bank, "asset": asset, "rate": 1372.0,
                    "timestamp": "2026-06-15T15:00:00+09:00"}

        bank_db_mock = MagicMock(return_value=[
            {"bank": "kb", "currency": "usd-krw", "rate": 1375.0,
             "timestamp": "2026-06-15T15:00:00+09:00"},  # DB 최신값 (안 쓰임)
        ])

        with patch("app.fx_topic_payload.get_latest_bank_rate_from_sync_job",
                   side_effect=fake_bank_redis), \
             patch("app.fx_topic_payload.get_latest_investing_rate_from_sync_job",
                   return_value={"source": "investing", "asset": "usd-krw",
                                 "rate": 1371.0,
                                 "timestamp": "2026-06-15T15:00:00+09:00"}), \
             patch("app.fx_topic_payload.select_latest_bank_rates_from_db", bank_db_mock), \
             patch("app.fx_topic_payload.select_a_latest_investing_rate_from_db",
                   return_value=None):
            payload = load_and_build_fx_topic_payload(MagicMock(), "usd-krw")

        banks = {e["source"]: e["rate"] for e in payload["data"]["banks"]}
        self.assertEqual(banks["kb"], 1370.0)  # 옛 Redis 값 — DB 최신(1375.0) 아님
        bank_db_mock.assert_not_called()  # fresh(None 아님)라 DB fallback 미발생


if __name__ == "__main__":
    unittest.main(verbosity=2)
