"""topic 별 **인가 정책표** + 순수 planner (R-GATE-1 S2c).

## 왜 표인가

`R-HAND-20`(spec/topic-snapshot-handoff.md) 이 요구한다 — *"인가 판정은 명시적 정책표로
구현한다 … (**미지정 topic 은 통과 불가**)"*. 그 표가 담을 내용은 `R-OPEN-1` 이 확정했다:
**비-KRX 최신 topic = Firebase 인증 + premium / KRX = premium + entitlement**.

⛔ **파생 규칙("모든 authorizable topic = premium")을 쓰지 않는 이유**: 규칙은 신규 topic 을
   조용히 `PREMIUM_ONLY` 로 분류한다. entitlement 가 필요한 신규 상품을 supported 집합에만
   추가하고 gated 등록을 잊으면 **premium 구독자 전원이 entitlement 검사 없이 수신**한다 —
   실패가 조용하고 보안 영향이다. 표 + 미매핑 hard fail 은 같은 실수를 **가용성 실패**(시끄럽다)로
   바꾼다.

## ⛔ 표의 key 를 publisher 상수로 comprehension 하지 말 것

아래 `TOPIC_POLICY` 는 topic 이름을 **문자 그대로** 적는다. `{t: ... for t in FX_TOPICS.values()}`
로 쓰면 `assert_policy_covers_universe()` 의 `==` 가 **항진명제**가 되어 아무것도 증명하지 못한다.
표(리터럴)와 universe(publisher 상수 파생)는 **독립적으로 구성**되어야 그 등식이 drift 를 잡는다.

## 두 불변식이 왜 둘 다 필요한가

- `implemented_topic_universe() == TOPIC_POLICY.keys()` — **flag 비의존**. 이것만이 "구현된 topic 중
  표에 없는 것"을 잡는다.
- `supported_snapshot_topics() ⊆ TOPIC_POLICY.keys()` — **런타임**. supported 는 KRX 를
  `KRX_CLIENT_DISTRIBUTION_EFFECTIVE` 조건부로만 포함하므로,
  ⛔ **이 검사만 두면 운영(flag off)에서 KRX 행이 빠진 표도 통과한다**(실측 조건). 반대로 위
  등식을 런타임 supported 에 걸면 flag off 에서 기동이 죽는다.

## entitlement 축의 단일 정본

⚠️ 정직하게 적는다 — 단일 정본은 **구조적 파생이 아니라 트립와이어**로 달성된다.
`visible_snapshot_topics_sync`(REST 가시성)는 `KRX_TOPIC` 을 직접 import 해 하드코딩으로
걸러내고, 그 함수를 gated 집합 순회로 일반화하려면 topic 별 entitlement evaluator 레지스트리가
필요하다 — 소비자 없이 지을 수 없다(ADR-040). 그래서 `assert_single_entitlement_topic()` 이
**두 번째 entitlement topic 자체를 금지**하고, 추가하는 사람이 evaluator 와 가시성 함수를 함께
고치도록 강제한다. WS 판정과 REST 가시성이 **같은 이 가드**를 공유한다.
"""
from __future__ import annotations

import enum
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import FrozenSet, List, Mapping, Sequence, Tuple

from app.config import TopicAuthStage

__all__ = [
    "AuthorizationClass",
    "TOPIC_POLICY",
    "AuthorizationPlan",
    "implemented_topic_universe",
    "entitlement_gated_topics",
    "assert_policy_covers_universe",
    "assert_runtime_supported_subset",
    "assert_single_entitlement_topic",
    "assert_topic_policy_invariants",
    "plan_anonymous",
    "plan_authenticated",
]


class AuthorizationClass(enum.Enum):
    """topic 이 **최종적으로** 요구하는 인가 축. stage 는 이걸 얼마나 강제할지만 정한다.

    ⛔ `IDENTITY_ONLY` 는 여기 없다 — 그건 topic 의 요구가 아니라 **rollout 중간 상태**다.
       표에 넣으면 "이 topic 은 영원히 인증만 필요"로 읽혀 R-OPEN-1 과 어긋난다.
    """

    PREMIUM_ONLY = "premium_only"
    PREMIUM_AND_ENTITLEMENT = "premium_and_entitlement"


# ⛔ **리터럴 열거**(모듈 docstring 참조). publisher 상수로 comprehension 하지 말 것.
#    MappingProxyType은 별칭을 통한 런타임 변이까지 막는다. AST 이름 검사만으로는
#    `alias = TOPIC_POLICY; alias.update(...)` 같은 우회를 완전히 탐지할 수 없다.
TOPIC_POLICY: Mapping[str, AuthorizationClass] = MappingProxyType({
    "fx:usd-krw": AuthorizationClass.PREMIUM_ONLY,
    "fx:jpy-krw": AuthorizationClass.PREMIUM_ONLY,
    "fx:eur-krw": AuthorizationClass.PREMIUM_ONLY,
    "usdt:krw": AuthorizationClass.PREMIUM_ONLY,
    "dxy:spot": AuthorizationClass.PREMIUM_ONLY,
    "krx:usd-krw-futures": AuthorizationClass.PREMIUM_AND_ENTITLEMENT,
})


def implemented_topic_universe() -> frozenset:
    """publisher 가 실제로 구현한 topic 전체 — **flag 비의존**.

    ⛔ `supported_snapshot_topics()` 를 쓰지 않는다. 그건 KRX 를 배포 flag 조건부로 포함하므로
       운영(flag off)에서 KRX 를 빠뜨린다 — 정확히 이 함수가 막아야 할 사각지대다.
    ⚠️ `TOPIC_POLICY` 에서 파생하지 않는다(그러면 등식이 항진명제다). 출처는 publisher 모듈이다.
    """
    from app.fx_topic_publisher import FX_TOPICS
    from app.dxy_topic_publisher import DXY_TOPIC
    from app.krx_topic_publisher import KRX_TOPIC
    from app.tether_topic_publisher import TETHER_TOPIC

    return frozenset(FX_TOPICS.values()) | {TETHER_TOPIC, DXY_TOPIC, KRX_TOPIC}


def entitlement_gated_topics() -> frozenset:
    """per-user entitlement 판정이 필요한 topic — 표에서 **파생**한다(정본은 표).

    ⛔ 잘못된 값을 단순히 "entitlement 아님"으로 취급하지 않는다. 이 함수는 REST shortcut과
       WS gated 집합의 공통 입력이라, malformed 행을 건너뛰면 해당 topic이 조용히 열린다.
    """
    invalid = {
        topic: type(klass).__name__
        for topic, klass in TOPIC_POLICY.items()
        if not isinstance(klass, AuthorizationClass)
    }
    if invalid:
        raise RuntimeError(f"정책표에 잘못된 authorization class 가 있다: {invalid}")
    return frozenset(
        topic
        for topic, klass in TOPIC_POLICY.items()
        if klass is AuthorizationClass.PREMIUM_AND_ENTITLEMENT
    )


def assert_policy_covers_universe() -> None:
    """구현된 topic 과 표가 **정확히** 일치하고 모든 분류값이 유효한지."""
    universe = implemented_topic_universe()
    keys = frozenset(TOPIC_POLICY)
    if universe != keys:
        raise RuntimeError(
            "정책표가 구현된 topic 집합과 다르다 — 표에 없는 구현: "
            f"{sorted(universe - keys)} / 구현 없는 표 행: {sorted(keys - universe)}"
        )
    invalid = {
        topic: type(klass).__name__
        for topic, klass in TOPIC_POLICY.items()
        if not isinstance(klass, AuthorizationClass)
    }
    if invalid:
        # 값 검증이 없으면 typo/잘못된 객체도 planner 의 broad fallback 에서 PREMIUM_ONLY 로
        # 취급될 수 있다. key coverage 가 맞아도 정책 자체는 유효하지 않다.
        raise RuntimeError(f"정책표에 잘못된 authorization class 가 있다: {invalid}")


def assert_runtime_supported_subset() -> None:
    """런타임 supported 가 표의 부분집합인지.

    ⛔ 등호(`==`)로 쓰지 말 것 — supported 는 flag-aware 라 KRX flag off 인 운영에서 즉시 죽는다.
    """
    from app.topic_initial_snapshot import supported_snapshot_topics

    supported = frozenset(supported_snapshot_topics())
    keys = frozenset(TOPIC_POLICY)
    if not supported <= keys:
        raise RuntimeError(f"표에 없는 supported topic: {sorted(supported - keys)}")


def assert_single_entitlement_topic() -> None:
    """entitlement topic 이 **정확히 하나**(KRX)인지 — flag 무관.

    ⛔ 두 번째 entitlement 상품이 생기면 여기서 죽어야 한다. `visible_snapshot_topics_sync` 가
       `KRX_TOPIC` 을 하드코딩으로 걸러내고, WS 분류 loop 도 하나의 verdict 를 gated 전체에
       적용한다 — 둘 다 새 상품을 조용히 열거나 잘못 판정한다. 추가하는 사람이 evaluator 와
       가시성 함수를 **먼저** 고치게 강제한다.
    """
    from app.krx_topic_publisher import KRX_TOPIC

    gated = entitlement_gated_topics()
    if gated != frozenset({KRX_TOPIC}):
        raise RuntimeError(
            f"entitlement topic 이 {{'{KRX_TOPIC}'}} 하나가 아니다: {sorted(gated)} — "
            "evaluator 와 REST 가시성 함수를 먼저 고칠 것"
        )


def assert_topic_policy_invariants() -> None:
    """세 불변식을 묶은 **기동 시** 검증기. `main.py` 가 manager 생성 **전에** 부른다.

    ⛔ 이 함수에 호출자가 없으면 위 assert 들은 **산출물일 뿐 기전이 아니다** — "잊으면 기동이
       죽는다"는 문장이 참이 되는 것은 오직 이 배선 때문이다. 산출물의 존재를 강제력으로
       서술하지 않기 위해 검증기를 하나로 묶고 배선 지점을 여기 명시한다.
    ⚠️ import 시점이 아니라 **호출 시점**에 판정한다 — module import 부수효과로 죽이면
       테스트가 정책표를 patch 할 수 없고 실패 지점도 읽기 어렵다.
    """
    assert_policy_covers_universe()
    assert_runtime_supported_subset()
    assert_single_entitlement_topic()


@dataclass(frozen=True)
class AuthorizationPlan:
    """식별된 요청의 topic partition.

    세 분류는 서로 겹치지 않고 입력의 모든 **요청 occurrence** 를 보존한다. 같은 topic 을 한
    요청에 반복한 경우에는 같은 분류 안에서 중복을 유지한다. 기존 wire 계약이 accepted topic 의
    요청 순서와 중복을 보존하기 때문이다.

    ⛔ `uid` 를 담는다 — ADR-040 재시작 계약 4항("주체가 박힌 관측 한 묶음"). 판정 결과가
       경계를 건널 때 주체가 호출자 기억으로 내려가면 다른 사용자의 결과를 섞을 수 있다.
    """

    uid: str
    identity_only: Tuple[str, ...]
    premium_only: Tuple[str, ...]
    premium_and_entitlement: Tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.uid, str) or not self.uid:
            raise ValueError(f"uid 는 비어 있지 않은 str 이어야 한다: {self.uid!r}")
        groups = (self.identity_only, self.premium_only, self.premium_and_entitlement)
        group_sets = tuple(set(group) for group in groups)
        if any(
            group_sets[left] & group_sets[right]
            for left in range(len(group_sets))
            for right in range(left + 1, len(group_sets))
        ):
            raise ValueError(f"partition 이 서로소가 아니다: {groups!r}")

    def requires_premium(self) -> bool:
        return bool(self.premium_only or self.premium_and_entitlement)

    def requires_entitlement(self) -> bool:
        return bool(self.premium_and_entitlement)


def _class_of(topic: str) -> AuthorizationClass:
    try:
        klass = TOPIC_POLICY[topic]
    except KeyError:
        # ⛔ 미매핑은 조용히 통과시키지 않는다(R-HAND-20 "미지정 topic 은 통과 불가").
        raise ValueError(f"정책표에 없는 topic: {topic!r}") from None
    if not isinstance(klass, AuthorizationClass):
        raise ValueError(
            f"topic 정책값이 AuthorizationClass 가 아니다: {topic!r} → {klass!r}"
        )
    return klass


def plan_anonymous(
    topics: Sequence[str], *, stage: TopicAuthStage, fx_topics: FrozenSet[str]
) -> List[str]:
    """익명(미식별) 요청에 허용할 topic.

    ⛔ **인증 경로와 별개 축이다.** `reject_anonymous_fx` 에서 익명 FX 는 거부되지만 식별 FX 는
       여전히 identity-only 다 — 한쪽에서 다른 쪽을 파생할 수 없다. 그래서 함수가 둘이다.
    ⛔ 호출자가 `identified: bool` 을 넘기는 형태를 쓰지 않는다 — dispatcher 의 `identified` 는
       인증 성공이 아니라 **클라 메시지 형태**로 Firebase 검증 전에 결정된다. 그 값을 받는 API는
       검증 전에 True 를 넘길 수 있다. 익명/식별은 **다른 함수**이고 식별 쪽은 `uid` 를 요구한다.
    ⛔ 순서와 중복을 보존한다(호출부가 그대로 `registry.register` 에 넘긴다).
    ⛔ 미지 stage 는 삼키지 않는다 — 원본을 돌려주면 fail-open 이다.
    """
    # ⛔ `frozenset("fx:usd-krw")` 는 문자 집합이 되어 실제 FX를 제거하지 못한다. 이 함수는
    #    익명 정책의 정본이므로, 생성자에서 검증했다고 가정하지 않고 검증된 불변 집합만 받는다.
    if not isinstance(fx_topics, frozenset):
        raise TypeError(
            "fx_topics 는 검증된 frozenset[str] 이어야 한다 "
            f"(got {type(fx_topics).__name__})"
        )
    if not fx_topics or any(
        not isinstance(topic, str)
        or not topic.startswith("fx:")
        or len(topic) <= len("fx:")
        or topic != topic.strip()
        or any(character.isspace() for character in topic)
        for topic in fx_topics
    ):
        raise ValueError(f"fx_topics 는 비어 있지 않은 topic 문자열 집합이어야 한다: {fx_topics!r}")
    fx = fx_topics
    if stage is TopicAuthStage.COMPATIBILITY:
        return list(topics)
    if stage is TopicAuthStage.REJECT_ANONYMOUS_FX:
        return [t for t in topics if t not in fx]
    if stage is TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM:
        # R-OPEN-1 = 인증 **∧** premium. 익명은 첫 접속사를 만족할 수 없다.
        # ⚠️ 응답은 없다(§E1 익명 계약) — 구 topic 클라는 오류가 아니라 **침묵**을 본다.
        return []
    raise ValueError(f"알 수 없는 rollout 단계: {stage!r}")


def plan_authenticated(
    topics: Sequence[str], *, stage: TopicAuthStage, uid: str
) -> AuthorizationPlan:
    """식별된 요청의 partition.

    ⚠️ 입력은 dispatcher ① 분류를 **통과한** topic 이어야 한다(supported ∧ enabled). `unknown_topic`
       / `topic_unavailable` 판정은 planner **앞**에 남는다 — 두 코드의 §8-C 구분이 무너진다.
    """
    if not isinstance(uid, str) or not uid:
        raise ValueError(f"uid 는 비어 있지 않은 str 이어야 한다: {uid!r}")
    if stage not in (
        TopicAuthStage.COMPATIBILITY,
        TopicAuthStage.REJECT_ANONYMOUS_FX,
        TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM,
    ):
        raise ValueError(f"알 수 없는 rollout 단계: {stage!r}")

    identity_only: List[str] = []
    premium_only: List[str] = []
    gated: List[str] = []
    for topic in topics:
        klass = _class_of(topic)                      # 미매핑 → ValueError (fail-closed)
        if klass is AuthorizationClass.PREMIUM_AND_ENTITLEMENT:
            # ⚠️ KRX 는 stage 와 **무관하게** 항상 full 이다 — 이미 운영 중인 계약이라
            #    rollout 사다리가 되돌릴 대상이 아니다.
            gated.append(topic)
        elif klass is AuthorizationClass.PREMIUM_ONLY:
            if stage is TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM:
                premium_only.append(topic)
            else:
                identity_only.append(topic)           # rollout 중간 상태
        else:                                          # 새 enum member 를 조용히 열지 않는다.
            raise ValueError(f"처리하지 않는 authorization class: {klass!r}")

    plan = AuthorizationPlan(
        uid=uid,
        identity_only=tuple(identity_only),
        premium_only=tuple(premium_only),
        premium_and_entitlement=tuple(gated),
    )
    # topic 집합뿐 아니라 중복 occurrence 수도 보존해야 한다. set 비교만 쓰면 planner 가 같은
    # topic 을 하나 잃거나 더 만들어도 통과한다.
    partitioned = identity_only + premium_only + gated
    if Counter(partitioned) != Counter(topics):
        raise RuntimeError(f"partition 이 입력 occurrence 를 보존하지 않는다: {topics!r} → {plan!r}")
    return plan
