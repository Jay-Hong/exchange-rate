"""FX(bank+investing) alert SHADOW — **보조 진단(execution-proof), parity 기준 아님** (fanout step 4 S3~S6).

[ALL_SOURCE_FANOUT_UNIFICATION_PLAN.md §6.1] FX 알림을 legacy authoritative
(crud.process_rate_alerts) 유지한 채 병렬 telemetry-only로 새 evaluator 경로를 돌려 진단.

⚠️ **이 post-legacy async shadow는 parity 기준이 될 수 없다** (2026-06-24 prod crossing 실측 + codex 검증):
- legacy가 writer에서 동기적으로 `mark_setting_triggered`(triggered=True **+ enabled=False**,
  crud.py:2168-2169) + commit 후에야 shadow가 async로 돎. 발사된 setting은 enabled=False가 되어
  `load_settings`(enabled==True AND triggered==False 필터, alert_storage_backend:266-267)로 **더는 안 잡힘**.
- shadow가 그 setting을 보려면 **발사 전 cache snapshot(cache-hit)**이어야 하나, cache TTL 10s가 은행 변경
  간격보다 짧아 crossing 시점엔 보통 만료 → fresh load(발사 후) → 0. 즉 **would_fire/matched_candidates는
  구조적으로 신뢰 불가**(cache-hit인 우연한 창만 잡힘) — parity 수치로 쓰지 말 것.
- ✅ **parity 기준 = legacy pre-mutation baseline** (crud `_fx_legacy_match_counts`: triggered_items 직후
  mark_triggered 전 = legacy와 같은 DB 상태, 신뢰 가능). ⚠️ inline dual-compute(S7)는 검토했으나 **폐기**
  — legacy condition ≡ `condition_matches_observation`(byte-identical >=/<=)이라 tautological. 최종
  cutover(real persist 전환) 판단 = **single-setting canary**(open decision 7 별도 plan-first).

이 모듈의 카운터 = **execution-proof 보조 진단**: 새 async 경로가 돌았나(batch_seen) / 설정 load됐나
(settings_loaded) / condition 매칭됐나(matched_candidates, cache-hit subset만) / refetch서 빠졌나
(refetch_skipped_triggered). cutover 시 이 async infra가 authoritative 되므로 prod 무에러 실행 검증용.
cache invalidation·cutover(real persist)는 별도 (open decision 7, canary plan-first).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from app.notifications.alert_evaluator import (
    AlertObservation,
    AlertSettingsCache,
    UsdtAlertEvaluator,
    condition_matches_observation,
    delivery_allowed,
)
from app.notifications.alert_storage_backend import FxNotificationBackend

logger = logging.getLogger("exchange_rate.notifications.fx_alert_shadow")

# would_fire = matched_candidates per (source, asset) — condition-match 지점(refetch 이전).
# ⚠️ 보조 진단(execution-proof)일 뿐 **parity 신호 아님** — cache-hit subset만 잡힘(module docstring 참조).
# parity 기준은 crud `_fx_legacy_match_counts`(legacy pre-mutation baseline).
_fx_would_fire_counts: dict[tuple[str, str], int] = {}

# execution-proof 보조 진단 카운터 (S6a). worker(to_thread refetch) ↔ loop 경계 + endpoint read →
# 모든 접근 _counter_lock 보호 (dict size 변경 중 snapshot RuntimeError 방지).
_fx_shadow_stats: dict[str, int] = {
    "batch_seen": 0,                 # 평가된 observation 수 (새 async 경로 실행 증거)
    "settings_loaded": 0,            # load된 후보 설정 수 (enabled+!triggered, cache)
    "matched_candidates": 0,         # condition-match 통과 수 (= would_fire 총합, cache-hit subset 진단)
    "refetch_skipped_triggered": 0,  # refetch서 triggered/disabled로 skip (post-legacy race 증거)
    "would_send": 0,                 # refetch 후 enabled+!triggered 통과 (실제 발사 보장 X —
                                     #   _evaluate_async의 source/asset·fresh condition 재검증 미포함)
}
_counter_lock = threading.Lock()

# lazy singleton (import side-effect 0 — get_fx_alert_evaluator 호출 전까지 미생성).
_fx_shadow_evaluator: Optional[UsdtAlertEvaluator] = None


def _noop_sender(tokens: list[str], title: str, body: str, data: dict) -> dict:
    """방어용 no-op sender — 이 S6 경로는 _do_send_and_persist 미사용이라 미호출이나,
    혹시 모를 호출에도 FCM 0 보장 (telemetry-only 불변)."""
    return {"success_count": 0, "failure_count": 0, "failed_tokens": []}


def get_fx_alert_evaluator() -> UsdtAlertEvaluator:
    """lazy-init FX shadow evaluator (FxNotificationBackend + dedicated cache + no-op sender).

    cache(per-key load 캐싱) + backend(load_settings/refetch_snapshot) 재사용용. 실제 카운팅
    orchestration은 evaluate_fx_batch_shadow가 직접 instrumented (shared _evaluate_async 미사용
    — 중간 단계 카운터를 공유 evaluator에 심지 않기 위해).
    """
    global _fx_shadow_evaluator
    if _fx_shadow_evaluator is None:
        _fx_shadow_evaluator = UsdtAlertEvaluator(
            backend=FxNotificationBackend(),
            cache=AlertSettingsCache(),
            sender=_noop_sender,
        )
    return _fx_shadow_evaluator


async def evaluate_fx_batch_shadow(observations: list[AlertObservation]) -> None:
    """instrumented shadow eval — **execution-proof 보조 진단** (telemetry-only, 발사/persist/FCM 0).

    ⚠️ parity 기준 아님 (module docstring 참조): matched_candidates는 post-legacy async라 cache-hit
    subset만 잡힘(legacy 발사 후 setting은 enabled=False라 fresh load서 사라짐). parity 기준은 crud
    `_fx_legacy_match_counts`(legacy pre-mutation baseline); cutover는 canary plan-first(S7 inline은 tautological이라 폐기).
    이 함수는 "새 async 경로가 prod서 무에러로 도는가 / 어디서 빠지는가"를 보는 진단 용도.
    refetch_skipped_triggered/would_send = post-legacy race 진단 보조 지표.
    각 obs는 try/except로 격리 (shadow 실패가 bridge/writer에 영향 0).
    """
    ev = get_fx_alert_evaluator()
    now = datetime.now(timezone.utc)
    for obs in observations:
        try:
            settings = await ev._get_settings_for_key(obs.source, obs.asset)
            candidates = [s for s in settings if condition_matches_observation(s, obs)]
            with _counter_lock:
                _fx_shadow_stats["batch_seen"] += 1
                _fx_shadow_stats["settings_loaded"] += len(settings)
                _fx_shadow_stats["matched_candidates"] += len(candidates)
                for c in candidates:
                    key = (c.source, c.asset)
                    _fx_would_fire_counts[key] = _fx_would_fire_counts.get(key, 0) + 1
            # 보조 — refetch로 race(legacy 선점) 분리 (parity 신호 아님, 진단용)
            for c in candidates:
                snapshot = await asyncio.to_thread(
                    ev._backend.refetch_snapshot, c.setting_id, c.user_id,
                )
                allowed = snapshot is not None and delivery_allowed(snapshot, now)
                with _counter_lock:
                    if allowed:
                        _fx_shadow_stats["would_send"] += 1
                    else:
                        _fx_shadow_stats["refetch_skipped_triggered"] += 1
        except Exception:
            logger.exception(
                "[fx_alert_shadow] eval 실패 (격리)",
                extra={"source": obs.source, "asset": obs.asset, "rate": obs.rate},
            )


def get_fx_would_fire_counts() -> dict[tuple[str, str], int]:
    """would_fire(matched_candidates per key) snapshot (lock-guarded)."""
    with _counter_lock:
        return dict(_fx_would_fire_counts)


def get_fx_shadow_stats() -> dict[str, int]:
    """execution-proof 카운터 snapshot (lock-guarded)."""
    with _counter_lock:
        return dict(_fx_shadow_stats)


def _reset_fx_shadow_state() -> None:
    """test-only — 카운터 clear + singleton reset (테스트 격리, lock-guarded)."""
    global _fx_shadow_evaluator
    with _counter_lock:
        _fx_would_fire_counts.clear()
        for k in _fx_shadow_stats:
            _fx_shadow_stats[k] = 0
    _fx_shadow_evaluator = None
