"""USDT_LEGACY_REST_POLLING_ENABLED flag + _register_usdt_legacy_polling_job 검증.

배경:
    상시 USDT REST polling(`collect_usdt_rates`)은 WS 도입 전 과도기 잔재.
    WS + source-specific REST fallback probe가 동일 fanout(Redis/DB/Alert)을
    모두 처리하므로 상시 polling은 중복. default 비활성 + flag로 rollback.

검증 범위:
    1. flag=false → add_job 호출 X + disabled log
    2. flag=true → add_job 호출 1회 (기존 cron 형식 보존)
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app import scheduler


class TestRegisterUsdtLegacyPollingJob(unittest.TestCase):

    def test_flag_false_skips_registration(self):
        """flag=false → scheduler.add_job 호출 X + False 반환 + disabled log."""
        fake_scheduler = MagicMock()
        with patch.object(scheduler.config, "USDT_LEGACY_REST_POLLING_ENABLED", False), \
             patch.object(scheduler.logger, "info") as mock_info:
            result = scheduler._register_usdt_legacy_polling_job(fake_scheduler)
        self.assertFalse(result)
        fake_scheduler.add_job.assert_not_called()
        # disabled log emit 확인 (rollback 안내 포함)
        log_msgs = [c.args[0] for c in mock_info.call_args_list]
        self.assertTrue(
            any("legacy polling disabled" in msg for msg in log_msgs),
            f"disabled log 미발견. logs={log_msgs}",
        )

    def test_flag_true_registers_cron(self):
        """flag=true → add_job 1회 호출 + 기존 cron 형식(id, second, max_instances) 보존."""
        fake_scheduler = MagicMock()
        with patch.object(scheduler.config, "USDT_LEGACY_REST_POLLING_ENABLED", True), \
             patch.object(scheduler.logger, "info"):
            result = scheduler._register_usdt_legacy_polling_job(fake_scheduler)
        self.assertTrue(result)
        fake_scheduler.add_job.assert_called_once()
        call = fake_scheduler.add_job.call_args
        # id / max_instances / coalesce / misfire_grace_time 잠금
        self.assertEqual(call.kwargs["id"], "usdt_sources")
        self.assertEqual(call.kwargs["max_instances"], 1)
        self.assertTrue(call.kwargs["coalesce"])
        self.assertEqual(call.kwargs["misfire_grace_time"], 5)
        # CronTrigger second='6,16,26,36,46,56' 잠금 — 기존 운영 형식 보존
        trigger = call.args[1]
        # CronTrigger는 fields list로 표현 — 'second' field 검증
        second_field = next(
            (f for f in trigger.fields if f.name == "second"), None,
        )
        self.assertIsNotNone(second_field)
        self.assertEqual(str(second_field), "6,16,26,36,46,56")


if __name__ == "__main__":
    unittest.main(verbosity=2)
