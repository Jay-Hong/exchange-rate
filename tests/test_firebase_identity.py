"""Firebase identity provider — A6 검증기가 쓰는 identity authority adapter.

계약 근거: `FREE_TIER_ACCESS_MODEL_PLAN.md` §8.1 A6-1 / A6-2, `app/strict_verifier.py`.

## 이 adapter가 넘어야 할 함정 (전부 고정 SDK 소스로 확인, firebase-admin==6.9.0)

    UserNotFoundError  → exceptions.NotFoundError
    ConfigurationNotFoundError / TenantNotFoundError → **같은 부모의 형제**
      ⇒ `except NotFoundError → NotFound`로 접으면 프로젝트 오설정이
        "전 계정 삭제됨"이 된다.

    _user_mgt.py:599  `if not body or not body.get('users'): raise UserNotFoundError`
      ⇒ 404가 아니라 **빈/이상한 200 body**에서도 난다. 중간 프록시가 `{}`를 200으로
        돌려주는 동안 재접속하는 전 사용자가 `account_deleted`로 축출되고, 그 부정 관측이
        `IDENTITY_NEGATIVE_RECHECK_SECONDS`(300s) 동안 캐시된다.

    _http_client.py:50  DEFAULT_TIMEOUT_SECONDS = 120
      ⇒ 검증기의 전체 deadline(8초)보다 훨씬 길다. 낮추지 않으면 멈춘 provider가 flight를
        오래 점유하고, 그 키는 그동안 계속 `temporarily_unavailable`이다.

    _auth_utils.py  validate_uid → 평범한 `ValueError`
      ⇒ `derive_verdict`의 축 가드와 **타입이 겹친다**. 그대로 새어 나가면 "우리 코드 버그"로
        오분류된다.

    google.auth 자격증명 예외(RefreshError / DefaultCredentialsError)는 `FirebaseError`가 **아니다**
      ⇒ `except FirebaseError`만 쓰면 서비스 계정 키 폐기가 통째로 새어 나간다.
"""
import asyncio
import time
import unittest

from app.strict_verifier import (
    IdentityFound,
    IdentityMisconfigured,
    IdentityNotFound,
    IdentityUnavailable,
)
from app.firebase_identity import (
    FIREBASE_HTTP_TIMEOUT_SECONDS,
    FirebaseIdentityProvider,
)

UID = "uid-1"


class _FakeUser:
    def __init__(self, disabled=False, tokens_valid_after_timestamp=1_699_999_900_000):
        self.disabled = disabled
        self.tokens_valid_after_timestamp = tokens_valid_after_timestamp


def _raiser(exc):
    def _get_user(uid, app=None):
        raise exc
    return _get_user


def _run(get_user, uid=UID):
    provider = FirebaseIdentityProvider(get_user=get_user)
    return asyncio.run(provider(uid))


class TestHappyPath(unittest.TestCase):
    def test_enabled_user_maps_to_found(self):
        result = _run(lambda uid, app=None: _FakeUser())
        self.assertEqual(
            result, IdentityFound(disabled=False, tokens_valid_after_ms=1_699_999_900_000)
        )

    def test_disabled_flag_is_carried_through(self):
        result = _run(lambda uid, app=None: _FakeUser(disabled=True))
        self.assertTrue(result.disabled)

    def test_watermark_is_passed_as_milliseconds_unchanged(self):
        """⚠️ SDK가 이미 ms다(`_user_mgt.py`: `1000 * int(valid_since)`). 여기서 또 곱하면
        **모든 토큰이 revoked로 오판**된다."""
        result = _run(lambda uid, app=None: _FakeUser(tokens_valid_after_timestamp=1_700_000_000_000))
        self.assertEqual(result.tokens_valid_after_ms, 1_700_000_000_000)


class TestNotFoundIsNarrow(unittest.TestCase):
    """⛔ `UserNotFoundError` **정확히 그것만** NotFound다."""

    def test_user_not_found_maps_to_not_found(self):
        from firebase_admin import auth

        self.assertIsInstance(_run(_raiser(auth.UserNotFoundError("gone"))), IdentityNotFound)

    def test_sibling_not_found_errors_are_misconfigured_not_deleted(self):
        """⛔ 형제 예외를 NotFound로 접으면 **프로젝트 오설정이 "전 계정 삭제"**가 된다."""
        from firebase_admin import auth

        for name in ("ConfigurationNotFoundError", "TenantNotFoundError"):
            exc_cls = getattr(auth, name, None)
            if exc_cls is None:
                continue
            with self.subTest(exception=name):
                result = _run(_raiser(exc_cls("boom")))
                self.assertIsInstance(result, IdentityMisconfigured)

    def test_bare_not_found_error_is_misconfigured(self):
        """파싱 불가 404가 접히는 평범한 `NotFoundError`도 '계정 삭제'가 아니다."""
        from firebase_admin import exceptions

        result = _run(_raiser(exceptions.NotFoundError("unparseable", cause=None)))
        self.assertIsInstance(result, IdentityMisconfigured)


class TestErrorBuckets(unittest.TestCase):
    def test_permission_denied_is_misconfigured(self):
        """서비스 계정이 권한을 잃은 것 — 재시도해도 낫지 않는다."""
        from firebase_admin import auth

        self.assertIsInstance(
            _run(_raiser(auth.InsufficientPermissionError("denied", cause=None, http_response=None))),
            IdentityMisconfigured,
        )

    def test_rate_limit_is_unavailable(self):
        """⚠️ 4xx라고 영구 오류로 접으면 안 된다 — 백오프하면 낫는다."""
        from firebase_admin import auth

        self.assertIsInstance(
            _run(_raiser(auth.TooManyAttemptsTryLaterError("slow down", cause=None, http_response=None))),
            IdentityUnavailable,
        )

    def test_permanent_firebase_errors_are_misconfigured(self):
        """⛔ 이걸 transient로 접으면 **죽은 자격증명이 retry storm**이 된다.

        `UnauthenticatedError`(401)가 정확히 그 경우다 — 재시도해도 영원히 401이다.
        """
        from firebase_admin import exceptions

        for name in ("UnauthenticatedError", "FailedPreconditionError", "InvalidArgumentError"):
            with self.subTest(name=name):
                exc_cls = getattr(exceptions, name)
                self.assertIsInstance(_run(_raiser(exc_cls("boom"))), IdentityMisconfigured)

    def test_unclassified_firebase_error_defaults_to_permanent(self):
        """⛔ SDK가 나중에 추가할 타입은 **영구 쪽**으로 보낸다.

        비대칭이 근거다 — 영구를 transient로 보면 **조용한 storm**이고, transient를 영구로 보면
        시끄럽지만 배선이 어차피 `temporarily_unavailable`로 접으므로 **알게 된다**.
        """
        from firebase_admin import exceptions

        unclassified = exceptions.FirebaseError("SOME_FUTURE_CODE", "not in either list")
        self.assertIsInstance(_run(_raiser(unclassified)), IdentityMisconfigured)

    def test_transient_firebase_error_is_unavailable(self):
        from firebase_admin import exceptions

        self.assertIsInstance(
            _run(_raiser(exceptions.UnavailableError("503", cause=None))), IdentityUnavailable
        )

    def test_google_auth_credential_errors_are_misconfigured(self):
        """⛔ 이것들은 `FirebaseError`가 **아니다** — `except FirebaseError`만 쓰면 새어 나간다."""
        from google.auth.exceptions import DefaultCredentialsError, RefreshError

        for exc in (RefreshError("key revoked"), DefaultCredentialsError("no creds")):
            with self.subTest(exception=type(exc).__name__):
                self.assertIsInstance(_run(_raiser(exc)), IdentityMisconfigured)

    def test_google_auth_transport_error_is_unavailable(self):
        from google.auth.exceptions import TransportError

        self.assertIsInstance(_run(_raiser(TransportError("network"))), IdentityUnavailable)

    def test_malformed_uid_value_error_does_not_escape_as_our_bug(self):
        """⛔ `validate_uid`의 `ValueError`는 `derive_verdict`의 축 가드와 **타입이 겹친다**.

        그대로 새어 나가면 "우리 코드 버그"로 오분류된다.
        """
        result = _run(_raiser(ValueError('Invalid uid: ""')))
        self.assertIsInstance(result, IdentityMisconfigured)

    def test_unknown_exception_is_not_silently_swallowed(self):
        """모르는 예외를 transient로 접으면 진짜 버그가 영원히 재시도된다."""
        with self.assertRaises(RuntimeError):
            _run(_raiser(RuntimeError("unexpected")))

    def test_error_codes_are_bounded_not_free_text(self):
        """카운터 cardinality — `code`는 SDK의 경계 있는 값이어야 한다."""
        from firebase_admin import exceptions

        result = _run(_raiser(exceptions.UnavailableError("503", cause=None)))
        self.assertTrue(result.code)
        self.assertNotIn("503", result.code, "원문 메시지가 code로 새면 cardinality가 열린다")


class TestTransportTimeout(unittest.TestCase):
    """⛔ High 1의 나머지 절반 — SDK 자체 timeout이 없으면 flight가 오래 점유된다."""

    def test_timeout_is_below_the_verifier_deadline(self):
        from app.strict_verifier import VERIFY_DEADLINE_SECONDS

        self.assertLess(
            FIREBASE_HTTP_TIMEOUT_SECONDS,
            VERIFY_DEADLINE_SECONDS,
            "SDK timeout이 검증 전체 상한보다 길면 flight가 deadline 뒤에도 남는다",
        )

    def test_timeout_is_far_below_the_sdk_default(self):
        """SDK 기본값은 120초(`_http_client.py:50`)다."""
        self.assertLess(FIREBASE_HTTP_TIMEOUT_SECONDS, 120)

    def test_provider_does_not_touch_the_default_app(self):
        """⛔ FCM이 쓰는 전역 앱의 timeout을 바꾸면 안 된다 — 별도 이름 있는 app을 쓴다."""
        provider = FirebaseIdentityProvider(get_user=lambda uid, app=None: _FakeUser())
        self.assertIsNotNone(provider.app_name)
        self.assertNotEqual(provider.app_name, "[DEFAULT]")


class TestLazyBindingActuallyRuns(unittest.TestCase):
    """⛔ 앞 테스트들은 전부 `get_user`를 주입해 **실제 lazy 경로를 한 번도 안 돈다**.

    "기본 app 미접촉"도 문자열만 봤을 뿐, timeout 전달·named app 사용·초기화 경쟁을 검증하지
    못했다. 여기서 SDK 모듈을 가짜로 두고 그 경로를 직접 돌린다.
    """

    def setUp(self):
        import firebase_admin

        self.fb = firebase_admin
        self.init_calls = []
        self.get_user_calls = []
        self._apps = {}
        self._orig = (firebase_admin.get_app, firebase_admin.initialize_app)

        def get_app(name="[DEFAULT]"):
            if name not in self._apps:
                raise ValueError(f"no app {name}")
            return self._apps[name]

        def initialize_app(credential=None, options=None, name="[DEFAULT]"):
            self.init_calls.append((name, options))
            if name in self._apps:
                raise ValueError("app already exists")
            self._apps[name] = f"app:{name}"
            return self._apps[name]

        self._apps["[DEFAULT]"] = _FakeDefaultApp()
        firebase_admin.get_app = get_app
        firebase_admin.initialize_app = initialize_app

        from firebase_admin import auth

        self._orig_get_user = getattr(auth, "get_user", None)
        auth.get_user = lambda uid, app=None: (
            self.get_user_calls.append((uid, app)) or _FakeUser()
        )

    def tearDown(self):
        self.fb.get_app, self.fb.initialize_app = self._orig
        from firebase_admin import auth

        if self._orig_get_user is not None:
            auth.get_user = self._orig_get_user

    def test_named_app_is_created_with_the_lowered_timeout(self):
        provider = FirebaseIdentityProvider()          # 주입 없음 → lazy 경로
        result = asyncio.run(provider(UID))
        self.assertIsInstance(result, IdentityFound)
        self.assertEqual(len(self.init_calls), 1)
        name, options = self.init_calls[0]
        self.assertEqual(name, provider.app_name)
        self.assertNotEqual(name, "[DEFAULT]", "기본 app을 만들면 FCM timeout이 바뀐다")
        self.assertEqual(options, {"httpTimeout": FIREBASE_HTTP_TIMEOUT_SECONDS})
        self.assertEqual(self.get_user_calls[0][1], f"app:{name}", "named app으로 안 불렀다")

    def test_second_call_reuses_the_app(self):
        provider = FirebaseIdentityProvider()
        asyncio.run(provider(UID))
        asyncio.run(provider("uid-2"))
        self.assertEqual(len(self.init_calls), 1, "매 호출마다 app을 다시 만들었다")

    def test_concurrent_first_touch_initialises_once(self):
        """⛔ TOCTOU 회귀 잠금 — 실측(수정 전): 초기화 2회 + 한 요청이 `INVALID_UID`로 오분류.

        ⚠️ 그냥 스레드를 동시에 던지면 경쟁 창이 좁아 **안 잡힌다**(lock을 지워도 통과했다).
        첫 초기화를 **붙잡아** 두 번째 스레드가 반드시 그 창 안에 들어오게 만든다.
        """
        import threading

        entered, release = threading.Event(), threading.Event()
        real_init = self.fb.initialize_app
        first = {"done": False}

        def blocking_init(credential=None, options=None, name="[DEFAULT]"):
            if not first["done"]:
                first["done"] = True
                entered.set()
                release.wait(timeout=5)      # 초기화를 창 안에 붙잡아 둔다
            return real_init(credential, options, name)

        self.fb.initialize_app = blocking_init
        provider = FirebaseIdentityProvider()
        results, errors = [], []

        def worker(n):
            try:
                results.append(asyncio.run(provider(f"uid-{n}")))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=(1,))
        t1.start()
        self.assertTrue(entered.wait(timeout=5), "첫 초기화가 창에 진입하지 못했다")
        t2 = threading.Thread(target=worker, args=(2,))
        t2.start()
        # t2가 lock에 막혀 있어야 한다 — 안 막히면 두 번째 초기화가 시작된다
        t2.join(timeout=0.3)
        blocked = t2.is_alive()
        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertTrue(blocked, "두 번째 스레드가 초기화 창에 함께 들어갔다 (lock 없음)")
        self.assertEqual(errors, [])
        self.assertEqual(len(self.init_calls), 1, "초기화 경쟁이 app을 여러 번 만들었다")
        self.assertTrue(all(isinstance(r, IdentityFound) for r in results),
                        f"경쟁이 오분류를 만들었다: {[type(r).__name__ for r in results]}")

    def test_app_acquisition_failure_is_not_reported_as_a_bad_uid(self):
        """⛔ app 초기화 실패가 **UID 검증 실패로 둔갑**하면 안 된다.

        기본 app이 없거나(`_default_credential()`) named app 초기화가 실패하면 마지막
        `get_app()`도 `ValueError`를 내고, 그게 분류기에 도달해 유효한 uid가
        `INVALID_UID`로 기록된다. 두 원인은 대응 주체가 완전히 다르다 —
        전자는 배포·자격증명 문제, 후자는 호출자가 넘긴 값 문제다.
        """
        def failing_init(credential=None, options=None, name="[DEFAULT]"):
            raise ValueError("cannot initialise")

        self.fb.initialize_app = failing_init
        self._apps.pop("[DEFAULT]", None)          # 기본 app도 없다

        result = asyncio.run(FirebaseIdentityProvider()("perfectly-valid-uid"))
        self.assertIsInstance(result, IdentityMisconfigured)
        self.assertNotEqual(
            result.code, "INVALID_UID", "app 초기화 실패가 UID 문제로 보고됐다"
        )

    def test_app_acquisition_failure_with_a_working_default_app(self):
        """⛔ 위 테스트와 **다른 경로**를 밟는다.

        기본 app이 살아 있으면 `_default_credential()`은 성공하고, 실패는 `initialize_app`과
        마지막 `get_app`에서만 난다. 두 감싸기(자격증명 쪽 / 마지막 get_app 쪽)가 **서로를
        가려주기** 때문에, 각각을 독립으로 잠그려면 두 경로가 다 필요하다.
        """
        def failing_init(credential=None, options=None, name="[DEFAULT]"):
            raise ValueError("quota exceeded")

        self.fb.initialize_app = failing_init      # 기본 app은 그대로 둔다

        result = asyncio.run(FirebaseIdentityProvider()("perfectly-valid-uid"))
        self.assertIsInstance(result, IdentityMisconfigured)
        self.assertEqual(result.code, "APP_INIT_FAILED")

    def test_duplicate_initialisation_is_recovered_not_raised(self):
        """⛔ lock 밖에서 남이 먼저 만들었을 때 `ValueError`를 그대로 내면 `INVALID_UID`로 둔갑한다."""
        provider = FirebaseIdentityProvider()
        real_init = self.fb.initialize_app

        def racing_init(credential=None, options=None, name="[DEFAULT]"):
            self._apps.setdefault(name, f"app:{name}")   # 남이 먼저 만든 상황
            return real_init(credential, options, name)  # → ValueError

        self.fb.initialize_app = racing_init
        self.assertIsInstance(asyncio.run(provider(UID)), IdentityFound)


class _FakeDefaultApp:
    credential = object()


class TestIntegrationWithVerifierAndSingleFlight(unittest.IsolatedAsyncioTestCase):
    """⛔ codex 완료 게이트 2개 — **실제 `FirebaseIdentityProvider`를 검증기에 주입해서** 본다.

        (a) provider의 SDK timeout 뒤 공유 flight가 실제로 종료돼 `in_flight_count() == 0`
        (b) 이후 요청이 **새 provider 호출**로 성공

    ⚠️ 구 버전은 별도 `async def identity()` fake가 `IdentityUnavailable`을 그냥 돌려줬다.
    그러면 이 adapter의 `to_thread`·SDK timeout 종료·예외 분류가 single-flight와 어떻게 맞물리는지
    **하나도 검증되지 않는다**. 여기서는 **동기** `get_user` fake가 SDK 상한만큼 실제로 블록한 뒤
    SDK의 timeout 예외를 던진다 — 실물과 같은 모양이다.
    """

    async def test_sdk_timeout_releases_the_flight_and_the_next_request_succeeds(self):
        import threading

        from firebase_admin import exceptions

        from app.strict_cache import StrictObservationCache
        from app.strict_single_flight import StrictSingleFlight
        from app.strict_verifier import TemporarilyUnavailable, VerifiedActive, verify_strict
        from app.subscription import Determined

        cache, flight = StrictObservationCache(), StrictSingleFlight()
        sdk_timeout = 0.05
        calls = []
        in_thread = threading.Event()

        def get_user(uid, app=None):
            """동기 SDK 흉내 — 상한까지 **실제로 블록**한 뒤 timeout 예외를 낸다."""
            calls.append(uid)
            in_thread.set()
            time.sleep(sdk_timeout)
            if len(calls) == 1:
                raise exceptions.DeadlineExceededError("timeout", cause=None)
            return _FakeUser()

        provider = FirebaseIdentityProvider(get_user=get_user)

        async def premium(uid):
            return Determined(is_premium=True)

        class _Clock:
            def mono(self):
                return time.monotonic()

        def _verify():
            return verify_strict("uid-x", token_iat_seconds=1_700_000_000, clock=_Clock(),
                                 cache=cache, premium_provider=premium,
                                 identity_provider=provider, single_flight=flight)

        first = await _verify()
        self.assertIsInstance(first, TemporarilyUnavailable,
                              "SDK timeout이 3-state로 접히지 않았다")
        self.assertTrue(in_thread.is_set(), "to_thread 경로를 안 탔다")
        # (a) provider가 자기 상한 안에 끝냈으므로 flight도 끝나 있어야 한다
        self.assertEqual(flight.in_flight_count(), 0, "SDK timeout 뒤 flight가 슬롯을 안 놨다")

        # (b) 다음 요청은 **새 호출**로 성공한다
        second = await _verify()
        self.assertIsInstance(second, VerifiedActive)
        self.assertEqual(len(calls), 2, "새 provider 호출 없이 성공했다면 낡은 결과를 쓴 것이다")

    async def test_blocking_sdk_call_does_not_stall_the_event_loop(self):
        """⛔ 동기 SDK를 `to_thread` 없이 부르면 **broadcast 포함 전체 loop가 멈춘다**.

        이 리포의 broadcast는 매초 도는 loop 작업이다. 검증 한 건이 그 초를 잡아먹으면
        전 사용자 데이터가 밀린다.
        """
        from app.strict_cache import StrictObservationCache
        from app.strict_single_flight import StrictSingleFlight
        from app.strict_verifier import VerifiedActive, verify_strict
        from app.subscription import Determined

        cache, flight = StrictObservationCache(), StrictSingleFlight()

        def get_user(uid, app=None):
            time.sleep(0.15)               # 동기 SDK가 실제로 블록하는 시간
            return _FakeUser()

        provider = FirebaseIdentityProvider(get_user=get_user)

        async def premium(uid):
            return Determined(is_premium=True)

        class _Clock:
            def mono(self):
                return time.monotonic()

        ticks = []

        async def ticker():
            while True:
                await asyncio.sleep(0.005)
                ticks.append(1)

        verification = asyncio.create_task(
            verify_strict("uid-z", token_iat_seconds=1_700_000_000, clock=_Clock(), cache=cache,
                          premium_provider=premium, identity_provider=provider,
                          single_flight=flight)
        )
        tick_task = asyncio.create_task(ticker())
        result = await verification
        # ⚠️ **검증이 끝난 순간**에 세야 한다. ticker를 끝까지 기다린 뒤 세면 항상 20이라
        # 단언이 공허해진다(그렇게 썼다가 mutation이 생존했다).
        ticks_during_verification = len(ticks)
        tick_task.cancel()
        try:
            await tick_task
        except asyncio.CancelledError:
            pass

        self.assertIsInstance(result, VerifiedActive)
        self.assertGreaterEqual(
            ticks_during_verification, 10,
            f"동기 호출이 event loop를 붙잡았다 — 검증 중 tick {ticks_during_verification}회",
        )

    async def test_concurrent_requests_share_one_sdk_call(self):
        """실제 adapter를 통해서도 합치기가 동작하는가 — `to_thread`가 끼어도."""
        from app.strict_cache import StrictObservationCache
        from app.strict_single_flight import StrictSingleFlight
        from app.strict_verifier import VerifiedActive, verify_strict
        from app.subscription import Determined

        cache, flight = StrictObservationCache(), StrictSingleFlight()
        calls = []

        def get_user(uid, app=None):
            calls.append(uid)
            time.sleep(0.02)
            return _FakeUser()

        provider = FirebaseIdentityProvider(get_user=get_user)

        async def premium(uid):
            return Determined(is_premium=True)

        class _Clock:
            def mono(self):
                return time.monotonic()

        results = await asyncio.gather(*(
            verify_strict("uid-y", token_iat_seconds=1_700_000_000, clock=_Clock(), cache=cache,
                          premium_provider=premium, identity_provider=provider,
                          single_flight=flight)
            for _ in range(5)
        ))
        self.assertTrue(all(isinstance(r, VerifiedActive) for r in results))
        self.assertEqual(len(calls), 1, "실제 adapter 경로에서 합치기가 안 됐다")


if __name__ == "__main__":
    unittest.main()
