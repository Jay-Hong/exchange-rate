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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Sequence, Set

from app import ws_connection_metrics
from app.config import TopicAuthStage

if TYPE_CHECKING:
    from app.topic_policy import AuthorizationPlan

__all__ = ["TopicAuthStage", "TopicAuthRollout", "ConnectionObservation"]


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


@dataclass
class ConnectionObservation:
    """한 연결의 채널별 first-seen 상태. **두 채널을 한 entry 에 담는다.**

    ⛔ 채널마다 top-level dict 를 만들지 않는 이유는 `disconnect` 소유권이 갈리기 때문이다.
       여기 담아 두면 `pop` 한 번으로 양쪽이 사라진다.
    """

    anonymous_seen_topics: Set[str] = field(default_factory=set)
    token_bearing_observed: bool = False
    token_bearing_seen_topics: Set[str] = field(default_factory=set)
    # S6: 이 **관측 에포크** 안에서 관측한 token-bearing subscribe 요청 수.
    # ⚠️ disconnect 가 이 entry 를 pop 하면 0 부터 다시 센다 — 그래서 max 는 rollout 쪽에 둔다.
    token_bearing_attempts: int = 0


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
        policy_topics: Iterable[str],
        final_stage_rc_candidate_topics: Iterable[str],
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

        # ⛔ 두 집합은 **주입**한다 — 요청마다 availability 를 다시 계산하거나 이 모듈이
        #    `topic_initial_snapshot` 을 import 하면 **계측용 두 번째 정책**이 생긴다. 게다가
        #    그 계산을 hot path 에 두면 거기서 난 예외가 flag-off wire 동작을 바꿀 수 있다.
        # ⚠️ policy 집합은 기동 검증된 리터럴 표에서, RC 후보 집합은 import 시점 availability
        #    상수(`KRX_CLIENT_DISTRIBUTION_EFFECTIVE` · `FX_TOPIC_ENABLED`)에서 파생된다.
        #    production process 수명 동안 불변이라 1회 계산이 맞다.
        for label, value in (("policy_topics", policy_topics),
                             ("final_stage_rc_candidate_topics", final_stage_rc_candidate_topics)):
            if isinstance(value, (str, bytes, bytearray, Mapping)):
                raise TypeError(f"{label} 는 topic 문자열 collection 이어야 한다 "
                                f"(got {type(value).__name__})")
        policy_list = list(policy_topics)
        if not policy_list:
            raise ValueError("policy_topics 가 비어 있다 — 주입 누락은 계측을 조용히 없앤다")
        for topic in policy_list:
            if not isinstance(topic, str) or not topic or topic != topic.strip():
                raise ValueError(f"policy_topics 원소가 잘못됐다: {topic!r}")
        if len(set(policy_list)) != len(policy_list):
            raise ValueError(f"policy_topics 에 중복이 있다: {policy_list!r}")

        candidate_list = list(final_stage_rc_candidate_topics)
        for topic in candidate_list:
            if not isinstance(topic, str) or not topic or topic != topic.strip():
                raise ValueError(f"final_stage_rc_candidate_topics 원소가 잘못됐다: {topic!r}")
        if len(set(candidate_list)) != len(candidate_list):
            raise ValueError(
                "final_stage_rc_candidate_topics 에 중복이 있다: "
                f"{candidate_list!r}"
            )

        policy_set = frozenset(policy_list)
        candidate_topics = frozenset(candidate_list)
        if not candidate_topics <= policy_set:
            raise ValueError(
                "final_stage_rc_candidate_topics 가 정책표 밖 topic 을 담았다: "
                f"{sorted(candidate_topics - policy_set)}")
        scoped_topics = frozenset(fx_list) | {usdt_topic}
        if not scoped_topics <= policy_set:
            raise ValueError(
                "policy_topics 가 rollout canonical topic 을 빠뜨렸다: "
                f"{sorted(scoped_topics - policy_set)}"
            )
        self._policy_topics = tuple(sorted(policy_list))
        self._final_stage_rc_candidate_topics = candidate_topics

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
        # 연결별 관측 상태. disconnect 에서 **한 번에** 지운다(현재 연결 수에 비례).
        # ⛔ top-level 저장소는 **이 하나**다. 두 번째 dict 를 만들면 `disconnect` 가 두 곳을
        #    지워야 하고 정리 상태가 갈린다 — 원래 이 주석이 경고한 것이 그것이다.
        # ⚠️ 다만 채널은 **나눈다**. 익명/token-bearing 은 **요청 단위로는** 서로소지만
        #    (`identified` 는 메시지마다 재계산된다) **연결 단위로는 서로소가 아니다** — 같은
        #    소켓이 익명 subscribe 뒤 token-bearing subscribe 를 보낼 수 있다. 그래서 한쪽에서
        #    다른 쪽을 파생할 수 없고, 채널별 first-seen 을 각각 들되 **소유자는 이 map 하나**다.
        self._seen_by_connection: Dict[object, ConnectionObservation] = {}

        # token-bearing 축 — **기존 익명 필드의 의미를 넓히지 않고** 별도로 센다.
        self._tb_attempts = 0
        self._tb_policy_attempts = 0
        self._tb_rc_candidate_attempts = 0
        self._tb_policy_first_seen_connections = 0
        # ── S6: 도착(arrival) ring — **요청 1건당 1회**. 폭주 판정은 시간 국소성이라
        #    누적 총계로는 30분에 퍼진 600건과 10초에 몰린 600건을 구분할 수 없다.
        # ⛔ topic 루프 **밖**에서 record 한다 — 안에 두면 한 요청의 topic 수만큼 부풀어
        #    "요청 1건당 1회" 규율(두 관측 함수 도크스트링의 dedupe 계약)과 어긋난다.
        self._anonymous_arrival = ws_connection_metrics.HandshakeBuckets()
        self._tb_arrival = ws_connection_metrics.HandshakeBuckets()
        # ⚠️ **관측 에포크당** high-water 다("연결당" 이 아니다) — broadcast 전송 실패가
        #    ConnectionManager.disconnect → 이 클래스의 disconnect 로 연결 관측을 pop 하는데
        #    수신 루프는 별 코루틴이라 같은 소켓이 계속 subscribe 할 수 있다. 즉 **하한**이다.
        # ⛔ process-wide 라 disconnect 로 줄지 않는다(연결별 entry 와 저장 위치가 다르다).
        self._tb_attempts_on_one_epoch_max = 0
        self._tb_per_topic_attempts: Dict[str, int] = {t: 0 for t in self._policy_topics}
        self._tb_per_topic_first_seen: Dict[str, int] = {t: 0 for t in self._policy_topics}

    # ── 관측 ────────────────────────────────────────────────────────────────

    def observe_anonymous_subscribe(self, websocket, topics: Sequence[str]) -> None:
        """유효한 **익명 subscribe** 1건을 센다.

        ⛔ 호출부가 best-effort 로 감싼다(계측 실패가 등록·거부를 바꾸면 안 된다).
           정책(`filter_anonymous_topics`)은 그렇게 감싸면 **fail-open** 이라 별개다.
        ⚠️ 한 요청 안의 **중복 topic 은 한 번만** 센다.
        """
        self._anonymous_attempts += 1
        self._anonymous_arrival.record()

        requested = {t for t in topics if t in self._per_topic_attempts}
        if not requested:
            return
        self._scoped_attempts += 1

        seen = self._seen_by_connection.setdefault(
            websocket, ConnectionObservation()).anonymous_seen_topics
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

    def observe_token_bearing_subscribe(self, websocket, topics: Sequence[str]) -> None:
        """형식 검증을 통과한 **token-bearing** subscribe 1건을 센다.

        ⛔ **이름이 계약이다.** `identified` 는 인증 성공이 아니라 *메시지 형태*
           (`request_id` 또는 `id_token` 존재)다. 이 지점은 Firebase 검증 **전**이므로 이 값은
           "인증 사용자 수요" 가 아니라 **미검증 token-bearing 후보**다. 공격·오타 토큰도 센다.
        ⛔ 여기서 Firebase·RevenueCat 을 부르지 않는다 — 외부 요청이 outbound 호출을 유발하면
           **amplification** 이 된다.
        ⚠️ 이 값이 보장하는 것: **관측된 이 요청 스트림**에 대해, 최종 stage 였다면 요청당 RC
           호출이 최대 1회라는 것. **미래 운영 부하의 상한은 아니다** — 활성화 후 latency·outcome
           이 재시도 패턴을 바꾸고, 클라 배포·arming 이 모수를 늘리며, 재연결 폭주는 지금 창에
           없을 수 있다.
        ⚠️ 한 요청 안의 **중복 topic 은 한 번만** 센다(익명 축과 같은 규율).
        """
        self._tb_attempts += 1
        self._tb_arrival.record()
        observation = self._seen_by_connection.setdefault(
            websocket, ConnectionObservation())
        observation.token_bearing_observed = True
        # await 없는 동기 구간에서 epoch-local current 와 process-wide high-water 를 연속 갱신한다.
        observation.token_bearing_attempts += 1
        if observation.token_bearing_attempts > self._tb_attempts_on_one_epoch_max:
            self._tb_attempts_on_one_epoch_max = observation.token_bearing_attempts

        requested = {t for t in topics if t in self._tb_per_topic_attempts}
        if not requested:
            return
        self._tb_policy_attempts += 1
        # ⚠️ **현재 topic availability + 최종 stage**에서 RC까지 갈 수 있는 교집합은 따로
        #    센다. 글로벌 dispatcher flag와 token 검증은 아직 통과하지 않았으므로 실제
        #    authorizable/RC 호출 수가 아니다. 지금 KRX는 distribution off라 이 집합에서 빠진다.
        if requested & self._final_stage_rc_candidate_topics:
            self._tb_rc_candidate_attempts += 1

        seen = observation.token_bearing_seen_topics
        first_policy = not seen                      # ⛔ 갱신 **전**에 판정한다
        for topic in requested:
            self._tb_per_topic_attempts[topic] += 1
            if topic not in seen:
                seen.add(topic)
                self._tb_per_topic_first_seen[topic] += 1
        if first_policy:
            self._tb_policy_first_seen_connections += 1

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
        # ⛔ 정책을 여기서 **다시 구현하지 않는다** — 익명 축의 정본은 `topic_policy.plan_anonymous`
        #    하나다. 두 곳에 두면 stage 를 추가할 때 한쪽만 고쳐 조용히 갈린다.
        #    (미지 stage 의 fail-closed `raise` 도 그쪽이 소유한다 — 삼키면 fail-open 이다.)
        # ⚠️ 주입받은 `_fx_topics` 를 넘긴다. 정책 모듈이 FX 이름을 따로 들면 같은 사실의 두 번째
        #    진실이 되고, 생성자에서 이미 검증한 집합과 갈릴 수 있다.
        from app.topic_policy import plan_anonymous

        return plan_anonymous(free_topics, stage=self._stage, fx_topics=self._fx_topics)

    def plan_authenticated_topics(
        self, topics: Sequence[str], *, uid: str
    ) -> "AuthorizationPlan":
        """같은 runtime stage로 식별 요청을 partition한다.

        ⛔ dispatcher가 config를 다시 읽으면 E2E에서 config만 patch한 채 singleton rollout은 옛
           stage를 유지하는 두 번째 진실이 생긴다. 익명·식별 축 모두 이 인스턴스의 stage를 쓴다.
        """
        from app.topic_policy import plan_authenticated

        return plan_authenticated(topics, stage=self._stage, uid=uid)

    # ── 노출 ────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """admin 전용 read-only 관측치. **고정 schema**.

        ⛔ raw topic 문자열·token·UID·IP 를 담지 않는다 — key 는 기동 시 고정한 canonical
           topic 이름뿐이다.
        ⚠️ `topic_dispatcher_enabled` 같은 런타임 맥락은 여기서 읽지 않는다 — endpoint 가 합친다.
        """
        # 두 ring 을 서로 다른 monotonic 시각으로 읽으면 10초 경계에서 익명/token-bearing
        # current_bucket 이 서로 다른 창을 가리킨다. 한 snapshot 은 한 시각을 공유한다.
        arrival_now = time.monotonic()
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
            # ⛔ 의미 보존 — map 에 token-bearing 전용 entry 가 들어오면서 `len()` 은 조용히
            #    다른 것을 세게 됐다. **익명 상태가 있는 entry 만** 센다(구 동작과 동일).
            "active_auth_scoped_connections_tracked": sum(
                1 for o in self._seen_by_connection.values() if o.anonymous_seen_topics),
            "unverified_token_bearing_subscribe_attempts_total": self._tb_attempts,
            "unverified_token_bearing_policy_attempts_total": self._tb_policy_attempts,
            "unverified_token_bearing_final_stage_rc_candidate_attempts_total": (
                self._tb_rc_candidate_attempts),
            "unverified_token_bearing_final_stage_rc_candidate_topics": sorted(
                self._final_stage_rc_candidate_topics),
            "unverified_token_bearing_policy_first_seen_connections_total": (
                self._tb_policy_first_seen_connections),
            "unverified_token_bearing_per_topic_attempts": dict(self._tb_per_topic_attempts),
            "unverified_token_bearing_per_topic_first_seen_connections": dict(
                self._tb_per_topic_first_seen),
            # ── S6 도착 축 (요청 1건당 1회 기록) ────────────────────────────
            "anonymous_subscribe_arrival": self._anonymous_arrival.snapshot(now=arrival_now),
            "unverified_token_bearing_subscribe_arrival": self._tb_arrival.snapshot(now=arrival_now),
            # ⚠️ **관측 에포크당** high-water(하한)이지 "연결당" 이 아니다 — caveat 참조.
            "unverified_token_bearing_attempts_on_one_observation_epoch_max": (
                self._tb_attempts_on_one_epoch_max),
            "unverified_token_bearing_active_connections_tracked": sum(
                1 for o in self._seen_by_connection.values() if o.token_bearing_observed),
            "caveat": (
                "process 수명 동안 관측한 WebSocket 연결·요청 이벤트 수다 — 사용자 수가 "
                "아니다. 재시도하는 클라 하나가 값을 부풀리고, 재기동 시 0으로 돌아간다. "
                "unverified_token_bearing_* 는 Firebase 검증 **전** 집계라 유효 token 수가 "
                "아니다. final_stage_rc_candidate 는 현재 topic availability에서 dispatcher와 "
                "최종 stage가 활성화되고 token 검증까지 성공한다는 가정의 후보일 뿐이다. "
                "관측된 이 요청 스트림에 한해 요청당 RC 호출 ≤1을 뜻하며 미래 운영 부하의 "
                "상한이 아니다. "
                "*_subscribe_arrival 은 요청 1건당 1회 기록하는 시간 버킷이다 — "
                "buckets_present/current_bucket/max_in_bucket 은 최근 창 gauge 라 차분하지 말 것이고, "
                "peak_in_bucket_since_start 만 eviction 과 무관한 수명 전체 피크다. "
                "unverified_token_bearing_attempts_on_one_observation_epoch_max 는 "
                "**관측 에포크당** 최대치의 **하한**이지 연결당 최대치가 아니다 — broadcast 전송 "
                "실패가 연결 관측 상태를 지우는데 수신 루프는 계속 살아 있어 같은 소켓의 관측이 "
                "잘릴 수 있다. 값이 크면 한 소켓 안 반복 시도가 증명되지만, 작다고 반복이 "
                "없었다고 추론할 수 없다."
            ),
        }
