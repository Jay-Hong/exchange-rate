"""ADR-039 Step 3 — /api/v2/free/snapshot endpoint wiring 테스트.

harness = conftest(firebase stub + sqlite). lifespan 미진입(context manager 미사용)이라 cron 미실행.
serve는 cron canonical만 반환(DB 재생성 안 함). canonical 없음(Redis/local 비어있음) → 503. Redis mock으로 canonical 주입 검증.
"""
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app.main as main_module
from app import models
from app.database import engine
from app.main import app

models.Base.metadata.create_all(engine)


class TestFreeSnapshotEndpoint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        main_module._free_snapshot_local.clear()   # 테스트 간 last-good 격리(순서 의존 방지)

    def test_auth_failure_propagates(self):
        # 인증 실패(401)면 핸들러가 데이터/검증보다 먼저 401 전파 — 무인증 데이터 유출 없음.
        # (실 verify_firebase_token은 test 환경에 google.auth 미설치라 exercise 불가 → 401 raise를 주입해
        #  게이트가 데이터보다 앞선다는 계약만 결정적으로 검증.)
        from fastapi import HTTPException

        async def _raise_401(request, check_revoked=False):
            raise HTTPException(status_code=401, detail="unauthorized")

        with patch("app.main.verify_firebase_token", new=_raise_401):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 401)

    def test_unknown_free_tab_404(self):
        # tether는 N4(source_rates rate reader) 전까지 무료 미허용 → 404 (auth 통과 후).
        # FX 3탭(usd/jpy/eur)은 free_tabs에 포함 확인.
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "tether", "period": "3m"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "unknown_tab")
        free_tabs = r.json()["free_tabs"]
        for t in ("usd", "jpy", "eur"):
            self.assertIn(t, free_tabs)
        self.assertNotIn("tether", free_tabs)   # N4 경계 — tether는 아직 미허용

    def test_jpy_eur_are_free_tabs_not_404(self):
        # FX jpy/eur는 무료 허용(FREE_SNAPSHOT_TABS) → tab 게이트 통과. canonical 없으면 503(unknown_tab 404 아님).
        # Redis miss를 명시 mock해 503 결정성 확보(로컬/CI Redis에 canonical이 있어도 무관). 404였다면 tab 게이트 실패.
        for tab in ("jpy", "eur"):
            with self.subTest(tab=tab), \
                 patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
                 patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=None)):
                r = self.client.get("/api/v2/free/snapshot", params={"tab": tab, "period": "3m"})
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.json()["error"], "snapshot_unavailable")

    def test_serves_jpy_canonical_200(self):
        # jpy canonical 있으면 200 + asset=jpy-krw 보존 — serve 계약(validate_snapshot_payload)이 FX 3탭에 적용됨을 잠금.
        canonical = {
            "tab": "jpy", "period": "3m",
            "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
            "rate": {"asset": "jpy-krw", "entries": [
                {"bank": "kb", "currency": "jpy-krw", "rate": 921.5, "timestamp": "2026-07-17T14:19:00+09:00"}]},
            "graph": {"series": [{"id": "investing.jpy", "data": [{"bucket_date": "2026-07-16", "rate": 921.5}]}],
                      "bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(canonical))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "jpy", "period": "3m"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["rate"]["asset"], "jpy-krw")   # FX 3탭 serve 계약 (usd-only 아님)

    def test_unsupported_period_400(self):
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "5y"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "unsupported_period")

    def test_no_canonical_returns_503_never_fabricates(self):
        # cron canonical 없음(Redis/local 비어있음) → serve는 DB 최신값으로 fabricate하지 않고 503.
        # build를 raise로 패치해도 503이면 serve가 DB build를 호출하지 않음(무료=1시간 고정 불변식)을 잠근다.
        from app import free_snapshot

        def _boom(*a, **k):
            raise AssertionError("serve가 DB build를 호출하면 안 됨 (1시간 고정 불변식)")

        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(free_snapshot, "build_free_snapshot_payload", _boom):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"], "snapshot_unavailable")

    def test_serves_redis_canonical_with_serve_time_domain(self):
        # serve는 cron canonical의 값(rate/series/range)은 그대로 반환(rebuild 아님)하고,
        # serve-time X축 domain만 graph 블록에 부착(ADR-039 X축 통일). anchor=as_of, 3m=fixed_start.
        canonical = {
            "tab": "usd", "period": "3m",
            "as_of": "2026-07-17T14:30:00+09:00",
            "generated_at": "2026-07-17T14:30:20+09:00",
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:19:00+09:00"}]},
            "graph": {"series": [{"id": "investing.usd", "data": [{"bucket_date": "2026-07-16", "rate": 1385.0}]}],
                      "bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(canonical))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # canonical 값 보존 (rebuild 아님)
        self.assertEqual(body["rate"], canonical["rate"])
        self.assertEqual(body["graph"]["series"], canonical["graph"]["series"])
        self.assertEqual(body["graph"]["range"], canonical["graph"]["range"])
        self.assertEqual(body["as_of"], canonical["as_of"])
        # serve-time domain (graph envelope — 프리미엄 metadata와 다름)
        self.assertEqual(body["graph"]["live_domain_mode"], "fixed_start")
        self.assertEqual(body["graph"]["domain_end_at"], "2026-07-17T14:30:00+09:00")   # = as_of
        self.assertEqual(body["graph"]["domain_start_at"], "2026-04-18T00:00:00+09:00")  # as_of 날짜(07-17)-90 00:00
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_redis_success_then_miss_serves_last_good(self):
        # Redis 성공 → process-local last-good 보존 → 이후 Redis miss(None)여도 마지막 canonical 반환.
        # keep-last-good(availability) + 1시간 고정(값 불변) 동시 확인.
        canonical = {
            "tab": "usd", "period": "3m",
            "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:19:00+09:00"}]},
            "graph": {"series": [{"id": "investing.usd", "data": [{"bucket_date": "2026-07-16", "rate": 1385.0}]}],
                      "bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")):
            with patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(canonical))):
                r1 = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
            self.assertEqual(r1.status_code, 200)   # Redis 성공 → local seeded
            with patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=None)):
                r2 = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r2.status_code, 200)          # Redis miss여도 last-good
        body2 = r2.json()
        self.assertEqual(body2["graph"]["series"], canonical["graph"]["series"])   # 마지막 canonical 값 불변
        self.assertEqual(body2["graph"]["domain_end_at"], canonical["as_of"])      # + serve-time domain(anchor=as_of)
        # 저장된 last-good은 domain 없는 canonical(원본 불변 — attach는 copy에만)
        self.assertNotIn("domain_start_at", main_module._free_snapshot_local["free:snapshot:usd:3m"]["graph"])

    def test_refresh_not_before_attached_both_paths_not_stored_not_on_503(self):
        # codex Medium 3 — refresh_not_before wiring 잠금:
        # (1) Redis 성공 응답에 존재 + tz-aware +09:00 (2) local last-good 응답에도 존재
        # (3) 저장된 canonical(_free_snapshot_local)엔 미저장 (4) 503엔 미부착.
        from datetime import datetime

        canonical = {
            "tab": "usd", "period": "3m",
            "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:19:00+09:00"}]},
            "graph": {"series": [{"id": "investing.usd", "data": [{"bucket_date": "2026-07-16", "rate": 1385.0}]}],
                      "bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")):
            with patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(canonical))):
                r1 = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
            # (1) Redis 성공 → 필드 존재 + tz-aware +09:00
            self.assertEqual(r1.status_code, 200)
            rnb1 = r1.json()["refresh_not_before"]
            parsed = datetime.fromisoformat(rnb1)
            self.assertIsNotNone(parsed.tzinfo)
            self.assertEqual(parsed.utcoffset().total_seconds(), 9 * 3600)   # +09:00
            # (3) 저장된 last-good canonical엔 미저장(attach는 copy에만)
            self.assertNotIn("refresh_not_before", main_module._free_snapshot_local["free:snapshot:usd:3m"])
            # (2) Redis miss → local last-good 응답에도 부착
            with patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=None)):
                r2 = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
            self.assertEqual(r2.status_code, 200)
            self.assertIn("refresh_not_before", r2.json())
        # (4) 503(canonical 전무)엔 미부착
        main_module._free_snapshot_local.clear()
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=None)):
            r3 = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r3.status_code, 503)
        self.assertNotIn("refresh_not_before", r3.json())

    def test_malformed_redis_no_local_returns_503(self):
        # 필수 필드 누락(as_of/generated_at 없음) canonical → validate 거부 → local 없음 → 503 (오염 200 방지).
        bad = {
            "tab": "usd", "period": "3m",
            "rate": {"asset": "usd-krw", "entries": [{"currency": "usd-krw"}]},
            "graph": {"series": [{"id": "investing.usd", "data": [1]}], "range": {}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(bad))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"], "snapshot_unavailable")

    def test_rejects_canonical_with_point_beyond_as_of(self):
        # serve-time cutoff(codex 2026-07-18): as_of 초과 graph point를 가진 canonical(배포 전 구 데이터/오염)은
        # validate가 거부 → local 없음 → 503 (last-good seed 차단).
        bad = {
            "tab": "usd", "period": "3m",
            "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:29:00+09:00"}]},
            "graph": {"series": [{"id": "investing.usd",
                                  "data": [{"ts": "2026-07-17T15:00:00+09:00", "rate": 1385.0}]}],   # as_of 초과
                      "bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(bad))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"], "snapshot_unavailable")

    def test_rejects_canonical_with_rate_beyond_as_of(self):
        # serve-time rate cutoff(codex 2026-07-18): as_of 초과 rate entry를 가진 canonical(구 get_all_rates_flat)은
        # graph와 대칭으로 validate가 거부 → local 없음 → 503.
        bad = {
            "tab": "usd", "period": "3m",
            "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:45:00+09:00"}]},  # as_of 초과
            "graph": {"series": [{"id": "investing.usd", "data": [{"bucket_date": "2026-07-16", "rate": 1385.0}]}],
                      "bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(bad))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"], "snapshot_unavailable")

    def test_rejects_off_grid_as_of_canonical(self):
        # serve-time HH:30 grid(codex 2026-07-18): as_of가 :30 아니면(구 :00 floor / 오염) 거부 → 503.
        bad = {
            "tab": "usd", "period": "3m",
            "as_of": "2026-07-17T14:00:00+09:00", "generated_at": "2026-07-17T14:20:03+09:00",   # off-grid :00
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T13:59:00+09:00"}]},   # as_of 이하(cutoff 통과)
            "graph": {"series": [{"id": "investing.usd", "data": [{"bucket_date": "2026-07-16", "rate": 1385.0}]}],
                      "bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(bad))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 503)   # cutoff은 통과하나 off-grid라 거부
        self.assertEqual(r.json()["error"], "snapshot_unavailable")

    def test_serves_1d_canonical(self):
        # 1d가 이제 무료 지원 period → 400 아님. 10min bucket canonical이 validate 통과 + 서빙.
        canonical = {
            "tab": "usd", "period": "1d",
            "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:19:00+09:00"}]},
            "graph": {"series": [{"id": "investing.usd", "data": [[123, 1.0, 2.0, 1385.0]]}],
                      "bucket_size": "10min", "range": {"start": "2026-07-16", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(canonical))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "1d"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["graph"]["bucket_size"], "10min")

    def test_naive_as_of_canonical_rejected_503(self):
        # P2#1 실제 endpoint 계약: naive as_of canonical은 attach의 fallback 이전에 validate가 as_of tz-aware assert로
        # 거부 → last-good 없으면 503. attach의 canonical-그대로 fallback은 도달 불가한 defense-in-depth.
        bad = {
            "tab": "usd", "period": "3m",
            "as_of": "2026-07-17T14:30:00", "generated_at": "2026-07-17T14:30:20",   # naive
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:19:00+09:00"}]},  # aware
            "graph": {"series": [{"id": "investing.usd", "data": [{"bucket_date": "2026-07-16", "rate": 1385.0}]}],
                      "bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(bad))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"], "snapshot_unavailable")

    def test_fully_naive_as_of_canonical_rejected_503(self):
        # codex 2026-07-19: as_of와 모든 timestamp가 naive면 cutoff 비교(naive-vs-naive)가 예외 없이 통과 →
        # as_of tz-aware assert가 없으면 fully-naive 오염 canonical이 새어 domain 없이 200. tz-aware assert로 503 잠금.
        bad = {
            "tab": "usd", "period": "3m",
            "as_of": "2026-07-17T14:30:00", "generated_at": "2026-07-17T14:30:20",   # naive
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:19:00"}]},   # naive
            "graph": {"series": [{"id": "investing.usd", "data": [{"bucket_date": "2026-07-16", "rate": 1385.0}]}],
                      "bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(bad))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"], "snapshot_unavailable")

    def test_1d_serve_time_domain_rolling(self):
        # 1d = rolling domain([as_of-24h, as_of]) — graph 블록에 부착.
        canonical = {
            "tab": "usd", "period": "1d",
            "as_of": "2026-07-17T14:30:00+09:00", "generated_at": "2026-07-17T14:30:20+09:00",
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:19:00+09:00"}]},
            "graph": {"series": [{"id": "investing.usd", "data": [[123, 1.0, 2.0, 1385.0]]}],
                      "bucket_size": "10min", "range": {"start": "2026-07-16", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(canonical))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "1d"})
        self.assertEqual(r.status_code, 200)
        g = r.json()["graph"]
        self.assertEqual(g["live_domain_mode"], "rolling")
        self.assertEqual(g["domain_start_at"], "2026-07-16T14:30:00+09:00")   # as_of - 24h
        self.assertEqual(g["domain_end_at"], "2026-07-17T14:30:00+09:00")     # = as_of


if __name__ == "__main__":
    unittest.main()
