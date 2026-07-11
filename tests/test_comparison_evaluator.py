"""비교알림 evaluator (ADR-037 S2) 단위 테스트.

conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 설정.
커버: spread_matches 4조합 / delivery_allowed once·repeat 재사용 / in-flight claim dedup /
get_latest_rate_unified DB fallback / mark once·repeat 분기 / persist(log+토큰 정리) /
emit flag gate / e2e 발사 경로(mock sender).
"""
import asyncio
import unittest
from datetime import timedelta
from unittest.mock import patch

from app import crud, models
from app.crud import WIDE_GAP
from app.database import SessionLocal, engine
from app.models import get_utc_now
from app.notifications.comparison_evaluator import (
    ComparisonAlertEvaluator,
    ComparisonCandidate,
    FreshComparisonSnapshot,
    UnifiedRate,
    emit_comparison_observation,
    get_latest_rate_unified,
    spread_matches,
)

models.Base.metadata.create_all(engine)


def _mk_alert(db, **over):
    """ComparisonAlert row helper — 김프(빗썸−인베스팅) gte 8 기본."""
    defaults = dict(
        user_id="u1", tab="tether",
        left_source="bithumb", left_asset="usdt-krw",
        right_source="investing", right_asset="usd-krw",
        diff_type="signed", operator="gte", threshold=8.0,
        enabled=True, triggered=False,
    )
    defaults.update(over)
    row = models.ComparisonAlert(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _cleanup(db):
    db.query(models.ComparisonNotificationLog).delete()
    db.query(models.ComparisonAlert).delete()
    db.query(models.UserDevice).delete()
    db.query(models.SourceRate).delete()
    db.query(models.BankExchangeRate).delete()
    db.query(models.InvestingExchangeRate).delete()
    db.commit()


class TestSpreadMatches(unittest.TestCase):
    """Decision D 4조합 + 경계값 + fail-closed."""

    def test_signed_gte_kimchi(self):
        self.assertTrue(spread_matches("signed", "gte", 8.0, 8.0))    # 경계 포함
        self.assertTrue(spread_matches("signed", "gte", 8.0, 12.3))
        self.assertFalse(spread_matches("signed", "gte", 8.0, 7.99))
        self.assertFalse(spread_matches("signed", "gte", 8.0, -9.0))  # 역프는 매칭 안 됨 (signed)

    def test_signed_lte_reverse(self):
        self.assertTrue(spread_matches("signed", "lte", -3.0, -3.0))
        self.assertTrue(spread_matches("signed", "lte", -3.0, -5.0))
        self.assertFalse(spread_matches("signed", "lte", -3.0, -2.9))
        self.assertFalse(spread_matches("signed", "lte", -3.0, 10.0))

    def test_absolute_gte_divergence(self):
        self.assertTrue(spread_matches("absolute", "gte", 10.0, -12.0))  # 방향 무관
        self.assertTrue(spread_matches("absolute", "gte", 10.0, 10.0))
        self.assertFalse(spread_matches("absolute", "gte", 10.0, 9.5))

    def test_absolute_lte_convergence(self):
        self.assertTrue(spread_matches("absolute", "lte", 1.0, -0.5))
        self.assertTrue(spread_matches("absolute", "lte", 1.0, 1.0))
        self.assertFalse(spread_matches("absolute", "lte", 1.0, -1.01))

    def test_unknown_fail_closed(self):
        self.assertFalse(spread_matches("percent", "gte", 1.0, 100.0))
        self.assertFalse(spread_matches("signed", "eq", 1.0, 1.0))


class TestDeliveryAllowedReuse(unittest.TestCase):
    """delivery_allowed duck-type 재사용 — FreshComparisonSnapshot 4필드 호환 (ADR-036 gate)."""

    def _snap(self, **over):
        defaults = dict(
            setting_id=1, enabled=True, triggered=False, tab="tether",
            left_source="bithumb", left_asset="usdt-krw",
            right_source="investing", right_asset="usd-krw",
            diff_type="signed", operator="gte", threshold=8.0,
        )
        defaults.update(over)
        return FreshComparisonSnapshot(**defaults)

    def test_once_mode(self):
        from app.notifications.alert_evaluator import delivery_allowed
        now = get_utc_now()
        self.assertTrue(delivery_allowed(self._snap(), now))
        self.assertFalse(delivery_allowed(self._snap(triggered=True), now))
        self.assertFalse(delivery_allowed(self._snap(enabled=False), now))

    def test_repeat_mode_interval_gate(self):
        from app.notifications.alert_evaluator import delivery_allowed
        now = get_utc_now()
        # 미발송 → 허용
        self.assertTrue(delivery_allowed(self._snap(repeat_interval_sec=300), now))
        # 간격 미경과 → 차단 / 경과 → 허용 (triggered 무관 — repeat는 once 종료 플래그 미사용)
        recent = now - timedelta(seconds=200)
        old = now - timedelta(seconds=301)
        self.assertFalse(delivery_allowed(
            self._snap(repeat_interval_sec=300, last_notified_at=recent, triggered=True), now))
        self.assertTrue(delivery_allowed(
            self._snap(repeat_interval_sec=300, last_notified_at=old, triggered=True), now))


class TestUnifiedLookup(unittest.TestCase):
    """get_latest_rate_unified — Redis 실패/부재 시 DB fallback + origin/observed_at."""

    def setUp(self):
        self.db = SessionLocal()
        _cleanup(self.db)

    def tearDown(self):
        _cleanup(self.db)
        self.db.close()

    def test_db_fallback_per_world(self):
        ts = get_utc_now()
        self.db.add(models.SourceRate(source="bithumb", asset="usdt-krw", rate=1510.0, timestamp=ts))
        self.db.add(models.BankExchangeRate(bank="kb", currency="usd-krw", rate=1500.5, timestamp=ts))
        self.db.add(models.InvestingExchangeRate(currency="usd-krw", rate=1502.0, timestamp=ts))
        self.db.commit()

        # Redis 헬퍼는 테스트 환경에 없음(연결 실패 → except → None) → DB fallback 경로 검증
        with patch("app.latest_rates_cache.get_latest_usdt_rate_from_sync_job", return_value=None), \
             patch("app.latest_rates_cache.get_latest_bank_rate_from_sync_job", return_value=None), \
             patch("app.latest_rates_cache.get_latest_investing_rate_from_sync_job", return_value=None):
            r1 = get_latest_rate_unified("bithumb", "usdt-krw")
            r2 = get_latest_rate_unified("kb", "usd-krw")
            r3 = get_latest_rate_unified("investing", "usd-krw")

        self.assertEqual((r1.rate, r1.origin), (1510.0, "db"))
        self.assertEqual((r2.rate, r2.origin), (1500.5, "db"))
        self.assertEqual((r3.rate, r3.origin), (1502.0, "db"))
        self.assertIsNotNone(r1.observed_at)   # codex B3 — observed_at 동봉

    def test_redis_hit_takes_priority(self):
        with patch("app.latest_rates_cache.get_latest_usdt_rate_from_sync_job",
                   return_value={"source": "bithumb", "asset": "usdt-krw", "rate": 1515.0,
                                 "timestamp": "2026-07-03T10:00:00+09:00",
                                 "rate_changed_at": "2026-07-03T10:00:00+09:00"}):
            r = get_latest_rate_unified("bithumb", "usdt-krw")
        self.assertEqual((r.rate, r.origin), (1515.0, "redis"))
        self.assertIsNotNone(r.observed_at)

    def test_missing_everywhere_returns_none(self):
        with patch("app.latest_rates_cache.get_latest_usdt_rate_from_sync_job", return_value=None):
            self.assertIsNone(get_latest_rate_unified("bithumb", "usdt-krw"))

    def test_krx_redis_hit_and_db_fallback(self):
        """KRX 달러선물 — usd absolute 비교 발화 경로의 시세 조회 (ADR-038 D4, codex NB2)."""
        # Redis hit
        with patch("app.latest_rates_cache.get_latest_krx_rate_from_sync_job",
                   return_value={"source": "krx", "asset": "usd-krw-futures", "rate": 1500.4,
                                 "timestamp": "2026-07-10T10:00:00+09:00"}):
            r = get_latest_rate_unified("krx", "usd-krw-futures")
        self.assertEqual((r.rate, r.origin), (1500.4, "redis"))
        # DB fallback (source_rates)
        ts = get_utc_now()
        self.db.add(models.SourceRate(source="krx", asset="usd-krw-futures", rate=1499.9, timestamp=ts))
        self.db.commit()
        with patch("app.latest_rates_cache.get_latest_krx_rate_from_sync_job", return_value=None):
            r2 = get_latest_rate_unified("krx", "usd-krw-futures")
        self.assertEqual((r2.rate, r2.origin), (1499.9, "db"))


class TestMarkTriggered(unittest.TestCase):
    """mark_comparison_alert_triggered — once/repeat 분기 + last_notified_spread (signed raw)."""

    def setUp(self):
        self.db = SessionLocal()
        _cleanup(self.db)

    def tearDown(self):
        _cleanup(self.db)
        self.db.close()

    def test_once_disables(self):
        row = _mk_alert(self.db)
        crud.mark_comparison_alert_triggered(self.db, row.id, 9.5)
        self.db.refresh(row)
        self.assertTrue(row.triggered)
        self.assertFalse(row.enabled)
        self.assertEqual(row.last_notified_spread, 9.5)
        self.assertIsNotNone(row.last_notified_at)

    def test_repeat_keeps_enabled(self):
        row = _mk_alert(self.db, repeat_interval_sec=300)
        crud.mark_comparison_alert_triggered(self.db, row.id, -4.2)
        self.db.refresh(row)
        self.assertFalse(row.triggered)
        self.assertTrue(row.enabled)
        self.assertEqual(row.last_notified_spread, -4.2)   # signed raw 보존


class TestEvaluatorE2E(unittest.TestCase):
    """평가 e2e (mock sender) — 발사/로그/claim dedup/조건 미충족 skip."""

    def setUp(self):
        self.db = SessionLocal()
        _cleanup(self.db)
        self.db.add(models.UserDevice(user_id="u1", device_token="tok1", platform="ios"))
        self.db.commit()
        self.sent = []

        def fake_sender(tokens, title, body, data):
            self.sent.append((tuple(tokens), data))
            return {"success_count": len(tokens), "failure_count": 0, "failed_tokens": []}

        self.evaluator = ComparisonAlertEvaluator(sender=fake_sender)

    def tearDown(self):
        _cleanup(self.db)
        self.db.close()

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def _patch_rates(self, left_rate, right_rate):
        def fake_unified(source, asset):
            if source == "bithumb":
                return UnifiedRate(rate=left_rate, observed_at=get_utc_now(), origin="redis")
            return UnifiedRate(rate=right_rate, observed_at=get_utc_now(), origin="db")
        return patch("app.notifications.comparison_evaluator.get_latest_rate_unified",
                     side_effect=fake_unified)

    def test_usd_absolute_krx_fires_e2e(self):
        """ADR-038 D4 — usd absolute 달러선물↔하나 발화 E2E (codex NB2).

        canonical 정렬로 (hana, krx) 저장 — krx tick 관측으로 평가돼 |spread|≥threshold 발사.
        payload title은 달러선물 표시명 사용 (raw 코드 아님)."""
        row = _mk_alert(self.db, tab="usd", diff_type="absolute", operator="gte", threshold=5.0,
                        left_source="hana", left_asset="usd-krw",
                        right_source="krx", right_asset="usd-krw-futures")

        def fake_unified(source, asset):
            if source == "hana":
                return UnifiedRate(rate=1503.4, observed_at=get_utc_now(), origin="redis")
            if source == "krx":
                return UnifiedRate(rate=1510.0, observed_at=get_utc_now(), origin="redis")
            return None

        with patch("app.notifications.comparison_evaluator.get_latest_rate_unified",
                   side_effect=fake_unified):
            # krx tick 관측으로 진입 (KrxAlertTickHandler → _emit_comparison 경로 시뮬)
            self._run(self.evaluator._evaluate_async("krx", "usd-krw-futures"))

        self.assertEqual(len(self.sent), 1)
        _, data = self.sent[0]
        self.assertEqual(data["type"], "comparison_alert")
        self.assertEqual(data["tab"], "usd")
        self.assertEqual(data["diff_type"], "absolute")
        # |1503.4 - 1510.0| = 6.6 ≥ 5 발사, spread는 signed raw
        self.assertAlmostEqual(float(data["spread"]), -6.6, places=6)
        self.assertEqual((data["left_source"], data["right_source"]), ("hana", "krx"))
        row2 = self.db.query(models.ComparisonAlert).get(row.id)
        self.db.refresh(row2)
        self.assertFalse(row2.enabled)   # once → 발사 후 비활성

    def test_fires_and_persists_log(self):
        row = _mk_alert(self.db)   # 김프 gte 8
        with self._patch_rates(1512.0, 1502.5):   # spread=9.5 ≥ 8 → 발사
            self._run(self.evaluator._evaluate_async("bithumb", "usdt-krw"))

        self.assertEqual(len(self.sent), 1)
        tokens, data = self.sent[0]
        self.assertEqual(tokens, ("tok1",))
        self.assertEqual(data["type"], "comparison_alert")
        self.assertEqual(data["spread"], "9.5")
        self.assertEqual(data["is_repeat"], "false")
        self.assertNotEqual(data["left_observed_at"], "")   # codex B3

        # persist: mark(once → disabled) + log 1건
        self.db.expire_all()
        fresh = self.db.get(models.ComparisonAlert, row.id)
        self.assertFalse(fresh.enabled)
        logs = self.db.query(models.ComparisonNotificationLog).all()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].spread, 9.5)
        self.assertEqual(logs[0].left_rate, 1512.0)
        self.assertFalse(logs[0].is_repeat)
        self.assertTrue(logs[0].success)

    def test_condition_not_met_no_send(self):
        _mk_alert(self.db)   # gte 8
        with self._patch_rates(1505.0, 1502.5):   # spread=2.5 < 8
            self._run(self.evaluator._evaluate_async("bithumb", "usdt-krw"))
        self.assertEqual(len(self.sent), 0)
        self.assertEqual(self.db.query(models.ComparisonNotificationLog).count(), 0)

    def test_dual_trigger_right_leg_also_evaluates(self):
        """right leg(investing) tick으로도 같은 알림 평가 (dual-trigger)."""
        _mk_alert(self.db)
        with self._patch_rates(1512.0, 1502.5):
            self._run(self.evaluator._evaluate_async("investing", "usd-krw"))
        self.assertEqual(len(self.sent), 1)

    def test_in_flight_claim_blocks_concurrent_duplicate(self):
        """left/right 두 평가가 동시에 같은 setting을 칠 때 1회만 발사 (claim)."""
        _mk_alert(self.db)

        async def both():
            with self._patch_rates(1512.0, 1502.5):
                await asyncio.gather(
                    self.evaluator._evaluate_async("bithumb", "usdt-krw"),
                    self.evaluator._evaluate_async("investing", "usd-krw"),
                )
        self._run(both())
        # claim이 동시 실행 중복을 차단 — refetch gate(once→disabled)가 2차 방어라
        # 순차 실행이어도 최대 1회. 어느 경로든 정확히 1 발사.
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.db.query(models.ComparisonNotificationLog).count(), 1)

    def test_repeat_first_fire_is_repeat_true(self):
        """repeat 모드 첫 발화도 is_repeat=true (ADR-036 payload-flag 계약 — codex B1)."""
        _mk_alert(self.db, repeat_interval_sec=300)
        with self._patch_rates(1512.0, 1502.5):
            self._run(self.evaluator._evaluate_async("bithumb", "usdt-krw"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["is_repeat"], "true")
        log = self.db.query(models.ComparisonNotificationLog).one()
        self.assertTrue(log.is_repeat)

    def test_pair_changed_in_ttl_window_skips(self):
        """cache TTL 창 내 pair 변경 → refetch 검증이 skip (codex B2 — old pair 값 오발송 방지)."""
        row = _mk_alert(self.db)
        # 캐시 로드 유도 후 pair 변경 (cache는 아직 old pair candidate 보유)
        self._run(self.evaluator._get_candidates("bithumb", "usdt-krw"))
        row.right_source = "kb"
        self.db.commit()
        with self._patch_rates(1512.0, 1502.5):
            self._run(self.evaluator._evaluate_async("bithumb", "usdt-krw"))
        self.assertEqual(len(self.sent), 0)
        self.assertEqual(self.db.query(models.ComparisonNotificationLog).count(), 0)

    def test_leg_missing_skips_gracefully(self):
        _mk_alert(self.db)
        with patch("app.notifications.comparison_evaluator.get_latest_rate_unified",
                   return_value=None):
            self._run(self.evaluator._evaluate_async("bithumb", "usdt-krw"))
        self.assertEqual(len(self.sent), 0)


class TestBuildPayloadMessages(unittest.TestCase):
    """푸시 문구 포맷 잠금 (사용자 2026-07-09) — 비교(↔·차이·N원) / 김프(-·김프·부호값%)."""

    def _fresh(self, **over):
        d = dict(setting_id=1, enabled=True, triggered=False, tab="tether",
                 left_source="upbit", left_asset="usdt-krw",
                 right_source="bithumb", right_asset="usdt-krw",
                 diff_type="absolute", operator="gte", threshold=3.0)
        d.update(over)
        return FreshComparisonSnapshot(**d)

    def _cand(self, fresh):
        return ComparisonCandidate(
            setting_id=fresh.setting_id, user_id="u", tab=fresh.tab,
            left_source=fresh.left_source, left_asset=fresh.left_asset,
            right_source=fresh.right_source, right_asset=fresh.right_asset,
            diff_type=fresh.diff_type, operator=fresh.operator, threshold=fresh.threshold,
            device_tokens=("t",),
        )

    def _rate(self, v):
        return UnifiedRate(rate=v, observed_at=get_utc_now(), origin="redis")

    def test_absolute_comparison_message(self):
        fresh = self._fresh(diff_type="absolute", operator="gte", threshold=3.0)
        title, body, data = ComparisonAlertEvaluator._build_payload(
            fresh, self._cand(fresh), self._rate(1509.0), self._rate(1505.0), spread=4.0)
        self.assertEqual(title, f"📊 업비트 ↔ 빗썸{WIDE_GAP}차이{WIDE_GAP}4")
        self.assertEqual(body, "[ 3 ↑이상 도달]")
        self.assertEqual(data["type"], "comparison_alert")

    def test_signed_kimchi_message_with_percent(self):
        # 빗썸(거래소) − 하나(환율): 김프, 부호값 + percent, 은행명 '은행' 접미 제거
        fresh = self._fresh(diff_type="signed", operator="lte", threshold=-20.0,
                            left_source="bithumb", left_asset="usdt-krw",
                            right_source="hana", right_asset="usd-krw")
        title, body, _ = ComparisonAlertEvaluator._build_payload(
            fresh, self._cand(fresh), self._rate(1483.3), self._rate(1508.0), spread=-24.7)
        self.assertEqual(title, f"📊 빗썸 - 하나{WIDE_GAP}김프{WIDE_GAP}-24.7 (-1.64%)")
        self.assertEqual(body, "[ -20 ↓이하 도달]")

    def test_signed_zero_right_rate_no_percent(self):
        # right.rate=0 → division 가드 (percent 생략)
        fresh = self._fresh(diff_type="signed", operator="gte", threshold=5.0,
                            left_source="bithumb", left_asset="usdt-krw",
                            right_source="hana", right_asset="usd-krw")
        title, _, _ = ComparisonAlertEvaluator._build_payload(
            fresh, self._cand(fresh), self._rate(10.0), self._rate(0.0), spread=10.0)
        self.assertEqual(title, f"📊 빗썸 - 하나{WIDE_GAP}김프{WIDE_GAP}10")   # percent 없음


class TestEmitGate(unittest.TestCase):
    """emit_comparison_observation flag gate — off면 zero-overhead (marshal 미시도)."""

    def test_flag_off_no_marshal(self):
        with patch("app.config.COMPARISON_ALERT_ENABLED", False), \
             patch("app.topic_trigger_bridge.schedule_on_loop") as mock_marshal:
            emit_comparison_observation("bithumb", "usdt-krw")
        mock_marshal.assert_not_called()

    def test_flag_on_sync_thread_marshals(self):
        with patch("app.config.COMPARISON_ALERT_ENABLED", True), \
             patch("app.topic_trigger_bridge.schedule_on_loop", return_value=True) as mock_marshal:
            emit_comparison_observation("kb", "usd-krw")   # 테스트 = loop 밖 → marshal 경로
        mock_marshal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
