"""atomic-write mode cache refresh orchestration (P1b A2-2).

`atomic_write_runtime.refresh_from_db(db)`를 위한 **session-opening no-throw wrapper**.
startup(1회) / scheduler poll / Selenium subprocess(runner) 공용 진입점.

atomic_write_runtime.py는 순수 cache(session 미관리)로 유지(A2-1 import-side-effect 0
trip-wire 보존) — session lifecycle은 본 모듈이 담당.

import-time side effect 0: 모듈 로드 시 SessionLocal/DB 접근 없음 (함수 내부 lazy import).
"""
from __future__ import annotations

import logging

from app import atomic_write_runtime

logger = logging.getLogger("exchange_rate.atomic_write_refresh")


def refresh_write_mode_cache() -> None:
    """DB session을 열어 write-mode cache를 1회 갱신 (no-throw).

    refresh_from_db 자체가 no-throw(read 실패→fail-close snapshot)이나, SessionLocal()
    생성/close 실패까지 흡수해 호출처(startup/poll/subprocess)에 예외 전파 0.
    """
    db = None
    try:
        from app.database import SessionLocal  # lazy — 모듈 import 시 engine 접근 회피
        db = SessionLocal()
        atomic_write_runtime.refresh_from_db(db)
    except Exception:
        logger.debug("write-mode cache refresh 실패 (무시 — cache는 last-good 유지)", exc_info=True)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
