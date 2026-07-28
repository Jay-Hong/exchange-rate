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
지키므로 유출은 아니다). 경합하면 그 연결만 건너뛰고 다음 주기로 미룬다 — 지연 상한이
sweep 주기 1회로 유지된다.

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
    scanned = notified = removed = skipped_busy = terminated = close_failed = 0

    doomed: list = []

    for ws in registry.connections_snapshot():
        scanned += 1
        terminate = False
        # ⛔ busy 포착을 **획득에만** 묶는다. `async with` 전체를 감싸면 본문 어디서
        #    `ConnectionLockBusy`가 나도 "정상 경합"으로 집계돼, 통지도 제거도 안 한 사이클이
        #    §C3의 "지연 상한 = sweep 주기 1회" 근거를 조용히 무너뜨린다.
        held = contextlib.AsyncExitStack()
        try:
            await held.enter_async_context(
                registry.hold_connection_lock(ws, timeout=lock_timeout)
            )
        except ConnectionLockBusy:
            # 그 연결만 건너뛴다 — 무한 대기하면 무관한 연결의 통지가 밀린다.
            skipped_busy += 1
            continue
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
                        notified += 1
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
                    doomed.append(ws)
        finally:
            await held.aclose()

    async def _close(target) -> bool:
        try:
            await asyncio.wait_for(close_connection(target), timeout=close_timeout)
            return True
        except asyncio.CancelledError:
            # ⚠️ 이 절은 **방어적 문서**다 — `CancelledError`는 `BaseException`이라 아래
            #    `except Exception`이 애초에 잡지 않는다. 의도를 남기려고 명시한다.
            raise
        except Exception:  # noqa: BLE001 — 이미 죽은 소켓이면 close도 실패한다.
            return False

    # ⛔ close는 **연결 간 순서가 없는 순수 I/O**라 직렬로 두면 K개가 합산돼 사이클이 D-const의
    #    sweep 주기를 넘긴다(상한 2s·정지 소켓 10개면 20s). 정리는 이미 끝났으므로 동시에 한다.
    if doomed:
        results = await asyncio.gather(*(_close(ws) for ws in doomed))
        close_failed = sum(1 for ok in results if not ok)
        terminated = len(doomed)

    return SweepOutcome(
        scanned=scanned,
        notified=notified,
        removed=removed,
        skipped_busy=skipped_busy,
        terminated=terminated,
        close_failed=close_failed,
    )
