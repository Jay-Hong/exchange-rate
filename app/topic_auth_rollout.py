"""익명 subscribe 의 **rollout 정책 + 계측** (R-GATE-1 첫 수직 슬라이스).

## 이 모듈이 존재하는 이유

익명(미식별) subscribe 는 **wire 응답이 없다** — `handle_client_message` 의 모든 프레임 송신이
`if identified:` 아래에 있기 때문이다. 그래서 익명 FX 를 거부하기 **전에**, 거부하면 무엇을 잃는지
먼저 세야 한다. 거부부터 하면 사용자 영향을 만든 뒤에 발견하게 된다.

## 경량 유지

⛔ topic 이름 상수(`FX_TOPICS` / `TETHER_TOPIC`)를 여기서 import 하지 않는다 — `topic_dispatcher`
가 publisher 를 끌어오면 순환·무거운 import 가 생긴다. **생성자 주입**으로 받고, 주입 출처는 두
publisher 를 이미 top-level import 하는 `app/main.py` 다.

⛔ 런타임 flag(`TOPIC_DISPATCHER_ENABLED` 등)를 여기서 읽지 않는다. `snapshot()` 은 이 객체가
아는 것만 담고, 그 밖의 맥락은 **admin endpoint 가 합친다**.

⚠️ `config` 에서 `TopicAuthStage` 를 가져오지만 이건 **경량 import 가 아니다** — `app/config.py`
   를 import 하면 `load_dotenv()` · 디렉터리 생성 · 전 환경 파싱이 실행된다. `app → config` 가
   허용 방향이라 위치는 유지하되, "타입만 로드한다"고 적지 않는다.

## 계측의 한계 (endpoint 응답에도 적는다)

이 값은 **process 수명 동안 관측한 WebSocket 연결·요청 이벤트 수**이지 사용자 수가 아니다.
재시도하는 클라 하나가 값을 부풀리고, process-local 이라 재기동 시 0으로 돌아간다.
출시 영향 추정에는 client-version 관측을 함께 봐야 한다.
"""
from __future__ import annotations

import math
import os
import time
from collections.abc import Mapping
from typing import Dict, Iterable, List, Optional, Sequence

from app.config import TopicAuthStage

__all__ = ["TopicAuthStage", "TopicAuthRollout"]


def _validate_topic(value, *, prefix: str, field: str) -> None:
    """배선값 하나를 검증한다 — **공백 없는 `<prefix>` + 비어 있지 않은 suffix**.

    ⛔ 클라 입력의 prefix 판정이 아니라 **신뢰된 배선값의 기동 시 검증**이다.
       `"fx:"`(빈 suffix)나 `" usdt:krw"`(공백) 같은 값이 통과하면 정책이 조용히 어긋난다(실측).
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} 는 비어 있지 않은 str 이어야 한다: {value!r}")
    if value != value.strip() or any(c.isspace() for c in value):
        raise ValueError(f"{field} 에 공백이 있다: {value!r}")
    if not value.startswith(prefix) or len(value) <= len(prefix):
        raise ValueError(f"{field} 는 '{prefix}' + 비어 있지 않은 suffix 여야 한다: {value!r}")


class TopicAuthRollout:
    """process 당 **하나**. `ConnectionManager` 가 소유하고 dispatcher·admin 이 같은 객체를 쓴다.

    ⛔ 기본 인스턴스를 만들지 않는다 — `handle_client_message` 인자에 기본값이 있으면 배선 누락이
       **계측을 조용히 없앤다**.
    """

    def __init__(
        self,
        *,
        stage: TopicAuthStage,
        fx_topics: Iterable[str],
        usdt_topic: str,
        started_at_epoch_seconds: Optional[float] = None,
    ) -> None:
        # ⛔ 배선 오류는 **생성 시점에** 접는다. 요청 경로에서 터지면 그때야 발견된다.
        if not isinstance(stage, TopicAuthStage):
            raise TypeError(f"stage 는 TopicAuthStage 여야 한다 (got {type(stage).__name__})")

        # ⛔ `list("fx:usd-krw")` 는 **문자 집합**이 되고, `list(FX_TOPICS)` 는 **키**(asset 이름)가
        #    된다. 둘 다 통과하면 `reject_anonymous_fx` 가 실제 topic 을 제거하지 못하는 조용한
        #    fail-open 이다(실측). 그래서 먼저 타입을 거부한다.
        if isinstance(fx_topics, (str, bytes, bytearray, Mapping)):
            raise TypeError(
                "fx_topics 는 topic 문자열 collection 이어야 한다 "
                f"(got {type(fx_topics).__name__} — dict 라면 .values() 를 빠뜨렸는가?)"
            )
        fx_list = list(fx_topics)
        if not fx_list:
            raise ValueError("fx_topics 가 비어 있다 — 주입이 누락되면 정책이 조용히 무력해진다")
        # 원소 형식을 먼저 검사한다. 중복 검사를 먼저 하면 list/dict 같은
        # unhashable 원소가 계약 오류 대신 `set()` TypeError 로 터진다.
        for topic in fx_list:
            _validate_topic(topic, prefix="fx:", field="fx_topics 원소")
        if len(set(fx_list)) != len(fx_list):
            # ⛔ frozenset 이 중복을 조용히 삼키면 배선 실수가 숨는다.
            raise ValueError(f"fx_topics 에 중복이 있다: {fx_list!r}")
        _validate_topic(usdt_topic, prefix="usdt:", field="usdt_topic")

        self._stage = stage
        # ⛔ prefix 판정("fx: 로 시작")을 쓰지 않는다 — 클라가 `fx:` 아무 문자열이나 보내 정책·
        #    counter 를 만지게 된다. **주입받은 정확한 집합**만 본다.
        self._fx_topics = frozenset(fx_list)
        self._usdt_topic = usdt_topic
        # counter key 는 **기동 시 고정 생성**한다. 요청 문자열로 key 를 늘리지 않는다.
        self._scoped: tuple = tuple(sorted(self._fx_topics)) + (usdt_topic,)

        if started_at_epoch_seconds is None:
            self._started_at = time.time()
        else:
            # ⛔ bool 은 float 의 subclass 라 그냥 두면 `True` 가 1.0 으로 통과한다.
            #    NaN/inf/음수도 관측 창 계산을 조용히 망가뜨린다.
            if isinstance(started_at_epoch_seconds, bool) or not isinstance(
                started_at_epoch_seconds, (int, float)
            ):
                raise TypeError("started_at_epoch_seconds 는 실수여야 한다")
            if not math.isfinite(started_at_epoch_seconds) or started_at_epoch_seconds < 0:
                raise ValueError("started_at_epoch_seconds 는 유한한 0 이상이어야 한다")
            self._started_at = float(started_at_epoch_seconds)
        self._anonymous_attempts = 0
        self._scoped_attempts = 0
        # ⛔ FX union 을 **따로** 센다. per_topic 을 합치면 한 요청이 FX 3종을 함께 요청할 때
        #    중복 계산되고, `_scoped_*` 는 USDT 를 포함해 FX 거부 영향과 다르다.
        self._fx_attempts = 0
        self._fx_first_seen_connections = 0
        self._per_topic_attempts: Dict[str, int] = {t: 0 for t in self._scoped}
        self._per_topic_first_seen: Dict[str, int] = {t: 0 for t in self._scoped}
        # 연결별로 **이미 센 topic** 집합. disconnect 에서 지운다(현재 연결 수에 비례).
        # ⛔ 연결별 상태는 **이 하나**로 끝낸다. FX 전용 집합을 따로 두면 같은 사실이 두 곳에
        #    저장돼 정리 상태가 갈릴 수 있다 — first-seen 은 여기서 **파생**한다.
        self._seen_by_connection: Dict[object, set] = {}

    # ── 관측 ────────────────────────────────────────────────────────────────

    def observe_anonymous_subscribe(self, websocket, topics: Sequence[str]) -> None:
        """유효한 **익명 subscribe** 1건을 센다.

        ⛔ 호출부가 best-effort 로 감싼다(계측 실패가 등록·거부를 바꾸면 안 된다).
           정책(`filter_anonymous_topics`)은 그렇게 감싸면 **fail-open** 이라 별개다.
        ⚠️ 한 요청 안의 **중복 topic 은 한 번만** 센다.
        """
        self._anonymous_attempts += 1

        requested = {t for t in topics if t in self._per_topic_attempts}
        if not requested:
            return
        self._scoped_attempts += 1

        seen = self._seen_by_connection.setdefault(websocket, set())
        # ⛔ per-topic 갱신 **전에** 판정한다 — 갱신 후면 항상 "이미 봤음" 이 된다.
        has_fx = bool(requested & self._fx_topics)
        first_fx = has_fx and not (seen & self._fx_topics)
        for topic in requested:
            self._per_topic_attempts[topic] += 1
            if topic not in seen:
                seen.add(topic)
                self._per_topic_first_seen[topic] += 1

        # FX union — `reject_anonymous_fx` 의 **직접** 영향은 이 두 값으로만 계산된다.
        # (per_topic 합산은 한 요청이 FX 3종을 함께 요청할 때 중복 계산된다.)
        if has_fx:
            self._fx_attempts += 1
            if first_fx:
                self._fx_first_seen_connections += 1

    def disconnect(self, websocket) -> None:
        """연결별 상태 제거. **멱등**이다.

        ⚠️ 누적 first-seen counter 는 **줄이지 않는다** — process 수명 동안 관측한 연결
           이벤트 수이지 현재 연결 수가 아니다.
        """
        self._seen_by_connection.pop(websocket, None)

    # ── 정책 ────────────────────────────────────────────────────────────────

    def filter_anonymous_topics(self, free_topics: Sequence[str]) -> List[str]:
        """익명 요청에 허용할 topic 만 남긴다.

        ⛔ **입력은 원본 `topics` 가 아니라 이미 걸러진 `free_topics`** 여야 한다
           (지원 집합 ∩ · per-user gated 제외). 원본에 적용하면 KRX·미지 topic 이 되살아난다.
        ⛔ 순서와 중복을 **보존**한다 — 호출부가 그대로 `registry.register` 에 넘긴다.
        ⛔ 예외를 삼키지 않는다. 실패를 무시하고 원본을 돌려주면 FX 가 허용되는 fail-open 이다.
        """
        if self._stage is TopicAuthStage.COMPATIBILITY:
            return list(free_topics)
        if self._stage is TopicAuthStage.REJECT_ANONYMOUS_FX:
            return [t for t in free_topics if t not in self._fx_topics]
        # ⛔ 미지 단계는 **최엄격**으로 접는다(fail-closed). 도달 불가지만 조용히 열지 않는다.
        raise ValueError(f"알 수 없는 rollout 단계: {self._stage!r}")

    # ── 노출 ────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """admin 전용 read-only 관측치. **고정 schema**.

        ⛔ raw topic 문자열·token·UID·IP 를 담지 않는다 — key 는 기동 시 고정한 canonical
           topic 이름뿐이다.
        ⚠️ `topic_dispatcher_enabled` 같은 런타임 맥락은 여기서 읽지 않는다 — endpoint 가 합친다.
        """
        return {
            "scope": "process",
            "pid": os.getpid(),
            "started_at_epoch_seconds": self._started_at,
            "stage": self._stage.value,
            "anonymous_subscribe_attempts_total": self._anonymous_attempts,
            "anonymous_auth_scoped_attempts_total": self._scoped_attempts,
            "anonymous_fx_attempts_total": self._fx_attempts,
            "anonymous_fx_first_seen_connections_total": self._fx_first_seen_connections,
            "per_topic_attempts": dict(self._per_topic_attempts),
            "per_topic_first_seen_connections": dict(self._per_topic_first_seen),
            "active_auth_scoped_connections_tracked": len(self._seen_by_connection),
            "caveat": (
                "process 수명 동안 관측한 WebSocket 연결·요청 이벤트 수다 — 사용자 수가 "
                "아니다. 재시도하는 클라 하나가 값을 부풀리고, 재기동 시 0으로 돌아간다."
            ),
        }
