"""DXY 현물 독립 topic publisher (``dxy:spot``).

Legacy ``rates.data.indices.dxy``에만 있던 DXY를 topic snapshot/live 경로로 전달한다.
크롤러의 변경 INSERT 직후에는 DB canonical 레코드를 loop로 넘기고, 초기 snapshot은
Redis-first + DB fallback으로 같은 payload를 만든다.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime
from functools import partial
from typing import Any, Callable, Dict, Optional

from app import config, topic_dispatcher, topic_trigger_bridge

logger = logging.getLogger("exchange_rate.dxy_topic")

DXY_TOPIC = "dxy:spot"
DXY_INSTRUMENT = "dxy"
DXY_SOURCES = frozenset({"investing", "cnbc", "yahoo"})

# asyncio loop은 Task를 약한 참조로만 보유할 수 있다. live publish가 socket send를
# 기다리는 동안 수집되지 않도록 완료 시점까지 강한 참조를 유지한다.
_publish_tasks: "set[asyncio.Task[None]]" = set()


def normalize_dxy_topic_entry(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """DB/Redis DXY 레코드를 wire entry로 엄격하게 정규화한다."""
    if not isinstance(raw, dict):
        return None
    instrument = raw.get("instrument", DXY_INSTRUMENT)
    if instrument != DXY_INSTRUMENT:
        return None

    source = raw.get("source")
    if not isinstance(source, str) or source not in DXY_SOURCES:
        return None

    raw_rate = raw.get("rate")
    if isinstance(raw_rate, bool):
        return None
    try:
        rate = float(raw_rate)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate) or rate <= 0:
        return None

    raw_timestamp = raw.get("timestamp")
    if isinstance(raw_timestamp, datetime):
        if raw_timestamp.tzinfo is None or raw_timestamp.utcoffset() is None:
            return None
        timestamp = raw_timestamp.isoformat()
    elif isinstance(raw_timestamp, str):
        try:
            parsed = datetime.fromisoformat(raw_timestamp)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        timestamp = raw_timestamp
    else:
        return None

    return {"rate": rate, "timestamp": timestamp, "source": source}


def build_dxy_topic_payload(entry: Dict[str, Any]) -> Dict[str, Any]:
    """DXY entry를 legacy ``indices.dxy``와 대칭인 topic payload로 만든다."""
    normalized = normalize_dxy_topic_entry(entry)
    if normalized is None:
        raise ValueError("유효하지 않은 DXY topic entry")
    return {
        "type": "snapshot",
        "topic": DXY_TOPIC,
        "version": 1,
        "data": {"dxy": normalized},
    }


def load_dxy_topic_entry(
    db=None,
    *,
    checkpoint: Optional[Callable[[], None]] = None,
    prefer_db: bool = False,
) -> Optional[Dict[str, Any]]:
    """DXY latest를 로드한다.

    초기 snapshot은 Redis-first + DB fallback이다. 크롤러 trigger는 방금 commit한 DB 값을
    보내야 하므로 ``prefer_db=True``로 bounded mirror race를 우회한다.
    """
    from app import crud
    from app.latest_rates_cache import get_latest_dxy_rate_from_sync_job

    if checkpoint is not None:
        checkpoint()
    if prefer_db:
        raw = crud.get_latest_dxy_rate(db) if db is not None else None
    else:
        raw = get_latest_dxy_rate_from_sync_job()
        if raw is None and db is not None:
            if checkpoint is not None:
                checkpoint()
            raw = crud.get_latest_dxy_rate(db)
    if raw is None:
        return None
    return normalize_dxy_topic_entry(raw)


async def publish_dxy_topic_snapshot(
    entry: Optional[Dict[str, Any]] = None, *, db=None
) -> bool:
    """DXY 구독자에게 latest snapshot을 publish한다."""
    if not config.TOPIC_DISPATCHER_ENABLED:
        return False
    if topic_dispatcher.registry.subscriber_count(DXY_TOPIC) == 0:
        return False

    normalized = (
        load_dxy_topic_entry(db)
        if entry is None
        else normalize_dxy_topic_entry(entry)
    )
    if normalized is None:
        return False
    sent = await topic_dispatcher.publish_topic(
        DXY_TOPIC, build_dxy_topic_payload(normalized)
    )
    return sent > 0


async def _run_publish(entry: Dict[str, Any]) -> None:
    try:
        await publish_dxy_topic_snapshot(entry)
    except Exception:
        logger.exception("[dxy_topic] publish 실패 (격리)")


def _schedule_publish(entry: Dict[str, Any]) -> None:
    """app loop에서 DXY publish task를 생성한다."""
    task = asyncio.get_running_loop().create_task(_run_publish(entry))
    _publish_tasks.add(task)
    task.add_done_callback(_publish_tasks.discard)


def request_dxy_topic_publish(db, reason: str) -> None:
    """DXY 변경 INSERT 직후 sync crawler thread에서 호출한다.

    dispatcher가 꺼져 있으면 DB read와 marshal을 모두 생략한다. 전달할 값은 방금
    commit된 DB canonical 레코드로 고정해 mirror 갱신 순서에 의존하지 않는다.

    구독자 판정은 반드시 main loop의 ``publish_dxy_topic_snapshot``에서 한다.
    ``TopicRegistry``는 event-loop 전용이라 crawler worker thread에서 읽으면 구독
    등록/해제와 경합해 발행 요청이 유실될 수 있다. 변경 INSERT 때의 DB read 1회는
    이 thread-safety를 위해 감수한다.
    """
    try:
        if not config.TOPIC_DISPATCHER_ENABLED:
            return
        entry = load_dxy_topic_entry(db, prefer_db=True)
        if entry is None:
            logger.warning(
                "[dxy_topic] 변경 후 latest 레코드 없음",
                extra={"reason": reason},
            )
            return
        topic_trigger_bridge.schedule_on_loop(partial(_schedule_publish, dict(entry)))
    except Exception:
        logger.exception(
            "[dxy_topic] publish 스케줄 실패 (격리)",
            extra={"reason": reason},
        )
