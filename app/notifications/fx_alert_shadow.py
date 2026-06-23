"""FX(bank+investing) alert SHADOW evaluator (fanout step 4 S3).

[ALL_SOURCE_FANOUT_UNIFICATION_PLAN.md §6.1] FX 알림을 legacy authoritative
(crud.process_rate_alerts) 유지한 채 병렬 telemetry-only shadow 평가.

⚠️ dead-until-S4: 이 모듈의 어떤 함수도 app/scripts에서 호출 안 됨 (behavior-change-0).
   crud 4-site 실주입 + flag(FX_ALERT_SHADOW_ENABLED)는 S4.

설계 노트:
- raw ``_evaluate_async`` path 사용 (coalescer 우회) — legacy FX 단건 평가와 정합 +
  create_task spam 회피. USDT/KRX tick-coalescing(``_evaluate_price_input_async``)
  모델과 다름 — cutover 시 평가 의미 변경됨 (shadow 카운트는 USDT 묶음 평가를 예측 X).
- dedicated ``AlertSettingsCache()`` — process-wide singleton
  (``get_default_alert_settings_cache``)은 USDT/KRX API invalidation과 공유.
  FX shadow는 invalidation hook 없음 → 최대 TTL setting staleness 허용
  (telemetry-only, non-user-facing).
- would_fire 카운트 = refetch + delivery_allowed + 조건 재검증 통과 후 발사 직전
  (shadow attempted-send). legacy sent_count와 1:1 아님 (coalescer 묶음 / in-flight
  dedup으로 의미 다름).
- sender는 ``_do_send_and_persist``에서 ``asyncio.to_thread`` worker thread로 실행 →
  module 카운터는 cross-thread 공유 상태. increment/snapshot/reset 전부 ``_counter_lock``
  으로 보호 (snapshot의 dict() 복사가 worker increment와 겹쳐 size 변경 RuntimeError
  나는 것 방지 — repo topic_trigger_bridge worker/loop 경계 lock 선례 정합).
"""
from __future__ import annotations

import threading
from typing import Optional

from app.notifications.alert_evaluator import (
    AlertObservation,
    AlertSettingsCache,
    UsdtAlertEvaluator,
)
from app.notifications.alert_storage_backend import FxNotificationBackend

# would_fire 카운터: key=(bank, currency). worker thread(increment) ↔ event loop
# (snapshot/reset) 경계 → 모든 접근 _counter_lock 보호.
_fx_would_fire_counts: dict[tuple[str, str], int] = {}
_counter_lock = threading.Lock()

# lazy singleton (import side-effect 0 — get_fx_alert_evaluator 호출 전까지 미생성).
_fx_shadow_evaluator: Optional[UsdtAlertEvaluator] = None


def _shadow_noop_sender(tokens: list[str], title: str, body: str, data: dict) -> dict:
    """FX shadow sender — FCM 미발송, would_fire 카운트만 (lock-guarded).

    ``asyncio.to_thread`` worker thread에서 실행됨 → module dict는 lock으로 보호.
    반환 dict는 ``persist_result``(FxNotificationBackend no-op)로만 흘러 무의미하나
    legacy schema 호환 형태 유지.
    """
    key = (data["bank"], data["currency"])
    with _counter_lock:
        _fx_would_fire_counts[key] = _fx_would_fire_counts.get(key, 0) + 1
    return {"success_count": 0, "failure_count": 0, "failed_tokens": []}


def get_fx_alert_evaluator() -> UsdtAlertEvaluator:
    """lazy-init FX shadow evaluator (FxNotificationBackend + dedicated cache + no-op sender)."""
    global _fx_shadow_evaluator
    if _fx_shadow_evaluator is None:
        _fx_shadow_evaluator = UsdtAlertEvaluator(
            backend=FxNotificationBackend(),
            cache=AlertSettingsCache(),
            sender=_shadow_noop_sender,
        )
    return _fx_shadow_evaluator


async def evaluate_fx_batch_shadow(observations: list[AlertObservation]) -> None:
    """bounded sequential shadow eval — create_task 미사용 (spam 회피).

    raw observation path (``_evaluate_async``) — legacy FX 단건 평가 정합. 각 obs는
    ``_evaluate_async`` 내부 try/except로 obs 단위 격리 (per-obs swallow,
    CancelledError re-raise) → 추가 try/except 금지 (중복).
    """
    ev = get_fx_alert_evaluator()
    for obs in observations:
        await ev._evaluate_async(obs)


def get_fx_would_fire_counts() -> dict[tuple[str, str], int]:
    """카운터 snapshot (shallow copy, lock-guarded)."""
    with _counter_lock:
        return dict(_fx_would_fire_counts)


def _reset_fx_shadow_state() -> None:
    """test-only — 카운터 clear + singleton reset (테스트 격리, lock-guarded)."""
    global _fx_shadow_evaluator
    with _counter_lock:
        _fx_would_fire_counts.clear()
    _fx_shadow_evaluator = None
