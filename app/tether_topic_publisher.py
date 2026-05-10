"""테더 탭 topic publish orchestration (PR Z-2b Stage 3 Level 1+2, 2026-05-10).

builder/helper와 publish_topic을 잇는 orchestration 계층 — Single Responsibility.

위치 분리 이유:
    - app/usdt_topic_payload.py: builder + DB helper (정규화/정렬/dispatch)
    - app/tether_topic_publisher.py: orchestration (config FF + subscriber guard +
      builder 호출 + publish_topic dispatch). topic_dispatcher/config/registry를
      모두 인지하는 계층은 builder와 분리.

Level 1 (완료, 9215eaf):
    - publish_tether_tab_snapshot wrapper — guard로 FF=false / subscriber 0 시
      builder/DB 호출 0 (hot path 비용 보호). include_krx=False 고정.

Level 2 (now): broadcast cycle 임시 hook 연결.
    - broadcast_rates_once `is_changed` 분기 안에서 safe_publish_tether_tab_snapshot
      호출. async/main loop 안이라 sync/async 경계 위험 회피.
    - safe_publish_tether_tab_snapshot이 예외 격리 — broadcast 정상 흐름 보호.
    - TOPIC_DISPATCHER_ENABLED=false default라 wrapper 진입 즉시 return →
      publish/builder 효과 0 (호출 경로만 활성, μs guard return).
    - **임시 위치**: USDT WebSocket/Redis-first 전환 후 mirror/topic pipeline으로
      이동 예정. 그때까지 broadcast cycle hook으로 동작.

Level 3 / activation (5/19+):
    - include_krx 정책 결정 (KRX_TOPIC_INCLUDE 신규 / KRX_BROADCAST_INCLUDE 재사용 /
      데이터 존재 게이트) 후 KRX 포함 wrapper 또는 매개변수 추가
    - TOPIC_DISPATCHER_ENABLED=true 활성화 + 클라이언트 release 동기화
    - dev/test client subscribe + 짧은 FF=true 시험으로 운영 검증

참조:
    - USDT_TOPIC_MIGRATION_PLAN.md §4 Z-2b
    - DECISIONS.md ADR-028 (Topic-only Tether/KRX)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app import config, topic_dispatcher
from app.usdt_topic_payload import load_and_build_tether_tab_payload

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger("exchange_rate.tether_topic_publisher")

# Topic 이름 상수 — 활성화 직전까지 자유 변경 (한 곳에서 끝).
# 5/19+ wire-up 시점에 client release와 함께 최종 확정.
TETHER_TOPIC: str = "usdt:krw"


async def publish_tether_tab_snapshot(db: "Session") -> bool:
    """테더 탭 snapshot을 TETHER_TOPIC 구독자에게 publish.

    Guard (builder 호출 비용 차단):
        1. config.TOPIC_DISPATCHER_ENABLED=false → 즉시 False
        2. registry.subscribed_connection_count == 0 → 즉시 False
        둘 다 통과 시에만 load_and_build_tether_tab_payload 호출.

    Args:
        db: SQLAlchemy Session (load_and_build_tether_tab_payload에 전달).

    Returns:
        True  — guard 통과 + 1명 이상 send 성공.
        False — FF=false / subscriber 0 / 모든 send 실패.

    KRX 포함:
        Level 1은 include_krx=False 고정. KRX 포함은 5/19+ wire-up 시 별도
        wrapper로 분리하거나 매개변수 추가 결정.
    """
    if not config.TOPIC_DISPATCHER_ENABLED:
        return False
    if topic_dispatcher.registry.subscribed_connection_count == 0:
        return False

    payload = load_and_build_tether_tab_payload(db, include_krx=False)
    sent = await topic_dispatcher.publish_topic(TETHER_TOPIC, payload)
    return sent > 0


async def safe_publish_tether_tab_snapshot(db: "Session") -> bool:
    """예외 격리 wrapper — 호출자(broadcast hot path)가 try/except 안 써도 안전.

    publish_tether_tab_snapshot의 어떤 단계에서 예외 발생해도 False 반환 + 로그.
    broadcast/mirror 같은 hot path의 정상 흐름을 보호하기 위한 entrypoint.

    Returns:
        publish_tether_tab_snapshot 결과 (True/False), 예외 시 False.

    Why 분리:
        main.py broadcast_rates_once 안 try/except + logger.exception 패턴은
        호출자 코드 가독성 떨어뜨리고 main.py가 firebase_admin 의존성 때문에
        단위 테스트 어려움. 격리 책임을 publisher 모듈로 옮기면 호출자는 한
        줄 호출 + 단위 테스트는 publisher 모듈에서 가능.
    """
    try:
        return await publish_tether_tab_snapshot(db)
    except Exception:
        logger.exception(
            "테더 topic publish 실패 (격리, hot path 영향 X)"
        )
        return False
