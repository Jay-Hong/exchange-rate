"""만료 lease sweep — §C3 claim-then-notify.

`TopicLeaseRegistry`를 건드리지 않는 **별도 모듈**이다. registry를 dispatcher와 분리했던 것과
같은 이유 — 문제가 났을 때 원인이 sweep인지 registry인지 갈려야 한다.

## 고정 순서: `claim(lock 안) → 통지 → 제거`

⛔ claim 표식의 역할은 **통지 중 인가 제외** 하나다. 계획은 "발신 후 제거"의 위험으로
send hang 시 중복 통지를 들지만, **이 설계에서는 그 경로가 없다** — 통지는 lock 안이고
hang·실패는 소켓 정리로 끝난다. 그래서 "이미 claim됐으면 건너뛴다"를 넣으면 안 된다:
claim이 남는 유일한 경로인 **취소**에서 그 lease가 통지도 제거도 못 받는 좀비가 된다(실측).
⛔ claim된 항목은 **제거 전이라도** 인가에서 즉시 빠져야 한다. 아니면 통지를 보내는 동안
live publish가 그 lease로 다시 통과한다 — 통지와 제거 사이는 `send_json` 하나만큼 벌어져 있고
그 사이 tick이 최소 한 번 지나간다. 그 제외는 registry의 claim 표식이 진다.

## lock 획득은 **blocking이 아니다**

ack이 최대 `ACK_TIMEOUT_SECONDS` 동안 연결 lock을 쥐므로(§B4), 단일 sweeper가 순회하며
무한 대기하면 역압 연결 K개에 대해 한 사이클이 최대 5s×K 늘어난다. 그러면 **무관한 다른
연결**의 만료 통지가 D-const의 "만료→통지 10초" 관측 계약을 넘긴다(보안 상한은 §B1이
지키므로 유출은 아니다). 경합하면 그 연결만 건너뛰고 다음 주기로 미룬다.

⚠️ 그런데 **bounded acquire만으로는 부족하다** — 그건 "lock을 기다리는 시간"만 묶을 뿐,
연결별 작업을 직렬로 돌리면 통지 자체가 K에 비례해 누적된다(실측: 4연결의 통지 시작이
0.000/0.051/0.102/0.154초). 그래서 연결별 작업을 **동시에** 돌린다. 사이클 길이는 K가 아니라
**한 연결의 최악값**(lock + notify + close)으로 묶인다.
⚠️ 정확히 적자면 "만료→통지"의 실제 상한은 sweep 주기 + 그 최악값이다 — 계획 D-const의
"주기 = 최대 지연"은 사이클이 주기에 비해 짧다는 전제의 단순화다.

⚠️ 실측 전제(py3.13): `asyncio.wait_for(lock.acquire(), timeout=...)`가 timeout돼도 lock을
**남기지 않는다**(보유자 해제 후 `locked()`가 False, 재획득 성공). 이 성질이 깨지는 런타임에서는
그 연결이 영구 정지하므로 회귀 테스트로 잠가 두었다.

## `send_reauth` 계약

`await send_reauth(ws, claimed)`는 **연결 lock을 쥔 채** 실행되며 §B4의 `send_ack`와 같은
규칙을 받는다: 성공 시 `True` 반환(제어 흐름은 배달의 증거가 아니다) · 자체 timeout 금지 ·
registry 재진입 금지. 실패·timeout·무확인은 전부 **소켓 전체 정리**다(§C3 — 부분 상태 잔존 금지).

## 이 모듈이 하지 않는 것

- **스케줄링**. 10초 주기 등록은 배선의 일이다. 여기는 `sweep_once(now_mono=...)` 한 번뿐이라
  시계를 읽지 않는다(§D1/§D2가 요구하는 "요청당 단일 시각"과 같은 이유 — 주입된 시각이어야
  단위 테스트로 잠글 수 있다).
- **wire schema 렌더링**(§D3 `reauth_required`). `(topic, lease_id)` 쌍만 넘기고 봉투는
  호출자가 만든다 — registry가 ack 봉투를 만들지 않는 것과 같은 경계다.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from app.topic_lease_registry import ConnectionLockBusy

# 경합 시 대기 상한. **짧아야** 한다 — 이 값이 곧 한 연결이 sweep 사이클을 붙드는 상한이고,
# D-const의 "만료→통지 10초"는 연결 수에 비례하면 안 된다.
LOCK_ACQUIRE_TIMEOUT_SECONDS = 0.05

# 통지 송신 상한. §B4의 ack 예산과 같은 이유 — 멈춘 클라가 연결 lock을 무한 점유하면 안 된다.
NOTIFY_TIMEOUT_SECONDS = 5.0

# close 송신 상한. ⛔ 없으면 모듈이 스스로 금지한 무제한 대기를 새 I/O로 다시 들인다 —
# uvicorn은 websockets `close_timeout` 기본 10s를 쓰고 `close()`는 최악 4×close_timeout이
# 걸릴 수 있다(실측: 응답 없는 피어 하나로 사이클이 25초 안에 끝나지 않았고 `close_failed`도
# 0이라 telemetry에 보이지도 않았다).
CLOSE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class SweepOutcome:
    """한 사이클의 결과. 계측·테스트용이며 상태를 담지 않는다.

    `skipped_busy`는 **정상 신호**다(경합은 다음 주기가 처리한다). 지속적으로 크면 그때가
    ack 예산이나 발행 경로를 볼 시점이다.
    """

    scanned: int = 0
    notified: int = 0
    removed: int = 0
    skipped_busy: int = 0
    terminated: int = 0
    close_failed: int = 0


async def sweep_once(
    registry,
    *,
    now_mono: float,
    send_reauth,
    close_connection,
    lock_timeout: float = LOCK_ACQUIRE_TIMEOUT_SECONDS,
    notify_timeout: float = NOTIFY_TIMEOUT_SECONDS,
    close_timeout: float = CLOSE_TIMEOUT_SECONDS,
) -> SweepOutcome:
    """만료 lease를 한 바퀴 claim·통지·제거한다.

    ⚠️ `now_mono`는 호출자가 사이클 경계에서 **1회** 읽은 값이다. 이 모듈은 시계를 읽지 않는다.
    ⚠️ `close_connection`은 **필수**다(기본값 없음). registry 정리만으로는 §C3의 "소켓 전체
    정리"가 아니다 — §B2a도 전송 실패를 연결 사망으로 보고 **소켓을 닫으라**고 한다. 선택으로
    두면 배선이 한 번 빠뜨렸을 때 죽은 소켓이 조용히 살아남는다(`send_ack`와 같은 논리).
    ⚠️ `close_connection`은 **idempotent**해야 한다. §B2a가 dispatcher의 전송 실패에도 close를
    요구하므로 이미 닫힌 소켓을 sweeper가 다시 닫는 경합은 정상 운영에서 흔하고(starlette
    WebSocket의 두 번째 `close()`는 `RuntimeError`다), 그때마다 `close_failed`가 오른다 —
    이 카운터를 단독 경보 조건으로 쓰지 말 것.
    """
    async def _visit(ws) -> SweepOutcome:
        """한 연결의 `claim → 통지 → 정리 → close`. **연결별로 독립**이라 서로 기다리지 않는다."""
        held = contextlib.AsyncExitStack()
        try:
            await held.enter_async_context(
                registry.hold_connection_lock(ws, timeout=lock_timeout)
            )
        except ConnectionLockBusy:
            # 그 연결만 건너뛴다 — 다음 주기가 처리한다.
            return SweepOutcome(scanned=1, skipped_busy=1)

        terminate = False
        notified = removed = 0
        try:
            claimed = registry.claim_expired_locked(ws, now_mono=now_mono)
            if claimed:
                try:
                    confirmed = await asyncio.wait_for(
                        send_reauth(ws, claimed), timeout=notify_timeout
                    )
                except asyncio.CancelledError:
                    # ⛔ 구조적 취소는 그대로 전파한다 — 감싸면 asyncio의 취소 전파가 깨진다.
                    raise
                except Exception:  # noqa: BLE001 — timeout 포함. 전송 실패는 연결 사망으로 본다.
                    terminate = True
                else:
                    if confirmed is not True:
                        # §B4와 같은 계약: 제어 흐름은 배달의 증거가 아니다(취소를 삼키고 정상
                        # 반환하면 `wait_for`도 정상 반환한다).
                        terminate = True
                    else:
                        notified = 1
                        for entry in claimed:
                            if registry.remove_locked(ws, entry.topic, entry.lease_id):
                                removed += 1
                if terminate:
                    # ⛔ 정리는 **이미 쥔 lock 아래에서** 한다. `remove_websocket`으로 재획득하면
                    #    (1) 상한이 없어 §C3가 없애려던 head-of-line이 되살아나고(실측: 무관한
                    #    연결의 통지 5.00s 지연, 그런데 `skipped_busy=0`이라 안 보인다)
                    #    (2) release→재획득 창에 경쟁 subscribe가 끼어 **죽은 연결에 900초
                    #    lease**가 발급된다(실측: `Applied` ack이 실제로 나갔다).
                    registry.teardown_locked(ws)
        finally:
            await held.aclose()

        if not terminate:
            return SweepOutcome(scanned=1, notified=notified, removed=removed)

        # close는 **lock 밖**이다 — stalled transport가 그 연결의 모든 전이를 막으면 안 된다.
        try:
            await asyncio.wait_for(close_connection(ws), timeout=close_timeout)
        except asyncio.CancelledError:
            # ⚠️ 이 절은 **방어적 문서**다 — `CancelledError`는 `BaseException`이라 아래
            #    `except Exception`이 애초에 잡지 않는다. 의도를 남기려고 명시한다.
            raise
        except Exception:  # noqa: BLE001 — 이미 죽은 소켓이면 close도 실패한다(정상 경합 포함).
            return SweepOutcome(scanned=1, terminated=1, close_failed=1)
        return SweepOutcome(scanned=1, terminated=1)

    # ⛔ **연결별로 동시에** 돈다. 직렬이면 사이클이 연결 수 K에 비례하고(실측: 통지 시작이
    #    0.000/0.051/0.102/0.154초로 누적) 기본값에서는 정지 연결 3개만으로 D-const의
    #    "만료→통지 10초"를 넘긴다. 각 작업은 **서로 다른 연결 lock**을 잡으므로 §B4의
    #    직렬화 계약(연결 단위)은 그대로다.
    # ⚠️ fan-out에 상한을 두지 않는다 — 상한 C를 두면 사이클이 다시 ceil(K/C)에 비례해
    #    보장이 깨진다. 무거운 부분(통지·close)은 **만료 lease를 가진 연결**에만 발생하고,
    #    나머지는 lock 획득 + 동기 claim으로 끝난다.
    # ⛔ `return_exceptions=True`가 **필수**다. 기본 `gather`는 첫 예외를 즉시 전파하면서
    #    나머지를 취소하지도 대기하지도 않는다 — 실측: 사이클이 예외로 끝난 시점에 sibling이
    #    여전히 lock을 쥐고 있었고 그 뒤 lease 제거까지 수행했다. 다음 주기와 겹치면 중복
    #    통지·claim 경쟁이 된다.
    # ⚠️ sibling **취소**가 아니라 **완료 대기**를 고른 이유: 취소는 성공 직전의 통지까지
    #    죽인다. 그리고 모든 `_visit`은 이미 상한(lock·notify·close)을 갖고 있어 대기가 무한할
    #    수 없다 — 그 상한들이 이 선택을 안전하게 만든다.
    results = await asyncio.gather(
        *(_visit(ws) for ws in registry.connections_snapshot()), return_exceptions=True
    )

    outcomes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]
    for failure in failures:
        if not isinstance(failure, Exception):
            # `CancelledError`·`SystemExit`·`KeyboardInterrupt`는 결과값으로 삼키지 않는다 —
            # 감싸면 구조적 취소와 프로세스 종료가 막힌다(registry의 같은 규칙과 동형).
            raise failure
    if failures:
        # 하나만 던지면 나머지 진단이 사라진다. 여럿이면 그대로 묶어 올린다.
        raise failures[0] if len(failures) == 1 else BaseExceptionGroup(
            "sweep 방문 실패", failures
        )

    return SweepOutcome(
        scanned=sum(r.scanned for r in outcomes),
        notified=sum(r.notified for r in outcomes),
        removed=sum(r.removed for r in outcomes),
        skipped_busy=sum(r.skipped_busy for r in outcomes),
        terminated=sum(r.terminated for r in outcomes),
        close_failed=sum(r.close_failed for r in outcomes),
    )
