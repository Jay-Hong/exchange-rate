# tests/test_ws_metrics_exposure.py
"""S7 — `ws-connection-metrics` 노출 계약.

설계 §4.1/4.2 의 변이 기준 4종:
  ① 신규 블록 예외가 **다른 세 블록을 지우면** red — 네 도메인은 서로 독립이다.
  ② `topic_dispatcher_enabled` 가 신규 블록(rollout)과 executor endpoint **양쪽**에 있는가.
  ③ 캡처 도구 스키마 검증이 여전히 통과하는가(별 파일에서 잠근다).
  ④ 무부하 상태에서 모든 gauge 가 0 인가.

⚠️ 별 endpoint 를 만들지 않는 이유는 캡처 도구가 `ADMIN_PATH` **하나만** 읽기 때문이다 —
   별 endpoint 면 두 body 가 다른 순간이 되어 rollout 대비 비율이 서로 다른 시점을 나눈 값이 된다.
   ⛔ 단 같은 body 라고 **전체가 원자적인 것은 아니다**(worker/executor 값은 여러 lock 의 순차
   snapshot). 창 identity 는 `topic_auth_rollout` 이 pin 하고 신규 블록은 복제하지 않는다.
"""

# 표준 라이브러리
import unittest
from unittest.mock import patch

# 로컬
from app import main as app_main

_BLOCKS = ("handshakes", "topic_auth_rollout", "subscribe_load", "ws_auth_executor")


class ExposureTestCase(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _metrics():
        return (await app_main.get_ws_connection_metrics())["metrics"]


class TestFailureDomainsAreIndependent(ExposureTestCase):
    """[S7-①] 한 블록의 예외가 다른 블록을 지우지 않는다."""

    async def test_all_four_blocks_present_when_healthy(self):
        metrics = await self._metrics()
        for block in _BLOCKS:
            self.assertIn(block, metrics, f"{block} 블록이 없다")

    async def test_subscribe_load_failure_keeps_other_three(self):
        executor_sentinel = {"source": "executor"}
        rollout_sentinel = {"source": "rollout"}
        handshakes_sentinel = {"source": "handshakes"}
        with patch.object(app_main.subscribe_load_metrics, "subscribe_load_metrics",
                          side_effect=RuntimeError("주입")), \
                patch.object(app_main.auth_executor, "auth_executor_metrics",
                             return_value=executor_sentinel), \
                patch.object(app_main.manager.topic_auth_rollout, "snapshot",
                             return_value=rollout_sentinel), \
                patch.object(app_main.manager, "connection_metrics",
                             return_value={"handshakes": handshakes_sentinel}):
            metrics = await self._metrics()
        self.assertEqual(metrics["subscribe_load"], {"error": "unavailable"})
        self.assertEqual(metrics["ws_auth_executor"], executor_sentinel)
        self.assertEqual(metrics["topic_auth_rollout"]["source"], "rollout")
        self.assertEqual(metrics["handshakes"], handshakes_sentinel)

    async def test_executor_mirror_failure_keeps_other_three(self):
        subscribe_sentinel = {"source": "subscribe_load"}
        rollout_sentinel = {"source": "rollout"}
        handshakes_sentinel = {"source": "handshakes"}
        with patch.object(app_main.auth_executor, "auth_executor_metrics",
                          side_effect=RuntimeError("주입")), \
                patch.object(app_main.subscribe_load_metrics, "subscribe_load_metrics",
                             return_value=subscribe_sentinel), \
                patch.object(app_main.manager.topic_auth_rollout, "snapshot",
                             return_value=rollout_sentinel), \
                patch.object(app_main.manager, "connection_metrics",
                             return_value={"handshakes": handshakes_sentinel}):
            metrics = await self._metrics()
        self.assertEqual(metrics["ws_auth_executor"], {"error": "unavailable"})
        self.assertEqual(metrics["subscribe_load"], subscribe_sentinel)
        self.assertEqual(metrics["topic_auth_rollout"]["source"], "rollout")
        self.assertEqual(metrics["handshakes"], handshakes_sentinel)

    async def test_rollout_failure_keeps_the_new_blocks(self):
        """⚠️ S0 가 세운 규율(manager/rollout 실패가 창 관측을 끊지 않는다)이 신규 블록에도 성립."""
        subscribe_sentinel = {"source": "subscribe_load"}
        executor_sentinel = {"source": "executor"}
        handshakes_sentinel = {"source": "handshakes"}
        with patch.object(app_main.manager.topic_auth_rollout, "snapshot",
                          side_effect=RuntimeError("주입")), \
                patch.object(app_main.subscribe_load_metrics, "subscribe_load_metrics",
                             return_value=subscribe_sentinel), \
                patch.object(app_main.auth_executor, "auth_executor_metrics",
                             return_value=executor_sentinel), \
                patch.object(app_main.manager, "connection_metrics",
                             return_value={"handshakes": handshakes_sentinel}):
            metrics = await self._metrics()
        self.assertEqual(metrics["topic_auth_rollout"].get("error"), "unavailable")
        self.assertEqual(metrics["subscribe_load"], subscribe_sentinel)
        self.assertEqual(metrics["ws_auth_executor"], executor_sentinel)
        self.assertEqual(metrics["handshakes"], handshakes_sentinel)


class TestFlagIsOnBothSurfaces(ExposureTestCase):
    """[S7-②] `topic_dispatcher_enabled` 는 rollout 블록과 executor endpoint 양쪽에 있다."""

    async def test_flag_present_on_both(self):
        metrics = await self._metrics()
        self.assertIn("topic_dispatcher_enabled", metrics["topic_auth_rollout"])
        executor = await app_main.get_ws_auth_executor_metrics()
        self.assertIn("topic_dispatcher_enabled", executor,
                      "executor endpoint 에 flag 합성이 빠졌다")
        # shape 유지 — 기존 두 키가 그대로여야 운영 문서·참조가 안 깨진다.
        self.assertIn("metrics", executor)
        self.assertIn("running", executor)

    async def test_flag_survives_executor_metrics_failure(self):
        """flag 는 executor 계측과 독립적이라 fallback 응답에서도 보존된다."""
        with patch.object(app_main.config, "TOPIC_DISPATCHER_ENABLED", True), \
                patch.object(app_main.auth_executor, "auth_executor_metrics",
                             side_effect=RuntimeError("주입")):
            executor = await app_main.get_ws_auth_executor_metrics()
        self.assertIsNone(executor["metrics"])
        self.assertIsNone(executor["running"])
        self.assertEqual(executor["error"], "unavailable")
        self.assertIs(executor["topic_dispatcher_enabled"], True)


class TestIdleGaugesAreZero(ExposureTestCase):
    """[S7-④] 무부하 상태에서 gauge 는 0 이다."""

    async def test_gauges_are_zero_when_idle(self):
        """무부하에서 **gauge** 는 0 이다.

        ⛔ counter 를 여기 섞지 말 것 — `count`/`submitted_total` 은 **누적**이라 같은 프로세스에서
           앞서 돈 테스트의 값이 남는다(실측: 전체 스위트에서 `count=12` 로 red). 기준 ④ 의
           대상은 gauge 다. 누적 0 은 아래 fresh-state 테스트가 hermetic 하게 잠근다.
        """
        mirror = (await self._metrics())["ws_auth_executor"]
        for key in ("in_flight", "queued_now"):
            self.assertEqual(mirror[key], 0, f"무부하인데 gauge {key} 가 0 이 아니다")
        # 불변식은 이력과 무관하게 성립해야 한다.
        self.assertGreaterEqual(mirror["queued_now"], 0)
        self.assertGreaterEqual(mirror["in_flight_max"], mirror["in_flight"])

    async def test_fresh_process_reads_all_zero(self):
        """[S7-④] **새 프로세스**라면 counter 도 0 이다 — 공유 스위트에선 hermetic 하게 시험한다."""
        live = app_main.auth_executor._metrics
        blank = {k: ({} if isinstance(v, dict) else type(v)()) for k, v in live.items()}
        with patch.object(app_main.auth_executor, "_metrics", blank):
            mirror = (await self._metrics())["ws_auth_executor"]
        for key in ("in_flight", "queued_now", "count", "submitted_total", "ledger_errors_total"):
            self.assertEqual(mirror[key], 0, f"새 프로세스인데 {key} 가 0 이 아니다")

    async def test_mirror_is_the_same_source_not_a_copy(self):
        """미러는 **수치 복사가 아니라 한 소스의 두 view** 다 — 같은 함수를 부른다."""
        sentinel = {"count": 7, "queued_now": 0}
        with patch.object(app_main.auth_executor, "auth_executor_metrics", return_value=sentinel):
            metrics = await self._metrics()
            executor = await app_main.get_ws_auth_executor_metrics()
        self.assertEqual(metrics["ws_auth_executor"], sentinel)
        self.assertEqual(executor["metrics"], sentinel)


if __name__ == "__main__":
    unittest.main()
