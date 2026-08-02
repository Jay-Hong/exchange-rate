"""시간 소스 주입 seam — wall + monotonic **두 축** (ADR-039 §8.1 harness 선행 (1)).

TTL·만료 판정이 `datetime.now()` / `time.monotonic()`을 메서드 안에서 직접 읽던 것을 대체한다.
소비자는 둘이고 **축이 다르다**:

- `app/subscription.py` — entitlement 캐시 TTL 판정 5곳이 `clock.wall()`만 읽는다.
- `app/topic_lease.py` — A1 lease horizon. 호출부가 `clock.mono()`를 **요청당 1회** 읽어
  순수 계산기에 `now_mono`로 넘긴다(§8.1 D2 "lock 아래 단일 snapshot").

**값이 아니라 콜러블을 주입한다.** 판정 지점들이 `_check_revenuecat_entitlement`의 HTTP 왕복을
사이에 두고 흩어져 있어, 패스 시작 시각 하나를 공유하면 `cached_at`이 호출 *이전* 시각으로 찍혀
캐시가 그만큼 일찍 만료된다(behavior change). ⚠️ `httpx.AsyncClient(timeout=5.0)`은 **단계별**
(connect/read/write/pool) 5초라 왕복 총시간의 상한이 5초인 것이 **아니다**. 또
`EntitlementCache.get`(miss)·`PendingCache.state`(미등록)는 클럭을 **읽기 전에** 반환하므로,
호출부에서 미리 평가하면 그 laziness가 깨진다.

**`mono`는 기본값이 없다**(§8.1 A2). 기본값을 주면 호출부가 빠뜨려도 실클럭으로 조용히 동작해
테스트에 실시간이 섞인다 — wall 축에서 같은 이유로 필수로 했다. 조립부는 3곳:
`system_clock` / `tests/test_subscription_clock.py`의 `_clock`(wall 전용, mono는 poison) /
`tests/test_topic_lease.py`의 `_clock`(두 축 독립 제어).

## 왜 `app/subscription.py`가 아니라 별 모듈인가

lease horizon(WS 인가)이 RevenueCat 결제 모듈에 결합되지 않게 하기 위해서다. 근거는
"결제와 인가는 다른 관심사"보다 구체적이다 — §8.1 A4가 `invalidate_user_cache`
(`app/subscription.py`)로 하여금 신규 **관측 캐시 결과까지** 무효화하게
규정하므로 `subscription → WS 인가` 엣지가 생길 예정이다. `Clock`이 subscription에 남으면
WS 인가 쪽이 그것을 import하는 **역엣지**로 순환이 된다. (⚠️ **prospective**다 — 오늘은
`app/config.py`가 app을 import하지 않아 어느 배치도 순환이 아니다.)
그래서 이 모듈은 **stdlib only**로 유지한다(`app.*` import 0 — `app/atomic_revision.py`의
"계층 역전/순환 회피" 선례와 같은 이유).

⚠️ **동명 혼동 주의**: P1b atomic 계열의 `CoordinatorHooks.clock`은 시각이 아니라 **KST isoformat
문자열**을 돌려주는 콜러블이며 이 `Clock`과 무관하다. (모듈 파일명을 적지 않는 이유: 그 계열은
dormancy trip-wire가 **문자열 리터럴까지** 위반으로 세므로 심볼 앵커로만 가리킨다.)

⚠️ **patch 대상**: subscription의 기본 클럭을 테스트에서 바꿔야 하면 **소비자 네임스페이스**
`app.subscription.system_clock`을 patch한다. `app.clock.system_clock`을 patch해도
subscription이 이미 바인딩한 이름은 바뀌지 않는다(조용한 no-op).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


@dataclass(frozen=True)
class Clock:
    """주입 가능한 시간 소스. 두 축은 **서로 독립**이며 섞어 쓰면 안 된다.

    - `wall`: aware UTC `datetime`. 사람이 읽는 시각·저장용. NTP step / VM restore로 **역행**한다.
    - `mono`: `time.monotonic()` 축의 초 단위 float. deadline 전용. 기준점은 정의되지 않으므로
      **절대값에 의미가 없고 음수일 수도 있다** — 차이만 쓴다.

    A1의 revoke 상한 증명은 lease deadline과 `authoritative_verified_at`이 **같은 monotonic 축**
    이라는 전제 위에 있다(A2). wall 값이 monotonic 파라미터로 새면 증명이 조용히 깨진다 —
    `app/topic_lease.py`가 입구에서 gross skew를 거부하는 이유다.
    """

    wall: Callable[[], datetime]
    mono: Callable[[], float]

    def __post_init__(self) -> None:
        # Crash Early — 콜러블 대신 **값**을 주입한 실수를 첫 호출까지 미루지 않는다.
        if not callable(self.wall):
            raise ValueError(f"Clock.wall: 콜러블이어야 — got {self.wall!r}")
        if not callable(self.mono):
            raise ValueError(f"Clock.mono: 콜러블이어야 — got {self.mono!r}")


def system_clock() -> Clock:
    """프로덕션 기본 클럭 (aware UTC + monotonic)."""
    return Clock(wall=lambda: datetime.now(timezone.utc), mono=time.monotonic)
