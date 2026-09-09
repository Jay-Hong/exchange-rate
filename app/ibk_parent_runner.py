"""IBK bootstrap→capture→결과 검증→배타적 계측의 호출 가능한 연결 후보.

worker에 명시적으로 주입할 수 있으나 운영 시작 지점은 아직 주입하지 않는다. child adapter와 실제 경보
dispatcher가 없으므로 deploy 후보가 아니다. event_sink는 비차단 enqueue 포트이며
True는 큐 접수이지 Telegram 전송 성공이 아니다. 기존 crawler_stats와 중복 집계하지 않는다.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import sys
import time
from uuid import uuid4

from app.ibk_result_protocol import (
    IbkExecutionFailure, IbkParentDecision, IbkProtocolError, IbkStatus,
    decide_ibk_process_result,
)
from app.ibk_run_context import MAX_WORK_BUDGET_SECONDS, IbkRunContext
from app.ibk_subprocess_capture import IbkProcessCapture, capture_ibk_subprocess

BOOTSTRAP = Path(__file__).resolve().parents[1] / "scripts" / "ibk_subprocess_bootstrap.py"

# 자식의 **작업** 기한을 실행 제한에서 뺄 몫. 정리(quit·세션 닫기)와 결과 프레임 출력·
# 프로세스 종료에 쓸 시간이다. 45초 기본에서 작업 34 / 정리 6 / 출력·종료 5 로 나눈 것.
# ⛔ 이 예약이 "정리가 그 안에 끝난다" 를 보장하지는 않는다 — 멈춘 `quit()` 은 부모 kill 로만
#    끝난다. 예약은 정상 경로에서 결과를 낼 여지를 남기는 것이다.
CHILD_RESERVE_SECONDS = 11.0
# 자식에게 의미 있게 줄 수 있는 최소 작업 시간.
MIN_CHILD_WORK_SECONDS = 1.0
# ⛔ 이보다 작은 실행 제한은 **거부한다**. 하한을 강제로 부여하면 timeout 0.1초에도 자식이
#    1초 예산을 받아 **부모 제한보다 큰 예산**이 생긴다(실측). 없는 예산을 지어내는 대신
#    지원하지 않는 값이라고 말한다.
MIN_SUPPORTED_TIMEOUT_SECONDS = CHILD_RESERVE_SECONDS + MIN_CHILD_WORK_SECONDS


def _now():
    return datetime.now(timezone.utc)


def _protocol_failure(code):
    return IbkParentDecision(None, IbkExecutionFailure.RESULT_PROTOCOL_ERROR, code, False)


@dataclass(frozen=True)
class IbkCleanupBlock:
    """First unresolved capture only; not a process-ownership/cleanup receipt."""
    run_id: str
    since: str
    reason: str


@dataclass(frozen=True)
class IbkNotice:
    kind: str
    run_id: str
    classification: str
    reason: str | None
    cleanup_block: IbkCleanupBlock | None = None


class IbkParentRunner:
    def __init__(self, *, event_sink, capture=capture_ibk_subprocess, clock=_now,
                 monotonic=time.monotonic, blocked_reminder_seconds=300):
        if not all(callable(f) for f in (event_sink, capture, clock, monotonic)):
            raise ValueError("INVALID_IBK_PARENT_DEPENDENCY")
        if (type(blocked_reminder_seconds) not in (int, float)
                or not math.isfinite(blocked_reminder_seconds) or blocked_reminder_seconds <= 0):
            raise ValueError("INVALID_BLOCKED_REMINDER_INTERVAL")
        self.event_sink, self.capture, self.clock = event_sink, capture, clock
        self.monotonic = monotonic
        self.blocked_reminder_seconds = blocked_reminder_seconds
        self._lock = asyncio.Lock()
        self._cleanup_block = None
        self.started_at = clock().isoformat()
        self.counts = Counter()
        self.cancelled_count = 0
        self.last_completed_at = None
        self.last_current_official_at = None
        self.last_classification = None
        self.notice_enqueued = 0
        self.notice_enqueue_failed = 0
        self.notice_suppressed = 0
        self._notified_problem = None
        self._last_notice_monotonic = None

    @property
    def cleanup_blocked(self):
        # No reset API: neither elapsed time nor a new object proves cleanup.
        return self._cleanup_block is not None

    def _block_cleanup(self, context, reason):
        if self._cleanup_block is None:
            self._cleanup_block = IbkCleanupBlock(context.run_id, self.clock().isoformat(), reason)

    def snapshot(self):
        return {
            "started_at": self.started_at, "counts": dict(self.counts),
            "cancelled_count": self.cancelled_count,
            "last_completed_at": self.last_completed_at,
            "last_current_official_at": self.last_current_official_at,
            "last_classification": self.last_classification,
            "cleanup_blocked": self.cleanup_blocked,
            "cleanup_block": ({"run_id": self._cleanup_block.run_id,
                               "since": self._cleanup_block.since,
                               "reason": self._cleanup_block.reason}
                              if self._cleanup_block else None),
            "blocked_reminder_seconds": self.blocked_reminder_seconds,
            "notice_enqueued": self.notice_enqueued,
            "notice_enqueue_failed": self.notice_enqueue_failed,
            "notice_suppressed": self.notice_suppressed,
        }

    def _record(self, context, decision, finished):
        result = decision.result
        classification = result.status.value if result else decision.failure.value
        reason = result.reason.value if result else decision.protocol_error
        self.counts[classification] += 1
        self.last_classification = classification
        self.last_completed_at = finished.isoformat()
        if result and result.status is IbkStatus.OBSERVED:
            old = self.last_current_official_at
            if old is None or datetime.fromisoformat(old) < datetime.fromisoformat(result.observed_at):
                self.last_current_official_at = result.observed_at

        self._notify(context, classification, reason, needs_attention=decision.needs_attention)

    def _notify(self, context, classification, reason, *, needs_attention):
        # Subsequent suppressed runs must not replace the original capture cause
        # or create a second transition solely because they say CLEANUP_UNCONFIRMED.
        problem = ((classification, self._cleanup_block.reason if self._cleanup_block else reason)
                   if needs_attention else None)
        now = self.monotonic()
        kind = "attention" if problem else "recovered"
        if problem == self._notified_problem:
            if problem is None:
                return
            if (not self.cleanup_blocked or self._last_notice_monotonic is None
                    or now - self._last_notice_monotonic < self.blocked_reminder_seconds):
                self.notice_suppressed += 1
                return
            # Checked only on scheduled execute(), not an independent timer.
            # This never retries capture or certifies Telegram delivery.
            kind = "reminder"
        notice = IbkNotice(kind, context.run_id, classification, reason, self._cleanup_block)
        try:
            queued = self.event_sink(notice) is True
        except Exception:
            queued = False  # no raw callback exception/credentials in statistics
        if queued:
            self.notice_enqueued += 1
            self._notified_problem = problem
            self._last_notice_monotonic = now
        else:
            self.notice_enqueue_failed += 1
            # Retry enqueue on the next regular execution, not another Chrome run.

    async def execute(self, *, is_retry=False, timeout=45, cleanup_timeout=2):
        if type(is_retry) is not bool:
            raise ValueError("INVALID_RETRY_FLAG")
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout < MIN_SUPPORTED_TIMEOUT_SECONDS
                or timeout - CHILD_RESERVE_SECONDS > MAX_WORK_BUDGET_SECONDS):
            # 자식을 띄우지 않는다 — 줄 수 있는 예산이 없거나 계약 상한을 넘는다.
            # 상한을 넘겨 보내면 자식이 그 기한을 거부해 결과 없이 끝난다.
            raise ValueError("UNSUPPORTED_EXECUTION_TIMEOUT")
        async with self._lock:
            # ⛔ 자식이 자기 예산을 새로 시작하면 안 된다. spawn·app import·설정 캐시
            #    갱신도 이 timeout 을 쓰므로, **부모 시각 기준의 절대 기한**을 넘긴다.
            #    그래야 timeout 을 바꿀 때 자식 예산도 함께 줄어든다.
            # 작업 기한은 **monotonic 기준**이다 — 부모의 asyncio timeout 과 같은 축이어야
            # 시스템 시각이 밀려도 잔여 예산이 늘지 않는다. `reference_time` 은 서비스일
            # 판정용 달력 시각으로 그대로 둔다.
            context = IbkRunContext(
                uuid4().hex, self.clock(),
                self.monotonic() + (timeout - CHILD_RESERVE_SECONDS))
            if self.cleanup_blocked:
                decision = _protocol_failure("CLEANUP_UNCONFIRMED")
            else:
                argv = (sys.executable, "-u", str(BOOTSTRAP), *context.arguments())
                try:
                    captured = await self.capture(argv, timeout=timeout, cleanup_timeout=cleanup_timeout)
                except asyncio.CancelledError:
                    # capture rethrows cancellation without a cleanup receipt. Do not
                    # infer reaping and permit another run on the same controller.
                    self._block_cleanup(context, "CANCELLED_WITHOUT_RECEIPT")
                    self.cancelled_count += 1
                    self._notify(context, IbkExecutionFailure.RESULT_PROTOCOL_ERROR.value,
                                 "CANCELLED_WITHOUT_RECEIPT", needs_attention=True)
                    raise
                except Exception:
                    # Unexpected adapter failure leaves ownership unknown; do not retry.
                    self._block_cleanup(context, "CAPTURE_ERROR")
                    decision = _protocol_failure("CAPTURE_ERROR")
                else:
                    if not isinstance(captured, IbkProcessCapture):
                        self._block_cleanup(context, "INVALID_CAPTURE")
                        decision = _protocol_failure("INVALID_CAPTURE")
                    elif captured.cleanup_incomplete or captured.error == "KILL_ERROR":
                        self._block_cleanup(context, captured.error or "CLEANUP_INCOMPLETE")
                        decision = _protocol_failure("CLEANUP_UNCONFIRMED")
                    elif captured.returncode is None and captured.error != "SPAWN_ERROR":
                        self._block_cleanup(context, "EXIT_UNCONFIRMED")
                        decision = _protocol_failure("CLEANUP_UNCONFIRMED")
                    elif captured.error:
                        # A complete result cannot override an actual pipe/transport error.
                        decision = decide_ibk_process_result(
                            b"", expected_run_id=context.run_id, returncode=1,
                            timed_out=captured.timed_out, is_retry=is_retry)
                    else:
                        decision = decide_ibk_process_result(
                            captured.stdout, expected_run_id=context.run_id,
                            returncode=captured.returncode, timed_out=captured.timed_out,
                            is_retry=is_retry, stdout_truncated=captured.stdout_truncated,
                        )
            finished = self.clock()
            if decision.result:
                try:
                    context.validate_result(decision.result, received_at=finished)
                except IbkProtocolError as exc:
                    decision = _protocol_failure(str(exc))
            self._record(context, decision, finished)
            return decision
