"""fetch_rates_from_redis() Z-2f contract tests.

Z-2f 핵심 변경 ([DECISIONS.md ADR-030](../DECISIONS.md)):
1. `latest:index.mirrored_at` stale gate 제거 (현 [latest_rates_cache.py:886](../app/latest_rates_cache.py#L886))
2. 개별 data key value의 `mirrored_at` per-key stale 검사 추가
3. 신규 fallback_reason: `per_key_stale` (1개 신규)

이 파일은 ADR-030 contract를 잠그는 RED→GREEN 회귀 가드:
- Z-2f 구현 전: ★ 표시된 2개 (index stale + data fresh / 1 key stale)가 RED
- Z-2f 구현 후: 8개 전체 GREEN

테스트 범위 (Codex 검토 합의 — Z-2f 핵심 contract만):
- index level: fallback (combined miss/parse fail)
- per-key level: stale (NEW) / miss (existing) / parse fail (existing)
- happy path: payload shape + key invariant
- DXY 격리: rates 성공 + DXY 별도 fallback path
- 백워드 호환: 기존 index schema 그대로 사용

비범위 (1차 제외):
- 다중 stale boundary (단일 stale로 동일 코드 경로 커버됨)
- redis infra 실패 4종 (client/circuit/MGET error/length mismatch) — Z-2f 핵심 외
- per_key_miss / per_key_parse_fail rename (Codex 합의로 1차 미포함)
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app import latest_rates_cache
from app.latest_rates_cache import (
    LATEST_DXY_KEY,
    LATEST_INDEX_KEY,
    _KST,
    fetch_rates_from_redis,
    serialize_dxy_value,
    serialize_index,
    serialize_value,
)


def _fresh_mirrored_at(seconds_ago: float = 0.5) -> datetime:
    """is_stale 통과 가능한 mirrored_at (now - seconds_ago)."""
    return datetime.now(_KST) - timedelta(seconds=seconds_ago)


def _stale_mirrored_at(seconds_ago: float = 20.0) -> datetime:
    """is_stale에 stale 판정 가능한 mirrored_at (interval 3s × STALE_RATIO 2 = 6s 초과)."""
    return datetime.now(_KST) - timedelta(seconds=seconds_ago)


def _make_data_value(rate: float, ts: str, mirrored_at: datetime) -> bytes:
    return serialize_value(rate, ts, mirrored_at).encode("utf-8")


def _make_index_value(keys: list[str], mirrored_at: datetime) -> bytes:
    return serialize_index(keys, mirrored_at).encode("utf-8")


def _make_dxy_value(rate: float, ts: str, source: str, mirrored_at: datetime) -> bytes:
    return serialize_dxy_value(rate, ts, source, mirrored_at).encode("utf-8")


class TestFetchRatesFromRedisZ2f(unittest.IsolatedAsyncioTestCase):
    """ADR-030 Z-2f contract — broadcast Redis-first read path.

    설계: mock at `_get_latest_with_reason` (index + DXY) + `redis_cache.client.mget`
    + `redis_cache.circuit.can_attempt`. 이렇게 하면 stage별로 다른 시나리오를
    independent 하게 구성 가능.
    """

    KEYS = ["latest:bank:kb:usd-krw", "latest:investing:usd-krw"]
    TS_KST = "2026-05-13T10:00:00+09:00"

    def _build_index_response(self, mirrored_at: datetime, keys=None):
        keys = keys or self.KEYS
        return (_make_index_value(keys, mirrored_at), None)

    async def _mock_redis_call(
        self,
        *,
        index_response,
        mget_values,
        dxy_response=(None, "redis_miss"),
    ):
        """returns context with patched redis primitives."""
        async def fake_get_with_reason(key):
            if key == LATEST_INDEX_KEY:
                return index_response
            elif key == LATEST_DXY_KEY:
                return dxy_response
            return (None, "redis_miss")

        mock_client = AsyncMock()
        mock_client.mget = AsyncMock(return_value=mget_values)
        mock_circuit = AsyncMock()
        mock_circuit.can_attempt = AsyncMock(return_value=True)
        mock_circuit.record_success = AsyncMock()
        mock_circuit.record_failure = AsyncMock()

        return (
            patch.object(latest_rates_cache, "_get_latest_with_reason",
                         side_effect=fake_get_with_reason),
            patch.object(latest_rates_cache.redis_cache, "client", mock_client),
            patch.object(latest_rates_cache.redis_cache, "circuit", mock_circuit),
        )

    # ── ★ Z-2f core RED tests (구현 전 실패 예상) ─────────────────────

    async def test_index_stale_data_fresh_returns_redis_success(self):
        """★ Z-2f KEY: index.mirrored_at stale이어도 data keys fresh면 Redis success.

        ADR-030 결정: index stale gate (line 886) 제거.
        구현 전: 현재는 index stale 시 `redis_stale` fallback → 이 테스트 RED.
        구현 후: data keys per-key 검사로 통과 → GREEN.
        """
        index_stale = _stale_mirrored_at(20.0)
        data_fresh = _fresh_mirrored_at(0.5)
        index_response = self._build_index_response(index_stale)
        mget_values = [
            _make_data_value(1371.5, self.TS_KST, data_fresh),
            _make_data_value(1372.0, self.TS_KST, data_fresh),
        ]
        patches = await self._mock_redis_call(
            index_response=index_response,
            mget_values=mget_values,
        )
        with patches[0], patches[1], patches[2]:
            rates, dxy, meta = await fetch_rates_from_redis()

        self.assertIsNotNone(rates, f"index stale도 data fresh면 success여야 함. meta={meta}")
        self.assertEqual(meta.get("latest_source"), "redis")
        self.assertEqual(len(rates), 2)

    async def test_one_data_key_stale_returns_db_fallback_per_key_stale(self):
        """★ Z-2f NEW: data key 1개 stale → `fallback_reason='per_key_stale'`.

        구현 전: per-key 검사 미존재 → Redis success (잘못된 동작) → 이 테스트 RED.
        구현 후: per-key stale 감지 → fallback 발동 → GREEN.
        """
        index_fresh = _fresh_mirrored_at(0.5)
        data_fresh = _fresh_mirrored_at(0.5)
        data_stale = _stale_mirrored_at(20.0)
        index_response = self._build_index_response(index_fresh)
        mget_values = [
            _make_data_value(1371.5, self.TS_KST, data_fresh),
            _make_data_value(1372.0, self.TS_KST, data_stale),  # 1개만 stale
        ]
        patches = await self._mock_redis_call(
            index_response=index_response,
            mget_values=mget_values,
        )
        with patches[0], patches[1], patches[2]:
            rates, dxy, meta = await fetch_rates_from_redis()

        self.assertIsNone(rates)
        self.assertEqual(meta.get("latest_source"), "db_fallback")
        self.assertEqual(meta.get("fallback_reason"), "per_key_stale")

    # ── 기존 동작 보존 GREEN tests (Z-2f 후에도 그대로 통과) ─────────

    async def test_data_key_miss_returns_redis_miss(self):
        """data key 1개 None → 기존 `redis_miss` 그대로 유지 (rename 없음)."""
        index_fresh = _fresh_mirrored_at(0.5)
        data_fresh = _fresh_mirrored_at(0.5)
        index_response = self._build_index_response(index_fresh)
        mget_values = [
            _make_data_value(1371.5, self.TS_KST, data_fresh),
            None,  # miss
        ]
        patches = await self._mock_redis_call(
            index_response=index_response,
            mget_values=mget_values,
        )
        with patches[0], patches[1], patches[2]:
            rates, dxy, meta = await fetch_rates_from_redis()

        self.assertIsNone(rates)
        self.assertEqual(meta.get("fallback_reason"), "redis_miss")

    async def test_data_key_parse_fail_returns_redis_error(self):
        """data key value parse 실패 → 기존 `redis_error` 그대로 유지."""
        index_fresh = _fresh_mirrored_at(0.5)
        index_response = self._build_index_response(index_fresh)
        mget_values = [
            b"not-a-valid-json",  # deserialize_value None 반환
            b"{\"rate\": 1372.0}",  # incomplete
        ]
        patches = await self._mock_redis_call(
            index_response=index_response,
            mget_values=mget_values,
        )
        with patches[0], patches[1], patches[2]:
            rates, dxy, meta = await fetch_rates_from_redis()

        self.assertIsNone(rates)
        self.assertEqual(meta.get("fallback_reason"), "redis_error")

    async def test_all_data_keys_fresh_returns_redis_rates_with_shape(self):
        """happy path: 모든 fresh → Redis success + rate record shape 유지."""
        index_fresh = _fresh_mirrored_at(0.5)
        data_fresh = _fresh_mirrored_at(0.5)
        index_response = self._build_index_response(index_fresh)
        mget_values = [
            _make_data_value(1371.5, self.TS_KST, data_fresh),
            _make_data_value(1372.0, self.TS_KST, data_fresh),
        ]
        patches = await self._mock_redis_call(
            index_response=index_response,
            mget_values=mget_values,
        )
        with patches[0], patches[1], patches[2]:
            rates, dxy, meta = await fetch_rates_from_redis()

        self.assertIsNotNone(rates)
        self.assertEqual(len(rates), 2)
        # legacy shape {currency, bank, rate, timestamp}
        first = rates[0]
        self.assertEqual(first["bank"], "kb")
        self.assertEqual(first["currency"], "usd-krw")
        self.assertEqual(first["rate"], 1371.5)
        self.assertEqual(first["timestamp"], self.TS_KST)
        second = rates[1]
        self.assertEqual(second["bank"], "investing")
        self.assertEqual(second["currency"], "usd-krw")
        self.assertIn("mirror_age_ms", meta)
        self.assertEqual(meta["latest_key_count"], 2)

    async def test_dxy_stale_does_not_affect_rates_success(self):
        """DXY 격리: DXY Redis stale이어도 rates path는 정상 동작 (PR5 invariant)."""
        index_fresh = _fresh_mirrored_at(0.5)
        data_fresh = _fresh_mirrored_at(0.5)
        dxy_stale = _stale_mirrored_at(20.0)
        index_response = self._build_index_response(index_fresh)
        mget_values = [
            _make_data_value(1371.5, self.TS_KST, data_fresh),
            _make_data_value(1372.0, self.TS_KST, data_fresh),
        ]
        dxy_response = (
            _make_dxy_value(99.5, self.TS_KST, "investing", dxy_stale),
            None,
        )
        patches = await self._mock_redis_call(
            index_response=index_response,
            mget_values=mget_values,
            dxy_response=dxy_response,
        )
        with patches[0], patches[1], patches[2]:
            rates, dxy, meta = await fetch_rates_from_redis()

        # rates 정상
        self.assertIsNotNone(rates)
        self.assertEqual(meta.get("latest_source"), "redis")
        # DXY는 별도 fallback (stale)
        self.assertIsNone(dxy)
        self.assertEqual(meta.get("latest_dxy_fallback_reason"), "redis_stale")

    async def test_index_miss_returns_db_fallback(self):
        """index miss/parse fail → 전체 fallback (기존 동작)."""
        index_response = (None, "redis_miss")
        patches = await self._mock_redis_call(
            index_response=index_response,
            mget_values=[],
        )
        with patches[0], patches[1], patches[2]:
            rates, dxy, meta = await fetch_rates_from_redis()

        self.assertIsNone(rates)
        self.assertEqual(meta.get("latest_source"), "db_fallback")
        self.assertEqual(meta.get("fallback_reason"), "redis_miss")

    async def test_legacy_index_schema_with_mirrored_at_still_readable(self):
        """백워드 호환: 기존 latest:index {keys, mirrored_at} JSON 그대로 처리.

        Z-2f가 schema를 바꾸지 않으므로(read path만 mirrored_at field ignore), mirror
        cycle이 계속 쓰는 기존 schema가 정상 deserialize되어야 함. rollback 안전성.
        """
        # mirror cycle이 적재한 기존 schema 그대로 (mirrored_at 포함)
        index_fresh = _fresh_mirrored_at(0.5)
        data_fresh = _fresh_mirrored_at(0.5)
        index_response = self._build_index_response(index_fresh)  # mirror cycle 작성 그대로
        mget_values = [
            _make_data_value(1371.5, self.TS_KST, data_fresh),
            _make_data_value(1372.0, self.TS_KST, data_fresh),
        ]
        patches = await self._mock_redis_call(
            index_response=index_response,
            mget_values=mget_values,
        )
        with patches[0], patches[1], patches[2]:
            rates, dxy, meta = await fetch_rates_from_redis()

        # 정상 처리 (schema 변경 없이 동작)
        self.assertIsNotNone(rates)
        self.assertEqual(meta.get("latest_source"), "redis")


if __name__ == "__main__":
    unittest.main(verbosity=2)
