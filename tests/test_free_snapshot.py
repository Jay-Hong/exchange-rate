"""ADR-039 Step 3 — 무료 hourly snapshot 회귀 테스트.

중점: KRX 제외(보안 핵심 — rate/graph 양쪽 fail-closed) + 시간 계약(as_of=HH:30 basis +
`timestamp <= as_of` cutoff — codex 2026-07-18) + payload 조립 + keep-last-good invariant.
"""

from datetime import datetime, timedelta

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


# ── payload 조립 (cutoff rate + as_of HH:30 경계 + exclude_krx 전달) ──

def test_build_free_snapshot_payload_shapes(monkeypatch):
    fake_rates = [
        {"bank": "investing", "currency": "usd-krw", "rate": 1385.0, "timestamp": "t"},
        {"bank": "kb", "currency": "usd-krw", "rate": 1386.0, "timestamp": "t"},
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

    def fake_fetch_until(db, asset, as_of):
        captured["cutoff_asset"] = asset
        captured["cutoff_as_of"] = as_of
        return [e for e in fake_rates if e["currency"] == asset]

    monkeypatch.setattr(free_snapshot, "fetch_rate_entries_until", fake_fetch_until)
    monkeypatch.setattr(free_snapshot.graph_v2, "build_tab", fake_build_tab)

    payload = free_snapshot.build_free_snapshot_payload(None, "usd", "3m")

    assert captured["exclude_krx"] is True   # 무료는 반드시 exclude_krx=True로 build_tab 호출
    # N5: graph range를 as_of.date()에 고정 (build_tab에 today_kst=as_of.date() 전달)
    assert captured["today_kst"] == datetime.fromisoformat(payload["as_of"]).date()
    # 시간 계약: rate는 as_of cutoff 조회로, payload as_of와 동일 경계 전달
    assert captured["cutoff_asset"] == "usd-krw"
    assert captured["cutoff_as_of"].isoformat() == payload["as_of"]
    assert payload["tab"] == "usd" and payload["period"] == "3m"
    assert payload["rate"]["asset"] == "usd-krw"
    assert len(payload["rate"]["entries"]) == 2
    assert all(e["currency"] == "usd-krw" for e in payload["rate"]["entries"])
    assert payload["graph"]["bucket_size"] == "1d"
    assert payload["graph"]["range"]["end"] == "2026-07-17"

    ao = datetime.fromisoformat(payload["as_of"])
    assert (ao.minute, ao.second, ao.microsecond) == (30, 0, 0)   # HH:30 basis 경계
    # generated_at은 실행 시각(as_of 이상)
    assert datetime.fromisoformat(payload["generated_at"]) >= ao


def test_assert_graph_within_as_of():
    """SET 전 fail-closed — graph point ts가 as_of 초과면 raise(백필/오염 우회 차단, codex 시간 계약)."""
    base = {
        "as_of": "2026-07-18T21:30:00+09:00",
        "graph": {"series": [{"id": "investing.usd", "data": [
            {"ts": "2026-07-18T21:00:00+09:00", "rate": 1400.0},   # 이하 → OK
            {"ts": "2026-07-18T21:30:00+09:00", "rate": 1401.0},   # 정확히 경계 → OK (<=)
        ]}]},
    }
    free_snapshot._assert_graph_within_as_of(base)   # 통과

    over = {
        "as_of": "2026-07-18T21:30:00+09:00",
        "graph": {"series": [{"id": "hana.usd", "data": [
            {"ts": "2026-07-18T22:00:00+09:00", "rate": 1402.0},   # 초과 → raise
        ]}]},
    }
    with pytest.raises(ValueError):
        free_snapshot._assert_graph_within_as_of(over)

    # ts 없는 point(비-ts 스키마)는 이 함수 영역 아님 — 통과
    free_snapshot._assert_graph_within_as_of(
        {"as_of": "2026-07-18T21:30:00+09:00",
         "graph": {"series": [{"id": "x", "data": [{"bucket_date": "2026-07-18", "rate": 1.0}]}]}})


def test_basis_as_of_boundaries():
    """마지막 HH:30 경계 — minute>=30이면 이번 시, <30이면 직전 시(자정 경계 포함)."""
    def kst(y, mo, d, h, mi, s=0):
        return free_snapshot.KST.localize(datetime(y, mo, d, h, mi, s))

    assert free_snapshot.basis_as_of(kst(2026, 7, 18, 21, 45)) == kst(2026, 7, 18, 21, 30)
    assert free_snapshot.basis_as_of(kst(2026, 7, 18, 21, 30)) == kst(2026, 7, 18, 21, 30)   # 정확히 :30
    assert free_snapshot.basis_as_of(kst(2026, 7, 18, 21, 29)) == kst(2026, 7, 18, 20, 30)
    assert free_snapshot.basis_as_of(kst(2026, 7, 18, 0, 10)) == kst(2026, 7, 17, 23, 30)    # 자정 경계


def test_fetch_rate_entries_until_cutoff_boundary():
    """시간 계약(codex): `timestamp <= as_of`인 마지막 값만 — as_of 초과 값은 최신이어도 제외."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import models

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    as_of = free_snapshot.KST.localize(datetime(2026, 7, 18, 21, 30))
    cutoff = as_of.astimezone(free_snapshot.pytz.utc).replace(tzinfo=None)   # DB=UTC naive

    # kb: as_of 이전 2건(구→신) + as_of 초과 1건 → 초과 제외, 이전 중 최신(1401) 선택
    db.add(models.BankExchangeRate(bank="kb", currency="usd-krw", rate=1400.0, timestamp=cutoff - timedelta(minutes=20)))
    db.add(models.BankExchangeRate(bank="kb", currency="usd-krw", rate=1401.0, timestamp=cutoff - timedelta(minutes=1)))
    db.add(models.BankExchangeRate(bank="kb", currency="usd-krw", rate=1499.0, timestamp=cutoff + timedelta(seconds=1)))
    # hana: 정확히 as_of 경계값 → 포함(<=)
    db.add(models.BankExchangeRate(bank="hana", currency="usd-krw", rate=1402.0, timestamp=cutoff))
    # investing: 이전 1 + 초과 1 → 이전 값
    db.add(models.InvestingExchangeRate(currency="usd-krw", rate=1398.0, timestamp=cutoff - timedelta(minutes=5)))
    db.add(models.InvestingExchangeRate(currency="usd-krw", rate=1500.0, timestamp=cutoff + timedelta(minutes=2)))
    # 다른 asset은 미포함
    db.add(models.BankExchangeRate(bank="kb", currency="jpy-krw", rate=960.0, timestamp=cutoff - timedelta(minutes=1)))
    db.commit()

    entries = free_snapshot.fetch_rate_entries_until(db, "usd-krw", as_of)
    by_bank = {e["bank"]: e for e in entries}

    assert by_bank["kb"]["rate"] == 1401.0          # 초과값(1499) 아님, 이전 중 최신
    assert by_bank["hana"]["rate"] == 1402.0        # 경계 == 포함
    assert by_bank["investing"]["rate"] == 1398.0   # 초과값(1500) 아님
    assert all(e["currency"] == "usd-krw" for e in entries)
    # timestamp는 KST ISO 문자열(legacy flat shape 미러)
    assert all(isinstance(e["timestamp"], str) and "+09:00" in e["timestamp"] for e in entries)


# ── 무료 1d (intraday, KRX 제외, hourly-frozen) — ADR-039 A ──────

def test_free_tab_1d_specs_excludes_krx():
    from app import graph_v2_intraday
    # tether 1d엔 krx 있음 → 제외 확인
    tether_ids = [s["id"] for s in graph_v2_intraday._free_tab_1d_specs("tether")]
    assert not any(i.startswith("krx.") for i in tether_ids)
    # usd 1d **도 krx 포함**(ADR-038 D4 ② 달러 탭 편입, TAB_1D_SERIES["usd"]) → exclude_krx가 load-bearing(제거 확인)
    assert all(not i.startswith("krx.") for i in [s["id"] for s in graph_v2_intraday._free_tab_1d_specs("usd")])


def test_build_free_snapshot_payload_1d_uses_intraday(monkeypatch):
    from app import graph_v2_intraday
    fake_rates = [{"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "t"}]
    captured = {}

    def fake_1d(tab, *, exclude_krx=False, now_kst=None):
        captured["exclude_krx"] = exclude_krx
        captured["now_kst"] = now_kst
        return {
            "tab": tab, "period": "1d",
            "series": [{"id": "investing.usd", "data": [[123, 1, 2, 1385.0]]}],
            "metadata": {"fetched_at": "x", "bucket_size": "10min", "range": {"start": "a", "end": "b"}},
        }

    monkeypatch.setattr(free_snapshot, "fetch_rate_entries_until", lambda db, asset, as_of: fake_rates)
    monkeypatch.setattr(graph_v2_intraday, "build_tab_1d_payload", fake_1d)

    payload = free_snapshot.build_free_snapshot_payload(None, "usd", "1d")
    assert captured["exclude_krx"] is True                      # 무료 1d도 KRX 무조건 제외
    # 시간 계약: 1d builder에 now_kst=as_of 주입 → 잘라낼 경계=as_of(초과 봉 유입 불가)
    assert captured["now_kst"].isoformat() == payload["as_of"]
    assert payload["period"] == "1d"
    assert payload["graph"]["bucket_size"] == "10min"           # intraday(build_tab 아님)
    assert payload["rate"]["asset"] == "usd-krw"
    assert "krx.usd-krw-futures" not in [s["id"] for s in payload["graph"]["series"]]


# ── B1: serve-time 캐시 재검증 (오염 캐시 거부) ──────────────────

def test_validate_snapshot_payload():
    # 완전한 canonical(precompute가 만드는 shape) — 강화된 검사 통과
    good = {
        "tab": "usd", "period": "3m",
        "as_of": "2026-07-17T14:00:00+09:00", "generated_at": "2026-07-17T14:20:03+09:00",
        "rate": {"asset": "usd-krw", "entries": [{"currency": "usd-krw"}]},
        "graph": {"series": [{"id": "investing.usd", "data": [1]}], "bucket_size": "1d", "range": {"start": "a", "end": "b"}},
    }
    assert free_snapshot.validate_snapshot_payload(good, "usd", "3m")
    # 오염/이상 payload는 각 이유로 거부(→ last-good/503)
    assert not free_snapshot.validate_snapshot_payload(None, "usd", "3m")
    assert not free_snapshot.validate_snapshot_payload([1, 2], "usd", "3m")
    assert not free_snapshot.validate_snapshot_payload(good, "jpy", "3m")   # tab mismatch
    assert not free_snapshot.validate_snapshot_payload(good, "usd", "1y")   # period mismatch
    # 강화(codex Medium): 필수 필드/타입/asset
    assert not free_snapshot.validate_snapshot_payload({**good, "as_of": None}, "usd", "3m")          # as_of 누락/타입
    assert not free_snapshot.validate_snapshot_payload(
        {**good, "rate": {"asset": "jpy-krw", "entries": [{"currency": "usd-krw"}]}}, "usd", "3m")     # asset 불일치
    # serve-time as_of cutoff(codex 2026-07-18) — 배포 전/오염 canonical의 초과 point가 last-good으로 seed되는 창 차단
    assert not free_snapshot.validate_snapshot_payload(
        {**good, "graph": {"series": [{"id": "investing.usd",
                                       "data": [{"ts": "2026-07-17T15:00:00+09:00", "rate": 1400.0}]}],   # as_of(14:00) 초과
                           "bucket_size": "1d", "range": {"start": "a", "end": "b"}}}, "usd", "3m")
    # Pydantic 스키마(codex Medium — 값 레벨): invalid date / bucket_size 누락 / empty range
    assert not free_snapshot.validate_snapshot_payload({**good, "as_of": "banana"}, "usd", "3m")       # invalid date string
    assert not free_snapshot.validate_snapshot_payload(
        {**good, "graph": {"series": good["graph"]["series"], "range": good["graph"]["range"]}}, "usd", "3m")  # bucket_size 누락
    assert not free_snapshot.validate_snapshot_payload(
        {**good, "graph": {**good["graph"], "range": {}}}, "usd", "3m")   # empty range(start/end 없음)
    assert not free_snapshot.validate_snapshot_payload(
        {**good, "rate": {"asset": "usd-krw", "entries": []}, "graph": {**good["graph"], "series": []}}, "usd", "3m")  # empty
    krx = {**good, "graph": {**good["graph"], "series": [{"id": "krx.usd-krw-futures", "data": [1]}]}}
    assert not free_snapshot.validate_snapshot_payload(krx, "usd", "3m")   # KRX 오염 → 거부(B1)
    # nested schema가 깨진 캐시(rate가 list)도 예외 아닌 False (fail-closed → last-good/503)
    assert not free_snapshot.validate_snapshot_payload(
        {**good, "rate": [], "graph": {"series": "bad", "range": {}}}, "usd", "3m")


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
