"""ADR-038 G1/G2 — KRX 노출 게이트 (entitlements 판정 + graph v2 필터 + 김프 counter 정책).

endpoint wiring(auth/403 변환)은 codex impl 리뷰로 검증 (comparison A1 선례) —
여기는 도메인 helper + graph accessor + validator 정책 레벨.

게이트 캐스케이드: G3(KRX_FUTURES_ENABLED) ∧ G2(KRX_CLIENT_DISTRIBUTION_ENABLED)
∧ G1(user_entitlements) ∧ premium → krx_visible.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import entitlements, models


def _patch_gates(g3: bool, g2: bool):
    """config의 G3/G2 flag patch — runtime accessor(krx_gates_open 등)가 읽는 지점."""
    return patch.multiple("app.config",
                          KRX_FUTURES_ENABLED=g3,
                          KRX_CLIENT_DISTRIBUTION_ENABLED=g2)


class _DbTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _grant(self, user_id="u1", key=entitlements.KRX_FUTURES_ENTITLEMENT_KEY):
        self.db.add(models.UserEntitlement(user_id=user_id, key=key))
        self.db.commit()


class TestEntitlementHelpers(_DbTestCase):

    def test_has_entitlement(self):
        self.assertFalse(entitlements.has_entitlement(
            self.db, "u1", entitlements.KRX_FUTURES_ENTITLEMENT_KEY))
        self._grant("u1")
        self.assertTrue(entitlements.has_entitlement(
            self.db, "u1", entitlements.KRX_FUTURES_ENTITLEMENT_KEY))
        # 다른 사용자/키는 미부여
        self.assertFalse(entitlements.has_entitlement(
            self.db, "u2", entitlements.KRX_FUTURES_ENTITLEMENT_KEY))
        self.assertFalse(entitlements.has_entitlement(self.db, "u1", "other_key"))

    def test_unique_user_key(self):
        """(user_id, key) UNIQUE — 중복 부여 방지 (grant 스크립트 SKIP 전제)."""
        from sqlalchemy.exc import IntegrityError
        self._grant("u1")
        self.db.add(models.UserEntitlement(
            user_id="u1", key=entitlements.KRX_FUTURES_ENTITLEMENT_KEY))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_krx_gates_open_matrix(self):
        """G3 ∧ G2 — 하나라도 닫히면 False (ADR-038 '하나라도 닫히면 미노출')."""
        cases = [(True, True, True), (True, False, False),
                 (False, True, False), (False, False, False)]
        for g3, g2, expected in cases:
            with _patch_gates(g3, g2):
                self.assertEqual(entitlements.krx_gates_open(), expected, (g3, g2))

    def test_krx_alert_gate_error(self):
        # 게이트 닫힘 → 사유 반환 (entitlement 있어도)
        self._grant("u1")
        with _patch_gates(True, False):
            self.assertIsNotNone(entitlements.krx_alert_gate_error(self.db, "u1"))
        # 게이트 열림 + entitlement 없음 → 사유 반환
        with _patch_gates(True, True):
            self.assertIsNotNone(entitlements.krx_alert_gate_error(self.db, "u2"))
            # 게이트 열림 + entitlement 있음 → 허용
            self.assertIsNone(entitlements.krx_alert_gate_error(self.db, "u1"))

    def test_compute_krx_visible(self):
        """krx_visible = G3 ∧ G2 ∧ G1 ∧ premium — 4축 매트릭스."""
        self._grant("u1")
        with _patch_gates(True, True):
            self.assertTrue(entitlements.compute_krx_visible(self.db, "u1", True))
            self.assertFalse(entitlements.compute_krx_visible(self.db, "u1", False))   # premium off
            self.assertFalse(entitlements.compute_krx_visible(self.db, "u2", True))    # G1 off
        with _patch_gates(False, True):
            self.assertFalse(entitlements.compute_krx_visible(self.db, "u1", True))    # G3 off
        with _patch_gates(True, False):
            self.assertFalse(entitlements.compute_krx_visible(self.db, "u1", True))    # G2 off


class TestDisableOnlyJudgment(unittest.TestCase):
    """PUT gate 예외 판정 — '끄기 전용'만 허용 (codex blocker 1 회귀 잠금).

    main.py의 _disable_only 식과 동일 규칙을 스키마 레벨에서 검증: is_enabled=False 단독이어야
    하며, 변경 필드가 하나라도 있으면(끄기 동반이어도) gate 대상."""

    def test_source_update_disable_only(self):
        from app import schemas
        # 순수 끄기 → disable-only
        b = schemas.SourceNotificationSettingUpdateRequest(is_enabled=False)
        self.assertTrue(b.is_enabled is False and b.source is None and b.asset is None
                        and b.condition is None and b.threshold is None
                        and "repeat_interval_sec" not in b.model_fields_set)
        # 끄기 + source 변경 → gate 대상 (미승인의 disabled KRX row 생성 차단)
        b2 = schemas.SourceNotificationSettingUpdateRequest(
            is_enabled=False, source="krx", asset="usd-krw-futures")
        self.assertFalse(b2.source is None)
        # 끄기 + repeat 변경 (model_fields_set 판정) → gate 대상
        b3 = schemas.SourceNotificationSettingUpdateRequest(
            is_enabled=False, repeat_interval_sec=60)
        self.assertIn("repeat_interval_sec", b3.model_fields_set)

    def test_comparison_update_disable_only(self):
        from app import schemas
        b = schemas.ComparisonAlertUpdateRequest(is_enabled=False)
        self.assertTrue(b.is_enabled is False and b.threshold is None and b.operator is None
                        and "repeat_interval_sec" not in b.model_fields_set)
        b2 = schemas.ComparisonAlertUpdateRequest(is_enabled=False, threshold=5.0)
        self.assertFalse(b2.threshold is None)


class TestKimchiCounterPolicy(unittest.TestCase):
    """KIMCHI_COUNTER_SOURCES에 krx 추가 (구조적 유효) — 게이트는 handler 403 (validator 순수성)."""

    def test_krx_counter_structurally_valid(self):
        from app.source_registry import validate_comparison_alert
        self.assertIsNone(validate_comparison_alert(
            "tether", "bithumb", "usdt-krw", "krx", "usd-krw-futures", "signed", -20.0))

    def test_krx_still_invalid_elsewhere(self):
        from app.source_registry import validate_comparison_alert
        # absolute에서 krx 불가 (거래소 5끼리만)
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "upbit", "usdt-krw", "krx", "usd-krw-futures", "absolute", 5.0))
        # signed base(왼쪽)로 krx 불가 (base는 거래소 5)
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "krx", "usd-krw-futures", "hana", "usd-krw", "signed", 3.0))


class TestGraphV2KrxFilter(unittest.TestCase):
    """무인증 graph v2의 krx 계열 노출 계약 (ADR-038 D3 Open 2 + D4).

    ⚠️ ADR-039 §6.1(2026-07-26)에서 이 노출은 **별도 승인 flag**
    (`KRX_GRAPH_ALLOW_UNAUTHENTICATED_EXPOSURE`, default false) 뒤로 분리됐다 —
    G2 한 줄로 무인증 KRX가 열리는 사고를 막기 위해서다. 계약 자체는 그대로라
    이 클래스는 flag를 **켜고** 검증한다. 꺼진 기본 상태(= 오늘의 운영)는
    tests/test_graph_v2_krx_exposure.py.
    """

    def setUp(self):
        exposure = patch("app.config.KRX_GRAPH_ALLOW_UNAUTHENTICATED_EXPOSURE", True)
        exposure.start()
        self.addCleanup(exposure.stop)

    def test_long_period_series_filtered(self):
        from app.graph_v2 import _effective_tab_series, _effective_default_visible
        with _patch_gates(True, True):
            self.assertIn("krx.usd-krw-futures", _effective_tab_series("tether"))
            self.assertIn("krx.usd-krw-futures", _effective_default_visible("tether"))
        with _patch_gates(True, False):
            self.assertNotIn("krx.usd-krw-futures", _effective_tab_series("tether"))
            self.assertNotIn("krx.usd-krw-futures", _effective_default_visible("tether"))
            # 비-krx series는 유지
            self.assertIn("bithumb.usdt-krw", _effective_tab_series("tether"))

    def test_tether_default_visible_composition(self):
        """ADR-038 D4 후속 (2026-07-09) — 테더 기본 토글 = 거래소 대표 + 참조(hana·krx 둘 다,
        클라가 상호배타) + DXY 제거. gate on 기준 base 구성 잠금 (클라 swap은 iOS 테스트)."""
        from app.graph_v2 import _effective_default_visible
        from app.graph_v2_intraday import tab_1d_default_visible
        with _patch_gates(True, True):
            dv_1d = tab_1d_default_visible("tether")
            self.assertEqual(set(dv_1d),
                             {"upbit.usdt-krw", "bithumb.usdt-krw", "hana.usd", "krx.usd-krw-futures"})
            self.assertNotIn("dxy", dv_1d)                 # DXY 기본 OFF (사용자 2026-07-09)
            dv_long = _effective_default_visible("tether")
            self.assertEqual(set(dv_long),
                             {"bithumb.usdt-krw", "hana.usd", "krx.usd-krw-futures"})
            self.assertNotIn("dxy", dv_long)               # 장기도 DXY 기본 OFF

    def test_intraday_specs_filtered(self):
        from app.graph_v2_intraday import tab_1d_specs, tab_1d_all_series, tab_1d_default_visible
        with _patch_gates(True, True):
            self.assertIn("krx.usd-krw-futures", tab_1d_all_series("tether"))
        with _patch_gates(False, True):   # G3 off도 동일 차단 (codex 보강 — 잔존값 노출 방지)
            ids = tab_1d_all_series("tether")
            self.assertNotIn("krx.usd-krw-futures", ids)
            self.assertEqual(len(ids), 10)   # 테더 11 → 10
            self.assertNotIn("krx.usd-krw-futures", tab_1d_default_visible("tether"))
            self.assertTrue(all(s["id"] != "krx.usd-krw-futures" for s in tab_1d_specs("tether")))

    def test_catalog_reflects_gate(self):
        from app.graph_v2 import build_catalog
        with _patch_gates(True, False):
            catalog = build_catalog()
            tether = next(t for t in catalog["tabs"] if t["id"] == "tether")
            for period, spec in tether["periods"].items():
                self.assertNotIn("krx.usd-krw-futures", spec["all_series"], period)
                self.assertNotIn("krx.usd-krw-futures", spec["default_visible_series"], period)
        with _patch_gates(True, True):
            catalog = build_catalog()
            tether = next(t for t in catalog["tabs"] if t["id"] == "tether")
            self.assertIn("krx.usd-krw-futures", tether["periods"]["3m"]["all_series"])
            self.assertIn("krx.usd-krw-futures", tether["periods"]["1d"]["all_series"])

    def test_usd_tab_gated_like_tether(self):
        """ADR-038 D4 ② — usd 탭도 krx 시리즈 편입: gate on=포함 / off=제외 (전 기간).
        jpy/eur는 krx 없음 — 게이트 무관 불변 (회귀 가드)."""
        from app.graph_v2 import build_catalog
        with _patch_gates(True, True):
            catalog = build_catalog()
            usd = next(t for t in catalog["tabs"] if t["id"] == "usd")
            self.assertIn("krx.usd-krw-futures", usd["periods"]["1d"]["all_series"])
            self.assertIn("krx.usd-krw-futures", usd["periods"]["3m"]["all_series"])
            # default OFF (2026-07-03 "최소 2개 시작" 결정과 정합)
            self.assertNotIn("krx.usd-krw-futures", usd["periods"]["1d"]["default_visible_series"])
        with _patch_gates(True, False):
            catalog = build_catalog()
            usd = next(t for t in catalog["tabs"] if t["id"] == "usd")
            for period in ("1d", "1w", "3m", "1y"):
                self.assertNotIn("krx.usd-krw-futures", usd["periods"][period]["all_series"])
            jpy = next(t for t in catalog["tabs"] if t["id"] == "jpy")
            self.assertNotIn("krx.usd-krw-futures", jpy["periods"]["1d"]["all_series"])


class TestConfigDerived(unittest.TestCase):
    """파생 상수 계산 규칙 잠금 — client-facing KRX 판정의 단일 소스."""

    def test_effective_definition(self):
        from app import config
        self.assertEqual(
            config.KRX_CLIENT_DISTRIBUTION_EFFECTIVE,
            config.KRX_FUTURES_ENABLED and config.KRX_CLIENT_DISTRIBUTION_ENABLED)
        # (ADR-038 D2) KRX_TOPIC_INCLUDE_EFFECTIVE는 usdt:krw group 제거와 함께 삭제됨
        self.assertFalse(hasattr(config, "KRX_TOPIC_INCLUDE_EFFECTIVE"))


if __name__ == "__main__":
    unittest.main()
