"""Step 4B (KRX 전환) 단위 — fut_bydd_trd HTTP helper 테스트.

network 0 (get_fn/sleep_fn/now_fn 주입). 실제 KRX 호출은 운영 dry-run 별도 GO.
중심: status 직접 분기(429/non-200/200) / JSON 실패 hard / **auth_key 미노출** / throttle 결정론.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_krx_openapi_source_daily_rates as K  # noqa: E402

_AUTH = "SECRET_AUTH_KEY_VALUE_DO_NOT_LEAK_98765"
_BAS = date(2026, 5, 18)


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, json_raises=False):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {"OutBlock_1": []}
        self.headers = headers or {}
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("Expecting value: line 1 column 1")
        return self._json_data


class _FakeGet:
    """requests.get 시그니처 mock — 호출 인자 기록."""
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return self.response


class TestFetchKrxFutByddTrd(unittest.TestCase):
    def test_normal_returns_parsed_rows(self):
        rows = [{"BAS_DD": "20260518", "PROD_NM": "미국달러 선물"}]
        get_fn = _FakeGet(_FakeResponse(200, {"OutBlock_1": rows}))
        out = K.fetch_krx_fut_bydd_trd(_BAS, _AUTH, get_fn=get_fn)
        self.assertEqual(out, rows)

    def test_sends_url_params_headers_timeout(self):
        get_fn = _FakeGet(_FakeResponse(200))
        K.fetch_krx_fut_bydd_trd(_BAS, _AUTH, get_fn=get_fn, timeout=15.0)
        call = get_fn.calls[0]
        self.assertEqual(call["url"], K.KRX_FUT_BYDD_TRD_URL)
        self.assertTrue(call["url"].startswith("https://"))   # redirect 무의존
        self.assertEqual(call["params"], {"basDd": "20260518"})
        self.assertEqual(call["headers"], {"AUTH_KEY": _AUTH})  # auth_key는 헤더로만 전달
        self.assertEqual(call["timeout"], 15.0)

    def test_429_raises_abort_with_retry_after(self):
        get_fn = _FakeGet(_FakeResponse(429, headers={"Retry-After": "120"}))
        with self.assertRaises(K.KrxRateLimitAbort) as cm:
            K.fetch_krx_fut_bydd_trd(_BAS, _AUTH, get_fn=get_fn)
        msg = str(cm.exception)
        self.assertIn("429", msg)
        self.assertIn("Retry-After=120", msg)
        self.assertNotIn(_AUTH, msg)  # auth_key 미노출

    def test_429_is_runtimeerror_subclass(self):
        # run_krx_range_dry_run의 except (RuntimeError)가 잡을 수 있어야 graceful FAIL
        self.assertTrue(issubclass(K.KrxRateLimitAbort, RuntimeError))

    def test_non200_raises_runtime(self):
        get_fn = _FakeGet(_FakeResponse(500))
        with self.assertRaises(RuntimeError) as cm:
            K.fetch_krx_fut_bydd_trd(_BAS, _AUTH, get_fn=get_fn)
        msg = str(cm.exception)
        self.assertIn("500", msg)
        self.assertNotIn(_AUTH, msg)

    def test_json_fail_raises_runtime_no_body(self):
        get_fn = _FakeGet(_FakeResponse(200, json_raises=True))
        with self.assertRaises(RuntimeError) as cm:
            K.fetch_krx_fut_bydd_trd(_BAS, _AUTH, get_fn=get_fn)
        msg = str(cm.exception)
        self.assertIn("JSON parse 실패", msg)
        self.assertNotIn(_AUTH, msg)
        self.assertNotIn("line 1 column 1", msg)  # body snippet 미노출 (type만)

    def test_auth_key_never_in_exception_all_error_statuses(self):
        # 401 / 429 / 500 / json 실패 전부에서 auth_key 미노출 잠금
        cases = [
            _FakeResponse(401),
            _FakeResponse(429, headers={"Retry-After": "1"}),
            _FakeResponse(500),
            _FakeResponse(200, json_raises=True),
        ]
        for resp in cases:
            with self.assertRaises(RuntimeError) as cm:  # KrxRateLimitAbort도 RuntimeError
                K.fetch_krx_fut_bydd_trd(_BAS, _AUTH, get_fn=_FakeGet(resp))
            self.assertNotIn(_AUTH, str(cm.exception))

    def test_invalid_outblock_raises(self):
        # parse_krx_fut_response 검증 위임 — OutBlock_1 없으면 ValueError
        get_fn = _FakeGet(_FakeResponse(200, {"wrong": []}))
        with self.assertRaises(ValueError):
            K.fetch_krx_fut_bydd_trd(_BAS, _AUTH, get_fn=get_fn)


class TestMakeKrxFetchFn(unittest.TestCase):
    def _ok_get(self):
        return _FakeGet(_FakeResponse(200, {"OutBlock_1": []}))

    def test_first_call_no_throttle(self):
        sleeps = []
        nows = iter([100.0])  # 첫 호출: last_start set 1회만
        fetch = K.make_krx_fetch_fn(
            _AUTH, get_fn=self._ok_get(), sleep_fn=sleeps.append, now_fn=lambda: next(nows),
            min_interval_sec=1.0,
        )
        fetch(date(2026, 5, 18))
        self.assertEqual(sleeps, [])  # 첫 요청 무throttle

    def test_second_call_throttles(self):
        sleeps = []
        # c1 set=100.0 / c2 elapsed=100.3(<1.0 → sleep 0.7) / c2 set=101.0
        nows = iter([100.0, 100.3, 101.0])
        fetch = K.make_krx_fetch_fn(
            _AUTH, get_fn=self._ok_get(), sleep_fn=sleeps.append, now_fn=lambda: next(nows),
            min_interval_sec=1.0,
        )
        fetch(date(2026, 5, 18))
        fetch(date(2026, 5, 19))
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.7)  # min_interval - elapsed

    def test_no_sleep_when_interval_elapsed(self):
        sleeps = []
        # c1 set=100.0 / c2 elapsed=102.0(>=1.0 → no sleep) / c2 set=102.0
        nows = iter([100.0, 102.0, 102.0])
        fetch = K.make_krx_fetch_fn(
            _AUTH, get_fn=self._ok_get(), sleep_fn=sleeps.append, now_fn=lambda: next(nows),
            min_interval_sec=1.0,
        )
        fetch(date(2026, 5, 18))
        fetch(date(2026, 5, 19))
        self.assertEqual(sleeps, [])

    def test_propagates_429_abort(self):
        get_fn = _FakeGet(_FakeResponse(429, headers={"Retry-After": "5"}))
        fetch = K.make_krx_fetch_fn(
            _AUTH, get_fn=get_fn, sleep_fn=lambda s: None, now_fn=lambda: 0.0,
        )
        with self.assertRaises(K.KrxRateLimitAbort):
            fetch(date(2026, 5, 18))


if __name__ == "__main__":
    unittest.main()
