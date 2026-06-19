"""P1b C6-4 — FX topic rich publisher mapping (count primitive → SendResult, island, dormant).

topic_dispatcher.publish_topic_detailed가 반환하는 raw count(attempted/sent/enabled)를 B2b
coordinator의 SendResult(disposition + sent_count, atomic_coordinator)로 변환하는 **순수** mapper.
C6-6 live adapter가 asset→topic resolve + publish_topic_detailed 호출 후 이 mapper로 분류한다.

설계 (Workflow 3-lens + codex high 의견일치):
- **island/pure**: live I/O import 0 (atomic_coordinator의 SendResult/SendDisposition만 import).
  topic_dispatcher.TopicSendCounts를 import하지 **않고** primitive args(attempted/sent/enabled)를 받음
  — positive trip-wire(_LIVE_IO_FORBIDDEN, topic_dispatcher 포함) 회피. asset→topic + 실제 publish
  호출은 C6-6 live adapter(이 island은 dispatcher/fx_topic_publisher 미접촉).
- **total function**: 모든 count → SendResult 1개. **EXCEPTION 절대 생성 안 함** — publish_topic_detailed는
  per-client 격리라 top-level raise 없고, 구조적 raise는 coordinator가 SEND_EXCEPTION으로 funnel
  (atomic_coordinator.py:332-336). 반환 SendResult(EXCEPTION) 경로(341-342)는 이 publisher엔 dead.
- **mapping**: sent>0 → SENT(sent) / enabled & attempted>0 & sent==0 → ALL_FAILED(0) / 그 외 →
  NO_SUBSCRIBERS(0). enabled=False는 NO_SUBSCRIBERS로 collapse — SendDisposition에 DISABLED 없고
  FF는 coordinator step②(atomic_coordinator.py:284-288)서 upstream gate라 publisher 도달 전 차단.
  SENT⟺sent_count>0은 SendResult.__post_init__가 재강제.

**dormant**: live caller 0 (AST trip-wire). C6-6이 wiring할 때까지 미호출.
"""
from __future__ import annotations

from app.atomic_coordinator import SendDisposition, SendResult


def send_counts_to_send_result(*, attempted: int, sent: int, enabled: bool) -> SendResult:
    """topic send raw count → coordinator SendResult (순수 total mapper, §5.5).

    Args (topic_dispatcher.TopicSendCounts의 필드를 primitive로 받음 — island import-cleanliness):
        attempted: send 시도한 구독자 수.
        sent: send 성공 수.
        enabled: FF(TOPIC_DISPATCHER_ENABLED) 상태. False는 NO_SUBSCRIBERS로 collapse
            (coordinator가 FF를 upstream gate, send-step DISABLED disposition 없음).

    Returns:
        SendResult — sent>0 → SENT(sent) / (enabled & attempted>0 & sent==0) → ALL_FAILED(0) /
        그 외(attempted==0 또는 enabled=False) → NO_SUBSCRIBERS(0).
        **EXCEPTION은 절대 반환 안 함** (raise origin 전용, coordinator SEND_EXCEPTION funnel).

    Raises:
        ValueError: attempted/sent가 non-negative int 아님(bool 거부) / sent>attempted. SPLIT(primitive
            args 채택)이 TopicSendCounts.__post_init__ 검증을 drop했으므로 **동일 구조 검증을 boundary에
            복원**(Crash-Early — C6-6 wiring 시 mismatched count는 plausible-but-wrong disposition으로
            조용히 매핑되지 않고 즉시 실패). FF coupling(enabled=False⟹attempted=0)은 미검증 —
            enabled=False는 defensive collapse(→NO_SUBSCRIBERS) 의도.
    """
    for _name, _v in (("attempted", attempted), ("sent", sent)):
        if isinstance(_v, bool) or not isinstance(_v, int) or _v < 0:
            raise ValueError(f"send_counts_to_send_result: {_name} non-negative int — got {_v!r}")
    if not isinstance(enabled, bool):
        raise ValueError(f"send_counts_to_send_result: enabled bool — got {enabled!r}")
    if sent > attempted:
        raise ValueError(
            f"send_counts_to_send_result: sent>attempted (sent={sent}, attempted={attempted})"
        )

    if sent > 0:
        return SendResult(disposition=SendDisposition.SENT, sent_count=sent)
    if enabled and attempted > 0:
        return SendResult(disposition=SendDisposition.ALL_FAILED, sent_count=0)
    return SendResult(disposition=SendDisposition.NO_SUBSCRIBERS, sent_count=0)
