"""P1b C6-quiesce Q4a — live-app startup halt-ACK writer.

recreate된 fresh app이 startup에서 (open quiesce session이 있고 자기가 durable halt를 관측했으면)
`cas_record_quiesce_app_ack`를 호출해 halt-관측 ACK를 durable write 한다. drain = recreate + fresh-process
halt ACK (§9 step6 개정, §19 C6-quiesce). main.py lifespan이 `scheduler.start_scheduler()`(startup
`refresh_write_mode_cache` 포함) **직후** 1회 호출한다 — 그래야 `snapshot()`이 stale `_INITIAL`(LEGACY) 대신
durable halt를 반영(refresh 전 호출 시 cond2 fail → ACK 영영 미기록 = FLIP liveness break).

**behavior-change-0 in legacy** (2층 방어): open quiesce session은 activation(C6-FLIP)에서만 transient하게
생긴다 — legacy steady state엔 없어 `find_open_quiesce_session`이 None → 즉시 return(0 write/0 commit/0
snapshot-branch). 설사 도달해도 brick `cas_record_quiesce_app_ack`가 cond1(live requested_mode==HALT)/
cond2(observed_enforced_action=='halt')로 fail-closed. + quiesce table은 create_all 제외(pre-migrate 부재)라
query가 raise해도 broad except가 흡수 → no-op (atomic_write_refresh "control 없으면 legacy" 정신).

**no-throw**: 전체 try/except → log + return (warmup_latest_rates / refresh_write_mode_cache 패턴). 어떤 경로도
lifespan에 예외 전파 안 함. 자체 SessionLocal + finally close. caller-commits — APPLIED만 commit.

**dormant 경계**: 본 모듈이 atomic_quiesce_durable(island)의 **유일 sanctioned app/ importer**(Q2b no-importer
trip-wire offenders-allowlist). app import는 전부 함수 내부 lazy — module-load 시 SessionLocal/island/scheduling
끌어오지 않음(import-time side effect 0). _BOOT_ID(uuid)만 module-level 순수 계산.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("exchange_rate.atomic_quiesce_startup")

# 프로세스 lifetime 동안 안정된 boot 식별자 (UNIQUE(session_id, boot_id) — 같은 boot 재-ACK는 dup→CAS_LOST
# harmless). import 시 1회 생성, per-call 재생성 금지(dup fence 무력화 방지). 테스트는 monkeypatch 가능.
_BOOT_ID = uuid.uuid4().hex


def _process_started_at() -> datetime:
    """OS process boot 시각 (naive UTC). psutil create_time — 무거운 lib이라 fn-local import (repo idiom)."""
    import psutil  # fn-local (heavy lib, scheduler.py:1161 / main.py:1093 idiom)

    created = psutil.Process().create_time()  # POSIX epoch float (UTC)
    return datetime.fromtimestamp(created, tz=timezone.utc).replace(tzinfo=None)


def record_app_ack_if_quiescing() -> Optional[object]:
    """open quiesce session 있으면 halt-관측 ACK durable write (else no-op). no-throw. 반환 advisory.

    main.py lifespan이 `scheduler.start_scheduler()` 직후 1회 호출(snapshot은 refresh된 cache 반영).
    Returns: CasResult | None — caller(main.py)는 무시(advisory only).
    """
    db = None
    try:
        # lazy import — module-load 시 island/DB 미로드 (import-time side effect 0)
        from app import atomic_write_runtime
        from app.atomic_cutover_durable import CasResult
        from app.atomic_quiesce_durable import (
            cas_record_quiesce_app_ack,
            find_open_quiesce_session,
        )
        from app.database import SessionLocal

        db = SessionLocal()
        session = find_open_quiesce_session(db)  # FIRST action — legacy면 None → no-op
        if session is None:
            return None
        snap = atomic_write_runtime.snapshot()  # scheduler가 채운 cache (refresh 후 durable halt 반영)
        result = cas_record_quiesce_app_ack(
            db,
            session_id=session.session_id,
            boot_id=_BOOT_ID,
            process_started_at=_process_started_at(),
            observed_writer_generation=snap.mode_generation,
            observed_enforced_action=snap.enforced_action,
        )
        if result is CasResult.APPLIED:
            db.commit()  # caller-commits — APPLIED만 (staged)
            logger.info(
                "✅ quiesce halt-ACK 기록", extra={"session_id": session.session_id, "boot_id": _BOOT_ID}
            )
        else:
            db.rollback()  # CAS_LOST/PRECONDITION_FAILED는 stage 0 — empty-tx commit 회피
            logger.debug("quiesce halt-ACK skip", extra={"result": getattr(result, "value", str(result))})
        return result
    except Exception:
        # no-throw: open-session 조회 실패(table 부재 포함)/snapshot/commit 어떤 예외도 startup 안 깸.
        # DEBUG 레벨 — pre-migrate(quiesce table 부재)는 매 boot 예상된 no-op이라 warning stacktrace noise
        # 회피(refresh_write_mode_cache "control 없으면 legacy" quiet 패턴). 실 활성화(C6-FLIP) 시 ACK 실패는
        # boundary가 fail-closed로 begin-atomic 거부 → 운영자가 인지(이 로그가 유일 신호 아님).
        logger.debug("quiesce halt-ACK writer skip(no-throw)", exc_info=True)
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
