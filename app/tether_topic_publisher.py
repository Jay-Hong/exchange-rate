"""테더 탭 topic publish orchestration (PR Z-2b Stage 3 Level 1, 2026-05-10).

builder/helper와 publish_topic을 잇는 orchestration 계층 — Single Responsibility.

위치 분리 이유:
    - app/usdt_topic_payload.py: builder + DB helper (정규화/정렬/dispatch)
    - app/tether_topic_publisher.py: orchestration (config FF + subscriber guard +
      builder 호출 + publish_topic dispatch). topic_dispatcher/config/registry를
      모두 인지하는 계층은 builder와 분리.

Level 1 범위 (지금):
    - hot path 미연결 (dead code) — 호출 경로 0, 운영 영향 0
    - guard로 FF=false / subscriber 0 시 builder 호출도 차단 (hot path 비용 보호)
    - include_krx=False 고정 — KRX 포함 wrapper는 별도 (5/19+ wire-up 시 추가)

Level 2 (5/19+):
    - hot path 연결: collect_usdt_rates `changed_rates` 후처리 / broadcast cycle /
      mirror cycle 중 baseline 분석 후 결정
    - include_krx 정책 (KRX_TOPIC_INCLUDE 신규 / KRX_BROADCAST_INCLUDE 재사용 /
      데이터 존재 게이트) 결정 후 KRX 포함 wrapper 추가
    - TOPIC_DISPATCHER_ENABLED=true 활성화 + 클라이언트 release 동기화

참조:
    - USDT_TOPIC_MIGRATION_PLAN.md §4 Z-2b
    - DECISIONS.md ADR-028 (Topic-only Tether/KRX)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app import config, topic_dispatcher
from app.usdt_topic_payload import load_and_build_tether_tab_payload

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

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
