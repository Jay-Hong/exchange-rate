"""`GET /admin/api/default-executor-probe` — **노출 배선**(route + auth + shape + 의미).

⛔ 왜 따로 두는가: 수명주기·집계 로직은 `test_default_executor_probe.py` 가 본다. 하지만 운영자가
   canary 에서 실제로 읽는 것은 **endpoint 응답**이다. 그 노출을 잠그지 않으면 `running` 필드를
   **통째로 지워도 전체 suite 가 통과한다**(실측: SURVIVED). 이 세션에서 반복된 형태 —
   helper 는 잠겼는데 그 helper 를 밖으로 내보내는 한 줄이 안 잠긴 경우다.
⛔ 그리고 `running` 은 **설정값 에코가 아니어야** 한다. `enabled` 로 대체해도 통과한다면 smoke 는
   "flag 를 켰다"만 확인하고 **"probe 가 실제로 돈다"는 끝내 모른다** — sentinel 전체의 존재
   이유가 바로 그 구분이다.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app import config, default_executor_probe as probe
from app.main import app, verify_admin

PATH = "/admin/api/default-executor-probe"


class _LiveTaskStub:
    """`asyncio.Task` 대역 — 살아 있는 task 를 loop 없이 흉내 낸다."""

    def done(self) -> bool:
        return False


class TestDefaultExecutorProbeEndpoint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        app.dependency_overrides[verify_admin] = lambda: "admin"
        cls.client = TestClient(app)          # lifespan 미진입 (A1 패턴)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(verify_admin, None)

    def setUp(self):
        self._task = probe._probe_task
        self._enabled = config.DEFAULT_EXECUTOR_PROBE_ENABLED
        probe.reset_default_executor_probe_metrics()

    def tearDown(self):
        probe._probe_task = self._task
        config.DEFAULT_EXECUTOR_PROBE_ENABLED = self._enabled
        probe.reset_default_executor_probe_metrics()

    def test_returns_200_with_probe_shape(self):
        body = self.client.get(PATH).json()
        for key in ("enabled", "running", "metrics"):
            self.assertIn(key, body, f"{key} 가 노출되지 않는다")
        for key in ("submitted_count", "started_count", "outstanding",
                    "queue_delay_ms_max", "histogram"):
            self.assertIn(key, body["metrics"], f"metrics.{key} 가 노출되지 않는다")

    def test_running_is_task_state_not_a_config_echo(self):
        """⛔ flag 는 켰는데 probe 가 안 도는 상태를 **구분해야** 한다."""
        config.DEFAULT_EXECUTOR_PROBE_ENABLED = True
        probe._probe_task = None                      # 기동 안 됨

        body = self.client.get(PATH).json()
        self.assertTrue(body["enabled"], "설정값이 반영되지 않았다")
        self.assertFalse(body["running"],
                         "task 가 없는데 running=true — running 이 설정값 에코다")

    def test_running_is_true_only_while_a_live_task_exists(self):
        config.DEFAULT_EXECUTOR_PROBE_ENABLED = True
        probe._probe_task = _LiveTaskStub()
        self.assertTrue(self.client.get(PATH).json()["running"],
                        "살아 있는 task 가 있는데 running=false")

        probe._probe_task = None
        self.assertFalse(self.client.get(PATH).json()["running"])

    def test_exposes_outstanding_so_saturation_is_visible(self):
        """⛔ 완전 포화는 `started_count == 0` 으로 보인다 — `outstanding` 이 함께 나가지 않으면
        운영자는 그것을 **"아직 표본 없음"과 구분할 수 없다**."""
        probe._record_submitted()                     # 제출됐지만 worker 미시작 = 갇힘

        metrics = self.client.get(PATH).json()["metrics"]
        self.assertEqual(metrics["submitted_count"], 1)
        self.assertEqual(metrics["started_count"], 0)
        self.assertEqual(metrics["outstanding"], 1, "포화가 endpoint 로 드러나지 않는다")
