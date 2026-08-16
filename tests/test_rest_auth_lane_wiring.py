"""S1b — `verify_firebase_token` 이 **REST lane 과 `rest-auth` app 으로** 나가는지.

## 왜 별 파일인가

`tests/test_firebase_auth_mapping.py` 는 예외 → HTTP 상태 **매핑**을 소유한다. 이 파일은
**배선**을 소유한다 — 어느 pool 로, 어느 firebase app 으로, 어느 스레드에서 나가는가.
둘을 섞으면 매핑 테스트가 배선을 증명한다는 착각이 생긴다.

## ⛔ 이 계약을 잠그는 것은 여기뿐이다

21개 REST endpoint 테스트는 `verify_firebase_token` 을 **통째로 patch** 한다(실측:
`tests/test_free_snapshot_endpoint.py` · `tests/test_comparison_api.py` 등에서
`patch("app.main.verify_firebase_token", new=AsyncMock(...))`). 그래서 helper 안에서
`app=` 이 빠지든 WS lane 으로 새든 **그 테스트들은 전부 초록이다**. 배선 회귀를 잡는 것은
이 파일이 유일하다.

## 무엇이 조용히 깨질 수 있는가

- `app=` 누락 → DEFAULT app 이 쓰여 **낮춘 `httpTimeout` 이 통째로 no-op**. 로그·타이밍
  어디에도 흔적이 없다 — 호출 인자만이 증거다.
- `run_in_rest_auth_executor` → `run_in_auth_executor` → WS 와 pool 을 공유해 **격리가 사라진다**.
  동작은 정상이라 아무 증상이 없다.
- `asyncio.to_thread` 로 회귀 → 기본 executor 오염(atomic loader/store, DB selector 와 동거).
- `check_revoked` 하드코딩 → `DELETE /api/user/me` 의 revoke 검사가 죽는다.
"""
import asyncio
import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from firebase_admin import auth as fb_auth

from app import auth_executor
from app.main import verify_firebase_token


def _request(auth_header: str | None = "Bearer tok") -> MagicMock:
    request = MagicMock()
    request.headers = {} if auth_header is None else {"Authorization": auth_header}
    return request


class _LaneBase(unittest.IsolatedAsyncioTestCase):
    """REST lane 을 **실제로** 띄운다 — executor 를 흉내내면 배선이 검증되지 않는다."""

    REST_APP = object()

    async def asyncSetUp(self):
        # ⛔ **앞선 테스트 상태에 기대지 않는다.** module-global lane 이라, 다른 파일이 남긴
        #    상태에 따라 같은 테스트가 통과/실패로 갈렸다(실측: 파일 순서만 바꿔도 실패 노드가
        #    달라졌다). 알려진 baseline(정지)에서 시작해 필요한 것만 띄운다.
        await self._stop()
        auth_executor.start_rest_auth_executor(2)
        self.addAsyncCleanup(self._stop)

    async def _stop(self):
        await auth_executor.await_rest_auth_executor_shutdown(
            auth_executor.begin_rest_auth_executor_shutdown()
        )

    async def _call(self, *, spy=None, check_revoked=False, rest_app=..., header="Bearer tok"):
        sdk = MagicMock(side_effect=spy) if spy else MagicMock(return_value={"uid": "u1"})
        app_obj = self.REST_APP if rest_app is ... else rest_app
        with patch.object(fb_auth, "verify_id_token", sdk), \
             patch("app.main.is_firebase_initialized", return_value=True), \
             patch("app.notifications.fcm.rest_auth_app", return_value=app_obj):
            result = await verify_firebase_token(_request(header), check_revoked=check_revoked)
        return result, sdk


class TestNamedAppBinding(_LaneBase):
    async def test_sdk_receives_the_rest_named_app_by_identity(self):
        """⛔ `is` 로 본다 — truthy 검사는 `app=` 누락을 못 잡는다(둘 다 통과한다)."""
        _, sdk = await self._call()
        self.assertEqual(sdk.call_count, 1)
        self.assertIn("app", sdk.call_args.kwargs, "`app=` 가 전달되지 않았다 — DEFAULT app 이 쓰인다")
        self.assertIs(sdk.call_args.kwargs["app"], self.REST_APP)

    async def test_token_is_passed_positionally_and_unchanged(self):
        _, sdk = await self._call()
        self.assertEqual(sdk.call_args.args, ("tok",))

    async def test_check_revoked_is_passed_through_both_ways(self):
        """⛔ 하드코딩하면 `DELETE /api/user/me` 의 revoke 검사가 죽는다 — 유일한 True 호출부다."""
        for value in (False, True):
            with self.subTest(check_revoked=value):
                _, sdk = await self._call(check_revoked=value)
                self.assertIs(sdk.call_args.kwargs["check_revoked"], value)

    async def test_missing_named_app_is_503_not_401(self):
        """미준비는 **판정 불가**다. 401 로 접으면 클라가 무의미한 재인증을 한다."""
        with self.assertRaises(HTTPException) as ctx:
            await self._call(rest_app=None)
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail),
                         (503, "Firebase auth unavailable"))


class TestOffLoopExecution(_LaneBase):
    async def test_sdk_runs_on_a_rest_auth_worker_not_the_loop_thread(self):
        """⛔ **두 가지를 함께** 단언한다.

        - loop thread 가 아니다 → 맨 동기 호출 회귀를 잡는다.
        - 스레드 이름이 `rest-auth` 다 → `to_thread`(기본 executor) 회귀와 WS lane 오배선을
          잡는다. 앞의 단언만으로는 그 둘이 **전부 통과한다**.
        """
        seen: dict[str, str] = {}

        def spy(*_a, **_k):
            seen["thread"] = threading.current_thread().name
            return {"uid": "u1"}

        loop_thread = threading.current_thread().name
        await self._call(spy=spy)
        self.assertIn("thread", seen, "SDK 가 호출되지 않았다")
        self.assertNotEqual(seen["thread"], loop_thread, "이벤트 루프 스레드에서 돌았다")
        self.assertTrue(seen["thread"].startswith("rest-auth"),
                        f"REST lane 이 아니다: {seen['thread']}")

    async def test_the_event_loop_keeps_running_while_the_sdk_blocks(self):
        """⛔ "await 했다"는 비차단의 증거가 아니다 — 동기 검증이 **막고 있는 동안** 다른
        coroutine 이 실제로 진행하는지 본다."""
        release = threading.Event()
        progressed = asyncio.Event()

        def spy(*_a, **_k):
            release.wait(timeout=5)
            return {"uid": "u1"}

        async def other():
            await asyncio.sleep(0)
            progressed.set()
            release.set()

        task = asyncio.create_task(other())
        await self._call(spy=spy)
        await task
        self.assertTrue(progressed.is_set(), "SDK 가 도는 동안 루프가 멈췄다")


class TestLaneSelection(_LaneBase):
    async def test_ws_lane_does_not_see_rest_traffic(self):
        """⛔ WS lane 으로 새면 격리가 사라지는데 **동작은 정상**이라 증상이 없다.

        계측이 유일한 관측면이다: REST 호출 뒤 WS 제출 수는 변하지 않아야 한다.
        """
        auth_executor.start_auth_executor(2)

        async def stop_ws():
            await auth_executor.await_auth_executor_shutdown(
                auth_executor.begin_auth_executor_shutdown())

        self.addAsyncCleanup(stop_ws)
        before_ws = auth_executor.auth_executor_metrics()
        before_rest = auth_executor.rest_auth_executor_metrics()
        await self._call()
        after_ws = auth_executor.auth_executor_metrics()
        after_rest = auth_executor.rest_auth_executor_metrics()
        self.assertEqual(after_ws, before_ws, "REST 호출이 WS 계측을 움직였다 — pool 이 공유됐다")
        self.assertNotEqual(after_rest, before_rest, "REST 계측이 전혀 움직이지 않았다")


class TestDormantLane(unittest.IsolatedAsyncioTestCase):
    """lane 을 **띄우지 않은** 상태 — 위 base 와 달리 start 하지 않는다."""

    async def asyncSetUp(self):
        # ⛔ 이 클래스는 lane 을 **띄우지 않는다**. 다만 baseline 은 명시적으로 만든다 —
        #    앞선 테스트가 띄워둔 채 넘겨주면 dormant 계약이 조용히 검증되지 않는다.
        await self._stop()
        self.addAsyncCleanup(self._stop)

    async def _stop(self):
        await auth_executor.await_rest_auth_executor_shutdown(
            auth_executor.begin_rest_auth_executor_shutdown()
        )

    async def test_not_started_lane_is_503_not_500(self):
        """⛔ `AuthExecutorNotReady` 절이 Firebase 분류기 **뒤**에 있으면 사다리가 모르는
        타입이라 재전파(500)가 된다 — 우리 준비 상태를 서버 버그로 위장한다."""
        self.assertFalse(auth_executor.is_rest_auth_executor_running())
        sdk = MagicMock(return_value={"uid": "u1"})
        with patch.object(fb_auth, "verify_id_token", sdk), \
             patch("app.main.is_firebase_initialized", return_value=True), \
             patch("app.notifications.fcm.rest_auth_app", return_value=object()):
            with self.assertRaises(HTTPException) as ctx:
                await verify_firebase_token(_request(), check_revoked=False)
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail),
                         (503, "Firebase auth unavailable"))
        self.assertEqual(sdk.call_count, 0, "미준비 lane 에서 SDK 가 불렸다")

    async def test_preflight_failures_never_submit(self):
        """⛔ 헤더 누락·app 미준비는 **제출 전에** 끝나야 한다. 제출하고 나서 거절하면
        미준비 상태에서도 pool 이 소모된다."""
        auth_executor.start_rest_auth_executor(2)
        # asyncSetUp 이 등록한 `_stop` 이 이 테스트에서 시작한 lane 도 종료한다.
        sdk = MagicMock(return_value={"uid": "u1"})
        cases = [
            ("헤더 없음", dict(header=None, app=object())),
            ("app 미준비", dict(header="Bearer tok", app=None)),
        ]
        for label, kw in cases:
            with self.subTest(case=label):
                before = auth_executor.rest_auth_executor_metrics()
                with patch.object(fb_auth, "verify_id_token", sdk), \
                     patch("app.main.is_firebase_initialized", return_value=True), \
                     patch("app.notifications.fcm.rest_auth_app", return_value=kw["app"]):
                    with self.assertRaises(HTTPException):
                        await verify_firebase_token(_request(kw["header"]), check_revoked=False)
                self.assertEqual(auth_executor.rest_auth_executor_metrics(), before,
                                 "preflight 실패인데 lane 계측이 움직였다")
                self.assertEqual(sdk.call_count, 0)


class TestNoBypass(unittest.TestCase):
    """`auth.verify_id_token` 직접 호출은 **두 곳뿐**이어야 한다."""

    def test_direct_sdk_calls_are_confined_to_the_two_verifiers(self):
        """⛔ 새 endpoint 가 helper 를 우회해 직접 부르면 lane·app·분류를 통째로 건너뛴다.

        ⚠️ 이름 기준이라 **참조(인자 전달)까지** 센다 — S1b 에서 REST 는 호출이 아니라
           executor 로 넘기는 인자가 됐다. 둘 다 "이 심볼을 쓴다"는 같은 사실이다.
        """
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path("app/main.py").read_text())
        owners = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(isinstance(n, ast.Attribute) and n.attr == "verify_id_token"
                   for n in ast.walk(fn)):
                owners.add(fn.name)
        self.assertEqual(owners, {"verify_firebase_token", "verify_ws_subscribe_token"},
                         f"SDK 를 직접 만지는 함수가 바뀌었다: {sorted(owners)}")
