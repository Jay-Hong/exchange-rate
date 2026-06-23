"""fanout step 3 — observation_from_tick 어댑터 검증 (behavior-change-0).

USDT 5소스 WS + KRX tick handler의 AlertObservation 생성을 단일 adapter로 통합.
핵심 invariant: 입력 tick dict → 생성 AlertObservation이 이전 직접 생성과 **identical**.
timestamp_ms 도출은 caller 책임(helper는 변환 안 함) — never-unify §4-3.
"""
from __future__ import annotations

import unittest

from app.notifications.alert_evaluator import AlertObservation, observation_from_tick


class TestObservationFromTick(unittest.TestCase):

    def test_tick_default_kind_identical(self):
        tick = {"source": "upbit", "asset": "usdt-krw", "rate": 1450.0, "timestamp_ms": 1700000000000}
        self.assertEqual(
            observation_from_tick(tick),
            AlertObservation(source="upbit", asset="usdt-krw", rate=1450.0,
                             timestamp_ms=1700000000000, kind="tick"),
        )

    def test_rest_probe_kind_identical(self):
        tick = {"source": "bithumb", "asset": "usdt-krw", "rate": 1451.5, "timestamp_ms": 1700000005000}
        self.assertEqual(
            observation_from_tick(tick, kind="rest_probe"),
            AlertObservation(source="bithumb", asset="usdt-krw", rate=1451.5,
                             timestamp_ms=1700000005000, kind="rest_probe"),
        )

    def test_krx_style_dict_identical(self):
        # KRX는 {source, asset, rate(normalized), timestamp_ms(received_at 변환값)} dict 빌드 후 호출
        krx_tick = {"source": "krx", "asset": "usd-krw-futures", "rate": 1450.5, "timestamp_ms": 1700000010000}
        self.assertEqual(
            observation_from_tick(krx_tick),
            AlertObservation(source="krx", asset="usd-krw-futures", rate=1450.5,
                             timestamp_ms=1700000010000, kind="tick"),
        )

    def test_extra_keys_ignored(self):
        # USDT tick dict은 4키 외 추가 키 보유 — helper는 4키만 읽음(이전 직접 생성과 동일)
        tick = {"source": "korbit", "asset": "usdt-krw", "rate": 1452.0, "timestamp_ms": 1700000015000,
                "raw": {"x": 1}, "received_at": "2026-06-23T12:00:00"}
        obs = observation_from_tick(tick)
        self.assertEqual(obs.source, "korbit")
        self.assertEqual(obs.timestamp_ms, 1700000015000)
        self.assertEqual(obs.kind, "tick")
        # frozen dataclass라 추가 키는 흡수 안 됨 (4 필드만)
        self.assertEqual(
            obs,
            AlertObservation(source="korbit", asset="usdt-krw", rate=1452.0,
                             timestamp_ms=1700000015000, kind="tick"),
        )

    def test_missing_key_raises_like_before(self):
        # 이전 직접 생성도 tick["source"] 누락 시 KeyError — 동일 동작 보존
        with self.assertRaises(KeyError):
            observation_from_tick({"asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})


if __name__ == "__main__":
    unittest.main()
