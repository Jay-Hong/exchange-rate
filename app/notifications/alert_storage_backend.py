"""Alert storage backend abstraction (fanout step 4 S1).

[ALL_SOURCE_FANOUT_UNIFICATION_PLAN.md §6.1] UsdtAlertEvaluator의 storage/IO coupling을 backend로
추출 — evaluator는 pure orchestration(cache / coalescer / condition / dispatch / in-flight guard)으로
남고, DB/payload-type touch는 backend에 위임.

- `SourceAlertBackend`: 현 evaluator static method 동작 **verbatim** (USDT 5 + KRX,
  `source_notification_settings`). USDT/KRX runtime byte-identical.
- `FxNotificationBackend` (S2 land 2026-06-23, S3 wiring 예정): `notification_settings`(bank/currency) — FX shadow.

설계 결정 (codex 019ef2fa):
- **sender(FCM delivery)는 backend ABC 밖** — evaluator constructor-injected callable(별 seam).
  여기엔 storage(load/refetch/persist) + payload만.
- **`persist_result`는 WHOLE** (mark_triggered + log + failed-token cleanup 통째) — split 시
  mark/log/cleanup 각자 `db.commit()`의 commit/session order가 바뀌어 byte-identity 위협.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # 순환 import 회피 — 타입 힌트 전용 (런타임은 body 내 lazy import)
    from app.notifications.alert_evaluator import CachedAlertSetting, FreshSettingSnapshot

# evaluator와 동일 logger name (log identity 보존 — byte-identity).
logger = logging.getLogger("exchange_rate.notifications.alert_evaluator")


class AlertStorageBackend(ABC):
    """Alert evaluator의 storage/IO 추상화 (load / refetch / persist / payload).

    evaluator는 이 backend를 통해서만 settings DB·payload-type을 touch. sender(FCM 발송)는
    별도 evaluator-injected seam(여기 아님).
    """

    @abstractmethod
    def load_settings(self, source: str, asset: str) -> tuple["CachedAlertSetting", ...]:
        """(source, asset) 활성 설정 → CachedAlertSetting tuple (empty device_tokens 제외)."""
        raise NotImplementedError

    @abstractmethod
    def refetch_snapshot(self, setting_id: int, user_id: str) -> Optional["FreshSettingSnapshot"]:
        """setting refetch → FreshSettingSnapshot (stale 검증용) 또는 None."""
        raise NotImplementedError

    @abstractmethod
    def persist_result(self, candidate: "CachedAlertSetting", rate: float, fcm_result: dict) -> None:
        """FCM 결과 persist — mark_triggered + log + failed-token cleanup (WHOLE, single-session)."""
        raise NotImplementedError

    @abstractmethod
    def build_payload(self, candidate: "CachedAlertSetting", triggered_rate: Decimal) -> tuple[str, str, dict]:
        """FCM title/body/data 생성 (data["type"] 소유)."""
        raise NotImplementedError


class SourceAlertBackend(AlertStorageBackend):
    """`source_notification_settings` 기반 (USDT 5 + KRX).

    현 `UsdtAlertEvaluator`의 `_load_settings_from_db` / `_refetch_setting_snapshot` /
    `_persist_result` / `_build_fcm_payload` 동작을 **verbatim** 이동 (byte-identity 보존).
    """

    def load_settings(self, source: str, asset: str) -> tuple["CachedAlertSetting", ...]:
        """sync DB query — to_thread 내부 실행. ORM → CachedAlertSetting snapshot 변환.

        Empty device_tokens settings는 제외 (조건 평가 의미 없음).
        """
        from app import models
        from app.database import get_db_context
        from app.notifications.alert_evaluator import CachedAlertSetting

        with get_db_context() as db:
            settings = db.query(models.SourceNotificationSetting).filter(
                models.SourceNotificationSetting.source == source,
                models.SourceNotificationSetting.asset == asset,
                models.SourceNotificationSetting.enabled == True,  # noqa: E712
                models.SourceNotificationSetting.triggered == False,  # noqa: E712
            ).all()

            if not settings:
                return tuple()

            # devices 일괄 조회 (user_id 기반)
            user_ids = {s.user_id for s in settings}
            devices = db.query(models.UserDevice).filter(
                models.UserDevice.user_id.in_(user_ids),
            ).all()

            tokens_by_user: dict[str, list[str]] = {}
            for dev in devices:
                tokens_by_user.setdefault(dev.user_id, []).append(dev.device_token)

            snapshots = []
            for setting in settings:
                tokens = tuple(tokens_by_user.get(setting.user_id, ()))
                if not tokens:
                    # empty device_tokens 제외 (조건 평가 + FCM 의미 없음)
                    continue
                snapshots.append(CachedAlertSetting(
                    setting_id=setting.id,
                    user_id=setting.user_id,
                    source=setting.source,
                    asset=setting.asset,
                    condition=setting.condition,
                    threshold=setting.threshold,
                    device_tokens=tokens,
                ))
            return tuple(snapshots)

    def refetch_snapshot(self, setting_id: int, user_id: str) -> Optional["FreshSettingSnapshot"]:
        """Session 1 — refetch + snapshot 추출 (ORM 객체 escape 차단)."""
        from app import crud
        from app.database import get_db_context
        from app.notifications.alert_evaluator import FreshSettingSnapshot

        with get_db_context() as db:
            setting = crud.get_source_notification_setting_by_id(
                db=db, setting_id=setting_id, user_id=user_id,
            )
            if setting is None:
                return None
            return FreshSettingSnapshot(
                setting_id=setting.id,
                enabled=setting.enabled,
                triggered=setting.triggered,
                source=setting.source,
                asset=setting.asset,
                condition=setting.condition,
                threshold=setting.threshold,
            )

    def persist_result(self, candidate: "CachedAlertSetting", rate: float, fcm_result: dict) -> None:
        """Session 2 — success → mark_triggered + log success / fail → log failure only.

        기존 `process_source_rate_alerts` 순서 보존. FCM 실패가 setting을 disabled 처리하면
        사용자 알림 영영 X. failed_tokens cleanup까지 같은 세션 (WHOLE — split 금지).
        """
        from app import crud, models
        from app.database import get_db_context

        with get_db_context() as db:
            if fcm_result["success_count"] > 0:
                crud.mark_source_setting_triggered(
                    db=db, setting_id=candidate.setting_id, rate=rate,
                )
                crud.create_source_notification_log(
                    db=db,
                    user_id=candidate.user_id,
                    setting_id=candidate.setting_id,
                    source=candidate.source,
                    asset=candidate.asset,
                    condition=candidate.condition,
                    threshold=candidate.threshold,
                    triggered_rate=rate,
                    success=True,
                )
                logger.info(
                    "🔔 alert_evaluator FCM sent",
                    extra={
                        "event": "alert_fcm_sent",
                        "source": candidate.source,
                        "asset": candidate.asset,
                        "rate": rate,
                        "threshold": candidate.threshold,
                        "setting_id": candidate.setting_id,
                        "user_id": candidate.user_id[:8] + "...",
                        "success_count": fcm_result["success_count"],
                    },
                )
            else:
                err_msg = fcm_result.get("error") or "no successful sends"
                crud.create_source_notification_log(
                    db=db,
                    user_id=candidate.user_id,
                    setting_id=candidate.setting_id,
                    source=candidate.source,
                    asset=candidate.asset,
                    condition=candidate.condition,
                    threshold=candidate.threshold,
                    triggered_rate=rate,
                    success=False,
                    error_message=err_msg,
                )

            # failed_tokens cleanup (기존 패턴 동일)
            failed_tokens = fcm_result.get("failed_tokens") or []
            if failed_tokens:
                try:
                    deleted = db.query(models.UserDevice).filter(
                        models.UserDevice.device_token.in_(failed_tokens),
                    ).delete(synchronize_session=False)
                    db.commit()
                    logger.info(
                        "[alert_evaluator] failed_tokens cleanup",
                        extra={"deleted_count": deleted, "tokens": len(failed_tokens)},
                    )
                except Exception:
                    logger.exception("[alert_evaluator] failed_tokens cleanup 실패")

    def build_payload(self, candidate: "CachedAlertSetting", triggered_rate: Decimal) -> tuple[str, str, dict]:
        """FCM title/body/data 생성 — 기존 `process_source_rate_alerts` 패턴 보존.

        format_threshold + source_registry display_name 사용. data["type"]="source_rate_alert".
        """
        from app import source_registry
        from app.crud import format_threshold

        definition = source_registry.get_source_definition(candidate.source, candidate.asset)
        source_display = definition.display_name if definition else candidate.source.upper()
        asset_display = candidate.asset.upper()

        icon = "📈" if candidate.condition == "above" else "📉"
        title = f"{icon}  {source_display}  {asset_display}"

        condition_arrow = "↑" if candidate.condition == "above" else "↓"
        condition_text = "이상" if candidate.condition == "above" else "이하"
        threshold_str = format_threshold(candidate.threshold)
        rate_str = f"{float(triggered_rate):.2f}"
        body = f"[ {threshold_str} {condition_arrow}{condition_text} 도달 ]   {rate_str}"

        data = {
            "type": "source_rate_alert",
            "title": title,
            "body": body,
            "source": candidate.source,
            "asset": candidate.asset,
            "rate": str(float(triggered_rate)),
            "threshold": str(candidate.threshold),
            "condition": candidate.condition,
            "setting_id": str(candidate.setting_id),
        }
        return title, body, data


class FxNotificationBackend(AlertStorageBackend):
    """`notification_settings`(bank/currency) 기반 — FX(bank+investing) shadow (fanout step 4 S2).

    [§6.1 open decision 7 = (a) adapter] **value pass-through**: changed_rates["bank"]=`c.source`가 이미
    "kb"/"investing"이고 `notification_settings.bank`도 동일 값(crud.py:275 `_changes_to_fcm`)이라
    `CachedAlertSetting`/`FreshSettingSnapshot`의 source=bank·asset=currency identity 매핑(remap 아님).

    - `persist_result`는 **shadow no-op** — FX shadow는 telemetry-only(mark_triggered/log/cleanup 0).
      legacy `crud.process_rate_alerts`가 authoritative. cutover 시 real persist는 open decision 7 후속.
    - `build_payload`는 **legacy FX 형식**(data["type"]="rate_alert", BANK_NAMES_KR/CURRENCY_NAMES_KR) —
      cutover 시 real 발송 fidelity 위해 (shadow는 no-op sender라 미발송, parity 비교용 아님).

    ⚠️ S3~S6에서 fx_alert_shadow에 wiring됨 — **보조 진단(execution-proof), parity 기준 아님**(S6 demote).
       parity 기준 = legacy pre-mutation baseline(crud `_fx_legacy_match_counts`). cutover=open decision 7.
    """

    def load_settings(self, source: str, asset: str) -> tuple["CachedAlertSetting", ...]:
        """notification_settings(bank=source, currency=asset) 활성 설정 → CachedAlertSetting tuple.

        SourceAlertBackend.load_settings의 NotificationSetting 버전(empty device_tokens 제외 동일).
        """
        from app import models
        from app.database import get_db_context
        from app.notifications.alert_evaluator import CachedAlertSetting

        with get_db_context() as db:
            settings = db.query(models.NotificationSetting).filter(
                models.NotificationSetting.bank == source,
                models.NotificationSetting.currency == asset,
                models.NotificationSetting.enabled == True,  # noqa: E712
                models.NotificationSetting.triggered == False,  # noqa: E712
            ).all()

            if not settings:
                return tuple()

            user_ids = {s.user_id for s in settings}
            devices = db.query(models.UserDevice).filter(
                models.UserDevice.user_id.in_(user_ids),
            ).all()

            tokens_by_user: dict[str, list[str]] = {}
            for dev in devices:
                tokens_by_user.setdefault(dev.user_id, []).append(dev.device_token)

            snapshots = []
            for setting in settings:
                tokens = tuple(tokens_by_user.get(setting.user_id, ()))
                if not tokens:
                    continue
                snapshots.append(CachedAlertSetting(
                    setting_id=setting.id,
                    user_id=setting.user_id,
                    source=setting.bank,       # value pass-through (bank → source)
                    asset=setting.currency,    # value pass-through (currency → asset)
                    condition=setting.condition,
                    threshold=setting.threshold,
                    device_tokens=tokens,
                ))
            return tuple(snapshots)

    def refetch_snapshot(self, setting_id: int, user_id: str) -> Optional["FreshSettingSnapshot"]:
        """notification_settings refetch (소유권 검증 포함) → FreshSettingSnapshot 또는 None."""
        from app import crud
        from app.database import get_db_context
        from app.notifications.alert_evaluator import FreshSettingSnapshot

        with get_db_context() as db:
            setting = crud.get_notification_setting_by_id(
                db=db, setting_id=setting_id, user_id=user_id,
            )
            if setting is None:
                return None
            return FreshSettingSnapshot(
                setting_id=setting.id,
                enabled=setting.enabled,
                triggered=setting.triggered,
                source=setting.bank,       # value pass-through
                asset=setting.currency,    # value pass-through
                condition=setting.condition,
                threshold=setting.threshold,
            )

    def persist_result(self, candidate: "CachedAlertSetting", rate: float, fcm_result: dict) -> None:
        """FX shadow: **no-op** — telemetry-only(mark_triggered/log/cleanup 0). legacy가 authoritative.

        cutover 시 real persist(mark_setting_triggered + create_notification_log) 전환은 open decision 7 후속.
        """
        return

    def build_payload(self, candidate: "CachedAlertSetting", triggered_rate: Decimal) -> tuple[str, str, dict]:
        """legacy FX 형식 (crud.process_rate_alerts 2200-2226 mirror) — data["type"]="rate_alert"."""
        from app.crud import BANK_NAMES_KR, CURRENCY_NAMES_KR, format_threshold

        bank = candidate.source       # value pass-through
        currency = candidate.asset
        bank_kr = BANK_NAMES_KR.get(bank, bank.upper())
        currency_kr = CURRENCY_NAMES_KR.get(currency, currency.upper())

        icon = "📈" if candidate.condition == "above" else "📉"
        title = f"{icon}  {bank_kr}  {currency_kr}"

        condition_arrow = "↑" if candidate.condition == "above" else "↓"
        condition_text = "이상" if candidate.condition == "above" else "이하"
        threshold_str = format_threshold(candidate.threshold)
        rate_str = f"{float(triggered_rate):.2f}"
        body = f"[ {threshold_str} {condition_arrow}{condition_text} 도달 ]   {rate_str}"

        data = {
            "type": "rate_alert",
            "title": title,
            "body": body,
            "bank": bank,
            "currency": currency,
            "rate": str(float(triggered_rate)),
            "threshold": str(candidate.threshold),
            "condition": candidate.condition,
            "setting_id": str(candidate.setting_id),
        }
        return title, body, data
