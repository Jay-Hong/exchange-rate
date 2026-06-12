"""Pydantic V2 ConfigDict 전환 후 from_attributes(ORM 속성 읽기) 동작 잠금.

`class Config: from_attributes=True` → `model_config = ConfigDict(from_attributes=True)`
마이그레이션(2026-06-12) 후, 세 response 모델이 **dict가 아닌 속성 객체**(SQLAlchemy ORM
대용)에서 `model_validate`로 필드를 읽는지 명시 검증. from_attributes 미설정 시
`model_validate(SimpleNamespace)`는 실패하므로, 이 테스트가 통과하면 from_attributes가
실제로 활성임을 보장 (Codex 권고 — 전체 suite 통과는 간접 증거, 본 테스트가 직접 잠금 + 회귀 가드).
"""
import unittest
from types import SimpleNamespace

from app import schemas


class TestResponseModelsFromAttributes(unittest.TestCase):
    """3 모델 × model_validate(속성 객체) — from_attributes=True 직접 잠금."""

    def test_bank_exchange_rate_response_from_attributes(self):
        obj = SimpleNamespace(
            currency="usd-krw", bank="kb", rate=1500.5,
            timestamp="2026-06-11T00:00:00",
        )
        m = schemas.BankExchangeRateResponse.model_validate(obj)
        self.assertEqual(m.currency, "usd-krw")
        self.assertEqual(m.bank, "kb")
        self.assertEqual(m.rate, 1500.5)
        self.assertEqual(m.timestamp, "2026-06-11T00:00:00")

    def test_notification_setting_response_from_attributes(self):
        obj = SimpleNamespace(
            id=1, user_id="u1", bank="hana", currency="usd-krw", condition="above",
            threshold=1475.0, is_enabled=True, triggered=False,
            created_at="2026-06-11T00:00:00", updated_at=None, triggered_at=None,
        )
        m = schemas.NotificationSettingResponse.model_validate(obj)
        self.assertEqual(m.id, 1)
        self.assertEqual(m.threshold, 1475.0)
        self.assertTrue(m.is_enabled)
        self.assertFalse(m.triggered)

    def test_source_notification_setting_response_from_attributes(self):
        obj = SimpleNamespace(
            id=2, user_id="u2", source="upbit", asset="usdt-krw", condition="below",
            threshold=1400.0, is_enabled=False, triggered=True,
            created_at="2026-06-11T00:00:00", updated_at=None, triggered_at=None,
        )
        m = schemas.SourceNotificationSettingResponse.model_validate(obj)
        self.assertEqual(m.source, "upbit")
        self.assertEqual(m.asset, "usdt-krw")
        self.assertFalse(m.is_enabled)
        self.assertTrue(m.triggered)

    def test_from_attributes_required_rejects_namespace_without_flag(self):
        """음성 대조 — from_attributes 없는 모델은 SimpleNamespace를 ValidationError로 거부.

        (양성 3개 통과 = from_attributes 활성의 직접 증거라는 잠금 전제 입증.)
        """
        from pydantic import BaseModel, ValidationError

        class _NoFromAttr(BaseModel):
            x: int

        # "Input should be a valid dictionary or instance of _NoFromAttr" — dict 아닌 객체 거부
        with self.assertRaises(ValidationError):
            _NoFromAttr.model_validate(SimpleNamespace(x=1))


if __name__ == "__main__":
    unittest.main()
