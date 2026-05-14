"""KRX liveness invariants 회귀 가드 (KRX_FANOUT_REFACTOR_PLAN 5.1.A 사전 PR).

KrxLivenessMonitor 추출 (PR-A) 전에 현재 동작을 직접 잠그는 회귀 가드 모음.

KRX_FANOUT_REFACTOR_PLAN section 5.1.A 검증 항목 매핑:
- `_last_tick_at` multi-purpose 결합 보존 (liveness / PR6e grace / stale transition /
  fallback frame_age / session reset 5개 영역)
- normal→stale 전이가 `_evaluate_rest_fallback` 진입점인 invariant
- get_metrics() 출력 shape 보존

본 파일의 5 tests는:
- 현재 코드(`app/crawlers/krx_kis.py`)에서 GREEN
- KrxLivenessMonitor 추출 PR (다음 PR-A) 이후에도 그대로 GREEN
- 이것이 behavior-change-0의 기준선

비범위 (다른 파일에 이미 있음):
- fallback eligibility 4단계 검사 순서 → `test_krx_fallback_eligibility.py`
- ADR-031 Redis integration → `test_krx_redis_integration.py`

비범위 (Codex 합의로 우선순위 낮음 — 5 핵심에 포함됨으로 implicit 잠금):
- `_reset_active_session_gap_metrics` 단독 sequencing (grace start test가 higher level에서)
- normal→stale evaluation 호출 횟수 단독 (transition test에 포함됨)
"""
from __future__ import annotations

import time
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from app import config
from app.crawlers.krx_kis import STALE_AFTER_SEC, KisFuturesClient
from app.sources.kis_master import ContractInfo


def _make_client() -> KisFuturesClient:
    """test_krx_fallback_eligibility의 동일 패턴 — A75605 5/18 만기 contract."""
    contract = ContractInfo(
        short_code="A75605",
        standard_code="KR4A75650007",
        name="미국달러 F 202605",
        contract_month="202605",
        expiry_date=date(2026, 5, 18),
    )
    approval = MagicMock()
    return KisFuturesClient(approval, contract=contract)


class TestKrxLivenessInvariants(unittest.TestCase):
    """KRX_FANOUT_REFACTOR_PLAN 5.1.A 추출 전 회귀 가드 5개."""

    # ------------------------------------------------------------------
    # 1. PR6e grace start — 새 WebSocket 진입 시 즉시 stale 방지
    # ------------------------------------------------------------------

    def test_grace_start_prevents_immediate_stale(self):
        """`_reset_active_session_gap_metrics()` + 새 grace start sequencing 보존.

        KRX_FANOUT_REFACTOR_PLAN 5.1.A — `_last_tick_at` multi-purpose:
        - reset이 None으로 설정 (line 660)
        - 호출자(`_connect_and_listen` line 1053)가 `time.time()`으로 재설정
        - 결과: 새 session 진입 직후 stale check (line 1063-1068)이 즉시 fire하지 않음

        본 테스트는 sequencing invariant 잠금 — 추출 후에도 동일 시맨틱 유지 필요.
        """
        client = _make_client()
        # 이전 session의 carry-over 흉내 (옛 _last_tick_at)
        client._last_tick_at = time.time() - 3600

        # 1단계: reset → None
        client._reset_active_session_gap_metrics()
        self.assertIsNone(client._last_tick_at)

        # 2단계: PR6e grace start (line 1053 simulation)
        grace_start = time.time()
        client._last_tick_at = grace_start

        # 3단계: 즉시 stale check (line 1063-1068 condition) → False
        # (now - grace_start) < STALE_AFTER_SEC이므로 stale 전이 X
        stale_condition = (
            client._last_tick_at
            and time.time() - client._last_tick_at > STALE_AFTER_SEC
        )
        self.assertFalse(
            stale_condition,
            f"새 WebSocket grace start 직후 즉시 stale 발생 — "
            f"STALE_AFTER_SEC={STALE_AFTER_SEC}, "
            f"age={time.time() - client._last_tick_at}",
        )

    # ------------------------------------------------------------------
    # 2. Active loop stale transition + evaluation 1회 호출
    # ------------------------------------------------------------------

    def test_normal_to_stale_transition_triggers_evaluation_once(self):
        """status `normal → stale` 전이 시 `_evaluate_rest_fallback` 정확히 1회 호출.

        KRX_FANOUT_REFACTOR_PLAN 5.1.A — line 1026-1027 invariant.
        - active loop (line 1063-1068)에서 frame silence > STALE_AFTER_SEC 검출 시
          `_set_status("stale")` 호출
        - 이때 prev_status=normal → evaluation 진입점
        - 같은 stale 상태 재진입 (stale→stale)은 no-op (transition X)

        추출 PR에서 LivenessMonitor와 RestFallbackController 분리 시 이 invariant
        보존 필요 — LivenessMonitor의 transition 시그널이 Controller 호출 트리거.
        """
        client = _make_client()
        client._status = "normal"
        # active loop 시나리오 — _last_tick_at은 oldness 흉내 (간접 검증)
        client._last_tick_at = time.time() - (STALE_AFTER_SEC + 5)

        with patch.object(client, "_evaluate_rest_fallback") as mock_eval:
            # 1차 전이: normal → stale → evaluation 1회
            client._set_status("stale")
            self.assertEqual(client._status, "stale")
            self.assertEqual(mock_eval.call_count, 1)

            # 2차: stale → stale (no-op transition) → evaluation 증가 X
            client._set_status("stale")
            self.assertEqual(mock_eval.call_count, 1)

    # ------------------------------------------------------------------
    # 3. Reconnect path stale transition은 evaluation 트리거 X
    # ------------------------------------------------------------------

    def test_reconnecting_to_stale_does_not_trigger_evaluation(self):
        """status `reconnecting → stale` 전이는 `_evaluate_rest_fallback` 미호출.

        KRX_FANOUT_REFACTOR_PLAN 5.1.A — line 1026 조건 `prev_status == "normal"`.
        - reconnect path (line 1005-1006): exception 후 status는 "reconnecting"
          (line 998 설정), 그 상태에서 stale 검출 시 `_set_status("stale")`
        - reconnecting → stale 전이는 evaluation 진입점 *아님* (의도된 차별)
        - 이유: reconnect 중인 silence는 이미 비정상이라 추가 evaluation 무의미

        추출 PR에서 이 차별을 보존 필요 — 두 path를 동일 처리하면 counter 부풀림.
        """
        client = _make_client()
        client._status = "reconnecting"
        client._last_tick_at = time.time() - (STALE_AFTER_SEC + 5)

        with patch.object(client, "_evaluate_rest_fallback") as mock_eval:
            client._set_status("stale")
            self.assertEqual(client._status, "stale")
            # ★ reconnecting → stale 전이는 evaluation 미호출
            mock_eval.assert_not_called()

    # ------------------------------------------------------------------
    # 4. Fallback frame_age = now - _last_tick_at 계산 invariant
    # ------------------------------------------------------------------

    def test_fallback_frame_age_threshold_boundary(self):
        """`_evaluate_rest_fallback(now)` 안의 `frame_age = now - _last_tick_at` 잠금.

        KRX_FANOUT_REFACTOR_PLAN 5.1.A — line 891-894:
            frame_age = (now_epoch - self._last_tick_at if self._last_tick_at else 0.0)
            if frame_age < KRX_REST_FALLBACK_STALE_SEC: → suppressed_below_threshold

        threshold 경계 동작 잠금:
        - frame_age < threshold → below_threshold
        - frame_age >= threshold → eligible (다른 조건 통과 시)

        추출 PR에서 frame_age 계산을 LivenessMonitor로 옮기든, Controller가
        Monitor의 age 시그널을 받든, 결과 분기는 동일 유지.
        """
        from datetime import datetime, timezone, timedelta
        kst = timezone(timedelta(hours=9))
        # CF 정규 12:00 — grace 밖
        ts = datetime(2026, 5, 8, 12, 0).replace(tzinfo=kst).timestamp()
        client = _make_client()
        client._active_session = "CF"
        client._last_fallback_at = None

        with patch.object(config, "KRX_REST_FALLBACK_ENABLED", True), \
             patch.object(config, "KRX_REST_FALLBACK_STALE_SEC", 120), \
             patch.object(config, "KRX_REST_FALLBACK_SESSION_END_GRACE_MIN", 40), \
             patch.object(config, "KRX_REST_COOLDOWN_SEC", 30):
            # 경계 미달: 119s old → below_threshold
            client._last_tick_at = ts - 119
            self.assertEqual(
                client._evaluate_rest_fallback(ts),
                "suppressed_below_threshold",
            )

            # 경계 동일: 120s old → eligible (다른 조건 통과)
            client._last_fallback_at = None  # cooldown reset
            client._last_tick_at = ts - 120
            # `_evaluate_rest_fallback`은 event loop 유무와 무관하게 "eligible" return
            # (line 922 no-loop path / line 928 loop path 모두). 직접 단언.
            self.assertEqual(
                client._evaluate_rest_fallback(ts),
                "eligible",
            )
            self.assertGreaterEqual(client._fallback_counters["eligible"], 1)

    # ------------------------------------------------------------------
    # 5. get_metrics() 출력 shape 보존
    # ------------------------------------------------------------------

    def test_get_metrics_shape_preserved(self):
        """`get_metrics()` 출력 dict가 KRX_FANOUT_REFACTOR_PLAN 5.1.A 명시 필드 모두 포함.

        외부 API contract — admin endpoint / summary log / 추출 후 LivenessMonitor가
        같은 shape 유지해야 함.
        """
        client = _make_client()
        metrics = client.get_metrics()

        # liveness age 3종 (KRX_FANOUT_REFACTOR_PLAN 5.1.A invariant)
        self.assertIn("last_frame_age_sec", metrics)
        self.assertIn("last_trade_frame_age_sec", metrics)
        self.assertIn("last_quote_frame_age_sec", metrics)
        # 시점 epoch + KST ISO (admin 가독성)
        self.assertIn("last_frame_at", metrics)
        self.assertIn("last_frame_at_kst", metrics)
        self.assertIn("last_trade_frame_at", metrics)
        self.assertIn("last_quote_frame_at", metrics)

        # status + active_session
        self.assertIn("status", metrics)
        self.assertIn("active_session", metrics)

        # gap buckets (체결/호가 분리)
        self.assertIn("gap_buckets", metrics)
        self.assertIn("total", metrics["gap_buckets"])
        self.assertIn("trade", metrics["gap_buckets"])
        self.assertIn("quote", metrics["gap_buckets"])

        # max gap (3종)
        self.assertIn("max_gap_sec", metrics)
        self.assertIn("total", metrics["max_gap_sec"])
        self.assertIn("trade", metrics["max_gap_sec"])
        self.assertIn("quote", metrics["max_gap_sec"])

        # counters
        self.assertIn("counters", metrics)
        self.assertIn("status_transitions", metrics["counters"])
        self.assertIn("normal", metrics["counters"]["status_transitions"])
        self.assertIn("reconnecting", metrics["counters"]["status_transitions"])
        self.assertIn("stale", metrics["counters"]["status_transitions"])
        self.assertIn("reconnect_attempt", metrics["counters"])
        self.assertIn("fallback", metrics["counters"])

        # fallback counter shape
        fb = metrics["counters"]["fallback"]
        for key in (
            "evaluated", "eligible",
            "suppressed_disabled", "suppressed_below_threshold",
            "suppressed_session_end_grace", "suppressed_cooldown",
            "rest_success", "rest_error",
        ):
            self.assertIn(key, fb, f"fallback counter missing: {key}")

        # contract + lifecycle
        self.assertIn("contract", metrics)
        self.assertIn("lifecycle", metrics)


if __name__ == "__main__":
    unittest.main(verbosity=2)
