"""Bank/Investing β PR C — C1 Increment 2 emission tests (§6.6.2).

axis #2 SET-only outcome / axis #3 alert 독립성 / axis #4 legacy-mode gate +
fx/usdt:krw routing. 판별 assertion (Meta E) — vacuous 금지.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, models


def _updates(*pairs):
    """(source, asset) → topic-native update dict 리스트."""
    return [
        {"source": s, "asset": a, "rate": 1.0, "timestamp": "t"} for s, a in pairs
    ]


# ── axis #2: SET-only outcome (writer가 SET 성공분만 반환) ──────────────

class TestWriteChangedReturnsSucceeded(unittest.TestCase):

    def test_bank_returns_only_set_succeeded(self):
        def fake_set(bank, asset, rate, timestamp):
            return asset == "usd-krw"  # usd ok, jpy fail

        with patch(
            "app.latest_rates_cache.set_latest_bank_rate_from_sync_job",
            side_effect=fake_set,
        ):
            result = crud._write_changed_bank_rates_to_redis(
                _updates(("kb", "usd-krw"), ("kb", "jpy-krw"))
            )
        self.assertEqual([u["asset"] for u in result], ["usd-krw"])

    def test_bank_empty_returns_empty(self):
        self.assertEqual(crud._write_changed_bank_rates_to_redis([]), [])

    def test_bank_exception_excluded(self):
        with patch(
            "app.latest_rates_cache.set_latest_bank_rate_from_sync_job",
            side_effect=RuntimeError("boom"),
        ):
            result = crud._write_changed_bank_rates_to_redis(_updates(("kb", "usd-krw")))
        self.assertEqual(result, [])  # 예외분은 trigger 대상 제외

    def test_investing_returns_only_set_succeeded(self):
        def fake_set(asset, rate, timestamp):
            return asset == "usd-krw"

        with patch(
            "app.latest_rates_cache.set_latest_investing_rate_from_sync_job",
            side_effect=fake_set,
        ):
            result = crud._write_changed_investing_rates_to_redis(
                _updates(("investing", "usd-krw"), ("investing", "eur-krw"))
            )
        self.assertEqual([u["asset"] for u in result], ["usd-krw"])


# ── routing: fx:* 전부 + usdt:krw cross-route 조건 ─────────────────────

class TestRunTopicEmissionRouting(unittest.TestCase):

    def test_fx_trigger_for_all_changes(self):
        with patch("app.fx_topic_trigger.request_fx_topic_trigger") as fx, patch(
            "app.tether_topic_trigger.request_tether_topic_trigger"
        ):
            crud._run_topic_emission(
                _updates(("kb", "usd-krw"), ("sc", "jpy-krw")), "direct_coalesced"
            )
        self.assertEqual(
            sorted(c.args[1] for c in fx.call_args_list), ["jpy-krw", "usd-krw"]
        )

    def test_cross_route_direct_for_kb_hana_investing_usdkrw(self):
        with patch("app.fx_topic_trigger.request_fx_topic_trigger"), patch(
            "app.tether_topic_trigger.request_tether_topic_trigger"
        ) as teth:
            crud._run_topic_emission(
                _updates(("kb", "usd-krw"), ("hana", "usd-krw"), ("investing", "usd-krw")),
                "direct_coalesced",
            )
        self.assertEqual(
            sorted(c.kwargs["source"] for c in teth.call_args_list),
            ["hana", "investing", "kb"],
        )

    def test_no_cross_route_for_non_tether_bank(self):
        with patch("app.fx_topic_trigger.request_fx_topic_trigger"), patch(
            "app.tether_topic_trigger.request_tether_topic_trigger"
        ) as teth:
            crud._run_topic_emission(_updates(("sc", "usd-krw")), "direct_coalesced")
        teth.assert_not_called()  # sc는 cross 대상 아님

    def test_no_cross_route_for_non_usdkrw(self):
        with patch("app.fx_topic_trigger.request_fx_topic_trigger"), patch(
            "app.tether_topic_trigger.request_tether_topic_trigger"
        ) as teth:
            crud._run_topic_emission(_updates(("kb", "jpy-krw")), "direct_coalesced")
        teth.assert_not_called()  # jpy는 usdt:krw context 아님

    def test_dual_shadow_does_not_call_tether(self):
        """dual_shadow: fx는 발사하되 live tether는 호출 안 함 (발행 방지)."""
        with patch("app.fx_topic_trigger.request_fx_topic_trigger") as fx, patch(
            "app.tether_topic_trigger.request_tether_topic_trigger"
        ) as teth:
            crud._run_topic_emission(_updates(("kb", "usd-krw")), "dual_shadow")
        fx.assert_called_once()
        teth.assert_not_called()

    def test_investing_vs_bank_reason(self):
        from app.fx_topic_trigger import (
            FX_TRIGGER_REASON_BANK_CHANGE,
            FX_TRIGGER_REASON_INVESTING_CHANGE,
        )

        with patch("app.fx_topic_trigger.request_fx_topic_trigger") as fx, patch(
            "app.tether_topic_trigger.request_tether_topic_trigger"
        ):
            crud._run_topic_emission(
                _updates(("investing", "usd-krw"), ("kb", "usd-krw")), "direct_coalesced"
            )
        reasons = {c.args[0]: c.args[2] for c in fx.call_args_list}
        self.assertEqual(reasons["investing"], FX_TRIGGER_REASON_INVESTING_CHANGE)
        self.assertEqual(reasons["kb"], FX_TRIGGER_REASON_BANK_CHANGE)


# ── axis #4: legacy-mode gate (bridge 호출 자체 X) ────────────────────

class TestEmitTopicTriggersGate(unittest.TestCase):

    def test_legacy_piggyback_no_bridge_call(self):
        with patch("app.config.BANK_INVESTING_TOPIC_TRIGGER_MODE", "legacy_piggyback"), patch(
            "app.topic_trigger_bridge.schedule_on_loop"
        ) as sched:
            crud._emit_topic_triggers(_updates(("kb", "usd-krw")))
        sched.assert_not_called()  # marshal 자체 안 함 (zero overhead)

    def test_direct_calls_bridge_with_run_emission(self):
        with patch("app.config.BANK_INVESTING_TOPIC_TRIGGER_MODE", "direct_coalesced"), patch(
            "app.topic_trigger_bridge.schedule_on_loop"
        ) as sched:
            crud._emit_topic_triggers(_updates(("kb", "usd-krw")))
        sched.assert_called_once()
        self.assertIs(sched.call_args.args[0], crud._run_topic_emission)
        self.assertEqual(sched.call_args.args[2], "direct_coalesced")

    def test_empty_no_bridge_call(self):
        with patch("app.topic_trigger_bridge.schedule_on_loop") as sched:
            crud._emit_topic_triggers([])
        sched.assert_not_called()


# ── axis #2+#3 통합: orchestrator wiring (real in-memory DB) ───────────

class TestInsertOrchestratorWiring(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_emission_set_only_alert_all(self):
        """SET usd ok / jpy fail → emission은 usd만, alert는 둘 다 (axis #2+#3)."""
        db = self.Session()
        try:
            def fake_set(bank, asset, rate, timestamp):
                return asset == "usd-krw"

            with patch(
                "app.latest_rates_cache.set_latest_bank_rate_from_sync_job",
                side_effect=fake_set,
            ), patch("app.crud.process_rate_alerts", return_value=0) as alerts, patch(
                "app.crud._emit_topic_triggers"
            ) as emit:
                crud.insert_bank_rates_into_db(
                    db, {"usd-krw": 1400.0, "jpy-krw": 9.0}, "kb"
                )
            alert_changes = alerts.call_args.args[1]
            self.assertEqual(
                {r["currency"] for r in alert_changes}, {"usd-krw", "jpy-krw"}
            )  # alert = 전 change (SET 무관)
            self.assertEqual(
                [u["asset"] for u in emit.call_args.args[0]], ["usd-krw"]
            )  # emission = SET 성공분만
        finally:
            db.close()

    def test_alert_fires_even_when_all_set_fail(self):
        """Redis SET 전부 실패해도 alert 발화 (axis #3 — Redis 장애 시 알림 누락 0)."""
        db = self.Session()
        try:
            with patch(
                "app.latest_rates_cache.set_latest_bank_rate_from_sync_job",
                return_value=False,
            ), patch("app.crud.process_rate_alerts", return_value=0) as alerts, patch(
                "app.crud._emit_topic_triggers"
            ) as emit:
                crud.insert_bank_rates_into_db(db, {"usd-krw": 1400.0}, "kb")
            alerts.assert_called_once()
            self.assertEqual(emit.call_args.args[0], [])  # emission 빈 리스트
        finally:
            db.close()

    def test_emission_exception_isolated(self):
        """emission 예외가 저장 흐름에 전파 X (try/except 격리)."""
        db = self.Session()
        try:
            with patch(
                "app.latest_rates_cache.set_latest_bank_rate_from_sync_job",
                return_value=True,
            ), patch("app.crud.process_rate_alerts", return_value=0), patch(
                "app.crud._emit_topic_triggers", side_effect=RuntimeError("boom")
            ):
                count = crud.insert_bank_rates_into_db(db, {"usd-krw": 1400.0}, "kb")
            self.assertEqual(count, 1)  # 예외 격리 → count 정상 반환
        finally:
            db.close()

    def test_investing_emission_set_only_alert_all(self):
        """Investing 대칭 통합 (Bank와 의도적 중복 구현) — SET-only + alert 독립성."""
        db = self.Session()
        try:
            def fake_set(asset, rate, timestamp):
                return asset == "usd-krw"  # usd ok, jpy fail

            with patch(
                "app.latest_rates_cache.set_latest_investing_rate_from_sync_job",
                side_effect=fake_set,
            ), patch("app.crud.process_rate_alerts", return_value=0) as alerts, patch(
                "app.crud._emit_topic_triggers"
            ) as emit:
                crud.insert_investing_rates_into_db(
                    db, {"usd-krw": 1400.0, "jpy-krw": 9.0}
                )
            self.assertEqual(
                {r["currency"] for r in alerts.call_args.args[1]},
                {"usd-krw", "jpy-krw"},
            )  # alert = 전 change
            self.assertEqual(
                [u["asset"] for u in emit.call_args.args[0]], ["usd-krw"]
            )  # emission = SET 성공분만
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
