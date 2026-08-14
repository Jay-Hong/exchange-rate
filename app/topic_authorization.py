"""topic partition의 premium·KRX entitlement **인가 coordinator** — §8.1 1C.

## 왜 bool 이 아닌가

`bool` 로 평탄화하면 §8-C 의 **범위 구분**이 사라진다 — 아래 넷이 전부 `False` 가 된다:
premium 없음(per-topic) / entitlement 없음(per-topic) / provider 일시 장애(**전체 요청**) /
설정·프로토콜 결함(**전체 요청** + 운영자 신호). 그래서 판별 가능한 결과 타입을 쓴다.

## 관측 시각은 **I/O 직전**에, **조회 경로 안에서**

두 조건을 모두 만족해야 한다:
- **I/O 직전** — 완료 후 찍으면 그 지연만큼 lease 가 연장된다(fail-open).
- **조회 경로 안** — 나중에 캐시가 붙어도 **원래 관측 시각을 함께 보존**하도록 강제된다.
  호출자나 `to_thread` 직전에 찍으면 캐시 히트가 "방금 관측"으로 찍힌다.

⛔ `verify_premium_status` / `main.require_premium` 을 쓰지 않는다 — **가용성 우선**(장애 시
stale 캐시로 ACTIVE 반환) 정책이라 lease 판정에 쓰면 **오래된 판정이 "방금 확인함"으로 승격**된다.
cache-free `fetch_revenuecat_result` 만 쓴다.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import sys
from dataclasses import dataclass
from typing import Optional, Union

from sqlalchemy import exc as sqlalchemy_exc

from app import subscribe_load_metrics as subscribe_load
from app.db_errors import TRANSIENT_DB_ERRORS, is_transient_db_error
from app.topic_policy import AuthorizationPlan

logger = logging.getLogger("exchange_rate.topic_authorization")


class UnavailableKind(enum.Enum):
    """판정 불가의 **성질**. ⚠️ wire 지연(초)은 여기 두지 않는다 — 도메인 verdict 가 config 를
    import 해 재시도 정책까지 소유하면 경계가 흐려진다. 매핑은 dispatcher 한 곳이 한다."""

    TRANSIENT = "transient"      # 재시도로 나을 수 있다 → WARNING + 짧은 간격
    PERSISTENT = "persistent"    # 우리 설정·계약 결함 → ERROR + 긴 간격


@dataclass(frozen=True)
class EntitlementObservation:
    """entitlement 조회 결과 + **그 조회의 관측 시각**.

    ⛔ 시각을 호출자가 찍지 않고 이 객체에 담는 이유: 나중에 캐시가 붙으면 **이 객체째로**
    보존해야 하므로, stale hit 이 "방금 관측"으로 둔갑할 수 없다.
    """

    allowed: bool
    observed_at_mono: float


@dataclass(frozen=True)
class PremiumGranted:
    """**중간** 결과 — premium 만 확인됐다. entitlement 는 아직 관측되지 않았다.

    ⛔ 여기서 최종 `Granted` 를 만들면 **관측하지 않은 entitlement 시각을 지어내게** 된다.
    이 리포는 같은 형태("다른 축 인자에 값 밀어 넣기")를 이미 두 번 고쳤다.
    """

    uid: str
    premium_observed_at_mono: float

    def __post_init__(self) -> None:
        if not isinstance(self.uid, str) or not self.uid:
            raise ValueError(f"premium 관측 UID 는 비어 있지 않은 str 이어야 한다: {self.uid!r}")


@dataclass(frozen=True)
class Granted:
    """**최종** 승인 — 두 축이 **실제로** 관측된 지점에서만 만들어진다."""

    uid: str
    premium_observed_at_mono: float
    entitlement_observed_at_mono: float

    def __post_init__(self) -> None:
        if not isinstance(self.uid, str) or not self.uid:
            raise ValueError(
                f"entitlement 관측 UID 는 비어 있지 않은 str 이어야 한다: {self.uid!r}"
            )


DENIABLE_ERRORS = frozenset({"premium_required", "krx_entitlement_required"})


@dataclass(frozen=True)
class Denied:
    """§8-C **per-topic** 코드. 이 요청의 다른 topic 은 살아남는다.

    ⛔ 어휘를 **생성 시점에 강제**한다 — `str` 로 두면 `Denied("typo")` 가 정상 생성되고,
    "타입으로 범위를 보존한다"는 목표가 무너진다(`build_subscription_error` 와 같은 규율).
    """

    error: str

    def __post_init__(self):
        if self.error not in DENIABLE_ERRORS:
            raise ValueError(
                f"per-topic 거부 코드가 아니다: {self.error!r} — 허용: {sorted(DENIABLE_ERRORS)}"
            )


@dataclass(frozen=True)
class Unavailable:
    """판정 **불가** — §8-C **전체-요청** `temporarily_unavailable`. registry 불변."""

    kind: UnavailableKind
    reason: str                  # 로그 extra 전용 — wire 에 나가지 않는다

    def __post_init__(self) -> None:
        if not isinstance(self.kind, UnavailableKind):
            raise TypeError(
                "Unavailable.kind 는 UnavailableKind 여야 한다: "
                f"{type(self.kind).__name__}"
            )


GatedVerdict = Union[Granted, Denied, Unavailable]
PremiumVerdict = Union[PremiumGranted, Denied, Unavailable]


@dataclass(frozen=True)
class AuthorizationOutcome:
    """한 요청의 topic partition과 실제 관측 결과를 **UID째** 묶는다.

    `Unavailable`은 이 객체 안에 넣지 않는다. 판정 불가는 §8-C 전체-요청 결과라 caller가
    registry를 전혀 건드리기 전에 즉시 반환해야 한다. 반면 `Denied`는 per-topic이므로
    premium과 entitlement 축에 각각 남겨 혼합 요청을 부분 fold할 수 있어야 한다.
    """

    plan: AuthorizationPlan
    premium: Optional[Union[PremiumGranted, Denied]]
    entitlement: Optional[Union[Granted, Denied]]

    def __post_init__(self) -> None:
        if not self.plan.requires_premium():
            if self.premium is not None or self.entitlement is not None:
                raise ValueError("premium 불필요 plan 에 인가 관측이 붙었다")
            return

        if isinstance(self.premium, Denied):
            if self.premium.error != "premium_required" or self.entitlement is not None:
                raise ValueError("premium 거부 outcome 의 축 조합이 잘못됐다")
            return
        if not isinstance(self.premium, PremiumGranted):
            raise ValueError("premium 필요 plan 에 premium 판정이 없다")
        if self.premium.uid != self.plan.uid:
            raise ValueError("premium 관측 UID 가 plan UID 와 다르다")

        if not self.plan.requires_entitlement():
            if self.entitlement is not None:
                raise ValueError("entitlement 불필요 plan 에 entitlement 판정이 붙었다")
            return

        if isinstance(self.entitlement, Denied):
            if self.entitlement.error != "krx_entitlement_required":
                raise ValueError("entitlement 거부 코드가 잘못됐다")
            return
        if not isinstance(self.entitlement, Granted):
            raise ValueError("entitlement 필요 plan 에 entitlement 판정이 없다")
        if self.entitlement.uid != self.plan.uid:
            raise ValueError("entitlement 관측 UID 가 plan UID 와 다르다")
        if (
            self.entitlement.premium_observed_at_mono
            != self.premium.premium_observed_at_mono
        ):
            raise ValueError("premium 관측 시각이 outcome 안에서 갈렸다")


AuthorizationVerdict = Union[AuthorizationOutcome, Unavailable]


def classify_premium(
    result, *, uid: str, premium_observed_at_mono: float
) -> PremiumVerdict:
    """RevenueCat 결과 → verdict. **순수 함수**(I/O 0).

    ⛔ 분류 불가는 삼키지 않는다 — 미지의 결과 타입은 `TypeError` 로 올린다(리포 선례).
    """
    from app import subscription

    if isinstance(result, subscription.Determined):
        return (
            PremiumGranted(uid=uid, premium_observed_at_mono=premium_observed_at_mono)
            if result.is_premium
            else Denied("premium_required")
        )
    if isinstance(result, subscription.ProviderUnavailable):
        return Unavailable(UnavailableKind.TRANSIENT, "revenuecat_unavailable")
    if isinstance(result, (subscription.ProviderMisconfigured,
                           subscription.BadRequest,
                           subscription.ProtocolViolation)):
        return Unavailable(UnavailableKind.PERSISTENT,
                           f"revenuecat_{type(result).__name__.lower()}")
    raise TypeError(f"분류할 수 없는 RevenueCat 결과: {type(result).__name__}")


def _axis_outcome(verdict) -> str:
    """verdict → subscribe-load 제출 outcome. **관측용 매핑일 뿐 정책이 아니다** — 여기서
    나온 문자열은 counter 버킷만 정하고, wire 로 반환되는 verdict 는 그대로다.

    ⛔ 문자열 리터럴을 각 분기마다 흩뿌리지 않는 이유: 오타 하나가 그 분기만 조용히
       `unclassified` 로 접히게 만든다(모듈이 진단은 남기지만 버킷은 이미 갈렸다).
       타입 주도 매핑 한 곳이면 갈릴 표면이 없다.
    """
    if isinstance(verdict, (PremiumGranted, Granted)):
        return "granted"
    if isinstance(verdict, Denied):
        return "denied"
    if isinstance(verdict, Unavailable):
        if verdict.kind is UnavailableKind.TRANSIENT:
            return "unavailable_transient"
        if verdict.kind is UnavailableKind.PERSISTENT:
            return "unavailable_persistent"
        raise TypeError(
            "관측 outcome 으로 매핑할 수 없는 Unavailable.kind: "
            f"{type(verdict.kind).__name__}"
        )
    raise TypeError(f"관측 outcome 으로 매핑할 수 없는 verdict: {type(verdict).__name__}")


def _observe_krx_entitlement_sync(user_id: str, *, mono) -> EntitlementObservation:
    """worker thread 전용 — **쿼리 직전**에 관측 시각을 찍고 세션을 여기서 열고 닫는다.

    ⛔ 세션을 밖에서 만들어 넘기지 않는다: Session 은 thread-safe 하지 않고, 풀이 3+2 라
    이중 소비를 피해야 하며, WS 는 receive loop **진입 전에** 요청 세션을 닫는 구조다.
    """
    from app import entitlements
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        # ⛔ **쿼리 직전**에 찍는다. 완료 후면 DB 지연만큼 lease 가 늘고(fail-open),
        #    세션 생성 전이면 커넥션 획득 대기(풀 3+2)가 관측 시각 밖으로 빠진다.
        observed_at_mono = mono()
        allowed = entitlements.has_entitlement(
            db, user_id, entitlements.KRX_FUTURES_ENTITLEMENT_KEY
        )
    finally:
        db.close()
    return EntitlementObservation(allowed=allowed, observed_at_mono=observed_at_mono)


def assert_single_gated_topic() -> None:
    """⛔ **fail-closed 트립와이어.** 이 판정기는 KRX entitlement **하나**만 조회한다.

    두 번째 gated topic 이 생기면 그 verdict 가 **다른 상품에도 적용**되어 조용히 열린다
    (분류 loop 는 `gated` 전체에 같은 verdict 를 쓴다). 일반화 대신 여기서 막아, 추가하는
    사람이 **판정기부터 고치게** 강제한다.

    ⚠️ 판정은 **공유 가드**가 한다 — REST 가시성(`visible_snapshot_topics_sync` 가 `KRX_TOPIC` 을
    하드코딩으로 걸러낸다)도 같은 사실에 걸려 있어, 두 곳이 따로 판정하면 갈린다.
    """
    from app.topic_policy import assert_single_entitlement_topic

    assert_single_entitlement_topic()


def _log_unavailable(
    kind: UnavailableKind, message: str, exc: Optional[BaseException] = None
) -> None:
    """⚠️ transient=WARNING / persistent=**ERROR**. 영구 결함을 WARNING 으로 내면 운영자가
    "재시도하면 낫는다"로 읽어 조사 우선순위가 밀린다."""
    log = logger.error if kind is UnavailableKind.PERSISTENT else logger.warning
    # ⚠️ 예외 컨텍스트: except 블록 **밖**에서 부르면 `sys.exc_info()` 가 비어 traceback 이
    #    소실된다 — 관측 CM 밖으로 로그를 옮기며(로깅 결함이 외부축 신호를 오염시키지 않게)
    #    잡아 둔 예외를 명시적으로 넘긴다. 안 넘기면 기존처럼 활성 컨텍스트에서만 붙는다.
    log(message, extra={"kind": kind.value},
        exc_info=exc if exc is not None else sys.exc_info()[0] is not None)


async def _observe_premium(user_id: str, *, mono) -> PremiumVerdict:
    """premium 축만 관측한다 — **topic 을 모른다**.

    ⛔ 여기서 `assert_single_gated_topic()` 을 부르지 않는다. 그 트립와이어는 *KRX
       entitlement 판정기*의 것이고(`gated != {KRX_TOPIC}` 이면 죽는다), premium 은
       상품 축이라 gated 집합과 무관하다. 둘을 한 함수에 묶어 두면 FX/USDT 에 premium 을
       요구하려는 사람이 **gated 집합을 넓히는 잘못된 길**로 유도된다 — 그러면 분류 loop 가
       하나의 verdict 를 gated 전체에 적용해 KRX 판정이 다른 상품으로 샌다(R-GATE-1 스코핑 실측).

    ⛔ 호출 순서가 계약이다: 관측 시각은 RC 호출 **직전**에 찍는다. 호출 뒤에 찍으면
       캐시 히트나 느린 응답이 "방금 관측"으로 승격돼 lease horizon 이 늘어난다.
    """
    from app.clock import system_clock
    from app.subscription import fetch_revenuecat_result

    # ⛔ 관측 CM 진입을 mono() **앞**에 둔다 — 진입 부기가 mono 와 RC 호출 사이에 끼면
    #    "RC 직전" 관측 시각 계약이 부기 비용만큼 흐려진다. CM 은 부기 실패 시에도 본문을
    #    막지 않고 본문 예외를 삼키지 않는다(subscribe_load_metrics 계약, 변이로 잠김).
    async with subscribe_load.observe(subscribe_load.PREMIUM_RC) as load:
        premium_observed_at_mono = mono()          # ⛔ RC 호출 **직전**
        result = await fetch_revenuecat_result(user_id, clock=system_clock())
        verdict = classify_premium(
            result, uid=user_id, premium_observed_at_mono=premium_observed_at_mono
        )
        load.finish(_axis_outcome(verdict))
    if isinstance(verdict, Unavailable) and verdict.kind is UnavailableKind.PERSISTENT:
        # ⚠️ **원인 로그는 leaf 가 이미 남긴다** — `fetch_revenuecat_result` 가 전송·HTTP·형식
        #    오류를 WARNING/ERROR 로 기록한다(확인함). 여기서 또 남기면 같은 사건이 두 줄이 된다.
        #    ⛔ 한때 "레벨 신호가 한 곳에서만 난다"고 적었는데 **사실이 아니었다**(codex).
        #    그래서 여기서는 **정책 승격**만 기록한다: "우리가 이 결과를 *영구 결함*으로 판정했다"는
        #    leaf 가 모르는 사실이고, transient 는 leaf 의 WARNING 으로 충분하다.
        logger.error(
            "RevenueCat 결과를 영구 결함으로 판정 — 재시도로 낫지 않는다",
            extra={"reason": verdict.reason},
        )
    return verdict


async def _observe_krx_entitlement(
    user_id: str, *, premium: PremiumGranted, mono
) -> GatedVerdict:
    """premium 승인 뒤 KRX entitlement 축만 관측한다."""
    assert_single_gated_topic()
    if premium.uid != user_id:
        raise ValueError("premium 관측 UID 와 entitlement 조회 UID 가 다르다")

    # ⛔ 위 두 pre-check 는 관측 CM **밖**이다 — 배선·계약 오류(트립와이어, UID 불일치)를
    #    krx 축의 `raised` 로 기록하면 외부축(DB) 결함 신호가 오염된다. 축 관측은 여기부터다.
    unavailable_log: Optional[str] = None
    caught: Optional[BaseException] = None
    async with subscribe_load.observe(subscribe_load.KRX_ENTITLEMENT) as load:
        try:
            # ⛔ worker 축은 `timed_call` 이 worker 스레드 안에서 센다 — caller 축과 분리
            #    (`to_thread` 는 취소를 전파하지 않는다). `_observe_krx_entitlement_sync`
            #    본문은 불변 — 세션 수명과 `observed_at_mono` 위치(쿼리 직전)는 계약이다.
            observation = await asyncio.to_thread(
                subscribe_load.timed_call, subscribe_load.KRX_ENTITLEMENT, load,
                _observe_krx_entitlement_sync, user_id, mono=mono,
            )
        except TRANSIENT_DB_ERRORS as exc:
            kind = (UnavailableKind.TRANSIENT if is_transient_db_error(exc)
                    else UnavailableKind.PERSISTENT)
            verdict = Unavailable(kind, "entitlement_db_error")
            unavailable_log, caught = "entitlement 조회 DB 오류", exc
        except sqlalchemy_exc.SQLAlchemyError as exc:
            # 그 밖 DB/ORM 오류(스키마 드리프트 등)는 재시도로 낫지 않는다.
            verdict = Unavailable(UnavailableKind.PERSISTENT, "entitlement_db_permanent")
            unavailable_log, caught = "entitlement 조회 DB 오류(영구)", exc
        # ⛔ **일반 `Exception` 은 잡지 않는다.** 한때 blast radius(스키마 드리프트 하나로 KRX
        #    요청 연결이 전부 끊긴다)를 근거로 삼키려 했는데 **틀렸다**(codex): 그건 별도의 운영
        #    방어 문제이고, `AttributeError`·`TypeError` 같은 **프로그래밍 오류를 가용성 verdict 로
        #    바꾸면** 클라는 재시도 가능한 정상 결과로 읽고 서버는 영영 안 낫는다.
        #    분류 불가는 삼키지 않는다 — 이 리포의 인증 경로가 이미 그 규율이다.
        #    (관측 CM 은 그 예외를 `raised` 로 기록만 하고 그대로 재전파한다.)
        else:
            if not observation.allowed:
                verdict = Denied("krx_entitlement_required")
            else:
                # ⛔ **여기가 유일한 `Granted` 생성 지점**이다 — 두 축이 실제로 관측된 뒤.
                verdict = Granted(
                    uid=user_id,
                    premium_observed_at_mono=premium.premium_observed_at_mono,
                    entitlement_observed_at_mono=observation.observed_at_mono,
                )
        load.finish(_axis_outcome(verdict))
    # ⛔ 로깅은 관측 CM **밖** — 로깅 인프라 결함(Filter/Logger 오류)이 실제 DB transient
    #    사건을 krx `raised` 로 둔갑시켜 `unavailable_*` 신호를 지우면 안 된다(pre-check 와
    #    같은 원칙, Workflow attribution probe 실측). duration 에도 로그 시간이 안 섞인다.
    #    traceback 은 `caught` 를 명시 전달해 보존한다(밖에서는 sys.exc_info() 가 빈다).
    if unavailable_log is not None:
        _log_unavailable(verdict.kind, unavailable_log, exc=caught)
    return verdict


async def authorize_subscription_plan(
    plan: AuthorizationPlan, *, mono
) -> AuthorizationVerdict:
    """partition 전체를 조정한다. RC 0/1회, entitlement 0/1회가 계약이다.

    순서: premium이 필요한 경우에만 RC를 한 번 관측하고, 그 승인이 나온 뒤 entitlement가
    필요한 경우에만 DB를 한 번 관측한다. 어느 축의 `Unavailable`도 전체 요청 결과로 즉시
    반환한다. `Denied`는 outcome 안에 남겨 dispatcher가 해당 partition만 철회한다.
    """
    if not isinstance(plan, AuthorizationPlan):
        raise TypeError(f"plan 은 AuthorizationPlan 이어야 한다: {type(plan).__name__}")
    if not plan.requires_premium():
        return AuthorizationOutcome(plan=plan, premium=None, entitlement=None)

    premium = await _observe_premium(plan.uid, mono=mono)
    if isinstance(premium, Unavailable):
        return premium
    if isinstance(premium, Denied):
        # ⛔ premium 없음/불가 → entitlement 조회 0회.
        return AuthorizationOutcome(plan=plan, premium=premium, entitlement=None)

    if not plan.requires_entitlement():
        return AuthorizationOutcome(plan=plan, premium=premium, entitlement=None)

    entitlement = await _observe_krx_entitlement(
        plan.uid, premium=premium, mono=mono
    )
    if isinstance(entitlement, Unavailable):
        return entitlement
    return AuthorizationOutcome(
        plan=plan,
        premium=premium,
        entitlement=entitlement,
    )
