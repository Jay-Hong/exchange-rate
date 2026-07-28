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
from dataclasses import dataclass

# 경합 시 대기 상한. **짧아야** 한다 — 이 값이 곧 한 연결이 sweep 사이클을 붙드는 상한이고,
# D-const의 "만료→통지 10초"는 연결 수에 비례하면 안 된다.
LOCK_ACQUIRE_TIMEOUT_SECONDS = 0.05

# 통지 송신 상한. §B4의 ack 예산과 같은 이유 — 멈춘 클라가 연결 lock을 무한 점유하면 안 된다.
NOTIFY_TIMEOUT_SECONDS = 5.0


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


async def sweep_once(
    registry,
    *,
    now_mono: float,
    send_reauth,
    lock_timeout: float = LOCK_ACQUIRE_TIMEOUT_SECONDS,
    notify_timeout: float = NOTIFY_TIMEOUT_SECONDS,
) -> SweepOutcome:
    """만료 lease를 한 바퀴 claim·통지·제거한다.

    ⚠️ `now_mono`는 호출자가 사이클 경계에서 **1회** 읽은 값이다. 이 모듈은 시계를 읽지 않는다.
    """
    scanned = notified = removed = skipped_busy = terminated = 0

    for ws in registry.connections_snapshot():
        scanned += 1
        lock = registry.connection_lock(ws)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=lock_timeout)
        except asyncio.TimeoutError:
            # 그 연결만 건너뛴다 — 무한 대기하면 무관한 연결의 통지가 밀린다.
            skipped_busy += 1
            continue

        terminate = False
        try:
            claimed = registry.claim_expired_locked(ws, now_mono=now_mono)
            if not claimed:
                continue
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
        finally:
            lock.release()

        if terminate:
            # ⛔ lock을 **놓은 뒤** 부른다 — `remove_websocket`이 같은 lock을 잡으므로
            #    안에서 부르면 데드락이다(`asyncio.Lock`은 재진입 불가).
            await registry.remove_websocket(ws)
            terminated += 1

    return SweepOutcome(
        scanned=scanned,
        notified=notified,
        removed=removed,
        skipped_busy=skipped_busy,
        terminated=terminated,
    )
