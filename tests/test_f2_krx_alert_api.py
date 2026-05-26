"""F-2 (2026-05-26) — KRX 알림 API 허용 + source_registry 변경 회귀 잠금.

Scope:
    - `source_registry`:
        - KRX `phase1_enabled=True` 잠금
        - KRX `category="derivative"` 잠금
        - `is_phase1_source(krx, usd-krw-futures)` True
        - `get_usdt_exchange_entries`에 KRX 비포함 (category=exchange + asset=usdt-krw 필터)
    - `source_registry.validate_alert_source_asset` (F-2 helper 분리):
        - KRX (derivative) 통과 → None
        - upbit/bithumb/coinone/gopax/korbit (exchange) 통과 (회귀)
        - investing/kb/hana (reference) 차단 → "exchange/derivative" detail
        - unknown source 차단 → "Unsupported" detail
    - docstring/module doc에 F-2 + dead alert gap 명시 잠금

설계 anchor:
    - 검증 helper는 `source_registry`에 위치 (FastAPI 비의존) — [memory:
      project_main_py_helper_placement] 영구 적용.
    - main.py `_validate_phase1_source_asset`은 thin HTTPException wrapper.

dead alert gap: F-2 land ~ F-3 (`KRX_ALERT_EVALUATOR_ENABLED=true`) 활성
사이 KRX는 API 등록 가능 + 발송 안 됨 = 의도된 canary staging gap. 운영 영향 0.
"""
from __future__ import annotations

import unittest

from app import source_registry
from app.source_registry import validate_alert_source_asset


# ---------------------------------------------------------------------------
# source_registry — KRX phase1_enabled / category 잠금
# ---------------------------------------------------------------------------

class TestSourceRegistryKrxPhase1(unittest.TestCase):
    """KRX SourceDefinition의 F-2 land 상태 잠금."""

    def test_krx_phase1_enabled_true(self):
        """F-2 변경 핵심: KRX `phase1_enabled=True`."""
        definition = source_registry.get_source_definition("krx", "usd-krw-futures")
        self.assertIsNotNone(definition)
        self.assertTrue(definition.phase1_enabled)

    def test_krx_category_derivative(self):
        """KRX category는 derivative — 변형 잠금 (exchange 등으로 잘못 바뀌지 않게)."""
        definition = source_registry.get_source_definition("krx", "usd-krw-futures")
        self.assertIsNotNone(definition)
        self.assertEqual(definition.category, "derivative")

    def test_is_phase1_source_returns_true_for_krx(self):
        """is_phase1_source helper도 True 반환 (외부 호출자가 등장하면 KRX 인식)."""
        self.assertTrue(
            source_registry.is_phase1_source("krx", "usd-krw-futures")
        )

    def test_get_usdt_exchange_entries_excludes_krx(self):
        """KRX `phase1_enabled=True` 변경이 USDT crawler에 KRX 섞이지 않음 잠금.

        `get_usdt_exchange_entries`는 category="exchange" + asset="usdt-krw"
        명시 필터 — KRX는 category="derivative" + asset="usd-krw-futures"라
        자연 제외. 본 테스트가 회귀 차단.
        """
        entries = source_registry.get_usdt_exchange_entries()
        sources = {e.source for e in entries}
        self.assertNotIn("krx", sources)
        # USDT 5 source는 모두 포함되어야 함 (회귀 잠금)
        for usdt_source in ("upbit", "bithumb", "coinone", "korbit", "gopax"):
            self.assertIn(usdt_source, sources,
                          f"{usdt_source} should be in USDT exchange entries")


# ---------------------------------------------------------------------------
# validate_alert_source_asset — derivative 허용 + reference 차단 잠금
# ---------------------------------------------------------------------------

class TestValidateAlertSourceAsset(unittest.TestCase):
    """F-2 검증 helper 동작 잠금 — source_registry 분리 후 단위 테스트."""

    def test_krx_derivative_returns_none(self):
        """F-2 핵심: KRX (derivative) validation 통과 — None 반환."""
        self.assertIsNone(validate_alert_source_asset("krx", "usd-krw-futures"))

    def test_exchange_sources_return_none(self):
        """기존 동작 회귀 잠금: USDT 5 source (exchange) 모두 통과."""
        for source in ("upbit", "bithumb", "coinone", "korbit", "gopax"):
            with self.subTest(source=source):
                self.assertIsNone(validate_alert_source_asset(source, "usdt-krw"))

    def test_reference_sources_return_block_message(self):
        """기존 동작 회귀 잠금: reference 소스(investing/kb/hana) 여전히 차단.

        Dead alert 방지 — `/api/notification-settings` 사용해야 함.
        F-2 신규 에러 메시지: "exchange/derivative" 포함.
        """
        for source in ("investing", "kb", "hana"):
            with self.subTest(source=source):
                error = validate_alert_source_asset(source, "usd-krw")
                self.assertIsNotNone(error)
                self.assertIn("exchange/derivative", error)

    def test_unknown_source_returns_unsupported_message(self):
        """미등록 source/asset 조합 차단 → 'Unsupported' detail."""
        error = validate_alert_source_asset("unknown_source", "unknown_asset")
        self.assertIsNotNone(error)
        self.assertIn("Unsupported", error)

    def test_krx_with_wrong_asset_returns_unsupported(self):
        """KRX source라도 등록되지 않은 asset 조합은 차단."""
        error = validate_alert_source_asset("krx", "usdt-krw")
        self.assertIsNotNone(error)
        self.assertIn("Unsupported", error)


# ---------------------------------------------------------------------------
# F-2 staging gap 문서 잠금 (dead alert 위험 인지 확인)
# ---------------------------------------------------------------------------

class TestF2DocstringMentionsDeadAlertGap(unittest.TestCase):
    """F-2 land 후 코드 주석/docstring이 dead alert gap을 명시하는지 잠금.

    F-2만 단독 land된 상태 = API 등록 가능 + 발송 안 됨 (KRX_ALERT_EVALUATOR_ENABLED
    default false). 향후 운영 confusion 방지 위해 source_registry / validation
    docstring에 명시 필요. 본 테스트가 문서화 누락 회귀 차단.
    """

    def test_source_registry_module_docstring_mentions_f2_and_gap(self):
        """source_registry 모듈 docstring에 F-2 + KRX_ALERT_EVALUATOR_ENABLED 명시."""
        import app.source_registry as sr
        docstring = sr.__doc__ or ""
        self.assertIn("F-2", docstring)
        self.assertIn("KRX_ALERT_EVALUATOR_ENABLED", docstring)

    def test_validate_helper_docstring_mentions_f3_gap(self):
        """`validate_alert_source_asset` docstring에 F-3 발송 분리 명시."""
        docstring = validate_alert_source_asset.__doc__ or ""
        self.assertIn("KRX_ALERT_EVALUATOR_ENABLED", docstring)


if __name__ == "__main__":
    unittest.main(verbosity=2)
