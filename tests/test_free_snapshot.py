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


def test_assert_krx_free_raises_on_krx_source_asset_shape():
    """N4 — rate entry가 topic-native source/asset shape로 KRX(달러선물)를 실어도 차단(테더 대비)."""
    for entry in ({"source": "krx", "asset": "usd-krw-futures"},
                  {"source": "krx", "asset": "usdt-krw"},          # source만 krx여도
                  {"source": "bithumb", "asset": "usd-krw-futures"}):  # asset만 futures여도
        payload = {"graph": {"series": []}, "rate": {"entries": [entry]}}
        with pytest.raises(ValueError):
            free_snapshot._assert_krx_free(payload)


def test_assert_krx_free_passes_clean():
    payload = {
        "graph": {"series": [{"id": "investing.usd", "data": [1]}]},
        "rate": {"entries": [{"bank": "kb", "currency": "usd-krw"}]},
    }
    free_snapshot._assert_krx_free(payload)  # no raise


def test_assert_krx_free_passes_tether_source_asset_shape():
    """N4 — 정상 테더 거래소 rate entry(source/asset shape, KRX 아님)는 통과."""
    payload = {
        "graph": {"series": [{"id": "bithumb.usdt-krw", "data": [1]}]},
        "rate": {"entries": [
            {"source": "upbit", "asset": "usdt-krw", "rate": 1400.0},
            {"source": "bithumb", "asset": "usdt-krw", "rate": 1401.0},
        ]},
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
        {"bank": "investing", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2020-01-01T00:00:00+09:00"},
        {"bank": "kb", "currency": "usd-krw", "rate": 1386.0, "timestamp": "2020-01-01T00:00:00+09:00"},
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


def test_free_snapshot_tabs_are_fx_only():
    """FREE_SNAPSHOT_TABS = FX 3탭(usd/jpy/eur). tether는 N4(source_rates rate reader) 전까지 제외 —
    free rate reader가 investing+banks만 조회하고 source_rates(거래소 데이터)를 안 읽어 "튜플 추가만"으론 rate가 빔."""
    assert free_snapshot.FREE_SNAPSHOT_TABS == ("usd", "jpy", "eur")
    for tab in free_snapshot.FREE_SNAPSHOT_TABS:
        asset = free_snapshot.TAB_ASSET[tab]           # 모든 free tab은 asset 매핑 존재
        assert asset in ("usd-krw", "jpy-krw", "eur-krw")
        assert "usdt" not in asset                     # non-FX(tether) 아님
    assert "tether" not in free_snapshot.FREE_SNAPSHOT_TABS


def test_build_free_snapshot_payload_asset_per_tab(monkeypatch):
    """build_free_snapshot_payload가 각 FX 탭에 TAB_ASSET[tab]를 rate cutoff·payload asset으로 전달(통화 파라미터화).
    jpy/eur가 usd 코드 경로를 그대로 재사용(별도 reader 없음)함을 잠근다."""
    captured = {}

    def fake_build_tab(db, tab, period, today_kst=None, exclude_krx=False):
        captured["exclude_krx"] = exclude_krx
        return {
            "series": [{"id": f"investing.{tab}", "data": [{"bucket_date": "2026-07-16", "rate": 1.0}]}],
            "metadata": {"bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
        }

    def fake_fetch_until(db, asset, as_of):
        captured["cutoff_asset"] = asset
        return [{"bank": "investing", "currency": asset, "rate": 1.0, "timestamp": as_of.isoformat()}]

    monkeypatch.setattr(free_snapshot, "fetch_rate_entries_until", fake_fetch_until)
    monkeypatch.setattr(free_snapshot.graph_v2, "build_tab", fake_build_tab)

    for tab, expected_asset in (("jpy", "jpy-krw"), ("eur", "eur-krw")):
        payload = free_snapshot.build_free_snapshot_payload(None, tab, "3m")
        assert payload["tab"] == tab
        assert payload["rate"]["asset"] == expected_asset
        assert captured["cutoff_asset"] == expected_asset
        assert captured["exclude_krx"] is True
        assert free_snapshot._snapshot_is_nonempty(payload)


def test_is_snapshot_too_stale():
    """S6 24h hard cutoff — as_of age>=24h 또는 future면 True(serve 거부). 경계 포함 + fail-closed."""
    from datetime import timedelta
    now = datetime.fromisoformat("2026-07-21T12:30:00+09:00")

    def p(dt):
        return {"as_of": dt.isoformat() if hasattr(dt, "isoformat") else dt}

    # fresh (1h old) → not stale
    assert not free_snapshot.is_snapshot_too_stale(p(now - timedelta(hours=1)), now)
    # 24h - 1s → not stale (경계 직전)
    assert not free_snapshot.is_snapshot_too_stale(p(now - timedelta(seconds=86399)), now)
    # 정확히 24h → stale (경계 포함, >=)
    assert free_snapshot.is_snapshot_too_stale(p(now - timedelta(seconds=86400)), now)
    # 25h old → stale
    assert free_snapshot.is_snapshot_too_stale(p(now - timedelta(hours=25)), now)
    # future as_of → stale (fail-closed, 미래 데이터 노출 금지)
    assert free_snapshot.is_snapshot_too_stale(p(now + timedelta(minutes=5)), now)
    # as_of 부재/파싱 불가 → stale (fail-closed)
    assert free_snapshot.is_snapshot_too_stale({}, now)
    assert free_snapshot.is_snapshot_too_stale({"as_of": "banana"}, now)
    # tz-naive as_of(파싱되나 aware now과 뺄셈 TypeError) → fail-closed True (codex Low)
    assert free_snapshot.is_snapshot_too_stale({"as_of": "2026-07-21T11:30:00"}, now)


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


def test_assert_rates_within_as_of():
    """SET 전 fail-closed — rate entry timestamp가 as_of 초과면 raise(구 get_all_rates_flat canonical 차단)."""
    ok = {
        "as_of": "2026-07-18T21:30:00+09:00",
        "rate": {"entries": [
            {"bank": "kb", "currency": "usd-krw", "rate": 1400.0, "timestamp": "2026-07-18T21:22:00+09:00"},
            {"bank": "hana", "currency": "usd-krw", "rate": 1401.0, "timestamp": "2026-07-18T21:30:00+09:00"},  # 경계 == OK
        ]},
    }
    free_snapshot._assert_rates_within_as_of(ok)   # 통과

    over = {
        "as_of": "2026-07-18T21:30:00+09:00",
        "rate": {"entries": [
            {"bank": "investing", "currency": "usd-krw", "rate": 1402.0, "timestamp": "2026-07-18T21:31:00+09:00"},  # 초과
        ]},
    }
    with pytest.raises(ValueError):
        free_snapshot._assert_rates_within_as_of(over)

    # timestamp 필수(codex 2026-07-18 완전 closure) — ts 없는 dict entry는 raise
    with pytest.raises(ValueError):
        free_snapshot._assert_rates_within_as_of(
            {"as_of": "2026-07-18T21:30:00+09:00", "rate": {"entries": [{"bank": "kb"}]}})
    # 비-dict entry는 이 함수 영역 아님(Pydantic이 거부) — skip(통과)
    free_snapshot._assert_rates_within_as_of(
        {"as_of": "2026-07-18T21:30:00+09:00", "rate": {"entries": ["junk"]}})


def test_basis_as_of_boundaries():
    """마지막 HH:30 경계 — minute>=30이면 이번 시, <30이면 직전 시(자정 경계 포함)."""
    def kst(y, mo, d, h, mi, s=0):
        return free_snapshot.KST.localize(datetime(y, mo, d, h, mi, s))

    assert free_snapshot.basis_as_of(kst(2026, 7, 18, 21, 45)) == kst(2026, 7, 18, 21, 30)
    assert free_snapshot.basis_as_of(kst(2026, 7, 18, 21, 30)) == kst(2026, 7, 18, 21, 30)   # 정확히 :30
    assert free_snapshot.basis_as_of(kst(2026, 7, 18, 21, 29)) == kst(2026, 7, 18, 20, 30)
    assert free_snapshot.basis_as_of(kst(2026, 7, 18, 0, 10)) == kst(2026, 7, 17, 23, 30)    # 자정 경계


def test_compute_refresh_not_before():
    """재요청 권장 시각 = 다음 HH:30 publish slot(now 초과) + 60초(:31). serve-time now 기준(as_of 무관), 항상 미래."""
    def kst(y, mo, d, h, mi, s=0):
        return free_snapshot.KST.localize(datetime(y, mo, d, h, mi, s))

    # minute<30 → 이번 시 :31 (이번 시 publish 전이라 이번 :30 대기)
    assert free_snapshot.compute_refresh_not_before(kst(2026, 7, 18, 21, 15)) == kst(2026, 7, 18, 21, 31)
    # minute>30 → 다음 시 :31 (이번 시 publish 지남)
    assert free_snapshot.compute_refresh_not_before(kst(2026, 7, 18, 21, 45)) == kst(2026, 7, 18, 22, 31)
    # :30:30 (cron :30:19 직후) → 다음 시 :31 (이번 것 이미 받음 → redundant refetch 회피)
    assert free_snapshot.compute_refresh_not_before(kst(2026, 7, 18, 21, 30, 30)) == kst(2026, 7, 18, 22, 31)
    # 시/자정 경계
    assert free_snapshot.compute_refresh_not_before(kst(2026, 7, 18, 23, 45)) == kst(2026, 7, 19, 0, 31)
    # 항상 미래(one-shot 스케줄이 과거로 즉시 발화하지 않음)
    now = kst(2026, 7, 18, 9, 5)
    assert free_snapshot.compute_refresh_not_before(now) > now

    # +09:00 출력 계약(codex Medium): naive 거부
    with pytest.raises(ValueError):
        free_snapshot.compute_refresh_not_before(datetime(2026, 7, 18, 21, 45))   # tz-naive
    # aware(UTC) 입력 → KST 정규화 후 판정·출력(UTC 12:45 = KST 21:45 → 다음 시 :31, offset +09:00)
    utc = free_snapshot.pytz.utc.localize(datetime(2026, 7, 18, 12, 45))
    out = free_snapshot.compute_refresh_not_before(utc)
    assert out == kst(2026, 7, 18, 22, 31)
    assert out.utcoffset() == timedelta(hours=9)


def test_attach_refresh_not_before_additive_and_immutable():
    """top-level refresh_not_before 부착 — 원본 canonical 불변(copy), compute 값과 일치, 나머지 필드 보존."""
    now = free_snapshot.KST.localize(datetime(2026, 7, 18, 21, 45))
    canonical = {"as_of": "2026-07-18T21:30:00+09:00", "rate": {"entries": []}, "graph": {"series": []}}
    out = free_snapshot.attach_refresh_not_before(canonical, now_kst=now)

    assert datetime.fromisoformat(out["refresh_not_before"]) == free_snapshot.compute_refresh_not_before(now)
    assert "refresh_not_before" not in canonical   # 원본 불변(canonical 미저장)
    assert out["as_of"] == canonical["as_of"] and out["rate"] == canonical["rate"]   # 나머지 보존


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


# ── N4-2b: 테더 무료 grouped rate reader (cutoff-aware, build_tether_tab_payload 재사용) ──

def _tether_test_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import models
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_get_source_rates_until_cutoff():
    """source_rates cutoff 변형 — timestamp<=cutoff 최신 1건/source, rate_changed_at 미포함(정적), partial 누락."""
    from app import crud, models
    db = _tether_test_db()
    as_of = free_snapshot.KST.localize(datetime(2026, 7, 18, 21, 30))
    cutoff = as_of.astimezone(free_snapshot.pytz.utc).replace(tzinfo=None)

    db.add(models.SourceRate(source="upbit", asset="usdt-krw", rate=1400.0, timestamp=cutoff - timedelta(minutes=10)))
    db.add(models.SourceRate(source="upbit", asset="usdt-krw", rate=1401.0, timestamp=cutoff - timedelta(minutes=1)))
    db.add(models.SourceRate(source="upbit", asset="usdt-krw", rate=1499.0, timestamp=cutoff + timedelta(seconds=1)))  # 초과
    db.add(models.SourceRate(source="bithumb", asset="usdt-krw", rate=1402.0, timestamp=cutoff))                       # 경계 포함
    # coinone: 데이터 없음 → partial 누락. 다른 asset(krx futures) 미포함:
    db.add(models.SourceRate(source="upbit", asset="usd-krw-futures", rate=9.0, timestamp=cutoff - timedelta(minutes=1)))
    db.commit()

    rows = crud.get_source_rates_until(db, "usdt-krw", ["upbit", "bithumb", "coinone"], cutoff)
    by_src = {r["source"]: r for r in rows}
    assert by_src["upbit"]["rate"] == 1401.0        # 초과(1499) 아님, 이전 최신
    assert by_src["bithumb"]["rate"] == 1402.0      # 경계 == 포함
    assert "coinone" not in by_src                  # partial (누락)
    assert all(r["asset"] == "usdt-krw" for r in rows)       # 다른 asset 미포함
    assert all("rate_changed_at" not in r for r in rows)    # 정적 스냅샷 — live-merge 필드 없음
    assert all("+09:00" in r["timestamp"] for r in rows)


def test_fetch_tether_grouped_rate_until():
    """grouped 블록 — usdt_krw(거래소 source_rates) + usd_krw_banks(kb·hana, 다른 은행 drop) +
    usd_krw_reference(investing singleton), 각 cutoff. build_tether_tab_payload 재사용(topic-native shape)."""
    from app import models
    db = _tether_test_db()
    as_of = free_snapshot.KST.localize(datetime(2026, 7, 18, 21, 30))
    cutoff = as_of.astimezone(free_snapshot.pytz.utc).replace(tzinfo=None)

    db.add(models.SourceRate(source="upbit", asset="usdt-krw", rate=1400.0, timestamp=cutoff - timedelta(minutes=1)))
    db.add(models.SourceRate(source="bithumb", asset="usdt-krw", rate=1401.0, timestamp=cutoff - timedelta(minutes=2)))
    db.add(models.SourceRate(source="upbit", asset="usdt-krw", rate=1499.0, timestamp=cutoff + timedelta(seconds=1)))  # 초과 제외
    db.add(models.BankExchangeRate(bank="kb", currency="usd-krw", rate=1385.0, timestamp=cutoff - timedelta(minutes=1)))
    db.add(models.BankExchangeRate(bank="hana", currency="usd-krw", rate=1386.0, timestamp=cutoff))
    db.add(models.BankExchangeRate(bank="shinhan", currency="usd-krw", rate=1387.0, timestamp=cutoff))   # tether 탭 아님 → drop
    db.add(models.InvestingExchangeRate(currency="usd-krw", rate=1384.0, timestamp=cutoff - timedelta(minutes=5)))
    db.commit()

    block = free_snapshot.fetch_tether_grouped_rate_until(db, as_of)
    assert block["kind"] == "source_grouped" and block["primary_asset"] == "usdt-krw"
    ex = {e["source"]: e for e in block["usdt_krw"]}
    assert ex["upbit"]["rate"] == 1400.0 and ex["upbit"]["asset"] == "usdt-krw"   # 초과 아님
    assert set(ex) == {"upbit", "bithumb"}                    # 나머지 거래소 누락 partial
    assert all("rate_changed_at" not in e for e in block["usdt_krw"])   # 정적
    banks = {e["source"]: e for e in block["usd_krw_banks"]}
    assert set(banks) == {"kb", "hana"}                       # shinhan drop
    assert banks["kb"]["asset"] == "usd-krw" and banks["kb"]["rate"] == 1385.0   # source/asset 정규화
    ref = block["usd_krw_reference"]                          # investing singleton
    assert ref["source"] == "investing" and ref["asset"] == "usd-krw" and ref["rate"] == 1384.0
    assert "+09:00" in ref["timestamp"]
    # KRX-free + within-as_of 통과(어느 소스에도 KRX 없음, 모든 ts <= as_of)
    free_snapshot._assert_krx_free({"graph": {"series": []}, "rate": block})
    free_snapshot._assert_rates_within_as_of({"as_of": as_of.isoformat(), "rate": block})


def test_fetch_tether_grouped_rate_until_partial_no_investing_no_banks():
    """investing/은행 부재 → usd_krw_reference={} · usd_krw_banks=[] (partial). nonempty는 거래소로 충족."""
    from app import models
    db = _tether_test_db()
    as_of = free_snapshot.KST.localize(datetime(2026, 7, 18, 21, 30))
    cutoff = as_of.astimezone(free_snapshot.pytz.utc).replace(tzinfo=None)
    db.add(models.SourceRate(source="upbit", asset="usdt-krw", rate=1400.0, timestamp=cutoff - timedelta(minutes=1)))
    db.commit()

    block = free_snapshot.fetch_tether_grouped_rate_until(db, as_of)
    assert block["usd_krw_reference"] == {}          # investing 부재 → 키 present(빈 dict)
    assert block["usd_krw_banks"] == []              # 은행 부재
    assert len(block["usdt_krw"]) == 1
    assert free_snapshot._snapshot_is_nonempty(      # 거래소 1개 + graph data → nonempty
        {"rate": block, "graph": {"series": [{"id": "bithumb.usdt-krw", "data": [1]}]}})


def test_build_free_snapshot_payload_tether_grouped(monkeypatch):
    """build_free_snapshot_payload(tab='tether')가 grouped rate 블록 생성 + build-time asserts 통과(FX는 flat)."""
    from app import models
    db = _tether_test_db()
    now = free_snapshot.KST.localize(datetime(2026, 7, 18, 21, 35))
    as_of = free_snapshot.basis_as_of(now)   # 21:30
    cutoff = as_of.astimezone(free_snapshot.pytz.utc).replace(tzinfo=None)
    db.add(models.SourceRate(source="upbit", asset="usdt-krw", rate=1400.0, timestamp=cutoff - timedelta(minutes=1)))
    db.add(models.BankExchangeRate(bank="kb", currency="usd-krw", rate=1385.0, timestamp=cutoff - timedelta(minutes=1)))
    db.add(models.InvestingExchangeRate(currency="usd-krw", rate=1384.0, timestamp=cutoff - timedelta(minutes=1)))
    db.commit()

    def fake_build_tab(db_, tab, period, today_kst=None, exclude_krx=False):
        assert tab == "tether" and exclude_krx is True
        return {"series": [{"id": "bithumb.usdt-krw", "data": [{"bucket_date": "2026-07-16", "rate": 1401.0}]}],
                "metadata": {"bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}}}
    monkeypatch.setattr(free_snapshot.graph_v2, "build_tab", fake_build_tab)

    payload = free_snapshot.build_free_snapshot_payload(db, "tether", "3m", now_kst=now)
    assert payload["rate"]["kind"] == "source_grouped"
    assert payload["rate"]["primary_asset"] == "usdt-krw"
    assert [e["source"] for e in payload["rate"]["usdt_krw"]] == ["upbit"]
    assert [e["source"] for e in payload["rate"]["usd_krw_banks"]] == ["kb"]
    assert payload["rate"]["usd_krw_reference"]["source"] == "investing"
    # 여기 도달 = build-time _assert_krx_free/_assert_rates_within_as_of/_assert_as_of_on_grid 모두 통과


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
    fake_rates = [{"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2020-01-01T00:00:00+09:00"}]
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
        "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",   # :30 grid
        "rate": {"asset": "usd-krw", "entries": [
            {"currency": "usd-krw", "timestamp": "2026-07-17T14:19:00+09:00"}]},   # ts 필수(<= as_of)
        "graph": {"series": [{"id": "investing.usd", "data": [1]}], "bucket_size": "1d", "range": {"start": "a", "end": "b"}},
    }
    assert free_snapshot.validate_snapshot_payload(good, "usd", "3m")
    # HH:30 grid(codex 2026-07-18) — off-grid as_of(구 :00 floor / 오염) 거부 → 시간 계약 grid 수준 closure
    assert not free_snapshot.validate_snapshot_payload({**good, "as_of": "2026-07-17T14:00:00+09:00"}, "usd", "3m")
    assert not free_snapshot.validate_snapshot_payload({**good, "as_of": "2026-07-17T14:15:00+09:00"}, "usd", "3m")
    # tz-aware as_of 강제(codex 2026-07-19) — naive as_of(fully-naive 오염)는 cutoff 비교가 무의미해 새므로 거부.
    # rate ts도 naive로 맞춰 cutoff는 통과시키고 as_of tz-aware assert만으로 거부됨을 잠금.
    assert not free_snapshot.validate_snapshot_payload(
        {**good, "as_of": "2026-07-17T14:30:00",   # naive(on-grid, offset 없음)
         "rate": {"asset": "usd-krw", "entries": [{"currency": "usd-krw", "timestamp": "2026-07-17T14:19:00"}]}},
        "usd", "3m")
    # grid는 KST 정규화 후 판정(codex 2026-07-19) — offset 무관 tz-aware 허용. 두 반대 방향 반례:
    assert free_snapshot.validate_snapshot_payload(       # +05:30 11:00 = 14:30 KST → 유효(offset raw minute=00 아님)
        {**good, "as_of": "2026-07-17T11:00:00+05:30"}, "usd", "3m")
    assert not free_snapshot.validate_snapshot_payload(   # +05:30 10:30 = 14:00 KST → off-grid 거부(fail-open 누수 차단)
        {**good, "as_of": "2026-07-17T10:30:00+05:30",    # rate ts를 13:59 KST로 → cutoff 통과 → grid 단독 거부 잠금(codex)
         "rate": {"asset": "usd-krw", "entries": [{"currency": "usd-krw", "timestamp": "2026-07-17T13:59:00+09:00"}]}},
        "usd", "3m")
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
                                       "data": [{"ts": "2026-07-17T15:00:00+09:00", "rate": 1400.0}]}],   # as_of(14:30) 초과
                           "bucket_size": "1d", "range": {"start": "a", "end": "b"}}}, "usd", "3m")
    # serve-time rate cutoff(codex 2026-07-18) — graph와 대칭, 초과 rate entry canonical 거부
    assert not free_snapshot.validate_snapshot_payload(
        {**good, "rate": {"asset": "usd-krw", "entries": [
            {"bank": "kb", "currency": "usd-krw", "rate": 1400.0, "timestamp": "2026-07-17T15:00:00+09:00"}]}},  # as_of(14:30) 초과
        "usd", "3m")
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
    """단일 (tab,period) build가 raise해도 나머지는 SET(격리 + keep-last-good) — 같은 period의 다른 탭까지.
    FX 3탭 확장 후 격리 단위가 (tab,period) 단위임을 명시(period 단위가 아님)."""
    def flaky(db, tab, period):
        if tab == "jpy" and period == "3m":   # 단일 (tab,period)만 실패
            raise RuntimeError("boom")
        return {"tab": tab, "period": period,
                "rate": {"entries": [{"currency": "usd-krw"}]},
                "graph": {"series": [{"id": "investing.usd", "data": [1]}]}}

    fake = _patch_precompute(monkeypatch, flaky)
    free_snapshot.precompute_free_snapshots()
    assert free_snapshot.free_snapshot_key("jpy", "3m") not in fake.setex_keys   # 실패한 것만 빠짐
    assert free_snapshot.free_snapshot_key("usd", "3m") in fake.setex_keys       # 같은 period 다른 탭 생존(cross-tab 격리)
    assert free_snapshot.free_snapshot_key("eur", "3m") in fake.setex_keys       # 같은 period 다른 탭 생존
    assert free_snapshot.free_snapshot_key("jpy", "1w") in fake.setex_keys       # 같은 탭 다른 period 생존


# ─── attach_free_snapshot_domain (serve-time X축 domain, graph envelope, anchor=as_of) — ADR-039 X축 통일 ───

def _free_canonical(period="3m", as_of="2026-07-19T10:30:00+09:00"):
    return {
        "tab": "usd", "period": period, "as_of": as_of, "generated_at": as_of,
        "rate": {"asset": "usd-krw", "entries": [{"bank": "kb", "currency": "usd-krw", "rate": 1385.0}]},
        "graph": {"series": [{"id": "investing.usd", "data": [{"bucket_date": "2026-07-18", "rate": 1385.0}]}],
                  "bucket_size": "1d", "range": {"start": "2026-04-20", "end": "2026-07-19"}},
    }


def test_attach_free_snapshot_domain_graph_envelope_fixed_start():
    canonical = _free_canonical("3m")
    out = free_snapshot.attach_free_snapshot_domain(canonical, "3m")
    # domain은 graph 블록에(프리미엄=metadata와 다른 envelope). anchor=as_of.
    assert out["graph"]["live_domain_mode"] == "fixed_start"
    assert out["graph"]["domain_end_at"] == "2026-07-19T10:30:00+09:00"       # = as_of
    assert out["graph"]["domain_start_at"] == "2026-04-20T00:00:00+09:00"     # (as_of 날짜 - 90) 00:00
    assert out["graph"]["range"] == {"start": "2026-04-20", "end": "2026-07-19"}   # 기존 range 보존
    assert "metadata" not in out                                              # 무료엔 metadata 없음
    # 입력 불변 (canonical에 domain 없음 — Redis/last-good은 깨끗)
    assert "domain_start_at" not in canonical["graph"]


def test_attach_free_snapshot_domain_1d_rolling():
    out = free_snapshot.attach_free_snapshot_domain(_free_canonical("1d"), "1d")
    assert out["graph"]["live_domain_mode"] == "rolling"
    assert out["graph"]["domain_start_at"] == "2026-07-18T10:30:00+09:00"     # as_of - 24h
    assert out["graph"]["domain_end_at"] == "2026-07-19T10:30:00+09:00"       # = as_of


def test_attach_free_snapshot_domain_normalizes_to_plus9():
    # as_of가 다른 offset(UTC)이어도 출력 domain은 +09:00(같은 순간). 입력 offset을 +09:00로 강제하진 않음.
    out = free_snapshot.attach_free_snapshot_domain(_free_canonical("3m", as_of="2026-07-19T01:30:00+00:00"), "3m")
    assert out["graph"]["domain_end_at"] == "2026-07-19T10:30:00+09:00"       # 01:30 UTC = 10:30 KST


def test_attach_free_snapshot_domain_naive_as_of_no_domain():
    # naive as_of(오염/구 canonical) → domain 없이 canonical 그대로(503 아님, 클라 data-derived fallback).
    canonical = _free_canonical("3m", as_of="2026-07-19T10:30:00")   # naive
    out = free_snapshot.attach_free_snapshot_domain(canonical, "3m")
    assert "domain_start_at" not in out["graph"]
    assert out is canonical                                          # 그대로 반환(copy도 안 함)


def test_attach_free_snapshot_domain_missing_as_of_no_domain():
    canonical = {"tab": "usd", "period": "3m", "graph": {"series": []}}   # as_of 키 없음
    out = free_snapshot.attach_free_snapshot_domain(canonical, "3m")
    assert "domain_start_at" not in out["graph"]
    assert out is canonical


# ── N4-2a: grouped(source_grouped) rate shape 검증 계층 ─────────────
# 테더 rate가 도입할 grouped shape을 flat과 함께 커버(리더/tuple/배포는 N4-2b/N4-3). payload는 손으로 구성.

def _grouped_rate(*, usdt_krw=None, usd_krw_banks=None, usd_krw_reference=None, primary_asset="usdt-krw"):
    """테스트용 grouped rate 블록 헬퍼 — 미지정 그룹은 빈 값(list/dict)."""
    return {
        "kind": "source_grouped",
        "primary_asset": primary_asset,
        "usdt_krw": [] if usdt_krw is None else usdt_krw,
        "usd_krw_banks": [] if usd_krw_banks is None else usd_krw_banks,
        "usd_krw_reference": {} if usd_krw_reference is None else usd_krw_reference,
    }


def test_iter_rate_items_flat():
    """flat(FX)은 entries 중 dict만 순회."""
    rate = {"asset": "usd-krw", "entries": [{"bank": "kb"}, {"bank": "hana"}]}
    assert list(free_snapshot._iter_rate_items(rate)) == [{"bank": "kb"}, {"bank": "hana"}]


def test_iter_rate_items_grouped_order_and_count():
    """grouped은 usdt_krw → usd_krw_banks → usd_krw_reference 순서로 평탄(개수·순서)."""
    rate = _grouped_rate(
        usdt_krw=[{"source": "upbit"}, {"source": "bithumb"}],
        usd_krw_banks=[{"source": "kb"}, {"source": "hana"}],
        usd_krw_reference={"source": "investing"},
    )
    assert list(free_snapshot._iter_rate_items(rate)) == [
        {"source": "upbit"}, {"source": "bithumb"},   # usdt_krw
        {"source": "kb"}, {"source": "hana"},         # usd_krw_banks
        {"source": "investing"},                      # usd_krw_reference (singleton)
    ]


def test_iter_rate_items_grouped_empty_reference_skipped():
    """빈 reference dict(투자 참조 부재)는 스킵 — partial 자연 허용."""
    rate = _grouped_rate(usdt_krw=[{"source": "upbit"}])   # banks/ref 비어있음
    assert list(free_snapshot._iter_rate_items(rate)) == [{"source": "upbit"}]


def test_iter_rate_items_defensive():
    """비-dict rate/None/비-list 그룹/비-dict item 방어."""
    assert list(free_snapshot._iter_rate_items(None)) == []
    assert list(free_snapshot._iter_rate_items("junk")) == []
    assert list(free_snapshot._iter_rate_items({})) == []                 # flat, entries 없음
    assert list(free_snapshot._iter_rate_items({"entries": "notalist"})) == []
    # flat + 비-dict item 스킵
    assert list(free_snapshot._iter_rate_items(
        {"entries": ["x", {"bank": "kb"}, 3]})) == [{"bank": "kb"}]
    # grouped + 비-list 그룹/비-dict item/None ref 방어
    assert list(free_snapshot._iter_rate_items(
        {"kind": "source_grouped", "usdt_krw": ["x", {"source": "upbit"}],
         "usd_krw_banks": "notalist", "usd_krw_reference": None})) == [{"source": "upbit"}]


def test_assert_krx_free_grouped_blocks_each_group():
    """grouped — KRX(source=='krx' 또는 asset=='usd-krw-futures')가 어느 그룹으로도 유입되면 차단(우회 방지)."""
    krx_source = {"source": "krx", "asset": "usdt-krw", "timestamp": "2026-07-18T21:00:00+09:00"}
    krx_asset = {"source": "bithumb", "asset": "usd-krw-futures", "timestamp": "2026-07-18T21:00:00+09:00"}
    for kw in (
        {"usdt_krw": [krx_source]},
        {"usdt_krw": [krx_asset]},
        {"usd_krw_banks": [krx_source]},
        {"usd_krw_banks": [krx_asset]},
        {"usd_krw_reference": krx_source},
        {"usd_krw_reference": krx_asset},
    ):
        payload = {"graph": {"series": []}, "rate": _grouped_rate(**kw)}
        with pytest.raises(ValueError):
            free_snapshot._assert_krx_free(payload)


def test_assert_krx_free_grouped_passes_clean():
    """정상 grouped(거래소/은행/investing, KRX 아님)는 통과."""
    payload = {"graph": {"series": [{"id": "bithumb.usdt-krw", "data": [1]}]},
               "rate": _grouped_rate(
                   usdt_krw=[{"source": "upbit", "asset": "usdt-krw"}],
                   usd_krw_banks=[{"source": "kb", "asset": "usd-krw"}],
                   usd_krw_reference={"source": "investing", "asset": "usd-krw"})}
    free_snapshot._assert_krx_free(payload)   # no raise


def test_assert_rates_within_as_of_grouped():
    """grouped — 각 그룹 entry ts <= as_of 강제(future면 raise, ts 필수)."""
    as_of = "2026-07-18T21:30:00+09:00"
    ok = {"as_of": as_of, "rate": _grouped_rate(
        usdt_krw=[{"source": "upbit", "asset": "usdt-krw", "timestamp": "2026-07-18T21:20:00+09:00"}],
        usd_krw_banks=[{"source": "kb", "asset": "usd-krw", "timestamp": "2026-07-18T21:30:00+09:00"}],   # 경계 == OK
        usd_krw_reference={"source": "investing", "asset": "usd-krw", "timestamp": "2026-07-18T21:00:00+09:00"})}
    free_snapshot._assert_rates_within_as_of(ok)   # 통과

    future = {"source": "x", "asset": "usdt-krw", "timestamp": "2026-07-18T22:00:00+09:00"}
    for kw in ({"usdt_krw": [future]}, {"usd_krw_banks": [future]}, {"usd_krw_reference": future}):
        over = {"as_of": as_of, "rate": _grouped_rate(**kw)}
        with pytest.raises(ValueError):
            free_snapshot._assert_rates_within_as_of(over)

    # ts 없는 grouped entry → raise(시간 계약 필수)
    with pytest.raises(ValueError):
        free_snapshot._assert_rates_within_as_of(
            {"as_of": as_of, "rate": _grouped_rate(usdt_krw=[{"source": "upbit", "asset": "usdt-krw"}])})


def test_snapshot_is_nonempty_grouped_partial():
    """grouped — 거래소 1개(또는 reference만)여도 nonempty True(partial) / 전부 빈 그룹이면 False."""
    graph = {"series": [{"id": "bithumb.usdt-krw", "data": [1]}]}
    # 거래소 1개만
    assert free_snapshot._snapshot_is_nonempty(
        {"rate": _grouped_rate(usdt_krw=[{"source": "upbit", "asset": "usdt-krw"}]), "graph": graph})
    # reference만
    assert free_snapshot._snapshot_is_nonempty(
        {"rate": _grouped_rate(usd_krw_reference={"source": "investing", "asset": "usd-krw"}), "graph": graph})
    # 전부 빈 그룹 → rate empty → False
    assert not free_snapshot._snapshot_is_nonempty({"rate": _grouped_rate(), "graph": graph})
    # rate 있지만 graph 비면 False (기존 조건 유지)
    assert not free_snapshot._snapshot_is_nonempty(
        {"rate": _grouped_rate(usdt_krw=[{"source": "upbit"}]), "graph": {"series": [{"data": []}]}})


def test_free_rate_union_flat_and_grouped_and_malformed():
    """_FreeSnapshotModel.rate union — flat(kind 없음)·grouped(source_grouped) 둘 다 통과, kind 없는 grouped-only 거부."""
    from pydantic import ValidationError

    from app.free_snapshot import _FreeSnapshotModel

    base = {
        "tab": "usd", "period": "3m",
        "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
        "graph": {"series": [{"id": "investing.usd", "data": [1]}],
                  "bucket_size": "1d", "range": {"start": "a", "end": "b"}},
    }
    # flat 통과
    _FreeSnapshotModel.model_validate({**base, "rate": {"asset": "usd-krw", "entries": [{"currency": "usd-krw"}]}})
    # grouped 통과
    _FreeSnapshotModel.model_validate({**base, "tab": "tether", "rate": _grouped_rate(
        usdt_krw=[{"source": "upbit", "asset": "usdt-krw"}],
        usd_krw_banks=[{"source": "kb", "asset": "usd-krw"}],
        usd_krw_reference={"source": "investing", "asset": "usd-krw"})})
    # malformed: kind 없이 grouped 그룹만 → flat(asset/entries)·grouped(kind) 둘 다 실패 → 거부
    malformed = {**base, "tab": "tether", "rate": {
        "primary_asset": "usdt-krw", "usdt_krw": [{"source": "upbit"}],
        "usd_krw_banks": [], "usd_krw_reference": {}}}   # kind 없음
    with pytest.raises(ValidationError):
        _FreeSnapshotModel.model_validate(malformed)


def test_validate_snapshot_payload_grouped_tether():
    """grouped canonical(테더)이 validate_snapshot_payload 전 계층 통과(asset=primary_asset grouped-aware) +
    primary_asset mismatch/KRX 유입 거부 + partial 통과."""
    grouped_good = {
        "tab": "tether", "period": "3m",
        "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
        "rate": _grouped_rate(
            usdt_krw=[{"source": "upbit", "asset": "usdt-krw", "rate": 1400.0, "timestamp": "2026-07-17T14:19:00+09:00"},
                      {"source": "bithumb", "asset": "usdt-krw", "rate": 1401.0, "timestamp": "2026-07-17T14:20:00+09:00"}],
            usd_krw_banks=[{"source": "kb", "asset": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:19:00+09:00"}],
            usd_krw_reference={"source": "investing", "asset": "usd-krw", "rate": 1386.0, "timestamp": "2026-07-17T14:18:00+09:00"}),
        "graph": {"series": [{"id": "bithumb.usdt-krw", "data": [{"bucket_date": "2026-07-16", "rate": 1401.0}]}],
                  "bucket_size": "1d", "range": {"start": "a", "end": "b"}},
    }
    assert free_snapshot.validate_snapshot_payload(grouped_good, "tether", "3m")
    # primary_asset mismatch → 거부(grouped asset 체크)
    assert not free_snapshot.validate_snapshot_payload(
        {**grouped_good, "rate": {**grouped_good["rate"], "primary_asset": "usd-krw"}}, "tether", "3m")
    # partial(거래소 1개만, banks/ref 비어있음)도 통과(nonempty True)
    assert free_snapshot.validate_snapshot_payload(
        {**grouped_good, "rate": _grouped_rate(
            usdt_krw=[{"source": "upbit", "asset": "usdt-krw", "rate": 1400.0,
                       "timestamp": "2026-07-17T14:19:00+09:00"}])}, "tether", "3m")
    # grouped에 KRX 유입 → 거부(_assert_krx_free)
    assert not free_snapshot.validate_snapshot_payload(
        {**grouped_good, "rate": {**grouped_good["rate"], "usd_krw_banks": [
            {"source": "krx", "asset": "usd-krw-futures", "rate": 1.0, "timestamp": "2026-07-17T14:19:00+09:00"}]}},
        "tether", "3m")


# ── N4-2a codex Blocker/Medium hardening (hybrid KRX 우회 + tab↔shape 결합) ──

def test_iter_all_rate_dicts_scans_all_containers():
    """_iter_all_rate_dicts는 kind 무관 entries+3그룹을 **동시에** 순회(KRX fail-closed 방어 — _iter_rate_items가
    kind로 한쪽만 보는 것과 대비)."""
    # hybrid: kind=grouped인데 entries도 있음 → 둘 다 순회
    rate = {
        "kind": "source_grouped", "primary_asset": "usdt-krw",
        "usdt_krw": [{"source": "upbit"}], "usd_krw_banks": [{"source": "kb"}],
        "usd_krw_reference": {"source": "investing"},
        "entries": [{"bank": "krx"}],   # 반대 shape 컨테이너
    }
    got = list(free_snapshot._iter_all_rate_dicts(rate))
    assert {"bank": "krx"} in got          # _iter_rate_items는 이걸 놓쳤음(kind=grouped라 entries 미검사)
    assert {"source": "upbit"} in got and {"source": "investing"} in got
    # 방어: 비-dict/None/비-list 그룹/빈 ref
    assert list(free_snapshot._iter_all_rate_dicts(None)) == []
    assert list(free_snapshot._iter_all_rate_dicts(
        {"entries": "x", "usdt_krw": None, "usd_krw_reference": {}})) == []


def test_krx_hybrid_bypass_blocked_grouped_with_krx_entries():
    """codex Blocker — grouped payload에 entries=[KRX]를 extra로 얹어도 _assert_krx_free가 잡는다(scan-all)."""
    rate = {
        "kind": "source_grouped", "primary_asset": "usdt-krw",
        "usdt_krw": [{"source": "upbit", "asset": "usdt-krw"}],
        "usd_krw_banks": [], "usd_krw_reference": {},
        "entries": [{"source": "krx", "asset": "usd-krw-futures", "timestamp": "2026-07-17T14:19:00+09:00"}],
    }
    with pytest.raises(ValueError):
        free_snapshot._assert_krx_free({"graph": {"series": []}, "rate": rate})


def test_krx_hybrid_bypass_blocked_flat_with_grouped_krx_field():
    """codex Blocker 역방향 — flat payload에 usd_krw_banks=[KRX]를 extra로 얹어도 잡는다."""
    rate = {
        "asset": "usd-krw", "entries": [{"bank": "kb"}],
        "usd_krw_banks": [{"source": "krx", "asset": "usd-krw-futures"}],   # grouped 컨테이너 extra
    }
    with pytest.raises(ValueError):
        free_snapshot._assert_krx_free({"graph": {"series": []}, "rate": rate})


def test_free_rate_forbid_rejects_hybrid_shape():
    """extra="forbid" — 반대 shape 키를 얹은 hybrid는 schema 층(model_validate)에서 거부(양방향)."""
    from pydantic import ValidationError

    from app.free_snapshot import _FreeSnapshotModel
    base = {
        "tab": "tether", "period": "3m",
        "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
        "graph": {"series": [{"id": "bithumb.usdt-krw", "data": [1]}],
                  "bucket_size": "1d", "range": {"start": "a", "end": "b"}},
    }
    # grouped + entries(flat 키) extra → 두 union member 모두 실패
    with pytest.raises(ValidationError):
        _FreeSnapshotModel.model_validate({**base, "rate": {
            **_grouped_rate(usdt_krw=[{"source": "upbit"}]), "entries": [{"bank": "krx"}]}})
    # flat + usd_krw_banks(grouped 키) extra → 두 member 모두 실패
    with pytest.raises(ValidationError):
        _FreeSnapshotModel.model_validate({**base, "tab": "usd", "rate": {
            "asset": "usd-krw", "entries": [{"bank": "kb"}], "usd_krw_banks": [{"source": "krx"}]}})


def test_validate_hybrid_krx_bypass_end_to_end_blocked():
    """codex Blocker end-to-end — hybrid KRX payload가 validate_snapshot_payload에서 False(서빙 안 됨)."""
    grouped_krx = {
        "tab": "tether", "period": "3m",
        "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
        "rate": {
            **_grouped_rate(usdt_krw=[{"source": "upbit", "asset": "usdt-krw", "rate": 1400.0,
                                       "timestamp": "2026-07-17T14:19:00+09:00"}]),
            "entries": [{"source": "krx", "asset": "usd-krw-futures", "rate": 1.0,
                         "timestamp": "2026-07-17T14:19:00+09:00"}]},   # extra KRX 컨테이너
        "graph": {"series": [{"id": "bithumb.usdt-krw", "data": [{"bucket_date": "2026-07-16", "rate": 1401.0}]}],
                  "bucket_size": "1d", "range": {"start": "a", "end": "b"}},
    }
    assert not free_snapshot.validate_snapshot_payload(grouped_krx, "tether", "3m")


def test_validate_tab_shape_coupling():
    """codex Medium — tab↔shape 결합: canonical 짝만 통과, 오염 grouped-usd/flat-tether 거부."""
    flat_usd = {
        "tab": "usd", "period": "3m",
        "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
        "rate": {"asset": "usd-krw", "entries": [{"bank": "kb", "currency": "usd-krw", "rate": 1385.0,
                                                  "timestamp": "2026-07-17T14:19:00+09:00"}]},
        "graph": {"series": [{"id": "investing.usd", "data": [{"bucket_date": "2026-07-16", "rate": 1385.0}]}],
                  "bucket_size": "1d", "range": {"start": "a", "end": "b"}},
    }
    grouped_tether = {
        "tab": "tether", "period": "3m",
        "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
        "rate": _grouped_rate(usdt_krw=[{"source": "upbit", "asset": "usdt-krw", "rate": 1400.0,
                                         "timestamp": "2026-07-17T14:19:00+09:00"}]),
        "graph": {"series": [{"id": "bithumb.usdt-krw", "data": [{"bucket_date": "2026-07-16", "rate": 1401.0}]}],
                  "bucket_size": "1d", "range": {"start": "a", "end": "b"}},
    }
    # canonical 짝 통과
    assert free_snapshot.validate_snapshot_payload(flat_usd, "usd", "3m")
    assert free_snapshot.validate_snapshot_payload(grouped_tether, "tether", "3m")
    # grouped-usd → 거부(usd는 flat 기대)
    assert not free_snapshot.validate_snapshot_payload({**grouped_tether, "tab": "usd"}, "usd", "3m")
    # flat-tether → 거부(tether는 grouped 기대)
    assert not free_snapshot.validate_snapshot_payload({**flat_usd, "tab": "tether"}, "tether", "3m")


def test_precompute_grouped_not_misrecorded_as_failure(monkeypatch):
    """grouped payload도 written에 shape-무관 item 수로 집계 + 실패 오기록 없음(구 payload['rate']['entries'] KeyError 회귀 잠금)."""
    calls = {"exception": [], "info": []}

    class _FakeLogger:
        def warning(self, *a, **k):
            pass

        def exception(self, *a, **k):
            calls["exception"].append(a)

        def info(self, *a, **k):
            calls["info"].append((a, k))

    def grouped_payload(db, tab, period):
        return {
            "tab": tab, "period": period,
            "rate": _grouped_rate(
                usdt_krw=[{"source": "upbit", "asset": "usdt-krw"}, {"source": "bithumb", "asset": "usdt-krw"}],
                usd_krw_banks=[{"source": "kb", "asset": "usd-krw"}],
                usd_krw_reference={"source": "investing", "asset": "usd-krw"}),   # 총 4 item
            "graph": {"series": [{"id": "bithumb.usdt-krw", "data": [1]}]},
        }

    fake = _patch_precompute(monkeypatch, grouped_payload)
    monkeypatch.setattr(free_snapshot, "logger", _FakeLogger())
    free_snapshot.precompute_free_snapshots()

    # grouped를 KeyError로 실패 처리하지 않음
    assert calls["exception"] == []
    # 전부 SET(setex)
    assert len(fake.setex_keys) == len(free_snapshot.FREE_SNAPSHOT_TABS) * len(free_snapshot.FREE_SNAPSHOT_PERIODS)
    # written(info extra)에 grouped item 수(4) 집계
    written = [k["extra"]["written"] for a, k in calls["info"]
               if a and "✅" in str(a[0]) and k.get("extra", {}).get("written") is not None]
    assert written and written[0]["usd/3m"] == 4
