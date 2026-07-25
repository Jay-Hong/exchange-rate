"""무인증 graph v2 표면의 KRX fail-closed 게이트 (ADR-039 §3.1 / §6.1, 2026-07-26).

`GET /api/v2/graph/tab`·`/catalog`은 **인증이 없는데** series 목록을 `krx_gates_open()`(G2∧G3)
**만으로** 정했다. 즉 `KRX_CLIENT_DISTRIBUTION_ENABLED=true`로 되돌리는 순간 무인증 caller가
KRX 그래프 series를 받는다 — E3가 REST twin에서 막은 것과 **같은 누수**가 sibling에 남아 있었다.

이 파일이 잠그는 것:
1. **전역 게이트를 전부 열어도** 무인증 graph 표면에 `krx.*`가 나타나지 않는다(3m/1y/1w + 1d + catalog)
2. 그 이유가 `krx_unauthenticated_graph_exposure_allowed()` 상수라는 것 — 이 값을 True로 바꾸면
   여기가 red가 되어 "per-user 게이트가 실제로 있는가"를 묻게 된다
3. 내용 레벨 검사 — series 목록뿐 아니라 **응답 JSON 어디에도** 'krx' 문자열이 없다
   (무료 snapshot의 `_assert_krx_free`와 같은 belt-and-suspenders)

⚠️ 전역 게이트 patch는 **3개**가 필요하다(`KRX_FUTURES_ENABLED` ∧ `KRX_CLIENT_DISTRIBUTION_ENABLED`
= runtime 조합). 하나라도 빠지면 게이트가 애초에 닫혀 있어 이 테스트가 vacuous해진다 —
`test_gates_are_actually_open_in_fixture`가 그걸 막는다.
"""
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import config, entitlements
from app.main import app


def _gates_open():
    """G2∧G3 전역 게이트를 실제로 연다(= 누수 조건 재현)."""
    return (
        patch.object(config, "KRX_FUTURES_ENABLED", True),
        patch.object(config, "KRX_CLIENT_DISTRIBUTION_ENABLED", True),
    )


class TestUnauthenticatedGraphKrxFailClosed(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_gates_are_actually_open_in_fixture(self):
        """이 파일의 다른 테스트가 vacuous하지 않음을 보증 — 전역 게이트는 실제로 열려 있다."""
        a, b = _gates_open()
        with a, b:
            self.assertTrue(entitlements.krx_gates_open(),
                            "전역 게이트가 안 열리면 아래 검사들이 무의미해진다")

    #: 사용자에게 실제로 서빙되는 진입점 — 여기 빠지면 그 경로가 통째로 무방비다.
    #: (도출이 0건이 되는 vacuous 실패를 막는 하한이기도 하다.)
    _REQUIRED_ENTRY_POINTS = {
        "app.graph_v2.build_catalog",
        "app.graph_v2.build_tab",
        "app.graph_v2.strip_krx_if_not_allowed",
        "app.graph_v2_intraday.build_tab_1d_payload",
        "app.graph_v2_intraday.build_tab_1d_in_progress",
    }

    def test_every_krx_visible_function_defaults_to_hidden(self):
        """`krx_visible`을 받는 **모든** 함수의 default가 False + keyword-only인지.

        목록을 **하드코딩하지 않고 모듈에서 도출**한다:
        (a) 새 함수를 추가하며 목록에 넣는 걸 잊으면 그 경로가 조용히 무방비가 되고,
        (b) 하드코딩 목록은 개수를 보고할 때 틀리기 쉽다(실제로 세 번 틀렸다 — codex 지적).
        도출하면 둘 다 사라진다.

        keyword-only까지 보는 이유: positional로 받을 수 있으면 인자 순서 실수로 의도치 않게
        True가 들어갈 수 있다.
        """
        import inspect
        from app import graph_v2, graph_v2_intraday

        discovered = set()
        for module in (graph_v2, graph_v2_intraday):
            for name, fn in sorted(vars(module).items()):
                if not inspect.isfunction(fn) or fn.__module__ != module.__name__:
                    continue
                param = inspect.signature(fn).parameters.get("krx_visible")
                if param is None:
                    continue
                qualified = f"{module.__name__}.{name}"
                discovered.add(qualified)
                with self.subTest(fn=qualified):
                    self.assertIs(param.default, False,
                                  f"{qualified} default가 fail-closed(False)가 아니다")
                    self.assertIs(param.kind, inspect.Parameter.KEYWORD_ONLY,
                                  f"{qualified} krx_visible이 keyword-only가 아니다")

        missing = self._REQUIRED_ENTRY_POINTS - discovered
        self.assertFalse(missing, f"krx_visible 파라미터가 없는 진입점: {sorted(missing)}")

    def test_long_period_series_exclude_krx_even_with_gates_open(self):
        from app.graph_v2 import _effective_tab_series, _effective_default_visible
        a, b = _gates_open()
        with a, b:
            for tab in ("usd", "tether"):
                with self.subTest(tab=tab):
                    self.assertFalse(
                        [s for s in _effective_tab_series(tab) if s.startswith("krx.")])
                    self.assertFalse(
                        [s for s in _effective_default_visible(tab) if s.startswith("krx.")])

    def test_intraday_1d_series_exclude_krx_even_with_gates_open(self):
        from app.graph_v2_intraday import tab_1d_specs, tab_1d_default_visible
        a, b = _gates_open()
        with a, b:
            for tab in ("usd", "tether"):
                with self.subTest(tab=tab):
                    self.assertFalse(
                        [s for s in tab_1d_specs(tab) if s["id"].startswith("krx.")])
                    self.assertFalse(
                        [s for s in tab_1d_default_visible(tab) if s.startswith("krx.")])

    def test_catalog_response_has_no_krx_anywhere(self):
        """내용 레벨 — series 목록뿐 아니라 응답 JSON 전체에 krx 문자열이 없어야 한다."""
        a, b = _gates_open()
        with a, b:
            r = self.client.get("/api/v2/graph/catalog")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("krx", json.dumps(r.json()).lower())

    def test_catalog_still_serves_other_series(self):
        """과잉 필터 회귀 차단 — krx만 빠지고 나머지는 그대로."""
        a, b = _gates_open()
        with a, b:
            body = self.client.get("/api/v2/graph/catalog").json()
        usd = next(t for t in body["tabs"] if t["id"] == "usd")
        all_series = usd["periods"]["3m"]["all_series"]
        self.assertIn("investing.usd", all_series)
        self.assertIn("hana.usd", all_series)


class TestCachedPayloadCannotBypassGate(unittest.TestCase):
    """**캐시 hit 우회 차단** (codex Major, 2026-07-26).

    `/api/v2/graph/tab`은 Redis read-through 캐시라 **cache hit은 build를 안 거친다**.
    승인 flag가 true였을 때 구워진 payload는 flag를 끈 뒤에도 TTL(최대 30분) 동안 살아 있고,
    startup DEL은 예외를 비치명으로 흡수해서(main.py) 최종 방어선이 못 된다.
    → serve-time `strip_krx_if_not_allowed`가 3경로 공통 exit에서 한 번 더 막는다.

    여기서는 **오염된 캐시를 직접 주입**해 재현한다(빌더를 거치지 않는 경로).
    """

    _KRX_SERIES = {"id": "krx.usd-krw-futures", "data": [{"t": "2026-07-01", "c": 1380.0}]}

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def _poisoned(self, extra=None):
        payload = {"tab": "usd", "period": "3m", "bucket_size": "1d",
                   "series": [{"id": "hana.usd", "data": []}, dict(self._KRX_SERIES)],
                   "metadata": {}}
        payload.update(extra or {})
        return json.dumps(payload)

    def _get(self, period, cache_map, builders):
        """Redis가 key별로 오염된 payload를 돌려주는 상황 — 빌더는 호출되지 않는다.

        ⚠️ key 무관 단일 반환값을 쓰면 1d의 `in_progress` 조회까지 같은 값을 받아
        krx 키가 애초에 없어 **테스트가 vacuous**해진다 → key별 side_effect 필수.
        """
        async def _fake_get(key):
            return cache_map.get(key)

        a, b = _gates_open()
        stack = [a, b,
                 patch("app.main.redis_cache.get", new=_fake_get),
                 patch("app.main.redis_cache.set", new=AsyncMock(return_value=None))]
        spies = {}
        for name in builders:
            pt = patch(name)
            spies[name] = pt
            stack.append(pt)
        started = [c.__enter__() if hasattr(c, "__enter__") else None for c in stack]
        try:
            r = self.client.get("/api/v2/graph/tab",
                                params={"tab": "usd", "period": period})
        finally:
            for c in reversed(stack):
                if hasattr(c, "__exit__"):
                    c.__exit__(None, None, None)
        return r, dict(zip(builders, started[len(stack) - len(builders):]))

    def test_long_period_cached_krx_is_stripped_at_serve_time(self):
        r, spies = self._get(
            "3m", {"graph_v2:tab:usd:3m": self._poisoned()}, ["app.graph_v2.build_tab"])
        self.assertEqual(r.status_code, 200)
        # 캐시 hit 경로임을 보증(= 이 테스트가 vacuous하지 않음)
        spies["app.graph_v2.build_tab"].assert_not_called()
        ids = [s["id"] for s in r.json()["series"]]
        self.assertNotIn("krx.usd-krw-futures", ids)
        self.assertIn("hana.usd", ids)     # 과잉 필터 아님

    def test_intraday_cached_krx_series_and_seed_are_stripped(self):
        """1d는 closed series + `in_progress` seed **두 shape** 모두 훑어야 한다."""
        import time
        from app.graph_v2_intraday import _bucket_align
        fresh_boundary = _bucket_align(int(time.time()))
        closed = self._poisoned({
            "period": "1d",
            "_in_progress_start_ts": fresh_boundary,   # fresh로 판정시켜 rebuild 회피
        })
        seed = json.dumps({
            "krx.usd-krw-futures": {"bucket_start": "x", "high": 1, "low": 1, "close": 1},
            "hana.usd": {"bucket_start": "x", "high": 2, "low": 2, "close": 2},
        })
        r, spies = self._get(
            "1d",
            {"graph_v2:tab:usd:1d": closed, "graph_v2:tab:usd:1d:in_progress": seed},
            ["app.graph_v2_intraday.build_tab_1d_payload",
             "app.graph_v2_intraday.build_tab_1d_in_progress"])
        self.assertEqual(r.status_code, 200)
        for name, spy in spies.items():
            spy.assert_not_called()        # 두 경로 다 캐시 hit — vacuous 아님
        body = r.json()
        self.assertNotIn("krx.usd-krw-futures", [s["id"] for s in body["series"]])
        self.assertIn("hana.usd", body["in_progress"])          # seed가 실제로 실렸고
        self.assertNotIn("krx.usd-krw-futures", body["in_progress"])  # krx만 빠졌다

    def test_strip_is_a_noop_for_entitled_callers(self):
        """대조군 — `krx_visible=True`면 필터가 걸리지 않는다(과잉 차단 아님).

        per-user 게이트가 land하면 endpoint가 이 값을 넘기게 된다.
        """
        import json as _json
        from app.graph_v2 import strip_krx_if_not_allowed
        payload = _json.loads(self._poisoned())
        kept = strip_krx_if_not_allowed(payload, krx_visible=True)
        self.assertIs(kept, payload)                       # copy조차 만들지 않는다
        self.assertIn("krx.usd-krw-futures", [s["id"] for s in kept["series"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
