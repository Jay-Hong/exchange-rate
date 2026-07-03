"""비교알림 S3 — tab-scope 검증 + CRUD (ADR-037).

endpoint wiring(auth/premium/cap)은 codex impl 리뷰로 검증 (source-notification 선례 —
test_source_notification_logs.py 참조). 여기는 검증 helper + crud 레벨.

핵심:
- validate_comparison_alert: within-tab/same-pair/unknown-tab/citi/index 차단
- COMPARISON_TAB_SOURCES ↔ graph_v2_intraday.TAB_1D_SERIES **정합 잠금** (drift 차단 —
  명시 상수가 카탈로그의 krw축 series와 어긋나면 여기서 red)
- crud: create dedup(재활성화 리셋) / update sentinel 3-state / delete 멱등 / logs 필터
"""
from __future__ import annotations

import unittest
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, models
from app.models import get_utc_now
from app.source_registry import COMPARISON_TAB_SOURCES, validate_comparison_alert


class TestValidateComparisonAlert(unittest.TestCase):

    def test_valid_combos(self):
        self.assertIsNone(validate_comparison_alert(
            "tether", "bithumb", "usdt-krw", "investing", "usd-krw"))   # 김프
        self.assertIsNone(validate_comparison_alert(
            "tether", "upbit", "usdt-krw", "krx", "usd-krw-futures"))   # USDT vs 선물
        self.assertIsNone(validate_comparison_alert(
            "usd", "investing", "usd-krw", "kb", "usd-krw"))            # 기준 대비 은행
        self.assertIsNone(validate_comparison_alert(
            "usd", "shinhan", "usd-krw", "woori", "usd-krw"))           # 은행 간
        self.assertIsNone(validate_comparison_alert(
            "jpy", "investing", "jpy-krw", "hana", "jpy-krw"))
        self.assertIsNone(validate_comparison_alert(
            "eur", "kb", "eur-krw", "bs", "eur-krw"))

    def test_same_pair_rejected(self):
        self.assertIsNotNone(validate_comparison_alert(
            "usd", "kb", "usd-krw", "kb", "usd-krw"))

    def test_cross_tab_rejected(self):
        # usd 탭에서 usdt 소스 (테더 전용)
        self.assertIsNotNone(validate_comparison_alert(
            "usd", "bithumb", "usdt-krw", "kb", "usd-krw"))
        # 테더 탭에서 shinhan (테더 탭 그래프에 없음 — investing/kb/hana만)
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "shinhan", "usd-krw", "investing", "usd-krw"))
        # jpy 탭에서 usd asset
        self.assertIsNotNone(validate_comparison_alert(
            "jpy", "investing", "usd-krw", "kb", "jpy-krw"))

    def test_unknown_tab_and_index_and_citi(self):
        self.assertIsNotNone(validate_comparison_alert(
            "news", "kb", "usd-krw", "hana", "usd-krw"))
        self.assertIsNotNone(validate_comparison_alert(
            "usd", "dxy", "dxy", "kb", "usd-krw"))               # index 축 제외
        self.assertIsNotNone(validate_comparison_alert(
            "usd", "citi", "usd-krw", "kb", "usd-krw"))          # Citi 제외 (ADR-033 D4)

    def test_tab_sources_match_graph_catalog(self):
        """정합 잠금 — COMPARISON_TAB_SOURCES == 탭 그래프 catalog의 krw축 (source, asset).

        ADR-037 Decision 1: 허용 소스의 단일 진실 소스 = graph_v2_intraday.TAB_1D_SERIES.
        registry가 graph 모듈 import를 피해 명시 상수를 두므로, drift는 이 테스트가 차단.
        (TAB_1D_SERIES 변경 시 COMPARISON_TAB_SOURCES도 함께 갱신할 것.)
        """
        from app.graph_v2_intraday import TAB_1D_SERIES

        def krw_pairs(tab):
            out = set()
            for spec in TAB_1D_SERIES[tab]:
                if spec.get("axis_group") != "krw":
                    continue   # index(DXY 계열) 제외 — 단위 불일치
                if spec["kind"] == "source":
                    out.add((spec["source"], spec["asset"]))
                elif spec["kind"] == "fx":
                    out.add((spec["fx_source"], spec["currency"]))
            return out

        for tab in ("tether", "usd", "jpy", "eur"):
            self.assertEqual(
                COMPARISON_TAB_SOURCES[tab], krw_pairs(tab),
                f"{tab}: COMPARISON_TAB_SOURCES가 TAB_1D_SERIES(krw축)와 어긋남 — 함께 갱신 필요",
            )


class TestComparisonCrud(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _create(self, **over):
        params = dict(
            db=self.db, user_id="u1", tab="tether",
            left_source="bithumb", left_asset="usdt-krw",
            right_source="investing", right_asset="usd-krw",
            diff_type="signed", operator="gte", threshold=8.0,
        )
        params.update(over)
        return crud.create_comparison_alert(**params)

    def test_create_and_dedup_reactivation(self):
        a1 = self._create()
        # 발송됨 상태 시뮬 후 동일 조합 재생성 → 같은 row 재활성화 + 리셋
        a1.enabled = False
        a1.triggered = True
        a1.last_notified_at = get_utc_now()
        a1.last_notified_spread = 9.9
        self.db.commit()

        a2 = self._create(is_enabled=True, repeat_interval_sec=300)
        self.assertEqual(a1.id, a2.id)              # dedup — 새 row 아님
        self.assertTrue(a2.enabled)
        self.assertFalse(a2.triggered)              # 재활성화 리셋
        self.assertIsNone(a2.last_notified_at)
        self.assertIsNone(a2.last_notified_spread)
        self.assertEqual(a2.repeat_interval_sec, 300)   # interval 갱신

    def test_dedup_key_includes_tab_and_direction(self):
        a1 = self._create(tab="tether", left_source="investing", left_asset="usd-krw",
                          right_source="hana", right_asset="usd-krw")
        # 다른 탭의 같은 조합 → 별개 row (모호 조합 탭별 독립 — ADR dedup 정책)
        a2 = self._create(tab="usd", left_source="investing", left_asset="usd-krw",
                          right_source="hana", right_asset="usd-krw")
        self.assertNotEqual(a1.id, a2.id)
        # A−B vs B−A → 별개 row (v1 exact match만 — ADR Open 6)
        a3 = self._create(tab="usd", left_source="hana", left_asset="usd-krw",
                          right_source="investing", right_asset="usd-krw")
        self.assertNotEqual(a2.id, a3.id)

    def test_update_sentinel_three_state(self):
        a = self._create(repeat_interval_sec=300)
        # 미제공 → interval 유지
        u1 = crud.update_comparison_alert(self.db, a.id, "u1", is_enabled=False)
        self.assertEqual(u1.repeat_interval_sec, 300)
        self.assertFalse(u1.enabled)
        # 명시적 None → once 전환
        u2 = crud.update_comparison_alert(self.db, a.id, "u1", is_enabled=True,
                                          repeat_interval_sec=None)
        self.assertIsNone(u2.repeat_interval_sec)
        self.assertTrue(u2.enabled)
        self.assertFalse(u2.triggered)
        # 정수 → repeat 전환
        u3 = crud.update_comparison_alert(self.db, a.id, "u1", repeat_interval_sec=600)
        self.assertEqual(u3.repeat_interval_sec, 600)

    def test_update_interval_change_resets_notified_state(self):
        """§7 (codex S3 blocker): interval 변경 시 last_notified_* 리셋 — 이전 발화 시각이
        새 repeat 설정을 suppress하지 않음. §8: once↔repeat 전환 시 clean active."""
        a = self._create(repeat_interval_sec=300)
        a.last_notified_at = get_utc_now()
        a.last_notified_spread = 9.1
        self.db.commit()
        # interval 변경(300→600) → 리셋
        u = crud.update_comparison_alert(self.db, a.id, "u1", repeat_interval_sec=600)
        self.assertIsNone(u.last_notified_at)
        self.assertIsNone(u.last_notified_spread)
        # repeat→once 전환 (§8): once 발사 종료 상태였다면 clean active 복귀
        u.enabled = False
        u.triggered = True
        self.db.commit()
        u2 = crud.update_comparison_alert(self.db, a.id, "u1", repeat_interval_sec=None)
        self.assertTrue(u2.enabled)      # §8 모드 전환 clean active
        self.assertFalse(u2.triggered)
        # 단 명시적 is_enabled=False 동반 시 그 의도 존중
        u3 = crud.update_comparison_alert(self.db, a.id, "u1", is_enabled=False,
                                          repeat_interval_sec=300)
        self.assertFalse(u3.enabled)

    def test_update_reactivation_resets(self):
        a = self._create()
        a.enabled = False
        a.triggered = True
        a.last_notified_at = get_utc_now()
        a.last_notified_spread = 8.8
        self.db.commit()
        u = crud.update_comparison_alert(self.db, a.id, "u1", is_enabled=True)
        self.assertFalse(u.triggered)
        self.assertIsNone(u.last_notified_at)
        self.assertIsNone(u.last_notified_spread)

    def test_update_cross_user_forbidden(self):
        a = self._create()
        self.assertIsNone(crud.update_comparison_alert(self.db, a.id, "other", is_enabled=False))

    def test_delete_idempotent_and_scoped(self):
        a = self._create()
        self.assertFalse(crud.delete_comparison_alert(self.db, a.id, "other"))   # cross-user
        self.assertTrue(crud.delete_comparison_alert(self.db, a.id, "u1"))
        self.assertFalse(crud.delete_comparison_alert(self.db, a.id, "u1"))      # 멱등

    def _add_log(self, user_id="u1", tab="tether", success=True, sent_at=None):
        log = models.ComparisonNotificationLog(
            user_id=user_id, setting_id=None, tab=tab,
            left_source="bithumb", left_asset="usdt-krw",
            right_source="investing", right_asset="usd-krw",
            diff_type="signed", operator="gte", threshold=8.0,
            left_rate=1512.0, right_rate=1502.5, spread=9.5,
            is_repeat=False, success=success,
            sent_at=sent_at or get_utc_now(),
        )
        self.db.add(log)
        self.db.commit()
        return log

    def test_logs_filters_and_order(self):
        now = get_utc_now()
        self._add_log(sent_at=now - timedelta(minutes=3))
        newest = self._add_log(sent_at=now)
        self._add_log(tab="usd", sent_at=now - timedelta(minutes=1))
        self._add_log(success=False, sent_at=now - timedelta(seconds=30))   # 실패 — 미노출
        self._add_log(user_id="other", sent_at=now)                          # cross-user 격리

        logs = crud.get_comparison_notification_logs(self.db, "u1")
        self.assertEqual(len(logs), 3)                       # 실패/타 유저 제외
        self.assertEqual(logs[0].id, newest.id)              # sent_at DESC

        tether_only = crud.get_comparison_notification_logs(self.db, "u1", tab="tether")
        self.assertEqual(len(tether_only), 2)

        limited = crud.get_comparison_notification_logs(self.db, "u1", limit=1)
        self.assertEqual(len(limited), 1)


if __name__ == "__main__":
    unittest.main()
