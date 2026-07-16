"""ADR-039 Step 3 — 무료 hourly snapshot 회귀 테스트.

중점: KRX 제외(보안 핵심 — rate/graph 양쪽 fail-closed) + payload 조립(as_of 시경계·asset 필터) + keep-last-good invariant.
"""

from datetime import datetime

import pytest

from app import free_snapshot, graph_v2


# ── 헬퍼 ─────────────────────────────────────────────────────────

def test_free_snapshot_key_namespace():
    assert free_snapshot.free_snapshot_key("usd", "3m") == "free:snapshot:usd:3m"


def test_snapshot_is_nonempty():
    good = {"rate": {"entries": [{"x": 1}]}, "graph": {"series": [{"data": [1]}]}}
    assert free_snapshot._snapshot_is_nonempty(good)
    # rate 비면 empty
    assert not free_snapshot._snapshot_is_nonempty({"rate": {"entries": []}, "graph": {"series": [{"data": [1]}]}})
    # 모든 series data 비면 empty
    assert not free_snapshot._snapshot_is_nonempty({"rate": {"entries": [{"x": 1}]}, "graph": {"series": [{"data": []}]}})


# ── KRX fail-closed (N6) ─────────────────────────────────────────

def test_assert_krx_free_raises_on_krx_series():
    payload = {"graph": {"series": [{"id": "krx.usd-krw-futures", "data": [1]}]}, "rate": {"entries": []}}
    with pytest.raises(ValueError):
        free_snapshot._assert_krx_free(payload)


def test_assert_krx_free_raises_on_krx_rate():
    payload = {"graph": {"series": []}, "rate": {"entries": [{"bank": "krx", "currency": "usd-krw-futures"}]}}
    with pytest.raises(ValueError):
        free_snapshot._assert_krx_free(payload)


def test_assert_krx_free_passes_clean():
    payload = {
        "graph": {"series": [{"id": "investing.usd", "data": [1]}]},
        "rate": {"entries": [{"bank": "kb", "currency": "usd-krw"}]},
    }
    free_snapshot._assert_krx_free(payload)  # no raise


def test_free_tab_series_excludes_krx():
    ids = graph_v2._free_tab_series("usd")
    assert "krx.usd-krw-futures" not in ids
    assert "investing.usd" in ids and "hana.usd" in ids and "dxy" in ids


def test_build_tab_exclude_krx_drops_krx_even_when_gate_open(monkeypatch):
    """게이트(G2/G3)가 열려도 exclude_krx=True면 KRX 제외 — 무료 fail-open 방어."""
    monkeypatch.setattr(graph_v2, "_krx_distribution_open", lambda: True)  # 게이트 열림
    monkeypatch.setattr(graph_v2, "_read_sdr_series", lambda db, sid, entry, s, e, g: {"id": sid, "data": []})
    monkeypatch.setattr(graph_v2, "_read_market_index_series", lambda db, sid, entry, s, e, g: {"id": sid, "data": []})

    free_ids = [s["id"] for s in graph_v2.build_tab(None, "usd", "3m", exclude_krx=True)["series"]]
    assert "krx.usd-krw-futures" not in free_ids

    # 대조: exclude_krx=False + 게이트 열림 → KRX 포함 (premium 경로 behavior-change-0)
    prem_ids = [s["id"] for s in graph_v2.build_tab(None, "usd", "3m", exclude_krx=False)["series"]]
    assert "krx.usd-krw-futures" in prem_ids


# ── payload 조립 (asset 필터 + as_of 시경계 + exclude_krx 전달) ──

def test_build_free_snapshot_payload_shapes(monkeypatch):
    fake_rates = [
        {"bank": "investing", "currency": "usd-krw", "rate": 1385.0, "timestamp": "t"},
        {"bank": "kb", "currency": "usd-krw", "rate": 1386.0, "timestamp": "t"},
        {"bank": "kb", "currency": "jpy-krw", "rate": 960.0, "timestamp": "t"},  # 다른 asset → 제외
    ]
    fake_graph = {
        "series": [
            {"id": "investing.usd", "data": [{"bucket_date": "2026-07-16", "rate": 1385.0}]},
            {"id": "dxy", "data": []},
        ],
        "metadata": {"bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
    }
    captured = {}

    def fake_build_tab(db, tab, period, today_kst=None, exclude_krx=False):
        captured["exclude_krx"] = exclude_krx
        captured["today_kst"] = today_kst
        return fake_graph

    monkeypatch.setattr(free_snapshot.crud, "get_all_rates_flat", lambda db: fake_rates)
    monkeypatch.setattr(free_snapshot.graph_v2, "build_tab", fake_build_tab)

    payload = free_snapshot.build_free_snapshot_payload(None, "usd", "3m")

    assert captured["exclude_krx"] is True   # 무료는 반드시 exclude_krx=True로 build_tab 호출
    # N5: graph range를 as_of.date()에 고정 (build_tab에 today_kst=as_of.date() 전달)
    assert captured["today_kst"] == datetime.fromisoformat(payload["as_of"]).date()
    assert payload["tab"] == "usd" and payload["period"] == "3m"
    assert payload["rate"]["asset"] == "usd-krw"
    assert len(payload["rate"]["entries"]) == 2   # jpy 제외
    assert all(e["currency"] == "usd-krw" for e in payload["rate"]["entries"])
    assert payload["graph"]["bucket_size"] == "1d"
    assert payload["graph"]["range"]["end"] == "2026-07-17"

    ao = datetime.fromisoformat(payload["as_of"])
    assert (ao.minute, ao.second, ao.microsecond) == (0, 0, 0)   # 시(hour) 경계 clamp
    # generated_at은 실행 시각(as_of 이상)
    assert datetime.fromisoformat(payload["generated_at"]) >= ao


# ── B1: serve-time 캐시 재검증 (오염 캐시 거부) ──────────────────

def test_validate_snapshot_payload():
    good = {
        "tab": "usd", "period": "3m",
        "rate": {"entries": [{"currency": "usd-krw"}]},
        "graph": {"series": [{"id": "investing.usd", "data": [1]}]},
    }
    assert free_snapshot.validate_snapshot_payload(good, "usd", "3m")
    # 오염/이상 payload는 전부 거부(→ rebuild)
    assert not free_snapshot.validate_snapshot_payload(None, "usd", "3m")
    assert not free_snapshot.validate_snapshot_payload([1, 2], "usd", "3m")
    assert not free_snapshot.validate_snapshot_payload(good, "jpy", "3m")   # tab mismatch
    assert not free_snapshot.validate_snapshot_payload(good, "usd", "1y")   # period mismatch
    assert not free_snapshot.validate_snapshot_payload(
        {"tab": "usd", "period": "3m", "rate": {"entries": []}, "graph": {"series": []}}, "usd", "3m")  # empty
    krx = {
        "tab": "usd", "period": "3m",
        "rate": {"entries": [{"currency": "usd-krw"}]},
        "graph": {"series": [{"id": "krx.usd-krw-futures", "data": [1]}]},
    }
    assert not free_snapshot.validate_snapshot_payload(krx, "usd", "3m")   # KRX 오염 → 거부(B1)
    # NB2: nested schema가 깨진 캐시(rate가 list)도 예외 아닌 False (fail-closed → rebuild)
    assert not free_snapshot.validate_snapshot_payload(
        {"tab": "usd", "period": "3m", "rate": [], "graph": {"series": "bad"}}, "usd", "3m")


# ── precompute keep-last-good (N2/N7) ──────────────────────────

class _FakeRedis:
    def __init__(self):
        self.setex_keys = []

    def ping(self):
        pass

    def setex(self, key, ttl, value):
        self.setex_keys.append(key)

    def close(self):
        pass


def _patch_precompute(monkeypatch, payload_fn):
    from contextlib import contextmanager
    import redis

    fake = _FakeRedis()
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: fake)

    @contextmanager
    def _noop_ctx():
        yield None

    monkeypatch.setattr(free_snapshot, "get_db_context", _noop_ctx)
    monkeypatch.setattr(free_snapshot, "build_free_snapshot_payload", payload_fn)
    return fake


def test_precompute_skips_setex_on_empty_build(monkeypatch):
    """빈 build → setex 하나도 안 함(이전 정상값 보존, keep-last-good)."""
    fake = _patch_precompute(monkeypatch, lambda db, tab, period: {
        "tab": tab, "period": period, "rate": {"entries": []}, "graph": {"series": []}})
    free_snapshot.precompute_free_snapshots()
    assert fake.setex_keys == []


def test_precompute_sets_all_on_nonempty(monkeypatch):
    """정상 build → FREE_SNAPSHOT_TABS×PERIODS 전부 SET."""
    fake = _patch_precompute(monkeypatch, lambda db, tab, period: {
        "tab": tab, "period": period,
        "rate": {"entries": [{"currency": "usd-krw"}]},
        "graph": {"series": [{"id": "investing.usd", "data": [1]}]}})
    free_snapshot.precompute_free_snapshots()
    expected = {free_snapshot.free_snapshot_key(t, p)
                for t in free_snapshot.FREE_SNAPSHOT_TABS
                for p in free_snapshot.FREE_SNAPSHOT_PERIODS}
    assert set(fake.setex_keys) == expected


def test_precompute_isolates_failure_keeps_others(monkeypatch):
    """한 (tab,period) build가 raise해도 나머지는 SET(격리 + keep-last-good)."""
    def flaky(db, tab, period):
        if period == "3m":
            raise RuntimeError("boom")
        return {"tab": tab, "period": period,
                "rate": {"entries": [{"currency": "usd-krw"}]},
                "graph": {"series": [{"id": "investing.usd", "data": [1]}]}}

    fake = _patch_precompute(monkeypatch, flaky)
    free_snapshot.precompute_free_snapshots()
    assert free_snapshot.free_snapshot_key("usd", "3m") not in fake.setex_keys   # 실패한 것만 빠짐
    assert free_snapshot.free_snapshot_key("usd", "1w") in fake.setex_keys       # 나머지는 SET
