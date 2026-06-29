"""ADR-036 B2 — source 알림 repeat_interval_sec crud 로직 테스트.

mark mode 분기(once/repeat) + create(필드/dedup) + update §7 리셋 + §8 모드전환 정규화.
in-memory SQLite (models.create_all → repeat_interval_sec 컬럼 포함).
"""
from __future__ import annotations

import os
import sys
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import crud, models  # noqa: E402

_USER = "test-user-b2"


class _Base(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def _create(self, repeat_interval_sec=None, **kw):
        return crud.create_source_notification_setting(
            db=self.db, user_id=_USER, source=kw.get("source", "upbit"),
            asset=kw.get("asset", "usdt-krw"), condition=kw.get("condition", "above"),
            threshold=kw.get("threshold", 1500.0), is_enabled=kw.get("is_enabled", True),
            repeat_interval_sec=repeat_interval_sec,
        )


class TestCreate(_Base):
    def test_create_once_default_null(self):
        s = self._create(repeat_interval_sec=None)
        self.assertIsNone(s.repeat_interval_sec)

    def test_create_repeat_stored(self):
        s = self._create(repeat_interval_sec=300)
        self.assertEqual(s.repeat_interval_sec, 300)

    def test_create_dedup_updates_interval(self):
        # 같은 (source,asset,condition,threshold) 재생성 → 기존 row의 interval 갱신
        s1 = self._create(repeat_interval_sec=None)
        s2 = self._create(repeat_interval_sec=600)
        self.assertEqual(s1.id, s2.id)            # dedup
        self.assertEqual(s2.repeat_interval_sec, 600)


class TestMarkModeBranch(_Base):
    def test_mark_once_disables(self):
        s = self._create(repeat_interval_sec=None)
        crud.mark_source_setting_triggered(self.db, s.id, rate=1501.0)
        self.db.refresh(s)
        self.assertTrue(s.triggered)
        self.assertFalse(s.enabled)
        self.assertIsNotNone(s.last_notified_at)
        self.assertEqual(s.last_notified_rate, 1501.0)

    def test_mark_repeat_keeps_enabled_no_triggered(self):
        s = self._create(repeat_interval_sec=300)
        crud.mark_source_setting_triggered(self.db, s.id, rate=1502.0)
        self.db.refresh(s)
        self.assertFalse(s.triggered)             # repeat: triggered 미설정
        self.assertTrue(s.enabled)                # repeat: enabled 유지
        self.assertIsNotNone(s.last_notified_at)  # gate 기준 갱신
        self.assertEqual(s.last_notified_rate, 1502.0)


class TestUpdateResetAndNormalize(_Base):
    def test_interval_change_resets_last_notified(self):
        # §7: interval 변경 시 last_notified_at 리셋
        s = self._create(repeat_interval_sec=300)
        crud.mark_source_setting_triggered(self.db, s.id, rate=1503.0)
        self.db.refresh(s)
        self.assertIsNotNone(s.last_notified_at)
        crud.update_source_notification_setting(
            self.db, s.id, _USER, repeat_interval_sec=600,
        )
        self.db.refresh(s)
        self.assertEqual(s.repeat_interval_sec, 600)
        self.assertIsNone(s.last_notified_at)     # §7 리셋
        self.assertFalse(s.triggered)

    def test_mode_transition_once_to_repeat_normalizes(self):
        # §8: 발사된 once-only(triggered=True, enabled=False) → repeat 전환 시 clean active
        s = self._create(repeat_interval_sec=None)
        crud.mark_source_setting_triggered(self.db, s.id, rate=1504.0)
        self.db.refresh(s)
        self.assertTrue(s.triggered)
        self.assertFalse(s.enabled)
        crud.update_source_notification_setting(
            self.db, s.id, _USER, repeat_interval_sec=300,
        )
        self.db.refresh(s)
        self.assertEqual(s.repeat_interval_sec, 300)
        self.assertTrue(s.enabled)                # §8 정규화: 활성
        self.assertFalse(s.triggered)             # §8 정규화: 종료플래그 해제
        self.assertIsNone(s.last_notified_at)

    def test_mode_transition_repeat_to_once(self):
        # repeat → once (명시적 None) 전환 + clean
        s = self._create(repeat_interval_sec=300)
        crud.mark_source_setting_triggered(self.db, s.id, rate=1505.0)
        crud.update_source_notification_setting(
            self.db, s.id, _USER, repeat_interval_sec=None,
        )
        self.db.refresh(s)
        self.assertIsNone(s.repeat_interval_sec)
        self.assertTrue(s.enabled)
        self.assertFalse(s.triggered)
        self.assertIsNone(s.last_notified_at)

    def test_mode_transition_respects_explicit_disable(self):
        # §8: 같은 PUT에서 enabled=False 명시 시 정규화가 활성으로 덮지 않음
        s = self._create(repeat_interval_sec=None)
        crud.mark_source_setting_triggered(self.db, s.id, rate=1506.0)
        crud.update_source_notification_setting(
            self.db, s.id, _USER, repeat_interval_sec=300, enabled=False,
        )
        self.db.refresh(s)
        self.assertEqual(s.repeat_interval_sec, 300)
        self.assertFalse(s.enabled)               # 사용자 명시 enabled=False 존중

    def test_unset_does_not_change_interval(self):
        # repeat_interval_sec 미제공(_UNSET) → 기존 값 유지
        s = self._create(repeat_interval_sec=300)
        crud.update_source_notification_setting(
            self.db, s.id, _USER, threshold=1490.0,
        )
        self.db.refresh(s)
        self.assertEqual(s.repeat_interval_sec, 300)   # 변경 안 됨


# ───────────────────────────────────────────────────────────────────────────
# PR2: bank(NotificationSetting) 경로 — source와 동일 정책 미러 + 신규 gate 로직
# ───────────────────────────────────────────────────────────────────────────


class _BankBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def _create(self, repeat_interval_sec=None, **kw):
        return crud.create_notification_setting(
            db=self.db, user_id=_USER, bank=kw.get("bank", "hana"),
            currency=kw.get("currency", "usd-krw"), condition=kw.get("condition", "above"),
            threshold=kw.get("threshold", 1500.0), is_enabled=kw.get("is_enabled", True),
            repeat_interval_sec=repeat_interval_sec,
        )

    def _add_device(self, token="tok-1"):
        d = models.UserDevice(user_id=_USER, device_token=token, platform="ios")
        self.db.add(d)
        self.db.commit()
        return d


class TestBankCreate(_BankBase):
    def test_create_once_default_null(self):
        s = self._create(repeat_interval_sec=None)
        self.assertIsNone(s.repeat_interval_sec)

    def test_create_repeat_stored(self):
        s = self._create(repeat_interval_sec=600)
        self.assertEqual(s.repeat_interval_sec, 600)

    def test_create_dedup_updates_interval(self):
        s1 = self._create(repeat_interval_sec=None)
        s2 = self._create(repeat_interval_sec=300)
        self.assertEqual(s1.id, s2.id)              # dedup
        self.assertEqual(s2.repeat_interval_sec, 300)


class TestBankMarkModeBranch(_BankBase):
    def test_mark_once_disables(self):
        s = self._create(repeat_interval_sec=None)
        crud.mark_setting_triggered(self.db, s.id, rate=1501.0)
        self.db.refresh(s)
        self.assertTrue(s.triggered)
        self.assertFalse(s.enabled)
        self.assertIsNotNone(s.last_notified_at)
        self.assertEqual(s.last_notified_rate, 1501.0)

    def test_mark_repeat_keeps_enabled_no_triggered(self):
        s = self._create(repeat_interval_sec=300)
        crud.mark_setting_triggered(self.db, s.id, rate=1502.0)
        self.db.refresh(s)
        self.assertFalse(s.triggered)               # repeat: triggered 미설정
        self.assertTrue(s.enabled)                  # repeat: enabled 유지
        self.assertIsNotNone(s.last_notified_at)
        self.assertEqual(s.last_notified_rate, 1502.0)


class TestBankUpdateResetAndNormalize(_BankBase):
    def test_interval_change_resets_last_notified(self):
        s = self._create(repeat_interval_sec=300)
        crud.mark_setting_triggered(self.db, s.id, rate=1503.0)
        self.db.refresh(s)
        self.assertIsNotNone(s.last_notified_at)
        crud.update_notification_setting(self.db, s.id, _USER, repeat_interval_sec=600)
        self.db.refresh(s)
        self.assertEqual(s.repeat_interval_sec, 600)
        self.assertIsNone(s.last_notified_at)       # §7 리셋
        self.assertFalse(s.triggered)

    def test_mode_transition_once_to_repeat_normalizes(self):
        s = self._create(repeat_interval_sec=None)
        crud.mark_setting_triggered(self.db, s.id, rate=1504.0)
        self.db.refresh(s)
        self.assertTrue(s.triggered)
        self.assertFalse(s.enabled)
        crud.update_notification_setting(self.db, s.id, _USER, repeat_interval_sec=300)
        self.db.refresh(s)
        self.assertEqual(s.repeat_interval_sec, 300)
        self.assertTrue(s.enabled)                  # §8 정규화
        self.assertFalse(s.triggered)
        self.assertIsNone(s.last_notified_at)

    def test_mode_transition_repeat_to_once(self):
        s = self._create(repeat_interval_sec=300)
        crud.mark_setting_triggered(self.db, s.id, rate=1505.0)
        crud.update_notification_setting(self.db, s.id, _USER, repeat_interval_sec=None)
        self.db.refresh(s)
        self.assertIsNone(s.repeat_interval_sec)
        self.assertTrue(s.enabled)
        self.assertFalse(s.triggered)
        self.assertIsNone(s.last_notified_at)

    def test_mode_transition_respects_explicit_disable(self):
        s = self._create(repeat_interval_sec=None)
        crud.mark_setting_triggered(self.db, s.id, rate=1506.0)
        crud.update_notification_setting(
            self.db, s.id, _USER, repeat_interval_sec=300, enabled=False,
        )
        self.db.refresh(s)
        self.assertEqual(s.repeat_interval_sec, 300)
        self.assertFalse(s.enabled)                 # 사용자 명시 enabled=False 존중

    def test_unset_does_not_change_interval(self):
        s = self._create(repeat_interval_sec=300)
        crud.update_notification_setting(self.db, s.id, _USER, threshold=1490.0)
        self.db.refresh(s)
        self.assertEqual(s.repeat_interval_sec, 300)


class TestBankGateIntervalThrottle(_BankBase):
    """get_triggered_settings_for_rate의 repeat interval gate (PR2 신규 로직)."""

    def _matched_ids(self, rate=1501.0):
        items = crud.get_triggered_settings_for_rate(self.db, "hana", "usd-krw", rate)
        return {it["setting"].id for it in items}

    def test_once_first_fire_then_query_gate_excludes(self):
        # once: 첫 발사 후 mark가 triggered=True → query !triggered가 제외 (gate 미진입)
        self._add_device()
        s = self._create(repeat_interval_sec=None)
        self.assertIn(s.id, self._matched_ids())    # 첫 tick 매칭
        crud.mark_setting_triggered(self.db, s.id, rate=1501.0)
        self.assertNotIn(s.id, self._matched_ids())  # 발사 후 제외

    def test_repeat_throttled_within_interval(self):
        # repeat: mark 후 interval 미경과면 gate가 skip (enabled/triggered는 그대로)
        self._add_device()
        s = self._create(repeat_interval_sec=60)
        self.assertIn(s.id, self._matched_ids())    # 첫 tick 매칭 (last_notified_at None)
        crud.mark_setting_triggered(self.db, s.id, rate=1501.0)
        self.assertNotIn(s.id, self._matched_ids())  # interval 내 → throttle

    def test_repeat_refires_after_interval_elapsed(self):
        # repeat: last_notified_at이 interval 이전이면 다시 매칭
        import datetime as _dt
        self._add_device()
        s = self._create(repeat_interval_sec=60)
        crud.mark_setting_triggered(self.db, s.id, rate=1501.0)
        self.db.refresh(s)
        s.last_notified_at = models.get_utc_now() - _dt.timedelta(seconds=120)
        self.db.commit()
        self.assertIn(s.id, self._matched_ids())    # interval 경과 → 재매칭


if __name__ == "__main__":
    unittest.main()
