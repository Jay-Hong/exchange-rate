"""P1b C6-9a — atomic FX cutover go/no-go status builder (read-only, never-crash, dormant observability).

GET /admin/api/atomic-cutover-status가 노출하는 진단 dict 빌더. behavior-change-0:
- cutover state = `atomic_cutover_runtime.read_cutover_snapshot_fresh`(**pure** — _current 미변경,
  observer side-effect 0; refresh_from_db의 cache mutate 회피). runtime이 첫 sanctioned live consumer.
- gate shadow = `fx_topic_publisher.get_fx_topic_telemetry`의 C6-7 gate_* field(이미 live).
- config / future 블록. per-block isolated never-crash (A1 atomic-write-control-status 패턴).
- available-now만 surface, future(conflict/migration/watermark_lag)는 `available:false`(미persist — no fabrication).

[memory project_main_py_helper_placement] main.py inline helper는 firebase import chain로 unit test 깨짐 →
도메인 모듈 분리. atomic_cutover_durable은 **직접 import 안 함**(runtime 경유 transitive) — dormancy sanction 1개.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

from app import atomic_cutover_runtime, config
from app.fx_membership import FX_MEMBERSHIP_VERSION
from app.fx_topic_publisher import (
    _GATE_COUNTER_FIELDS,
    _GATE_LAST_FIELDS,
    get_fx_topic_telemetry,
)


async def build_cutover_status_dict(db_factory: Callable[[], Any]) -> Dict[str, Any]:
    """C6-9a go/no-go 진단 dict — 항상 dict 반환(per-block isolated, authorized 기준 always-200).

    db_factory: SessionLocal (테스트는 fake session factory 주입).

    **status 의미**: block **exception** health (config.error / cutover_read_error / gate_shadow.error 시
    "degraded", A1 정합). cutover.read_ok은 **별도** read-health 신호 — status에 접지 않음: dormant
    phase(cutover table 미migration)에선 read_ok=False가 **정상**이라 folding하면 false-degraded.
    operator는 cutover.read_ok으로 cutover read-health를 별도 판단.
    """
    result: Dict[str, Any] = {}
    # gate_shadow_note는 success/error 무관 항상 surface (가장 필요한 error path에서도 의미 전달)
    result["gate_shadow_note"] = (
        "PASS_THROUGH/0이 정상 (C6-7 dry-run; refresh_from_db 미스케줄이라 live gate는 _INITIAL "
        "PASS_THROUGH 상시, WOULD_BLOCK은 test-injected만). FX_CUTOVER_GATE_OBSERVE_ENABLED로 활성."
    )

    # ── config block (cheap, interpretation에 영향) ──
    try:
        result["config"] = {
            "fx_topic_enabled": config.FX_TOPIC_ENABLED,
            "topic_dispatcher_enabled": config.TOPIC_DISPATCHER_ENABLED,
            "fx_cutover_gate_observe_enabled": config.FX_CUTOVER_GATE_OBSERVE_ENABLED,
            "fx_membership_version": FX_MEMBERSHIP_VERSION,
        }
    except Exception:
        result["config"] = {"error": "config_read_error"}

    # ── cutover block (pure fresh read — _current 미변경, gate snapshot source 무간섭) ──
    db = None
    try:
        db = db_factory()  # try 안 open — factory 예외도 never-crash 포섭
        snap = atomic_cutover_runtime.read_cutover_snapshot_fresh(db)
        result["cutover"] = {
            "cutover_state": snap.cutover_state.value,
            "read_ok": snap.read_ok,          # False = 미refresh/last-good 아닌 fresh read 실패 구분
            "publisher_gate_open": snap.publisher_gate_open,
            "writer_enforced_action": snap.writer_enforced_action,
            "bootstrap_status": snap.bootstrap_status,
            "bootstrap_generation": snap.bootstrap_generation,
            "bootstrap_session_id": snap.bootstrap_session_id,
            "per_asset_publish_state": {a: s for a, s in snap.per_asset_publish_state},
        }
    except Exception as e:
        result["cutover"] = {
            "cutover_read_error": {"reason": type(e).__name__, "message": "cutover state 조회 실패"}
        }
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    # ── gate shadow block (C6-7 dry-run shadow surface, 1 Redis round-trip) ──
    try:
        telem = await get_fx_topic_telemetry()
        gate_fields = set(_GATE_COUNTER_FIELDS) | set(_GATE_LAST_FIELDS)
        result["gate_shadow"] = {
            topic: {k: v for k, v in snapshot.items() if k in gate_fields}
            for topic, snapshot in telem.items()
            if isinstance(snapshot, dict)  # malformed 1개가 전체 block nuke 방지(defensive)
        }
    except Exception:
        result["gate_shadow"] = {"error": "gate_shadow_read_error"}

    # ── future signals (현재 미persist — no fabrication) ──
    result["future"] = {
        "conflict_counters": {"available": False, "reason": "coordinator dormant — no persisted counters"},
        "migration_verification": {"available": False, "reason": "no persisted migration surface"},
        "watermark_lag": {"available": False, "reason": "no watermark lag reader"},
    }

    # status: block error 있으면 degraded (A1 정합 — monitoring이 top-level status로 health 판단).
    _has_error = (
        "error" in result.get("config", {})
        or "cutover_read_error" in result.get("cutover", {})
        or "error" in result.get("gate_shadow", {})
    )
    result["status"] = "degraded" if _has_error else "ok"
    return result
