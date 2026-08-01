"""WS topic 오류 프레임의 **단일 생성 지점** (§8-B / §8-C).

## 왜 별 모듈인가

`app/topic_dispatcher.py` 는 `app/main.py` 를 import 할 수 없다(firebase_admin 같은 무거운
import-time 의존성이 단위 테스트를 깨뜨린다). 그런데 인증 실패의 **분류**는 firebase 예외 타입을
알아야 하고, **프레임 생성**은 dispatcher 가 해야 한다. 그래서 둘을 나눈다:

- 분류: verifier 쪽(firebase 를 이미 lazy import 하는 곳)이 하고, 결과를 이 모듈의
  **firebase-free 예외** `SubscribeAuthFailed` 에 담아 던진다.
- 프레임: dispatcher 가 이 모듈의 `build_subscription_error` 하나로만 만든다.

이렇게 하면 dispatcher 에 firebase 의존이 생기지 않고, 오류 프레임이 **한 곳**에서 나온다.

## 왜 단일 생성 지점인가

오류가 3종 이상으로 늘면 `send_json({...})` 리터럴이 흩어지고, 그때 §8-C 의 **결합 규칙**
(“`temporarily_unavailable` 은 `retry_after_seconds` 를 **동반**한다”)이 한 곳에서만 지켜진다.
이 모듈은 그 규칙을 **생성 시점에 강제**한다 — 위반하면 프레임을 만들 수 없다.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence, Tuple

# §8-C 의 **전체-요청** 오류 코드 전부. per-topic 코드(`unknown_topic` / `topic_unavailable` /
# `premium_required` / `krx_entitlement_required` / `topics_disabled`)는 ack 의 `rejected_topics`
# 에 실리므로 여기 오지 않는다.
# ⛔ 어휘를 강제하지 않으면 오타나 즉흥 코드가 정상 프레임으로 나가 **코드가 제3의 계약을
#    만든다**(실측: `error="typo_not_in_section_8"` 이 그대로 통과했다).
WHOLE_REQUEST_ERRORS = frozenset({
    "invalid_token",
    "temporarily_unavailable",
    "invalid_request",
    "request_too_large",
})

# §8-C 의 **per-topic** 오류 코드 전부. ack 의 `rejected_topics` 에만 실린다.
# ⛔ 위 주석이 말로만 적어 두던 집합을 **실행 가능한 형태**로 올린다 — ack builder 가 이걸로
#    검증하지 않으면 오타·즉흥 코드가 rejected 항목에 그대로 실려 나간다.
PER_TOPIC_ERRORS = frozenset({
    "topics_disabled",
    "unknown_topic",
    "premium_required",
    "krx_entitlement_required",
    "topic_unavailable",
})

_ACK_OPERATIONS = frozenset({"subscribe", "unsubscribe"})

# §8-C: 이 코드는 `retry_after_seconds` 를 **반드시** 동반한다. 클라의 재시도 공식 입력이다.
_ERRORS_REQUIRING_RETRY_AFTER = frozenset({"temporarily_unavailable"})


class SubscribeAuthFailed(Exception):
    """subscribe 인증 실패를 **wire 어휘로** 전달하는 firebase-free 예외.

    ⚠️ 이 예외는 "연결을 닫아라"가 아니라 **"이 요청을 이 코드로 접어라"**를 뜻한다.
    §8-C 의 두 코드는 전체-요청 범위이므로 연결과 registry 는 **불변**이어야 한다 —
    그 구분이 이 타입의 존재 이유다(현행은 어떤 raise 든 연결 자체가 닫혀 그 연결의 다른
    구독까지 사라진다).
    """

    def __init__(self, error: str, retry_after_seconds: Optional[int] = None):
        super().__init__(error)
        self.error = error
        self.retry_after_seconds = retry_after_seconds


class FirebaseNotInitialized(RuntimeError):
    """Firebase SDK 미초기화 — **자격 문제가 아니라 우리 쪽 상태**다.

    ⚠️ 전용 타입인 이유: `RuntimeError` 를 통째로 잡으면 무관한 프로그래밍 오류까지
    `temporarily_unavailable` 로 접혀 조용해진다.
    """


def build_subscription_error(
    *,
    request_id,
    error: str,
    retry_after_seconds: Optional[int] = None,
) -> dict:
    """§8-B 의 `subscription_error` 프레임. **오류 프레임은 전부 여기서 나온다.**

    ⛔ §8-C 결합 규칙을 생성 시점에 강제한다:
      - `temporarily_unavailable` → `retry_after_seconds` **필수**(정수, 1 이상)
      - 그 밖의 코드 → `retry_after_seconds` **금지**(있으면 클라가 재시도 가능으로 오해한다.
        특히 `invalid_token` 에 붙으면 자격이 죽은 채 영구 재시도 루프가 된다)

    ⚠️ 상한은 강제하지 않는다 — 정본 §8-C 는 "동반"만 요구하고 값·jitter 정책은 별도 슬라이스다.
    여기서 임의 상한을 박으면 그 정책을 **코드가 먼저 결정**해 버린다.

    ⛔ `error` 는 §8-C 의 **전체-요청 코드**여야 한다. per-topic 코드나 즉흥 문자열은 거부한다.

    Raises:
        ValueError: 어휘 밖 코드, 또는 위 결합 규칙 위반.
    """
    if error not in WHOLE_REQUEST_ERRORS:
        raise ValueError(
            f"§8-C 의 전체-요청 오류 코드가 아니다: {error!r} — "
            f"허용: {sorted(WHOLE_REQUEST_ERRORS)}"
        )
    if error in _ERRORS_REQUIRING_RETRY_AFTER:
        if (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, int)
            or retry_after_seconds < 1
        ):
            raise ValueError(
                f"{error!r} 는 retry_after_seconds(1 이상 정수)를 동반해야 한다 — "
                f"got {retry_after_seconds!r}"
            )
    elif retry_after_seconds is not None:
        raise ValueError(
            f"{error!r} 에는 retry_after_seconds 를 붙일 수 없다 — "
            "클라가 재시도 가능으로 오해한다"
        )

    frame = {
        "type": "subscription_error",
        "request_id": request_id,
        "error": error,
    }
    if retry_after_seconds is not None:
        frame["retry_after_seconds"] = retry_after_seconds
    return frame


def build_subscription_ack(
    *,
    request_id: str,
    operation: str,
    accepted: Sequence[str],
    rejected: Sequence[Tuple[str, str]],
    active: Iterable[str],
    leases: Optional[Mapping[str, Tuple[str, int]]] = None,
) -> dict:
    """§8-B / §8-B-stage Stage 1 의 `subscription_ack`. **ack 프레임은 전부 여기서 나온다.**

    ## 왜 인자가 **문자열**인가 (객체가 아니라)

    ⛔ 호출자가 `[{"topic": t}, …]` 를 만들어 넘기면 컨테이너 형태가 **호출자 규율**이 된다.
    정본 §8-B-stage 의 ⛔ 항목("컨테이너는 Stage 1 부터 최종형 객체 배열")은 그렇게 지켜지지
    않는다 — 한 호출자가 문자열 배열을 넘기면 그대로 나간다. 그리고 그 사고는 **조용하다**:
    클라(iOS)의 `activeSubscriptions` 는 non-optional 객체 배열이라 형태가 어긋나면 디코드가
    통째로 실패하고, 그러면 "프레임 0개"와 **구분되지 않는다**(pending 이 영영 안 지워진다).
    그래서 wrap 을 여기서 한다 — 형태를 규율이 아니라 **타입**으로 만든다.

    ⛔ `removed_topics` 는 **인자가 아니다.** Stage 1 에서 항상 `[]` 이고(§C2 eviction 미구현),
    인자로 두면 "클라가 요청한 unsubscribe 결과"를 담고 싶은 유혹이 생긴다 — 그건 다른 축이다.

    ⚠️ **정렬 정책이 축마다 다르다.** `accepted`/`rejected` 는 **요청 순서 보존**(호출자가 그
    순서로 넘긴다), `active` 만 **사전순 정렬**한다. `active` 의 입력이 `set` 이라 정렬하지 않으면
    `PYTHONHASHSEED` 에 따라 프로세스마다 wire 순서가 달라진다. 이 정렬은 정본에 없고 코드에만
    있던 계약이라, 여기 적어 두지 않으면 다음 재작성에서 조용히 사라진다(실제로 그럴 뻔했다).

    Raises:
        ValueError: `request_id` 가 non-empty 문자열이 아니거나(ack 은 nullable 이 **아니다**),
            `operation` 이 §8-B 어휘 밖이거나, `rejected` 의 오류 코드가 §8-C per-topic 어휘 밖.
    """
    if not isinstance(request_id, str) or not request_id:
        raise ValueError(
            "ack 의 request_id 는 non-empty 문자열이어야 한다 — "
            "ack 은 nullable 이 아니다(§8-B-stage). "
            f"got {request_id!r}"
        )
    if operation not in _ACK_OPERATIONS:
        raise ValueError(
            f"§8-B 의 operation 이 아니다: {operation!r} — 허용: {sorted(_ACK_OPERATIONS)}"
        )
    for topic, error in rejected:
        if error not in PER_TOPIC_ERRORS:
            raise ValueError(
                f"§8-C 의 per-topic 오류 코드가 아니다: {error!r} (topic={topic!r}) — "
                f"허용: {sorted(PER_TOPIC_ERRORS)}"
            )
    def _entry(topic: str) -> dict:
        entry: dict = {"topic": topic}
        lease = (leases or {}).get(topic)
        if lease is not None:
            # §8-B-stage Stage 2 — 컨테이너 형태는 Stage 1 부터 최종형이라 **필드만 는다**.
            entry["lease_id"], entry["lease_duration_seconds"] = lease
        return entry

    return {
        "type": "subscription_ack",
        "request_id": request_id,
        "operation": operation,
        "accepted_topics": [_entry(t) for t in accepted],
        "rejected_topics": [{"topic": t, "error": e} for t, e in rejected],
        "removed_topics": [],
        "active_subscriptions": [_entry(t) for t in sorted(active)],
    }
