"""Bank/Investing direct-write Redis SET-outcome telemetry (item 4, 2026-06-15).

PR D(legacy FX hook 격하) precondition의 **운영 계측 축**. usdt_redis_stats.py 패턴을
재사용하되 별 모듈로 둔다(USDT와 측정 대상이 다름 — 통계 의미 혼선 회피).

배경:
    bank/investing의 latest:* direct SET이 실패하면 그 변경은 topic trigger를 못 만들고
    (SET-only gating, §6.6.2 C1), mirror cycle(3s)이 Redis 값은 복구해도 **trigger는
    안 만든다**(mirror는 데이터 복사). 현재는 legacy FX hook이 broadcast diff로 공백을
    메우므로 PR D 전엔 무해하나, hook 제거(PR D) 전에 SET 실패 빈도·원인·영향을 계측해야
    한다. 단 telemetry는 PR D의 **충분조건이 아니다** — 결정적 복구 발행 경로(correctness)가
    별 단계로 필요(failure-injection test → recovery-trigger/retry → PR D).

scope:
    bank/investing direct SET (`set_latest_bank_rate_from_sync_job` /
    `set_latest_investing_rate_from_sync_job`) 한정. USDT(usdt_redis_stats)·topic·
    broadcast 영역 대상 X.

설계 (usdt_redis_stats 패턴):
    - 프로세스 메모리 counter (threading.Lock 보호) — sync scheduler thread가 기록.
    - **Redis 미저장**: Redis 장애 순간에 발생하는 실패(client_unavailable / set_exception)
      까지 기록해야 하는데, telemetry sink를 Redis로 두면 바로 그 순간의 실패가 유실된다 →
      Redis 자체를 sink로 쓸 수 없어 process-local 사용. (writer_exception은 Redis와 무관한
      key/serialize 오류지만 일관성 위해 동일 sink에 기록.) process-local은 장기 durable
      store가 아님 — 재시작 전 구간만 보존, `started_at` 기준 해석. admin endpoint
      (`/admin/api/bank-investing-redis-stats`)가 in-process 직접 read.
    - `record_*`는 **예외를 절대 전파하지 않음**(telemetry 버그가 writer hot path 오염 금지).
    - `started_at`으로 카운터 누적 시작 시각 명시(프로세스 재시작 시 reset).
    - `reset_stats()`는 테스트 전용, admin 미노출(운영 실수 회피).

attempt = DB 변경 후 direct SET **시도** 횟수(크롤 횟수 아님 — redis_updates는 PR B changes).

실패 원인 3종 (set_latest_*가 client.set try 바깥에 key 생성/serialize를 두고, crud에도
최종 except 격리가 있어 2종으로는 누수):
    - client_unavailable: `_get_sync_client()` None (Redis client 미가용 — 서버 장애 한정 X).
    - writer_exception: client.set() **이전** 단계(key 생성/serialize) 예외.
    - set_exception: client.set() 예외.

참조:
    - app/usdt_redis_stats.py: 패턴 원본 (USDT 전용)
    - app/latest_rates_cache.py: set_latest_bank/investing_rate_from_sync_job (기록 site)
    - USDT_TOPIC_MIGRATION_PLAN.md §6.6: PR D precondition (SET-failure 복구 경로)
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

_KST = timezone(timedelta(hours=9))
_LAST_ERROR_MAX_LEN = 300

# 실패 원인 분류 (record_failure reason 인자 — Layer A 3-zone)
FAILURE_REASONS = ("client_unavailable", "writer_exception", "set_exception")
# failure_by_reason 버킷 = 3 reason + unknown(미등록 reason 정규화) →
# failure == sum(failure_by_reason.values()) 불변식 보존(운영자 기대).
_REASON_BUCKETS = FAILURE_REASONS + ("unknown",)


def _now_kst_iso() -> str:
    return datetime.now(_KST).isoformat()


def _empty_asset_stats() -> Dict[str, Any]:
    return {
        "attempt": 0,
        "success": 0,
        "failure": 0,
        "failure_by_reason": {r: 0 for r in _REASON_BUCKETS},
        "consecutive_failures": 0,  # success 시 0으로 reset
        "last_attempt_at": None,
        "last_success_at": None,
        "last_failure_at": None,
        "last_failure_reason": None,
        "last_failure_error": None,  # 마지막 실패의 exception type+message (bounded, traceback 미저장)
    }


def _empty_state() -> Dict[str, Any]:
    return {
        "started_at": _now_kst_iso(),
        # lazy populate: {source: {asset: _empty_asset_stats()}}
        "per_source": {},
    }


_lock = threading.Lock()
_state: Dict[str, Any] = _empty_state()


def _get_asset_stats_locked(source: str, asset: str) -> Dict[str, Any]:
    """lock 보유 상태에서 호출. lazy init source→asset entry."""
    per_source = _state["per_source"]
    if source not in per_source:
        per_source[source] = {}
    assets = per_source[source]
    if asset not in assets:
        assets[asset] = _empty_asset_stats()
    return assets[asset]


# ── Record functions (sync, lock 보호, 예외 절대 비전파) ──


def record_attempt(source: str, asset: str) -> None:
    """DB 변경 후 direct SET 시도 — set_latest_*_from_sync_job 진입 시 1회."""
    try:
        with _lock:
            s = _get_asset_stats_locked(source, asset)
            s["attempt"] += 1
            s["last_attempt_at"] = _now_kst_iso()
    except Exception:
        pass  # telemetry는 writer hot path를 절대 오염하지 않음


def record_success(source: str, asset: str) -> None:
    """Redis SET 성공."""
    try:
        with _lock:
            s = _get_asset_stats_locked(source, asset)
            s["success"] += 1
            s["consecutive_failures"] = 0
            s["last_success_at"] = _now_kst_iso()
    except Exception:
        pass


def record_failure(
    source: str, asset: str, reason: str, error: Optional[str] = None
) -> None:
    """Redis SET 실패. reason ∈ FAILURE_REASONS. error는 bounded 문자열(type+message)."""
    try:
        with _lock:
            s = _get_asset_stats_locked(source, asset)
            s["failure"] += 1
            # 미등록 reason은 unknown 버킷으로 정규화 → failure == sum(by_reason) 불변식.
            bucket = reason if reason in FAILURE_REASONS else "unknown"
            s["failure_by_reason"][bucket] += 1
            s["consecutive_failures"] += 1
            s["last_failure_at"] = _now_kst_iso()
            s["last_failure_reason"] = reason  # 원래 문자열 보존(unknown이어도)
            # reason은 무조건 갱신되므로 error도 무조건 갱신(없으면 None) — (reason, error)가
            # 항상 동일 최신 사건을 가리키게 유지(과거 메시지 잔존 → 운영자 오독 방지).
            s["last_failure_error"] = (
                error[:_LAST_ERROR_MAX_LEN] if error is not None else None
            )
    except Exception:
        pass


def _aggregate(stats_iter: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """per_asset stats들을 attempt/success/failure/failure_by_reason로 합산(derived)."""
    agg: Dict[str, Any] = {
        "attempt": 0,
        "success": 0,
        "failure": 0,
        "failure_by_reason": {r: 0 for r in _REASON_BUCKETS},
    }
    for s in stats_iter:
        agg["attempt"] += s["attempt"]
        agg["success"] += s["success"]
        agg["failure"] += s["failure"]
        for r in _REASON_BUCKETS:
            agg["failure_by_reason"][r] += s["failure_by_reason"][r]
    return agg


def get_stats() -> Dict[str, Any]:
    """admin endpoint 노출용 snapshot.

    구조: started_at + aggregate(전체 derived) + per_source{source: {aggregate(source
    derived), per_asset{asset: 상세}}}. aggregate는 per_asset에서 합산(double-write 회피,
    단일 진실 소스). deep copy라 호출자 mutation이 internal state에 영향 X.
    """
    with _lock:
        snap = copy.deepcopy(_state)
    out: Dict[str, Any] = {"started_at": snap["started_at"], "per_source": {}}
    all_assets = []
    for source, assets in snap["per_source"].items():
        out["per_source"][source] = {
            "aggregate": _aggregate(assets.values()),
            "per_asset": assets,
        }
        all_assets.extend(assets.values())
    out["aggregate"] = _aggregate(all_assets)
    return out


def reset_stats() -> None:
    """테스트 전용. started_at 재설정 + 모든 counter 초기화. admin 미노출."""
    global _state
    with _lock:
        _state = _empty_state()
