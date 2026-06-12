"""broadcast_rates_once() topic-publish wiring characterization (PR A — Bank/Investing β).

behavior-change-0, tests-only. broadcast_rates_once는 그동안 미테스트(tests/ 참조 0)였고,
Bank/Investing β의 PR D(main.py legacy fx hook 격하) 전에 현 wiring 계약을 잠근다.

⚠️ outer try/except (main.py:798): 함수 본문 전체(publisher 호출 730/741 포함)가
try/except로 감싸여 있다. mock 빈틈으로 앞단이 raise하면 except가 삼켜 publisher가
호출되지 않아 "publisher 미호출"이 엉뚱한 이유로 통과(vacuous)할 수 있다. 그래서 각
케이스에 record_failure 미호출 + 기대 record 경로(success/skip) positive control로
차단한다 (Meta E — 측정 형태 의심).

publisher-raise 테스트는 제외 — 내부 격리는 test_fx_topic_publisher가 잠금. 현 계약 FACT:
outer except = crash backstop / wrapper no-raise = per-cycle legacy send 가용성
(publisher raise 시 프로세스는 생존하나 그 cycle의 manager.broadcast send는 유실).
"""
from __future__ import annotations

import contextlib
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import main


class TestBroadcastRatesOnceWiring(unittest.IsolatedAsyncioTestCase):
    """is_changed → tether/fx publisher → (active 시) legacy send 의 분기 계약 잠금."""

    async def _run(self, *, unchanged, active_connections):
        # payload는 매 호출 fresh (graph_buckets 변이 + 테스트 간 공유 회피).
        payload = {"type": "rates", "data": {"rates": [{"bank": "kb", "rate": 1371.5}]}}
        # is_changed = (new_json != cached_json). unchanged면 동일 직렬화로 맞춘다.
        cached_json = json.dumps(payload, ensure_ascii=False) if unchanged else "{}"

        mock_db = MagicMock()
        mock_manager = MagicMock()
        mock_manager.active_connections = active_connections
        mock_manager.broadcast = AsyncMock()
        mock_stats = MagicMock()
        mock_redis = MagicMock()
        mock_redis.get = AsyncMock(return_value=cached_json)
        mock_redis.set = AsyncMock()
        mock_tether = AsyncMock()
        mock_fx = AsyncMock()

        with contextlib.ExitStack() as stack:
            enter = stack.enter_context
            enter(patch.object(main, "_should_run_broadcast", return_value=(True, None)))
            enter(patch.object(main, "_resolved_send_timeout", return_value=1.0))
            enter(patch.object(main, "REDIS_LATEST_ENABLED", False))
            enter(patch.object(main, "build_rates_payload_with_timings",
                               return_value=(payload, {})))
            enter(patch.object(main, "SessionLocal", return_value=mock_db))
            enter(patch.object(main, "redis_cache", mock_redis))
            enter(patch.object(main, "manager", mock_manager))
            enter(patch.object(main, "broadcast_stats", mock_stats))
            enter(patch.object(main, "build_graph_buckets", new=AsyncMock(return_value={})))
            enter(patch.object(main, "_get_pool_status", return_value={}))
            enter(patch.object(main.tether_topic_publisher,
                               "safe_publish_tether_tab_snapshot", new=mock_tether))
            enter(patch.object(main.fx_topic_publisher,
                               "safe_publish_all_fx_snapshots", new=mock_fx))
            await main.broadcast_rates_once()

        return {
            "db": mock_db, "manager": mock_manager, "stats": mock_stats,
            "tether": mock_tether, "fx": mock_fx,
        }

    async def test_changed_with_connection_publishes_both_and_sends(self):
        """payload 변경 + active connection → tether/fx publisher 각 1회 + legacy send + record_success."""
        m = await self._run(unchanged=False, active_connections=[MagicMock()])
        m["tether"].assert_awaited_once()
        m["fx"].assert_awaited_once()
        m["manager"].broadcast.assert_awaited_once()
        m["stats"].record_success.assert_called_once()
        m["stats"].record_failure.assert_not_called()  # vacuous 가드 (outer except 삼킴 차단)
        m["db"].close.assert_called_once()

    async def test_unchanged_skips_publish_and_send(self):
        """payload 불변(cached==new) → publisher/legacy send 미호출 + record_skip(no_changes)."""
        m = await self._run(unchanged=True, active_connections=[MagicMock()])
        m["tether"].assert_not_awaited()
        m["fx"].assert_not_awaited()
        m["manager"].broadcast.assert_not_awaited()
        m["stats"].record_skip.assert_called_once_with(reason="no_changes")
        m["stats"].record_failure.assert_not_called()
        m["db"].close.assert_called_once()

    async def test_changed_without_connection_publishes_but_no_send(self):
        """payload 변경 + active connection 0 → publisher는 호출(hook이 active 분기 밖) + legacy send 미호출 + record_skip(no_connections)."""
        m = await self._run(unchanged=False, active_connections=[])
        m["tether"].assert_awaited_once()
        m["fx"].assert_awaited_once()
        m["manager"].broadcast.assert_not_awaited()
        m["stats"].record_skip.assert_called_once_with(reason="no_connections")
        m["stats"].record_failure.assert_not_called()
        m["db"].close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
