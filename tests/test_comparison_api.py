"""비교알림 A1 — 조합 정책 검증 + CRUD (ADR-037 Amendment 2026-07-04).

endpoint wiring(auth/premium/cap)은 codex impl 리뷰로 검증 (source-notification 선례).
여기는 정책 validator + canonical + crud 레벨.

정책 (Amendment — 제품 의미 분리):
- absolute(일반 비교) = 탭별 대칭 집합. 테더는 거래소 5끼리만. threshold ≥ 0.
  저장 전 canonical ordering (A−B/B−A dedup 중복 차단).
- signed(김프/역프) = 테더 전용. left ∈ 거래소 5 × right ∈ {hana, kb, investing}.
  threshold 부호 자유(음수=역프). krx는 ADR-038 후 추가.
- 구 invariant(그래프 catalog krw series와 1:1)는 폐기 — 그래프 표시 ≠ 비교 허용.
"""
from __future__ import annotations

import unittest
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, models
from app.models import get_utc_now
from app.source_registry import (
    COMPARISON_ABSOLUTE_SOURCES,
    KIMCHI_BASE_SOURCES,
    KIMCHI_COUNTER_SOURCES,
    canonicalize_absolute_pair,
    validate_comparison_alert,
)


class TestComparisonPolicy(unittest.TestCase):
    """Amendment 조합 정책 — diff_type이 정책을 가름."""

    # -- absolute (일반 비교) --------------------------------------------

    def test_absolute_tether_exchanges_only(self):
        # 거래소 5끼리 OK
        self.assertIsNone(validate_comparison_alert(
            "tether", "upbit", "usdt-krw", "bithumb", "usdt-krw", "absolute", 3.0))
        self.assertIsNone(validate_comparison_alert(
            "tether", "coinone", "usdt-krw", "gopax", "usdt-krw", "absolute", 1.0))
        # cross-world(거래소 vs 환율계)는 absolute에서 거부 — 김프알림 전담
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "bithumb", "usdt-krw", "investing", "usd-krw", "absolute", 8.0))
        # krx/참조도 absolute 비교 불가 (그래프에 있어도 — 구 invariant 폐기)
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "upbit", "usdt-krw", "krx", "usd-krw-futures", "absolute", 5.0))
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "investing", "usd-krw", "hana", "usd-krw", "absolute", 2.0))

    def test_absolute_usd_krx_allowed(self):
        """ADR-038 D4 비교알림 (2026-07-10) — usd absolute에 달러선물↔은행/인베스팅 허용.

        entitled 전용은 validator가 아닌 main.py 403 게이트 담당 (validator 순수성 —
        김프 counter와 동일 구조). canonical 정렬로 krx가 좌/우 어느 쪽이든 가능."""
        # krx ↔ 은행/인베스팅 (양방향 — canonical 이전 원본 방향 모두)
        self.assertIsNone(validate_comparison_alert(
            "usd", "krx", "usd-krw-futures", "hana", "usd-krw", "absolute", 5.0))
        self.assertIsNone(validate_comparison_alert(
            "usd", "investing", "usd-krw", "krx", "usd-krw-futures", "absolute", 3.0))
        # jpy/eur는 여전히 거부 (usd 전용)
        self.assertIsNotNone(validate_comparison_alert(
            "jpy", "krx", "usd-krw-futures", "hana", "jpy-krw", "absolute", 5.0))
        self.assertIsNotNone(validate_comparison_alert(
            "eur", "hana", "eur-krw", "krx", "usd-krw-futures", "absolute", 5.0))
        # tether absolute는 여전히 거래소 5끼리만 (Amendment 4 유지)
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "upbit", "usdt-krw", "krx", "usd-krw-futures", "absolute", 5.0))
        # canonical: krx는 사전순으로 hana 뒤 / shinhan 앞 — 양쪽 슬롯 모두 가능 실증
        self.assertEqual(canonicalize_absolute_pair("hana", "usd-krw", "krx", "usd-krw-futures"),
                         ("hana", "usd-krw", "krx", "usd-krw-futures"))
        self.assertEqual(canonicalize_absolute_pair("shinhan", "usd-krw", "krx", "usd-krw-futures"),
                         ("krx", "usd-krw-futures", "shinhan", "usd-krw"))

    def test_absolute_fx_tabs(self):
        self.assertIsNone(validate_comparison_alert(
            "usd", "investing", "usd-krw", "kb", "usd-krw", "absolute", 2.0))
        self.assertIsNone(validate_comparison_alert(
            "jpy", "shinhan", "jpy-krw", "woori", "jpy-krw", "absolute", 1.0))
        # 탭 불일치 asset 거부
        self.assertIsNotNone(validate_comparison_alert(
            "jpy", "investing", "usd-krw", "kb", "jpy-krw", "absolute", 1.0))
        # citi/dxy 거부
        self.assertIsNotNone(validate_comparison_alert(
            "usd", "citi", "usd-krw", "kb", "usd-krw", "absolute", 1.0))
        self.assertIsNotNone(validate_comparison_alert(
            "usd", "dxy", "dxy", "kb", "usd-krw", "absolute", 1.0))

    def test_absolute_threshold_must_be_non_negative(self):
        """음수 absolute gte는 항상 참에 수렴 — 422/400 (codex blocker)."""
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "upbit", "usdt-krw", "bithumb", "usdt-krw", "absolute", -1.0))
        self.assertIsNone(validate_comparison_alert(
            "tether", "upbit", "usdt-krw", "bithumb", "usdt-krw", "absolute", 0.0))

    # -- signed (김프/역프) ----------------------------------------------

    def test_signed_kimchi_tether_only(self):
        # 거래소 × hana/kb/investing OK — threshold 음수(역프) 허용
        self.assertIsNone(validate_comparison_alert(
            "tether", "bithumb", "usdt-krw", "investing", "usd-krw", "signed", 8.0))
        self.assertIsNone(validate_comparison_alert(
            "tether", "upbit", "usdt-krw", "hana", "usd-krw", "signed", -30.0))
        self.assertIsNone(validate_comparison_alert(
            "tether", "gopax", "usdt-krw", "kb", "usd-krw", "signed", -10.0))
        # 테더 외 탭 거부
        self.assertIsNotNone(validate_comparison_alert(
            "usd", "investing", "usd-krw", "kb", "usd-krw", "signed", 2.0))
        # left가 거래소 아님 / right가 상대 집합 아님
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "investing", "usd-krw", "bithumb", "usdt-krw", "signed", 8.0))
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "bithumb", "usdt-krw", "upbit", "usdt-krw", "signed", 8.0))
        # krx counter는 구조적으로 유효 (ADR-038 G1/G2 land 2026-07-08 — 게이트는 handler 403,
        # test_krx_entitlement_gate.py에서 검증)
        self.assertIsNone(validate_comparison_alert(
            "tether", "bithumb", "usdt-krw", "krx", "usd-krw-futures", "signed", 5.0))

    def test_same_pair_and_unknown(self):
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "upbit", "usdt-krw", "upbit", "usdt-krw", "absolute", 1.0))
        self.assertIsNotNone(validate_comparison_alert(
            "news", "upbit", "usdt-krw", "bithumb", "usdt-krw", "absolute", 1.0))
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "upbit", "usdt-krw", "bithumb", "usdt-krw", "percent", 1.0))

    # -- canonical ordering ----------------------------------------------

    def test_threshold_abs_hard_cap(self):
        """서버 sanity 상한 ±10000 (codex) — signed/absolute 공통. 정상값(±1000)은 통과."""
        # 넘김 → 거부
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "bithumb", "usdt-krw", "investing", "usd-krw", "signed", 10000.1))
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "bithumb", "usdt-krw", "investing", "usd-krw", "signed", -10000.1))
        self.assertIsNotNone(validate_comparison_alert(
            "tether", "upbit", "usdt-krw", "bithumb", "usdt-krw", "absolute", 10001))
        # 경계/정상 → 통과
        self.assertIsNone(validate_comparison_alert(
            "tether", "bithumb", "usdt-krw", "investing", "usd-krw", "signed", -1000))
        self.assertIsNone(validate_comparison_alert(
            "tether", "bithumb", "usdt-krw", "investing", "usd-krw", "signed", 10000))

    def test_canonicalize_absolute_pair(self):
        """(source,asset) 사전순 정규화 — A−B/B−A 동일 결과 (Open 6 absolute closed)."""
        a = canonicalize_absolute_pair("upbit", "usdt-krw", "bithumb", "usdt-krw")
        b = canonicalize_absolute_pair("bithumb", "usdt-krw", "upbit", "usdt-krw")
        self.assertEqual(a, b)
        self.assertEqual(a, ("bithumb", "usdt-krw", "upbit", "usdt-krw"))   # 사전순

    def test_policy_sets_content(self):
        """정책 집합 잠금 — 거래소 5 / 김프 상대 4 (krx 포함 = ADR-038, 게이트는 handler)."""
        self.assertEqual(len(KIMCHI_BASE_SOURCES), 5)
        self.assertEqual(KIMCHI_COUNTER_SOURCES, frozenset({
            ("hana", "usd-krw"), ("kb", "usd-krw"), ("investing", "usd-krw"),
            ("krx", "usd-krw-futures")}))
        self.assertEqual(COMPARISON_ABSOLUTE_SOURCES["tether"], KIMCHI_BASE_SOURCES)
        # usd = investing + 8 banks + krx(ADR-038 D4 비교알림, 2026-07-10 — entitled 게이트는 handler)
        self.assertEqual(len(COMPARISON_ABSOLUTE_SOURCES["usd"]), 10)
        self.assertIn(("krx", "usd-krw-futures"), COMPARISON_ABSOLUTE_SOURCES["usd"])
        for tab in ("jpy", "eur"):
            self.assertEqual(len(COMPARISON_ABSOLUTE_SOURCES[tab]), 9)   # investing + 8 banks (krx 없음)
            self.assertNotIn(("krx", "usd-krw-futures"), COMPARISON_ABSOLUTE_SOURCES[tab])


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

    def test_update_threshold_operator_resets_and_revalidates(self):
        """A4 편집 — threshold/operator 변경 시 §7 리셋 + 신정책 재검증."""
        # 발송됨(once, triggered, disabled) 상태의 signed 알림
        a = models.ComparisonAlert(
            user_id="u1", tab="tether",
            left_source="bithumb", left_asset="usdt-krw",
            right_source="hana", right_asset="usd-krw",
            diff_type="signed", operator="lte", threshold=-30.0,
            enabled=False, triggered=True, repeat_interval_sec=None,
            last_notified_at=get_utc_now(), last_notified_spread=-31.0)
        self.db.add(a)
        self.db.commit()
        # threshold -30→-10 + operator lte→gte + 재활성화(edit sheet는 is_enabled=true 동반)
        updated = crud.update_comparison_alert(
            self.db, a.id, "u1", is_enabled=True, threshold=-10.0, operator="gte")
        self.assertEqual(updated.threshold, -10.0)
        self.assertEqual(updated.operator, "gte")
        self.assertTrue(updated.enabled)            # 재활성화
        self.assertFalse(updated.triggered)         # §7 리셋
        self.assertIsNone(updated.last_notified_at)
        self.assertIsNone(updated.last_notified_spread)

    def test_update_absolute_negative_threshold_rejected(self):
        """A4 편집 — absolute 알림을 음수 threshold로 편집 시 재검증 400 (endpoint helper)."""
        from app.source_registry import validate_comparison_alert
        a = models.ComparisonAlert(
            user_id="u1", tab="tether",
            left_source="bithumb", left_asset="usdt-krw",
            right_source="upbit", right_asset="usdt-krw",
            diff_type="absolute", operator="gte", threshold=2.0,
            enabled=True, triggered=False)
        self.db.add(a)
        self.db.commit()
        # 기존 pair/diff_type + 새 음수 threshold → validate가 에러 반환 (endpoint가 400 변환)
        err = validate_comparison_alert(
            a.tab, a.left_source, a.left_asset, a.right_source, a.right_asset,
            a.diff_type, -5.0)
        self.assertIsNotNone(err)
        # hard cap 초과도 거부
        err2 = validate_comparison_alert(
            a.tab, a.left_source, a.left_asset, a.right_source, a.right_asset,
            a.diff_type, 20000.0)
        self.assertIsNotNone(err2)

    def test_update_no_content_change_keeps_state(self):
        """A4 편집 — threshold/operator 미제공(None)이면 값·triggered 불변 (기존 토글 경로 회귀)."""
        a = models.ComparisonAlert(
            user_id="u1", tab="tether",
            left_source="bithumb", left_asset="usdt-krw",
            right_source="upbit", right_asset="usdt-krw",
            diff_type="absolute", operator="gte", threshold=3.0,
            enabled=True, triggered=True)
        self.db.add(a)
        self.db.commit()
        updated = crud.update_comparison_alert(self.db, a.id, "u1")  # 아무것도 안 바꿈
        self.assertEqual(updated.threshold, 3.0)
        self.assertEqual(updated.operator, "gte")
        self.assertTrue(updated.triggered)          # 변경 없음 → 리셋 안 함

    def test_logs_diff_type_filter(self):
        """diff_type 필터 — signed=김프 / absolute=비교 히스토리 분리 (ADR-037 Amendment)."""
        now = get_utc_now()
        # signed(김프) 2 + absolute(비교) 1
        for dt, op in (("signed", "gte"), ("signed", "lte"), ("absolute", "gte")):
            self.db.add(models.ComparisonNotificationLog(
                user_id="u1", setting_id=None, tab="tether",
                left_source="bithumb", left_asset="usdt-krw",
                right_source="hana" if dt == "signed" else "upbit",
                right_asset="usd-krw" if dt == "signed" else "usdt-krw",
                diff_type=dt, operator=op, threshold=1.0,
                left_rate=1507.0, right_rate=1531.0, spread=-24.0,
                is_repeat=False, success=True, sent_at=now))
        self.db.commit()
        kimchi = crud.get_comparison_notification_logs(self.db, "u1", diff_type="signed")
        comp = crud.get_comparison_notification_logs(self.db, "u1", diff_type="absolute")
        self.assertEqual(len(kimchi), 2)
        self.assertTrue(all(l.diff_type == "signed" for l in kimchi))
        self.assertEqual(len(comp), 1)
        self.assertEqual(comp[0].diff_type, "absolute")

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




class TestComparisonEndpointKrxGate(unittest.TestCase):
    """POST /api/comparison-alerts의 KRX 403 게이트 — endpoint 레벨 잠금 (codex NB1).

    분기 계약: KRX_PAIR가 left/right 어느 쪽이든(absolute 포함) `_require_krx_alert_allowed_or_403`
    경유. 게이트 판정 자체(krx_alert_gate_error)는 helper 테스트가 커버 — 여기선 gate가
    호출·적용되는지(403)와 통과 시 생성(200)만 잠금. auth/premium은 patch.
    """

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    def _post(self, body, gate_error):
        from unittest.mock import AsyncMock, patch as _patch
        with _patch("app.main.verify_firebase_token", new=AsyncMock(return_value="gate-test-user")), \
             _patch("app.main.require_premium", new=AsyncMock(return_value=None)), \
             _patch("app.main.notify_user_devices_sync", new=AsyncMock(return_value=None)), \
             _patch("app.entitlements.krx_alert_gate_error", return_value=gate_error):
            return self.client.post("/api/comparison-alerts", json=body)

    def _cleanup(self):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            db.query(models.ComparisonAlert).filter(
                models.ComparisonAlert.user_id == "gate-test-user").delete()
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        self._cleanup()

    def _krx_absolute_body(self, left=("krx", "usd-krw-futures"), right=("hana", "usd-krw")):
        return {"tab": "usd", "left_source": left[0], "left_asset": left[1],
                "right_source": right[0], "right_asset": right[1],
                "diff_type": "absolute", "operator": "gte", "threshold": 5.0,
                "is_enabled": True}

    def test_absolute_krx_left_gated_403(self):
        r = self._post(self._krx_absolute_body(), gate_error="krx_entitlement_required")
        self.assertEqual(r.status_code, 403)

    def test_absolute_krx_right_gated_403(self):
        r = self._post(self._krx_absolute_body(left=("hana", "usd-krw"),
                                               right=("krx", "usd-krw-futures")),
                       gate_error="krx_entitlement_required")
        self.assertEqual(r.status_code, 403)

    def test_absolute_krx_entitled_creates_200(self):
        r = self._post(self._krx_absolute_body(), gate_error=None)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # canonical 정렬: hana < krx → left=hana
        self.assertEqual((data["left_source"], data["right_source"]), ("hana", "krx"))

    def test_absolute_non_krx_no_gate(self):
        # krx 무관 비교는 게이트 미경유 — gate_error가 있어도 200
        body = self._krx_absolute_body(left=("investing", "usd-krw"), right=("kb", "usd-krw"))
        r = self._post(body, gate_error="krx_entitlement_required")
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
