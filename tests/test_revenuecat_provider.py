"""RevenueCat provider — characterization + typed 결과 계약 (ADR-039 §8.1 A4-1).

## 왜 characterization인가

`_check_revenuecat_entitlement`는 `(is_premium, should_cache)` 튜플을 돌려주는데,
`(False, False)`가 **성격이 전혀 다른 5개 경로**에서 나온다 — API key 미설정(config) /
JSON 파싱 실패(계약 위반) / `expires_date` 파싱 실패 / 4xx·5xx 일괄(config ⊎ transient) /
`except Exception`(transient ⊎ programming). WS 인가 경로가 이걸 그대로 소비하면
3-bucket 계약(terminal / transient / 내부 예외)이 즉시 깨진다.
⚠️ 이 파일 곳곳의 `§8.1 A6` 표기는 ADR-040 에서 **폐기된 설계**의 번호다 — 계약 자체는
살아 있고, **현재 소비자는 `app/topic_authorization.py`** 의 `classify_premium` /
`authorize_subscription_plan` 이다(각각 per-topic `Denied` / 전체-요청 `Unavailable` /
재전파로 접는다).

그래서 **아래에 typed provider를 두고 REST adapter가 되접는다** — 예외·전송·HTTP 상태
경로는 구 동작 그대로이고, **malformed 200만 §8.1 A4-1 hardening에서 의도적으로 바꿨다**.
이 파일의 표는 리팩터 *이전*에 작성해 **현행 코드에 21건 전수 대조**한 뒤 시작했다 —
기대값을 내 독해가 아니라 실행 결과로 확정하기 위해서다(실제로 초안의 lifetime 기대값이 틀렸다).

⚠️ **이력**: typed provider 도입까지는 `tests/test_subscription_clock.py`가 **무수정 green**인
것이 behavior-change-0의 증거였다. 이후 malformed-200 hardening(§8.1 A4-1)이 **승인된 REST 동작
변경**을 포함하면서 그 파일의 lifetime fixture 1건이 red가 되어 갱신됐다 — 규칙이 의도대로
"변경을 드러내는" 역할을 한 것이다. 지금도 **그 파일의 red는 REST 동작이 바뀐다는 신호**이므로
승인 없이 고치지 말 것.

⚠️ **축 주의**: `expires_dt > clock.wall()`의 wall 축은 **구독 유효성** 판정이라 맞다.
lease horizon의 monotonic 축(§8.1 A2)과 목적이 다르므로 "일관성" 명목으로 바꾸지 말 것.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from app import subscription
from app.clock import Clock
from app.subscription import (
    CACHE_TTL,
    BadRequest,
    Determined,
    ProviderMisconfigured,
    ProviderUnavailable,
    ProtocolViolation,
    PremiumStatus,
    _check_revenuecat_entitlement,
    fetch_revenuecat_result,
    verify_premium_status,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _clock(instant: datetime = T0) -> Clock:
    """wall 전용 — 이 모듈은 monotonic 축을 읽지 않는다(§8.1 A4)."""

    def _poison_mono() -> float:
        raise AssertionError("subscription은 wall 축만 읽는다 (ADR-039 §8.1 A4)")

    return Clock(wall=lambda: instant, mono=_poison_mono)


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, raises: Exception | None = None):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


class _FakeAsyncClient:
    """`httpx.AsyncClient(...)` 컨텍스트 매니저 대역."""

    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, *args, **kwargs):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _patch_http(result):
    return patch.object(subscription.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(result))


_FUTURE = "2026-06-01T00:00:00Z"
_PAST = "2025-06-01T00:00:00Z"

# 현행 계약(§8.1 A4-1 hardening) — 판정은 **키 존재 + 타입**이고 truthiness 분기는 폐기됐다:
#      premium={pid, expires_date: null}  -> lifetime            <- 공식 샘플 형태
#      premium={pid}  (expires_date 누락)  -> ProtocolViolation
#      premium={} / null / [] / "yes"      -> ProtocolViolation
#      entitlements에 premium 키 없음       -> 정상 inactive
# 이력: 구 코드는 truthiness로 분기해 빈 dict를 "구독 없음"으로, expires_date 누락과 ""를
#    lifetime으로 통과시켰다. 초안에서 lifetime 픽스처를 빈 dict로 잡아 기대값을 (True, True)로
#    적었다가 리팩터 전 characterization이 잡았고, 그 분기 자체가 이후 hardening 대상이 됐다.
_ACTIVE = {"product_identifier": "p", "expires_date": _FUTURE}
_EXPIRED = {"product_identifier": "p", "expires_date": _PAST}
_LIFETIME = {"product_identifier": "onetime", "expires_date": None}  # 공식 샘플 형태
_BAD_DATE = {"product_identifier": "p", "expires_date": "not-a-date"}


def _entitlement_payload(premium):
    return {"subscriber": {"entitlements": {"premium": premium}}}


def _no_premium_key_payload():
    return {"subscriber": {"entitlements": {}}}


# ── 시나리오 표: (라벨, 응답/예외, 기대 legacy 튜플, 기대 typed 변종) ────────────────

SCENARIOS = [
    ("200 active",        _FakeResponse(200, _entitlement_payload(_ACTIVE)),        (True, True),   Determined),
    ("200 lifetime(null)", _FakeResponse(200, _entitlement_payload(_LIFETIME)),      (True, True),   Determined),
    ("200 expired",       _FakeResponse(200, _entitlement_payload(_EXPIRED)),       (False, True),  Determined),
    ("premium 키 누락",    _FakeResponse(200, _no_premium_key_payload()),            (False, True),  Determined),
    ("entitlements {}",   _FakeResponse(200, {"subscriber": {"entitlements": {}}}), (False, True),  Determined),
    ("404 unknown user",  _FakeResponse(404),                                       (False, True),  Determined),
    ("400 bad request",   _FakeResponse(400),                                       (False, False), BadRequest),
    ("401 unauthorized",  _FakeResponse(401),                                       (False, False), ProviderMisconfigured),
    ("403 forbidden",     _FakeResponse(403),                                       (False, False), ProviderMisconfigured),
    ("408 timeout",       _FakeResponse(408),                                       (False, False), ProviderUnavailable),
    ("429 rate limited",  _FakeResponse(429),                                       (False, False), ProviderUnavailable),
    ("500 server error",  _FakeResponse(500),                                       (False, False), ProviderUnavailable),
    ("503 unavailable",   _FakeResponse(503),                                       (False, False), ProviderUnavailable),
    ("409 unexpected 4xx", _FakeResponse(409),                                      (False, False), ProtocolViolation),
    ("malformed JSON",    _FakeResponse(200, raises=ValueError("not json")),        (False, False), ProtocolViolation),
    ("transport error",   httpx.ConnectError("connection reset"),                   (False, False), ProviderUnavailable),
    ("read timeout",      httpx.ReadTimeout("slow"),                                (False, False), ProviderUnavailable),
    # ── 형식 위반 (§8.1 A4-1 hardening) ─────────────────────────────────────────
    ("body not an object", _FakeResponse(200, [1, 2, 3]),                           (False, False), ProtocolViolation),
    ("subscriber 누락",    _FakeResponse(200, {}),                                   (False, False), ProtocolViolation),
    ("subscriber is str", _FakeResponse(200, {"subscriber": "nope"}),               (False, False), ProtocolViolation),
    ("entitlements 누락",  _FakeResponse(200, {"subscriber": {}}),                   (False, False), ProtocolViolation),
    ("entitlements is str", _FakeResponse(200, {"subscriber": {"entitlements": "x"}}), (False, False), ProtocolViolation),
    ("premium null",      _FakeResponse(200, _entitlement_payload(None)),           (False, False), ProtocolViolation),
    ("premium []",        _FakeResponse(200, _entitlement_payload([])),             (False, False), ProtocolViolation),
    ("premium {}",        _FakeResponse(200, _entitlement_payload({})),             (False, False), ProtocolViolation),
    ("premium is str",    _FakeResponse(200, _entitlement_payload("yes")),          (False, False), ProtocolViolation),
    ("expires_date 키 누락", _FakeResponse(200, _entitlement_payload({"product_identifier": "p"})), (False, False), ProtocolViolation),
    ("expires_date \"\"",  _FakeResponse(200, _entitlement_payload({"product_identifier": "p", "expires_date": ""})), (False, False), ProtocolViolation),
    ("expires_date 숫자",  _FakeResponse(200, _entitlement_payload({"product_identifier": "p", "expires_date": 12345})), (False, False), ProtocolViolation),
    ("expires_date 형식오류", _FakeResponse(200, _entitlement_payload(_BAD_DATE)),   (False, False), ProtocolViolation),
    ("expires_date tz 없음", _FakeResponse(200, _entitlement_payload({"product_identifier": "p", "expires_date": "2026-06-01T00:00:00"})), (False, False), ProtocolViolation),
]


class TestLegacyTupleCharacterization(unittest.IsolatedAsyncioTestCase):
    """`(is_premium, should_cache)` 매핑을 표로 잠근다.

    ⚠️ 이 표에서 `(False, False)` 행이 여러 개인 것이 **핵심**이다 — typed provider가 내부적으로
    5가지로 구분해도 **REST가 보는 결과는 하나로 합쳐진다**.

    ⚠️ **"리팩터 전후 불변"은 이제 전 행에 대해 참이 아니다.** 이름의 characterization은 이 표의
    *출발점*을 가리킨다 — 리팩터 전 현행 코드에 21건을 전수 대조해 기대값을 실측으로 확정했다.
    이후 §8.1 A4-1 hardening에서 **malformed 200 행의 기대값은 승인 아래 의도적으로 바뀌었고**,
    바뀐 행의 근거는 `TestMalformedIsNotAuthoritative`가 따로 잠근다.
    불변인 것: **정상·예외·전송·HTTP 상태 경로**의 REST 결과.
    """

    async def test_each_scenario_preserves_legacy_tuple(self):
        for label, result, expected, _variant in SCENARIOS:
            with self.subTest(scenario=label):
                with _patch_http(result), patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                    got = await _check_revenuecat_entitlement("u", clock=_clock())
                self.assertEqual(got, expected, label)

    async def test_missing_api_key_short_circuits_before_http(self):
        """config 오류 — HTTP를 **타지 않고** 반환한다(현행 동작)."""
        boom = _patch_http(AssertionError("HTTP를 타면 안 된다"))
        with boom, patch.object(subscription, "REVENUECAT_API_KEY", ""):
            got = await _check_revenuecat_entitlement("u", clock=_clock())
        self.assertEqual(got, (False, False))

    async def test_expiry_boundary_uses_injected_wall_clock(self):
        """`expires_dt > clock.wall()` — **strict**이므로 정확히 같은 시각은 만료다.

        clock을 주입해 판정이 뒤집히는 것으로 wall 축 사용을 잠근다(축을 monotonic으로
        바꾸는 mutation은 여기서 `_poison_mono`로 터진다).
        """
        payload = _entitlement_payload({"product_identifier": "p", "expires_date": "2026-01-01T00:00:00Z"})
        with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
            with _patch_http(_FakeResponse(200, payload)):
                at_boundary = await _check_revenuecat_entitlement("u", clock=_clock(T0))
            with _patch_http(_FakeResponse(200, payload)):
                just_before = await _check_revenuecat_entitlement(
                    "u", clock=_clock(T0 - timedelta(seconds=1))
                )
        self.assertEqual(at_boundary, (False, True), "경계는 만료")
        self.assertEqual(just_before, (True, True), "1초 전은 유효")


class TestTypedResult(unittest.IsolatedAsyncioTestCase):
    """typed provider — REST가 평탄화한 것을 **구분해서** 돌려준다(strict가 소비할 축)."""

    async def test_each_scenario_maps_to_expected_variant(self):
        for label, result, _expected, variant in SCENARIOS:
            with self.subTest(scenario=label):
                with _patch_http(result), patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                    got = await fetch_revenuecat_result("u", clock=_clock())
                self.assertIsInstance(got, variant, label)

    async def test_missing_api_key_is_misconfigured_not_unavailable(self):
        """⚠️ 이 구분이 이 슬라이스의 존재 이유다 — 재시도로 낫지 않는 오류다."""
        boom = _patch_http(AssertionError("HTTP를 타면 안 된다"))
        with boom, patch.object(subscription, "REVENUECAT_API_KEY", ""):
            got = await fetch_revenuecat_result("u", clock=_clock())
        self.assertIsInstance(got, ProviderMisconfigured)

    async def test_determined_carries_is_premium(self):
        with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
            with _patch_http(_FakeResponse(200, _entitlement_payload(_ACTIVE))):
                active = await fetch_revenuecat_result("u", clock=_clock())
            with _patch_http(_FakeResponse(404)):
                unknown = await fetch_revenuecat_result("u", clock=_clock())
        self.assertEqual((active.is_premium, unknown.is_premium), (True, False))

    async def test_429_is_not_misconfigured(self):
        """rate limit을 config 오류로 접으면 **영구 포기 + 허위 알람**이 된다.

        "4xx 대 5xx"로만 나누는 mutation은 이 테스트와 시나리오 표의 429 행이 함께 잡는다
        (무력화 시 2건 red).
        """
        with _patch_http(_FakeResponse(429)), patch.object(subscription, "REVENUECAT_API_KEY", "k"):
            got = await fetch_revenuecat_result("u", clock=_clock())
        self.assertIsInstance(got, ProviderUnavailable)
        self.assertNotIsInstance(got, ProviderMisconfigured)

    async def test_unavailable_carries_status_for_telemetry(self):
        with _patch_http(_FakeResponse(503)), patch.object(subscription, "REVENUECAT_API_KEY", "k"):
            got = await fetch_revenuecat_result("u", clock=_clock())
        self.assertEqual(got.status, 503)

    async def test_transport_error_has_no_status(self):
        """HTTP 응답이 없었으므로 status는 None — 있는 척하면 텔레메트리가 거짓이 된다."""
        with _patch_http(httpx.ConnectError("boom")), patch.object(subscription, "REVENUECAT_API_KEY", "k"):
            got = await fetch_revenuecat_result("u", clock=_clock())
        self.assertIsInstance(got, ProviderUnavailable)
        self.assertIsNone(got.status)


class TestAdapterEquivalence(unittest.IsolatedAsyncioTestCase):
    """adapter가 typed 결과를 legacy 튜플로 되접는 규칙을 **직접** 잠근다.

    위 두 클래스는 같은 입력에 대한 두 출력을 각각 보지만, 여기서는 **매핑 자체**를 본다 —
    `Determined`만 `should_cache=True`이고 나머지 4변종은 전부 `(False, False)`.
    """

    def test_determined_maps_to_should_cache_true(self):
        self.assertEqual(subscription._result_to_legacy_tuple(Determined(is_premium=True)), (True, True))
        self.assertEqual(subscription._result_to_legacy_tuple(Determined(is_premium=False)), (False, True))

    def test_every_non_determined_variant_maps_to_false_false(self):
        for result in (
            ProviderUnavailable(status=503),
            ProviderUnavailable(status=None),
            ProviderMisconfigured(status=401),
            BadRequest(status=400),
            ProtocolViolation(detail="malformed"),
        ):
            with self.subTest(variant=type(result).__name__):
                self.assertEqual(subscription._result_to_legacy_tuple(result), (False, False))

class TestUnexpectedErrorsPropagate(unittest.IsolatedAsyncioTestCase):
    """**programming 오류는 verdict가 아니다.**

    provider는 예상 밖 예외를 **그대로 전파**해야 **WS 인가 경로**
    (`app/topic_authorization.py` — 일반 `Exception` 을 의도적으로 잡지 않는다)가 그것을
    retryable로 오인하지 않는다. REST 호환(`(False, False)`)은 **adapter가 떠안는다**.

    ⚠️ 이 클래스가 잡는 회귀: provider를 광범위 `except Exception`으로 되돌리는 변경.
    구 코드가 정확히 그랬고, JSON이 list인 경우·`premium`이 문자열인 경우까지
    `ProviderUnavailable`(retryable)로 접혔다(실측 확인 후 이 슬라이스에서 교정).
    """

    async def test_provider_propagates_unexpected_exception(self):
        with _patch_http(RuntimeError("bug in our code")):
            with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                with self.assertRaises(RuntimeError):
                    await fetch_revenuecat_result("u", clock=_clock())

    async def test_adapter_absorbs_it_for_rest_compatibility(self):
        """같은 예외가 REST에서는 구 동작 그대로 `(False, False)`여야 한다."""
        with _patch_http(RuntimeError("bug in our code")):
            with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                got = await _check_revenuecat_entitlement("u", clock=_clock())
        self.assertEqual(got, (False, False))

    async def test_clock_failure_is_not_swallowed_as_unavailable(self):
        """주입 clock이 터지는 건 우리 버그다 — retryable로 접히면 원인이 영영 안 보인다."""

        def _boom() -> datetime:
            raise RuntimeError("clock exploded")

        clock = Clock(wall=_boom, mono=lambda: 0.0)
        with _patch_http(_FakeResponse(200, _entitlement_payload(_ACTIVE))):
            with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                with self.assertRaises(RuntimeError):
                    await fetch_revenuecat_result("u", clock=clock)

    # ⚠️ `test_date_block_does_not_swallow_arbitrary_exceptions`는 **삭제**했다 —
    #    hardening이 `isinstance(expires, str)`를 먼저 검사하게 되면서 그 테스트가 겨냥한
    #    "임의 객체의 `.replace()`가 터지는" 경로가 **도달 불가**해졌다. 남은 계약(예상 밖 예외
    #    전파)은 위 두 테스트 + `test_clock_failure_is_not_swallowed_as_unavailable`이 덮는다.


class TestMalformedIsNotAuthoritative(unittest.IsolatedAsyncioTestCase):
    """§8.1 A4-1 hardening — malformed 200을 **authoritative 상태로 캐시하지 않는다**.

    이전 동작(실측, 2026-07-28):

    | 입력 | typed | legacy | 문제 |
    |---|---|---|---|
    | `{}` / `premium=[]` | `Determined(False)` | `(False, True)` | 유료 사용자 강등 + 캐시 |
    | `expires_date=""` / 숫자 | `Determined(True)` | `(True, True)` | **빈 값에 프리미엄 부여** |

    ⚠️ 이건 A4의 stale fallback을 **제거**하는 변경이 아니라 **복원**이다 —
    `(False, False)`로 보내면 stale 캐시가 있으면 fallback, 없으면 PENDING이 된다.
    """

    async def test_no_malformed_input_yields_authoritative_determined(self):
        malformed = [
            ("body {}", {}),
            ("subscriber str", {"subscriber": "x"}),
            ("entitlements 누락", {"subscriber": {}}),
            ("premium null", _entitlement_payload(None)),
            ("premium []", _entitlement_payload([])),
            ("premium {}", _entitlement_payload({})),
            ("premium str", _entitlement_payload("yes")),
            ("expires 키 누락", _entitlement_payload({"product_identifier": "p"})),
            ('expires ""', _entitlement_payload({"product_identifier": "p", "expires_date": ""})),
            ("expires 숫자", _entitlement_payload({"product_identifier": "p", "expires_date": 0})),
        ]
        for label, payload in malformed:
            with self.subTest(case=label):
                with _patch_http(_FakeResponse(200, payload)):
                    with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                        typed = await fetch_revenuecat_result("u", clock=_clock())
                self.assertIsInstance(typed, ProtocolViolation, label)
                with _patch_http(_FakeResponse(200, payload)):
                    with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                        legacy = await _check_revenuecat_entitlement("u", clock=_clock())
                self.assertEqual(legacy, (False, False), label)

    async def test_valid_no_subscription_is_still_authoritative(self):
        """positive control — 정상 "구독 없음"까지 위반으로 접으면 안 된다."""
        for label, payload in [
            ("premium 키 누락", _no_premium_key_payload()),
            ("entitlements {}", {"subscriber": {"entitlements": {}}}),
        ]:
            with self.subTest(case=label):
                with _patch_http(_FakeResponse(200, payload)):
                    with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                        typed = await fetch_revenuecat_result("u", clock=_clock())
                self.assertEqual(typed, Determined(is_premium=False), label)

    async def test_lifetime_requires_explicit_null(self):
        """공식 샘플이 `"expires_date": null`이므로 **명시적 null만** lifetime이다."""
        with _patch_http(_FakeResponse(200, _entitlement_payload(_LIFETIME))):
            with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                self.assertEqual(
                    await fetch_revenuecat_result("u", clock=_clock()),
                    Determined(is_premium=True),
                )


class TestStaleFallbackRestoredForMalformed(unittest.IsolatedAsyncioTestCase):
    """이 hardening이 실제로 지키려는 것 — **stale 캐시를 가진 유료 사용자 보호**.

    구 동작에서는 malformed 200이 `should_cache=True`로 통과해 캐시의 `True`를 `False`로
    덮어써 INACTIVE로 강등시켰다. 같은 상황에서 503(진짜 장애)은 ACTIVE를 유지했다 —
    **쓰레기 데이터가 장애보다 더 신뢰받는** 역전이었다.
    """

    def setUp(self):
        subscription._cache.clear()
        subscription._pending.clear_all()

    tearDown = setUp

    async def _status_with_stale_paid_cache(self, payload):
        subscription._cache.set("u", True, clock=_clock(T0))
        later = T0 + CACHE_TTL + timedelta(minutes=1)
        with _patch_http(_FakeResponse(200, payload)):
            with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                return await verify_premium_status("u", clock=_clock(later))

    async def test_malformed_does_not_demote_stale_paying_user(self):
        for label, payload in [
            ("body {}", {}),
            ("premium []", _entitlement_payload([])),
            ("premium {}", _entitlement_payload({})),
            ('expires ""', _entitlement_payload({"product_identifier": "p", "expires_date": ""})),
        ]:
            with self.subTest(case=label):
                self.setUp()
                status = await self._status_with_stale_paid_cache(payload)
                self.assertEqual(status, PremiumStatus.ACTIVE, label)

    async def test_authoritative_inactive_still_demotes(self):
        """positive control — **정상** "구독 없음"은 여전히 강등시켜야 한다.

        이게 없으면 "전부 ACTIVE 반환"하는 구현도 위 테스트를 통과한다.
        """
        status = await self._status_with_stale_paid_cache(_no_premium_key_payload())
        self.assertEqual(status, PremiumStatus.INACTIVE)


class TestViolationAttribution(unittest.IsolatedAsyncioTestCase):
    """`ProtocolViolation.detail`이 **실제 위반 지점**을 가리켜야 한다.

    ⚠️ 이게 없으면 규칙이 서로를 가려도 아무 테스트가 red가 되지 않는다 — 무력화로 실증했다:
    `premium`의 "비어 있지 않은 dict" 검사를 지우면 `{}`가 `expires_date` 규칙에 걸려
    **여전히 ProtocolViolation**이라 타입 단언만으로는 통과한다. 그러면 운영자가 로그에서
    `field=expires_date_missing`을 보고 엉뚱한 곳을 찾게 된다.

    detail은 `_shape_violation`이 그대로 로그 `extra={"field": ...}`에도 싣는 값이라,
    이 테스트가 곧 **로그 진단 정확도**의 계약이다.
    """

    async def _detail(self, payload):
        with _patch_http(_FakeResponse(200, payload)):
            with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                result = await fetch_revenuecat_result("u", clock=_clock())
        self.assertIsInstance(result, ProtocolViolation)
        return result.detail

    async def test_each_violation_names_its_own_field(self):
        cases = [
            ("body", [1, 2, 3]),
            ("subscriber", {"subscriber": "x"}),
            ("entitlements", {"subscriber": {"entitlements": "x"}}),
            ("premium", _entitlement_payload(None)),
            ("premium", _entitlement_payload([])),
            ("premium", _entitlement_payload({})),          # ← 규칙이 서로 가리는지 잡는 핵심
            ("expires_date_missing", _entitlement_payload({"product_identifier": "p"})),
            ("expires_date_value", _entitlement_payload({"product_identifier": "p", "expires_date": ""})),
            ("expires_date_value", _entitlement_payload({"product_identifier": "p", "expires_date": 7})),
            ("expires_date", _entitlement_payload(_BAD_DATE)),
            ("expires_date_naive", _entitlement_payload({"product_identifier": "p", "expires_date": "2026-06-01T00:00:00"})),
        ]
        for expected, payload in cases:
            with self.subTest(expected=expected, payload=payload):
                self.assertEqual(await self._detail(payload), expected)


if __name__ == "__main__":
    unittest.main()
