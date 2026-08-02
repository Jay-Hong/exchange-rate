"""snapshot-on-subscribe e2e wire 테스트 (realtime topic release readiness smoke harness 1).

목적: subscriber=0이라 그동안 미검증이던 **실 WebSocket wire 경로**를 자동 회귀로 잠근다 —
TestClient ws connect → subscribe → send_initial_snapshots → to_thread → ws.send_json →
client 수신. 즉 "구독하면 snapshot이 실제로 날아간다"를 ASGI transport 레벨로 검증.

flakiness 방어 (feedback_flaky_sleep_async_tests):
- `_build_snapshot_sync`를 canned로 patch → wire를 builder/Redis/DB와 분리(builder 로직은
  test_topic_initial_snapshot.py 단위 테스트).
- 모든 receive는 thread + join **timeout** 경유(`_receive_json`) → snapshot 미전달 regression
  시 hang이 아니라 fast assertion fail (codex 019efdd5 blocker). green path는 patch로 보장돼
  즉시 도착(no sleep).
- redis_cache.get을 None으로 patch → connect 시 Redis 연결 지연/불확정 제거(→ DB fallback
  legacy payload, 빈 테이블).
- conftest: firebase stub + file-backed sqlite. lifespan(scheduler)은 TestClient context
  manager 미사용으로 미진입.

scope: connect→subscribe→snapshot 수신(fx + usdt) + reconnect→재수신. send-failure cleanup은
단위 테스트(test_topic_initial_snapshot.py), live/synthetic publish 수신은 별도 follow-up.
"""
import asyncio
import contextlib
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

# conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 설정.
from app import config, models
from app.database import engine
from app.krx_topic_publisher import KRX_TOPIC
from app import main as app_main
from app import topic_dispatcher
from app.main import app
from app.subscription import PremiumStatus

models.Base.metadata.create_all(engine)  # 빈 테이블 (connect 초기 legacy payload용)


def _canned_snapshot(topic):
    return {"type": "snapshot", "version": 1, "topic": topic, "data": {"_canned": topic}}


class TestSnapshotOnSubscribeE2E(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)  # context manager 미사용 → lifespan 미진입

    def _ctx(self):
        """FF on + canned builder + redis None patch 컨텍스트."""
        return (
            patch.object(config, "TOPIC_DISPATCHER_ENABLED", True),
            patch.object(config, "FX_TOPIC_ENABLED", True),
            patch(
                "app.topic_initial_snapshot._build_snapshot_sync",
                side_effect=_canned_snapshot,
            ),
            patch("app.main.redis_cache.get", new=AsyncMock(return_value=None)),
        )

    def _receive_json(self, ws, timeout=5.0):
        """ws.receive_json()을 thread+join timeout으로 감쌈 — regression(미전달) 시 hang 대신
        fast assertion fail (codex 019efdd5: TestClient receive_json 자체엔 timeout 없음).
        green path는 patch로 보장돼 즉시 반환."""
        box = {}

        def _recv():
            try:
                box["msg"] = ws.receive_json()
            except Exception as exc:  # noqa: BLE001 — 그대로 재전파
                box["err"] = exc

        t = threading.Thread(target=_recv, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self.fail(f"ws.receive_json {timeout}s timeout — 메시지 미전달(regression)")
        if "err" in box:
            raise box["err"]
        return box["msg"]

    def _recv_snapshot(self, ws, topic, max_msgs=4):
        """초기 legacy payload(type=rates 등)를 건너뛰고 해당 topic snapshot을 찾음.

        각 receive는 timeout 경유 → snapshot 미전달 시 max_msgs 도달 전이라도 fast fail.
        """
        for _ in range(max_msgs):
            msg = self._receive_json(ws)
            if msg.get("type") == "snapshot" and msg.get("topic") == topic:
                return msg
        self.fail(f"snapshot for {topic} not received within {max_msgs} messages")

    def test_subscribe_fx_receives_snapshot(self):
        p1, p2, p3, p4 = self._ctx()
        with p1, p2, p3, p4:
            with self.client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "subscribe", "topics": ["fx:usd-krw"]})
                snap = self._recv_snapshot(ws, "fx:usd-krw")
        self.assertEqual(snap["version"], 1)
        self.assertEqual(snap["data"], {"_canned": "fx:usd-krw"})

    def test_subscribe_tether_receives_snapshot(self):
        p1, p2, p3, p4 = self._ctx()
        with p1, p2, p3, p4:
            with self.client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "subscribe", "topics": ["usdt:krw"]})
                snap = self._recv_snapshot(ws, "usdt:krw")
        self.assertEqual(snap["topic"], "usdt:krw")

    def test_subscribe_multiple_topics_receives_each_snapshot(self):
        p1, p2, p3, p4 = self._ctx()
        with p1, p2, p3, p4:
            with self.client.websocket_connect("/ws") as ws:
                ws.send_json(
                    {"type": "subscribe", "topics": ["fx:usd-krw", "usdt:krw"]}
                )
                # 두 snapshot 모두 도착 (순서 무관 — topic으로 식별)
                got = set()
                for _ in range(6):
                    msg = self._receive_json(ws)
                    if msg.get("type") == "snapshot":
                        got.add(msg.get("topic"))
                    if {"fx:usd-krw", "usdt:krw"} <= got:
                        break
        self.assertEqual(got & {"fx:usd-krw", "usdt:krw"}, {"fx:usd-krw", "usdt:krw"})

    def test_reconnect_resubscribe_receives_snapshot_again(self):
        """재연결(새 connection) 후 subscribe → snapshot 재수신 (resync)."""
        p1, p2, p3, p4 = self._ctx()
        with p1, p2, p3, p4:
            with self.client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "subscribe", "topics": ["fx:usd-krw"]})
                self._recv_snapshot(ws, "fx:usd-krw")
            # 새 connection
            with self.client.websocket_connect("/ws") as ws2:
                ws2.send_json({"type": "subscribe", "topics": ["fx:usd-krw"]})
                snap = self._recv_snapshot(ws2, "fx:usd-krw")
        self.assertEqual(snap["topic"], "fx:usd-krw")


class TestTopicSnapshotRestBootstrap(unittest.TestCase):
    """GET /api/v2/topics/snapshot — REST bootstrap (OPEN 1 해소). _build_snapshot_sync patch로 격리.

    ⚠️ ADR-039 §8.1 E3 land 이후 이 endpoint는 **인증+premium**을 요구한다 → dormant 케이스를
    제외한 모든 케이스가 auth/premium을 patch해야 한다(그 자체가 계약 문서).
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def _authed(self, uid="rest-twin-user"):
        """인증 통과 + premium ACTIVE patch 쌍 (E3 게이트 통과용)."""
        return (
            patch("app.main.verify_firebase_token", new=AsyncMock(return_value=uid)),
            patch("app.main.require_premium", new=AsyncMock(return_value=True)),
        )

    def test_dormant_when_dispatcher_disabled(self):
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            r = self.client.get("/api/v2/topics/snapshot", params={"topic": "usdt:krw"})
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertEqual(body["error"], "topics_disabled")
        self.assertNotIn("supported_topics", body)  # dormant 시 미노출 (codex 019efe2d)

    def test_unknown_topic_404(self):
        p_auth, p_prem = self._authed()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), p_auth, p_prem:
            r = self.client.get("/api/v2/topics/snapshot", params={"topic": "dxy"})
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertEqual(body["error"], "unknown_topic")
        self.assertIn("usdt:krw", body["supported_topics"])

    def test_supported_topic_returns_snapshot_with_no_store(self):
        canned = {"type": "snapshot", "version": 1, "topic": "usdt:krw",
                  "data": {"_canned": True}}
        p_auth, p_prem = self._authed()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), p_auth, p_prem, \
             patch("app.topic_initial_snapshot._build_snapshot_sync", return_value=canned):
            r = self.client.get("/api/v2/topics/snapshot", params={"topic": "usdt:krw"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), canned)  # WS snapshot과 동일 contract
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_topic_unavailable_when_build_none(self):
        """지원 topic이나 _build_snapshot_sync None(예: fx FX_TOPIC_ENABLED off) → 404 topic_unavailable."""
        p_auth, p_prem = self._authed()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), p_auth, p_prem, \
             patch("app.topic_initial_snapshot._build_snapshot_sync", return_value=None):
            r = self.client.get("/api/v2/topics/snapshot", params={"topic": "fx:usd-krw"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "topic_unavailable")


class TestTopicSnapshotRestAuthGate(unittest.TestCase):
    """ADR-039 §8.1 **E3** — REST twin 인증·premium·KRX per-user 게이트.

    배경: 이 endpoint는 `TOPIC_DISPATCHER_ENABLED`로만 막혀 있었다(off → 404). 그런데 1C는 그
    flag를 켜야 동작하므로, 게이트 없이 flag를 켜면 **무인증 KRX REST 경로가 되열린다**
    (운영은 현재 flag off로 완화 중). → flag ON *이전에* 이 게이트가 land해야 한다.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def _get(self, topic="usdt:krw"):
        return self.client.get("/api/v2/topics/snapshot", params={"topic": topic})

    # ── 인증 ────────────────────────────────────────────────────────────────
    def test_dormant_does_not_invoke_auth(self):
        """flag off는 인증보다 먼저 — dormant 계약 보존 + 불필요한 Firebase RTT 회피."""
        auth = AsyncMock(return_value="uid")
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False), \
             patch("app.main.verify_firebase_token", new=auth):
            r = self._get()
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "topics_disabled")
        auth.assert_not_awaited()

    def test_unauthenticated_401_and_no_downstream_work(self):
        """401이면 premium 판정·DB 세션·entitlement 조회·빌드 **어디에도 도달하지 않는다**(부수효과 0).

        ⚠️ `KRX_CLIENT_DISTRIBUTION_EFFECTIVE`를 **켜고** 검사해야 한다 — 기본값 false에선
        가시성 helper가 조기 반환해 DB를 안 건드리므로, 조회를 인증 앞으로 옮기는 회귀가 나도
        `compute_krx_visible` 미호출 단언이 조용히 통과한다(vacuous). 세션 미오픈까지 함께 잠근다.
        """
        from fastapi import HTTPException

        async def _raise_401(request, check_revoked=False):
            raise HTTPException(status_code=401, detail="Unauthorized")

        build = MagicMock(return_value=_canned_snapshot("usdt:krw"))
        premium = AsyncMock(return_value=True)
        krx = MagicMock(return_value=True)
        session = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch("app.main.verify_firebase_token", new=_raise_401), \
             patch("app.main.require_premium", new=premium), \
             patch("app.database.SessionLocal", new=session), \
             patch("app.entitlements.compute_krx_visible", new=krx), \
             patch("app.topic_initial_snapshot._build_snapshot_sync", new=build):
            r = self._get()
        self.assertEqual(r.status_code, 401)
        premium.assert_not_awaited()
        session.assert_not_called()
        krx.assert_not_called()
        build.assert_not_called()
        self.assertNotIn("supported_topics", r.json())  # 미인증자는 topic 열거 불가

    def test_authenticated_uid_is_used_for_premium_and_entitlement(self):
        """인증이 돌려준 uid가 **그대로** premium·entitlement 판정에 쓰여야 한다
        (상수/다른 uid로 판정하면 남의 권한으로 서빙된다).

        게이팅 topic(krx)으로 요청해야 entitlement 경로까지 관통한다 — 비-게이팅 topic은
        판정이 결과를 못 바꿔 조회를 생략하기 때문(`resolve_snapshot_topic_access_sync`).
        """
        premium = AsyncMock(return_value=True)
        krx = MagicMock(return_value=True)
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch("app.main.verify_firebase_token",
                   new=AsyncMock(return_value="uid-from-token")), \
             patch("app.main.require_premium", new=premium), \
             patch("app.entitlements.compute_krx_visible", new=krx), \
             patch("app.topic_initial_snapshot._build_snapshot_sync",
                   return_value=_canned_snapshot("krx:usd-krw-futures")):
            r = self._get(topic="krx:usd-krw-futures")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(premium.await_args.args[0], "uid-from-token")
        self.assertEqual(krx.call_args.args[1], "uid-from-token")

    def test_premium_verdict_is_forwarded_not_assumed(self):
        """`premium_active`는 require_premium **반환값**이어야 한다(핸들러의 True 하드코딩 금지).

        ⚠️ 이 테스트가 잠그는 건 핸들러의 하드코딩까지다. `require_premium` 자체는 PENDING·INACTIVE가
        아닌 **모든** 상태를 True로 접으므로(main.py `require_premium`), premium 상태가 새로 늘 때의
        fail-open은 그 공통 helper에서 별도로 막아야 한다 — 이 테스트의 보장 범위 밖.
        """
        krx = MagicMock(return_value=True)
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch("app.main.require_premium", new=AsyncMock(return_value=False)), \
             patch("app.entitlements.compute_krx_visible", new=krx), \
             patch("app.topic_initial_snapshot._build_snapshot_sync",
                   return_value=_canned_snapshot("krx:usd-krw-futures")):
            self._get(topic="krx:usd-krw-futures")
        self.assertIs(krx.call_args.kwargs["premium_active"], False)

    def test_error_responses_are_no_store(self):
        """개인화 404(비-entitled의 KRX 포함)도 캐시 금지 — private 캐시가 404를 저장하면
        entitlement 부여 뒤에도 404가 남는다."""
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch("app.main.require_premium", new=AsyncMock(return_value=True)):
            unknown = self._get(topic="dxy")
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            dormant = self._get()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch("app.main.require_premium", new=AsyncMock(return_value=True)), \
             patch("app.topic_initial_snapshot._build_snapshot_sync", return_value=None):
            unavailable = self._get(topic="fx:usd-krw")
        for label, resp in (("unknown", unknown), ("dormant", dormant),
                            ("unavailable", unavailable)):
            with self.subTest(label):
                self.assertEqual(resp.headers.get("cache-control"), "no-store")

    def test_unauthenticated_unknown_topic_also_401(self):
        """미인증이면 unknown topic도 401 — 인증이 topic 판정보다 먼저."""
        from fastapi import HTTPException

        async def _raise_401(request, check_revoked=False):
            raise HTTPException(status_code=401, detail="Unauthorized")

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.main.verify_firebase_token", new=_raise_401):
            r = self._get(topic="dxy")
        self.assertEqual(r.status_code, 401)

    # ── premium ────────────────────────────────────────────────────────────
    def test_premium_inactive_403_and_no_build(self):
        build = MagicMock(return_value=_canned_snapshot("usdt:krw"))
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.main.verify_firebase_token", new=AsyncMock(return_value="free-user")), \
             patch("app.main.verify_premium_status",
                   new=AsyncMock(return_value=PremiumStatus.INACTIVE)), \
             patch("app.topic_initial_snapshot._build_snapshot_sync", new=build):
            r = self._get()
        self.assertEqual(r.status_code, 403)
        build.assert_not_called()

    def test_premium_pending_503(self):
        """PENDING은 거부가 아니라 **재시도**(require_premium 공통 정책) — 503 + Retry-After."""
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.main.verify_firebase_token", new=AsyncMock(return_value="pending-user")), \
             patch("app.main.verify_premium_status",
                   new=AsyncMock(return_value=PremiumStatus.PENDING)):
            r = self._get()
        self.assertEqual(r.status_code, 503)
        self.assertIn("retry-after", {k.lower() for k in r.headers})

    def test_premium_active_serves(self):
        canned = _canned_snapshot("usdt:krw")
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.main.verify_firebase_token", new=AsyncMock(return_value="paid-user")), \
             patch("app.main.verify_premium_status",
                   new=AsyncMock(return_value=PremiumStatus.ACTIVE)), \
             patch("app.topic_initial_snapshot._build_snapshot_sync", return_value=canned):
            r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), canned)

    # ── KRX per-user (§3.2 존재 완전 비노출) ────────────────────────────────
    def _krx_ctx(self, krx_visible):
        """전역 KRX 게이트 on + per-user 가시성 주입."""
        return (
            patch.object(config, "TOPIC_DISPATCHER_ENABLED", True),
            patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True),
            patch("app.main.verify_firebase_token", new=AsyncMock(return_value="paid-user")),
            patch("app.main.require_premium", new=AsyncMock(return_value=True)),
            patch("app.entitlements.compute_krx_visible", return_value=krx_visible),
        )

    def test_krx_topic_hidden_from_non_entitled_premium_user(self):
        """entitlement 없으면 KRX는 **미지원 topic과 구분 불가**해야 한다(존재 비노출)."""
        build = MagicMock(return_value=_canned_snapshot("krx:usd-krw-futures"))
        a, b, c, d, e = self._krx_ctx(krx_visible=False)
        with a, b, c, d, e, \
             patch("app.topic_initial_snapshot._build_snapshot_sync", new=build):
            r = self._get(topic="krx:usd-krw-futures")
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertEqual(body["error"], "unknown_topic")
        build.assert_not_called()
        # 에코에도 존재하면 안 됨 — 404 코드만 맞추고 목록으로 새는 회귀 차단
        self.assertNotIn("krx:usd-krw-futures", body["supported_topics"])

    def test_supported_topics_echo_hides_krx_from_non_entitled(self):
        """다른 unknown topic 요청의 에코에서도 KRX가 빠진다."""
        a, b, c, d, e = self._krx_ctx(krx_visible=False)
        with a, b, c, d, e:
            r = self._get(topic="dxy")
        body = r.json()
        self.assertEqual(body["error"], "unknown_topic")
        self.assertNotIn("krx:usd-krw-futures", body["supported_topics"])
        self.assertIn("usdt:krw", body["supported_topics"])  # 다른 topic은 정상 노출

    def test_krx_topic_served_to_entitled_user(self):
        canned = _canned_snapshot("krx:usd-krw-futures")
        a, b, c, d, e = self._krx_ctx(krx_visible=True)
        with a, b, c, d, e, \
             patch("app.topic_initial_snapshot._build_snapshot_sync", return_value=canned):
            r = self._get(topic="krx:usd-krw-futures")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), canned)


class TestTopicSnapshotEntitlementLookupScope(unittest.TestCase):
    """entitlement 조회는 **판정을 바꿀 수 있는 요청에서만** 일어난다 (ADR-039 §8.1 E3).

    견고성 속성: FX/USDT builder는 Redis-first라 warm이면 DB 커넥션 0개다. 무조건 조회하면
    그 요청이 DB 필수로 승격돼, entitlement DB 순단 하나로 entitlement와 무관한 FX bootstrap이
    죽고 콜드런치 4건(FX 3 동시 + tether 1)이 좁은 풀(3+2)을 동시에 문다.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def _probe(self, topic):
        """(compute_krx_visible mock, SessionLocal mock, 응답)"""
        krx = MagicMock(return_value=False)
        session = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch("app.main.require_premium", new=AsyncMock(return_value=True)), \
             patch("app.database.SessionLocal", new=session), \
             patch("app.entitlements.compute_krx_visible", new=krx), \
             patch("app.topic_initial_snapshot._build_snapshot_sync",
                   side_effect=_canned_snapshot):
            r = self.client.get("/api/v2/topics/snapshot", params={"topic": topic})
        return krx, session, r

    def test_known_non_gated_topic_skips_entitlement_lookup(self):
        """usdt:krw / fx:* 는 KRX 가시성이 판정을 못 바꾼다 → 조회·세션 0."""
        for topic in ("usdt:krw", "fx:usd-krw"):
            with self.subTest(topic=topic):
                krx, session, r = self._probe(topic)
                self.assertEqual(r.status_code, 200)
                krx.assert_not_called()
                session.assert_not_called()

    def test_unknown_topic_still_consults_entitlement(self):
        """⛔ 미지원 topic을 조회 없이 조기 반환하면 §3.2가 깨진다 — 에코에 KRX가 실리고,
        비-entitled KRX(조회 1회)와 지연으로 갈린다. 그 '다음 최적화'를 여기서 금지한다."""
        krx, session, r = self._probe("dxy")
        self.assertEqual(r.status_code, 404)
        krx.assert_called_once()
        self.assertNotIn("krx:usd-krw-futures", r.json()["supported_topics"])

    def test_gated_topic_consults_entitlement(self):
        krx, session, r = self._probe("krx:usd-krw-futures")
        self.assertEqual(r.status_code, 404)
        krx.assert_called_once()


class TestTopicSnapshotTransientDbFailure(unittest.TestCase):
    """DB 순단의 HTTP 계약 (ADR-039 §6.1 pre-flip 필수, 2026-07-26).

    entitlement 조회/빌드가 DB 장애로 실패하면 지금까지 **plain 500**(exception handler 0건)이라
    클라 재시도 분류에서 빠졌다. 인프라 transient는 결함이 아니므로 **503**이 의미상 맞다.

    계약:
    - 경계는 **`TRANSIENT_DB_ERRORS` 4종**(`OperationalError`/`InterfaceError`/`TimeoutError`[풀 고갈]/
      `DisconnectionError`) **∧ `is_transient_db_error`**(영구 SQLSTATE deny-list). 그 밖은 그대로
      500으로 올려 **버그를 가리지 않는다**. `SQLAlchemyError` 전체는 ProgrammingError·
      InvalidRequestError 등 영구 결함을 포함해 **너무 넓다**(codex Major).
    - 바디는 이 endpoint의 오류 schema(`{"error": ...}`)를 따르고 WS twin과 **같은 어휘**
      (`temporarily_unavailable`, §8-C "판정 불가")를 쓴다.
    - **`Retry-After` 없음** — 이 헤더는 구독 판정 PENDING(초 단위)의 신호로 이미 쓰이고 있고,
      DB failover는 분 단위라 5초 재시도를 지시하면 storm이 된다. 클라 자체 backoff에 맡긴다.
    - §3.2: DB 장애에서도 **비-entitled KRX와 미지원 topic의 응답이 구분 불가**여야 한다.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def _get(self, topic, *, visibility_exc=None, build_exc=None):
        from app.topic_initial_snapshot import SnapshotTopicAccess

        visibility = (MagicMock(side_effect=visibility_exc) if visibility_exc
                      else MagicMock(return_value=SnapshotTopicAccess(True, None)))
        build = (MagicMock(side_effect=build_exc) if build_exc
                 else MagicMock(return_value=_canned_snapshot(topic)))
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch("app.main.require_premium", new=AsyncMock(return_value=True)), \
             patch("app.topic_initial_snapshot.resolve_snapshot_topic_access_sync",
                   new=visibility), \
             patch("app.topic_initial_snapshot._build_snapshot_sync", new=build):
            r = self.client.get("/api/v2/topics/snapshot", params={"topic": topic})
        return r, visibility, build

    def _assert_transient(self, r):
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"error": "temporarily_unavailable"})
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        self.assertNotIn("retry-after", {k.lower() for k in r.headers})

    def test_entitlement_lookup_db_failure_is_503(self):
        from sqlalchemy.exc import OperationalError
        exc = OperationalError("SELECT 1", {}, Exception("connection lost"))
        r, _, build = self._get("krx:usd-krw-futures", visibility_exc=exc)
        self._assert_transient(r)
        build.assert_not_called()          # 인가 판정 전이라 빌드에 도달하면 안 된다

    def test_pool_timeout_is_503(self):
        """`sqlalchemy.exc.TimeoutError`(풀 고갈)도 같은 계약 — 좁은 풀(3+2)에서 실현 가능하다."""
        from sqlalchemy.exc import TimeoutError as SATimeoutError
        r, _, _ = self._get("krx:usd-krw-futures", visibility_exc=SATimeoutError("pool"))
        self._assert_transient(r)

    def test_builder_db_failure_is_503(self):
        """빌드 경로도 같이 감싸야 한다 — 한쪽만 고치면 상태코드가 topic 종류에 따라 갈린다."""
        from sqlalchemy.exc import OperationalError
        exc = OperationalError("SELECT 1", {}, Exception("connection lost"))
        r, _, _ = self._get("usdt:krw", build_exc=exc)
        self._assert_transient(r)

    def test_non_db_exception_is_not_masked(self):
        """`except Exception`이었다면 **영구 결함이 무한 재시도로 안내**된다 — 500으로 남긴다."""
        with self.assertRaises(ValueError):
            self._get("usdt:krw", build_exc=ValueError("programming bug"))

    def test_sqlalchemy_programming_errors_are_not_masked(self):
        """⚠️ **`except SQLAlchemyError`는 너무 넓다** — 이 음성 테스트가 그걸 잡는다 (codex Major).

        구 초안은 `SQLAlchemyError` 전체를 잡고 "그 밖은 500"이라 주장했는데, 아래 예외들이 전부
        그 하위라 **영구 결함이 503(재시도 안내)으로 오분류**됐다. `ValueError`만으로 검증한
        음성 테스트로는 이 회귀를 못 잡는다 — 같은 계열에서 골라야 한다.
        """
        from sqlalchemy.exc import ProgrammingError, InvalidRequestError, ResourceClosedError

        cases = [
            ProgrammingError("SELECT x", {}, Exception("relation does not exist")),  # migration 누락
            InvalidRequestError("bad join"),                                          # ORM 사용 결함
            ResourceClosedError("session closed"),                                    # 상태 결함
        ]
        for exc in cases:
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(type(exc)):
                    self._get("krx:usd-krw-futures", visibility_exc=exc)
                with self.assertRaises(type(exc)):
                    self._get("usdt:krw", build_exc=exc)

    def test_permanent_config_errors_are_not_masked(self):
        """클래스만으로는 못 거르는 영구 오류 — 인증 실패·DB 부재는 `OperationalError`로 온다.

        재시도로 절대 안 풀리므로 503(재시도 안내)이 아니라 500이어야 한다 (codex Medium).
        """
        from sqlalchemy.exc import OperationalError

        class _Orig(Exception):
            def __init__(self, sqlstate):
                self.sqlstate = sqlstate

        for state, label in (("28P01", "invalid_password"), ("3D000", "invalid_catalog_name"),
                             ("28000", "invalid_authorization"), ("42501", "insufficient_privilege")):
            with self.subTest(sqlstate=state, label=label):
                exc = OperationalError("SELECT 1", {}, _Orig(state))
                with self.assertRaises(OperationalError):
                    self._get("krx:usd-krw-futures", visibility_exc=exc)

    def test_connect_failure_without_sqlstate_is_transient(self):
        """⚠️ **가장 중요한 케이스** — RDS 도달 불가/failover는 서버 응답이 없어 SQLSTATE가 없다.

        psycopg 3.3 실측: 도달 불가 호스트 연결 → `OperationalError`, `sqlstate is None`.
        SQLSTATE allow-list였다면 이게 500이 되어 이 503의 존재 이유가 사라진다 —
        그래서 분류는 **deny-list**(모르면 transient)다.
        """
        from sqlalchemy.exc import OperationalError

        class _Orig(Exception):
            sqlstate = None

        r, _, _ = self._get("krx:usd-krw-futures",
                            visibility_exc=OperationalError("connect", {}, _Orig()))
        self._assert_transient(r)

    def test_unknown_sqlstate_is_transient(self):
        """모르는 SQLSTATE도 transient — 회복 가능한 상태를 포기하는 쪽이 더 나쁜 오류다."""
        from sqlalchemy.exc import OperationalError

        class _Orig(Exception):
            sqlstate = "53300"   # too_many_connections (실제로 transient)

        r, _, _ = self._get("usdt:krw", build_exc=OperationalError("x", {}, _Orig()))
        self._assert_transient(r)

    def test_real_resolver_db_failure_keeps_krx_indistinguishable(self):
        """helper를 mock하지 않고 **실제 resolver + to_thread를 관통**시켜 §3.2를 확인한다.

        위 `_get`은 `resolve_snapshot_topic_access_sync`를 통째로 mock하므로 "handler seam에서
        예외가 나면 503"만 증명한다. 여기서는 **실제 entitlement 조회 지점**
        (`compute_krx_visible`)에서 DB 예외를 내 KRX와 미지원 topic이 상태·본문·헤더까지
        구분 불가한지 본다.
        """
        from sqlalchemy.exc import OperationalError
        exc = OperationalError("SELECT 1", {}, Exception("down"))

        def _fetch(topic):
            with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
                 patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
                 patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
                 patch("app.main.require_premium", new=AsyncMock(return_value=True)), \
                 patch("app.database.SessionLocal", return_value=MagicMock()), \
                 patch("app.entitlements.compute_krx_visible", side_effect=exc):
                return self.client.get("/api/v2/topics/snapshot", params={"topic": topic})

        krx = _fetch("krx:usd-krw-futures")
        unknown = _fetch("dxy")
        self._assert_transient(krx)
        self.assertEqual(krx.status_code, unknown.status_code)
        self.assertEqual(krx.json(), unknown.json())
        self.assertEqual(krx.headers.get("cache-control"), unknown.headers.get("cache-control"))
        self.assertEqual(krx.headers.get("retry-after"), unknown.headers.get("retry-after"))

    def test_krx_and_unknown_topic_are_indistinguishable_under_db_failure(self):
        """§3.2 — DB가 죽어도 비-entitled KRX와 미지원 topic의 응답이 같아야 한다.

        (둘 다 per-user 판정 경로를 타므로 같은 지점에서 실패한다.)
        """
        from sqlalchemy.exc import OperationalError
        exc = OperationalError("SELECT 1", {}, Exception("down"))
        krx, _, _ = self._get("krx:usd-krw-futures", visibility_exc=exc)
        unknown, _, _ = self._get("dxy", visibility_exc=exc)
        self.assertEqual((krx.status_code, krx.json()), (unknown.status_code, unknown.json()))
        self._assert_transient(krx)


class TestTopicSnapshotKrxCascadeIntegration(unittest.TestCase):
    """KRX 게이트 캐스케이드를 **stub 없이** HTTP 경로로 관통시킨다 (ADR-039 §8.1 E3).

    위 `TestTopicSnapshotRestAuthGate`의 KRX 케이스는 전역 게이트를 patch하고 `compute_krx_visible`도
    mock한다 → 잠기는 것이 boolean 배선뿐이다. 실제 `krx_gates_open()` · `has_entitlement()` ·
    `user_entitlements` row와의 결합은 이 endpoint 경로에서 무검증으로 남는다.

    여기서는 **entitlement row 유/무만으로** 200/404가 갈리는지 본다.

    ⚠️ flag patch가 셋 다 필요하다: `supported_snapshot_topics()`는 import-time 파생 상수
    `KRX_CLIENT_DISTRIBUTION_EFFECTIVE`를, `krx_gates_open()`은 runtime `KRX_FUTURES_ENABLED` ∧
    `KRX_CLIENT_DISTRIBUTION_ENABLED`를 읽는다(KRX topic 계열은 EFFECTIVE, graph v2 계열은 runtime
    accessor라는 리포 관례 — 통일 리팩터는 `krx_topic_publisher`까지 번져 별 슬라이스). 셋 중 하나라도
    빠지면 조용히 통과한다.
    """

    _KRX = "krx:usd-krw-futures"
    _UID = "krx-cascade-user"

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        models.Base.metadata.create_all(engine)

    def tearDown(self):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            db.query(models.UserEntitlement).filter(
                models.UserEntitlement.user_id == self._UID).delete()
            db.commit()
        finally:
            db.close()

    def _grant(self):
        from app.database import SessionLocal
        from app.entitlements import KRX_FUTURES_ENTITLEMENT_KEY
        db = SessionLocal()
        try:
            db.add(models.UserEntitlement(user_id=self._UID, key=KRX_FUTURES_ENTITLEMENT_KEY))
            db.commit()
        finally:
            db.close()

    def _get_krx(self):
        canned = _canned_snapshot(self._KRX)
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_ENABLED", True), \
             patch("app.main.verify_firebase_token", new=AsyncMock(return_value=self._UID)), \
             patch("app.main.require_premium", new=AsyncMock(return_value=True)), \
             patch("app.topic_initial_snapshot._build_snapshot_sync", return_value=canned):
            return self.client.get("/api/v2/topics/snapshot", params={"topic": self._KRX})

    def test_without_entitlement_row_404(self):
        r = self._get_krx()
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "unknown_topic")
        self.assertNotIn(self._KRX, r.json()["supported_topics"])

    def test_with_entitlement_row_200(self):
        self._grant()
        r = self._get_krx()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["topic"], self._KRX)

    def test_entitlement_is_scoped_to_the_authenticated_user(self):
        """다른 사용자의 row가 있어도 이 사용자에겐 안 보인다(uid 결합 실검증)."""
        from app.database import SessionLocal
        from app.entitlements import KRX_FUTURES_ENTITLEMENT_KEY
        db = SessionLocal()
        try:
            db.add(models.UserEntitlement(user_id="somebody-else",
                                          key=KRX_FUTURES_ENTITLEMENT_KEY))
            db.commit()
            r = self._get_krx()
        finally:
            db.query(models.UserEntitlement).filter(
                models.UserEntitlement.user_id == "somebody-else").delete()
            db.commit()
            db.close()
        self.assertEqual(r.status_code, 404)


class TestTopicSnapshotRouteHasNoRequestScopedSession(unittest.TestCase):
    """`Depends(get_db)` 금지 계약을 회귀로 잠근다 (codex Major, ADR-039 §8.1 E3 item 5).

    request-scoped 세션은 응답까지 커넥션을 쥐고, 그 뒤 builder가 **두 번째** SessionLocal을 연다 →
    운영 풀(`pool_size=3 + max_overflow=2`)에서 bootstrap 동시 요청이 서로의 builder 커넥션을
    기다린다. 리뷰로 한 번 잡혔지만 **되돌려도 green**이었던 속성이라 여기서 못박는다.
    """

    def test_route_declares_no_db_dependency(self):
        from app.main import get_db

        route = next(r for r in app.routes
                     if getattr(r, "path", None) == "/api/v2/topics/snapshot")
        deps = [d.call for d in route.dependant.dependencies]
        self.assertNotIn(get_db, deps,
                         "topics/snapshot은 request-scoped 세션을 받으면 안 된다 — "
                         "가시성 조회는 to_thread 안에서 열고 닫는다(E3 item 5)")


def _google_auth_error(message):
    """`google.auth` 는 로컬 미설치라 conftest 가 **진짜 예외 클래스**로 stub 한다.

    설치된 CI 에서는 실물을 쓴다 — 어느 환경이든 `GoogleAuthError` 하위이므로 계약은 동일하다
    (그 이유가 `tests/conftest.py` 의 google.auth 분기 주석에 적혀 있다).
    """
    from google.auth import exceptions as google_auth_exceptions

    return google_auth_exceptions.GoogleAuthError(message)


def _firebase_error(class_name, message):
    """`firebase_admin.exceptions` 의 특정 예외 인스턴스. 로컬은 stub, CI 는 실물."""
    from firebase_admin import exceptions as fb_exceptions

    return getattr(fb_exceptions, class_name)(message)


def _publish_on_app_loop(ws, topic, payload):
    """`publish_topic` 을 **TestClient 가 앱을 돌리는 그 이벤트 루프**에서 실행한다.

    ⛔ `asyncio.run()` 으로 부르면 **다른 스레드의 새 루프**에서 서버 쪽 WebSocket 객체의
    `send_json` 을 건드린다. registry 는 "단일 FastAPI 루프"를 전제하므로(그 파일 주석),
    그 방식은 우연히 통과할 뿐 실제 배선을 검증하지 못한다(codex Medium).
    `WebSocketTestSession.__enter__` 가 만든 portal 이 곧 그 루프다.
    """
    from app import topic_dispatcher as dispatcher

    portal = getattr(ws, "portal", None)
    assert portal is not None, "TestClient 세션이 portal 을 노출하지 않는다 — 앱 루프 검증 불가"
    return portal.call(dispatcher.publish_topic, topic, payload)


def _granted_verdict():
    """판정기를 통과한 것처럼 보이는 최소 verdict (관측 시각은 현재 mono 축)."""
    from app import topic_authorization as ta
    from app.clock import system_clock

    now = system_clock().mono()
    return ta.Granted(premium_observed_at_mono=now, entitlement_observed_at_mono=now)


def _registry_connection_count():
    """구독 중인 연결 수 (registry 는 모듈 전역 싱글톤이라 **증분**으로만 의미가 있다).

    ⛔ `registry.get_subscriptions(ws)` 로 단언하지 말 것 — 그 `ws` 는 **클라 쪽 객체**이고
    registry 는 **서버 쪽 WebSocket** 을 키로 쓴다. 그래서 `== set()` 단언이 코드와 무관하게
    **언제나 참**이었다(실측: 등록이 실제로 일어난 경우에도 빈 집합이었다).
    """
    from app import topic_dispatcher

    return topic_dispatcher.registry.subscribed_connection_count


def _receive_json_or_fail(ws, fail, timeout=2.0):
    """`ws.receive_json()` 을 thread+join 으로 감싼다 — 미전달 시 hang 대신 fast fail.

    `TestSnapshotOnSubscribeE2E._receive_json` 과 같은 이유(TestClient receive_json 자체엔
    timeout 이 없다). 그 메서드를 그대로 쓰려면 상속이 필요해, 모듈 레벨로 하나 둔다.
    """
    box = {}

    def _recv():
        try:
            box["msg"] = ws.receive_json()
        except Exception as exc:  # noqa: BLE001 — 그대로 재전파
            box["err"] = exc

    th = threading.Thread(target=_recv, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        fail(f"ws.receive_json {timeout}s timeout — 서버가 아무것도 보내지 않았다")
    if "err" in box:
        raise box["err"]
    return box["msg"]


class TestAuthenticatedSubscribeIsAcknowledged(unittest.TestCase):
    """1C 재시작의 **첫 수직 슬라이스** — 인증된 subscribe 는 ack 을 받아야 한다 (ADR-040).

    ## 이 테스트가 존재하는 이유

    폐기된 트랙의 실패 원인은 "실제 진입점이 코드를 한 번도 호출하지 않은 채 부품을 키운 것"이었다.
    그래서 이 슬라이스는 **경계에서 관측되는 것부터** 정의한다 — 실제 `/ws` 소켓으로 subscribe 를
    보내고, `subscription_ack` 또는 연결 종료를 **그 소켓에서** 본다. helper 를 직접 호출하는
    테스트는 이 자리에 두지 않는다(그것이 폐기된 트랙의 형태였다).

    ## harness 규율 (ADR-040 "스파이크 harness 제약")

    - `TestClient(app)` 을 **`with` 없이** → lifespan 미진입(스케줄러·크롤러 미시작).
    - ⛔ 그것만으로는 부족하다 — **요청 경로**가 그대로 외부를 부른다(`/ws` 는 연결 즉시 DB +
      Redis). 그래서 Redis·snapshot builder·Firebase·premium 을 **명시적으로 fake** 한다.
      fake 를 빼면 이 테스트는 실제 외부 서비스를 호출한다.
    - 이 클래스는 위 `TestSnapshotOnSubscribeE2E` 가 확립한 패턴을 확장한 것이다(새 harness 아님).
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)  # context manager 미사용 → lifespan 미진입

    def _patchers(self):
        """FF on + 외부 의존 전부 fake — **이름으로** 돌려준다.

        ⛔ 위치 튜플로 돌려주다 실패했다(실측): `redis_cache.set` fake를 추가하자 인덱스가
        밀려 엉뚱한 patcher를 집었고, `with` 에 하나를 빠뜨려 **premium fake가 아예 걸리지
        않았다**. 커지는 튜플을 위치로 푸는 형태 자체가 원인이라 dict + ExitStack로 바꿨다.
        """
        return {
            "dispatcher_flag": patch.object(config, "TOPIC_DISPATCHER_ENABLED", True),
            "fx_flag": patch.object(config, "FX_TOPIC_ENABLED", True),
            "snapshot_builder": patch(
                "app.topic_initial_snapshot._build_snapshot_sync",
                side_effect=_canned_snapshot,
            ),
            "redis_get": patch("app.main.redis_cache.get", new=AsyncMock(return_value=None)),
            # ⛔ `get`만 fake하면 부족하다 — miss 시 핸들러가 `redis_cache.set`을 부르므로
            #    그 호출이 **실제 Redis 연결**을 시도한다(codex).
            "redis_set": patch("app.main.redis_cache.set", new=AsyncMock(return_value=None)),
            "verify_token": patch(
                "app.main.verify_ws_subscribe_token",
                new=AsyncMock(return_value="uid-spike"),
            ),
            # ⛔ premium을 True로 patch하지 **않는다**. FX는 per-user 판정 대상이 아니므로
            #    이 경로가 premium을 부르면 그것이 결함이다. 부르면 실패하게 두면 그 실수가
            #    **실제 RevenueCat 호출** 대신 red가 된다(patch를 제거하면 실제 호출이 나간다).
            # ⛔ **외부 호출 fail-closed.** 실측: 새 인가 경로가 `fetch_revenuecat_result` 를
            #    직접 부르는데 harness 는 `main.require_premium` 만 fake 해서, KRX E2E 가
            #    **실제 RevenueCat 에 HTTPS 요청을 보냈다**(GET /v1/subscribers/... 200 OK).
            #    이름으로 지키는 fake 는 경로가 바뀌면 아무것도 막지 못한다 → leaf 를 막는다.
            "revenuecat_must_not_be_called": patch(
                "app.subscription.fetch_revenuecat_result",
                new=AsyncMock(side_effect=AssertionError(
                    "이 테스트는 RevenueCat 을 부르면 안 된다 — 부른다면 fake 를 명시하라")),
            ),
            "db_session_must_not_be_opened": patch(
                "app.database.SessionLocal",
                side_effect=AssertionError(
                    "이 테스트는 DB 세션을 열면 안 된다 — 부른다면 fake 를 명시하라"),
            ),
            "premium_must_not_be_called": patch(
                "app.main.require_premium",
                new=AsyncMock(side_effect=AssertionError(
                    "FX subscribe 경로가 premium 게이트를 불렀다 — REST 정책 재사용은 금지다"
                )),
            ),
        }

    def test_authorize_subscribe_is_required_and_keyword_only(self):
        """⛔ 기본값이 생기면 호출자가 빠뜨린 순간 인증이 **조용히** 사라진다(fail-open).

        직전 슬라이스의 교훈: 서명을 좁히는 변경은 **그 서명 자체를 잠그는 단언**이 없으면
        한 줄로 되돌아간다(구 인자를 optional로 되살리는 변이가 전체 스위트를 통과했다).
        """
        import inspect

        from app.topic_dispatcher import handle_client_message as target

        param = inspect.signature(target).parameters["authorize_subscribe"]
        self.assertIs(
            param.default, inspect.Parameter.empty,
            "authorize_subscribe에 기본값이 생겼다 — 빠뜨리면 인증이 조용히 꺼진다",
        )
        self.assertIs(
            param.kind, inspect.Parameter.KEYWORD_ONLY,
            "positional로 바뀌면 인자 순서 실수가 조용히 통과한다",
        )
        # ⛔ `identity` 도 **기본값 없는 keyword-only** 여야 한다 — 기본값을 주면 주입을
        #    빠뜨린 순간 UID 결속이 **조용히 사라진다**(cross-UID 가 통과한다).
        identity_param = inspect.signature(target).parameters["identity"]
        self.assertIs(
            identity_param.default, inspect.Parameter.empty,
            "identity 에 기본값이 생기면 주입 누락이 조용한 결속 소실이 된다",
        )
        self.assertIs(identity_param.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_authenticated_subscribe_receives_a_subscription_ack(self):
        """⛔ 현재 red 다 — 서버는 `id_token` 을 보지 않고 ack 도 보내지 않는다.

        red 의 형태가 timeout 이 아니라 **명확한 타입 불일치**여야 한다: 지금은 subscribe 직후
        `snapshot` 이 오므로, 그 사실이 그대로 실패 메시지에 드러난다.
        """
        patchers = self._patchers()
        authz = patchers["verify_token"].new
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():      # ⛔ 전부 enter — 하나도 빠뜨리지 않는다
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)      # 연결 직후 legacy rates 프레임 소비
                ws.send_json({
                    "type": "subscribe",
                    "request_id": "spike-1",
                    "id_token": "token-spike",
                    # ⛔ 세 종류를 **함께** 보낸다:
                    #    - 무료 지원 topic → accept
                    #    - 유료(per-user 판정) topic → identity만 확인하고 accept하면
                    #      *인가된 것처럼 보이는* 유료 데이터 유출이라 무인증보다 나쁘다
                    #    - 미지원 topic → 구 판정("게이트가 아니면 허용")은 이걸 accept하고
                    #      registry에 등록했다(실측 재현)
                    "topics": ["fx:usd-krw", KRX_TOPIC, "not:a:topic"],
                })
                msg = _receive_json_or_fail(ws, self.fail)
        self.assertEqual(
            msg.get("type"), "subscription_ack",
            f"인증된 subscribe 에 ack 이 오지 않았다 — 실제 수신: {msg.get('type')!r}",
        )
        self.assertEqual(msg.get("request_id"), "spike-1", "ack 이 요청과 상관되지 않는다")
        # ⛔ **정본 §8-B의 객체 배열**이다 — 문자열 배열로 두면 lease 필드가 들어올 때 클라가
        #    shape를 바꿔야 하고, 그게 코드가 만든 제3의 계약이었다(§8-B-stage 표).
        self.assertEqual(msg.get("operation"), "subscribe", "같은 schema라 구분자가 필요하다")
        # §8-B-stage **Stage 2** — 컨테이너 형태는 그대로고 **필드만 는다**(lease).
        accepted = msg.get("accepted_topics") or []
        self.assertEqual([x["topic"] for x in accepted], ["fx:usd-krw"])
        self.assertEqual(
            set(accepted[0]), {"topic", "lease_id", "lease_duration_seconds"},
            "인증된 accept 는 lease 를 실어야 한다(§8-B Stage 2)",
        )
        self.assertEqual(
            msg.get("rejected_topics"),
            [
                {"topic": KRX_TOPIC, "error": "topic_unavailable"},
                {"topic": "not:a:topic", "error": "unknown_topic"},
            ],
            "topic 분류가 정본 오류 코드와 다르다 (per-user 판정 vs 미지원)",
        )
        self.assertEqual(msg.get("removed_topics"), [], "이 슬라이스엔 제거 전이가 없다")
        self.assertEqual(
            msg.get("active_subscriptions"), accepted,
            "ack이 연결의 최종 상태를 담지 않는다 — 클라가 이걸로 수렴한다",
        )
        # 인증자가 **메시지의 토큰으로 정확히 1회** 불렸는가 (codex: 토큰·호출 횟수 단언)
        authz.assert_awaited_once_with("token-spike")

    def test_disabled_topic_is_not_accepted(self):
        """⛔ availability flag 가 off 인 topic 을 accept 하면 **데이터가 영원히 안 온다**.

        실측 재현(codex): `FX_TOPIC_ENABLED=false` 인데 ack 과 registry 모두 fx 를 활성으로
        기록했다. 원인은 `supported_snapshot_topics()` 만 보고 판정한 것 — 그 집합은 docstring
        그대로 **구현상 지원**이고 availability gate 와 무관하다.
        """
        patchers = self._patchers()
        patchers["fx_flag"] = patch.object(config, "FX_TOPIC_ENABLED", False)
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({
                    "type": "subscribe", "request_id": "off-1",
                    "id_token": "t", "topics": ["fx:usd-krw"],
                })
                msg = _receive_json_or_fail(ws, self.fail)
        self.assertEqual(msg.get("accepted_topics"), [], "flag off 인 topic 을 accept 했다")
        self.assertEqual(
            msg.get("rejected_topics"), [{"topic": "fx:usd-krw", "error": "topic_unavailable"}],
        )
        self.assertEqual(
            msg.get("active_subscriptions"), [], "flag off 인 topic 이 registry 에 등록됐다",
        )

    def test_unjudgeable_gated_topic_fails_the_whole_request(self):
        """⛔ 판정기가 없는데 배포 flag 가 켜져 있으면 **transient 가 아니라 설정 결함**이다.

        `topic_unavailable`(개별 flag off) 로 돌려주면 운영자가 flag 를 보고 코드와 모순을 겪는다
        (codex Medium). §8-C 에 `internal_error` 가 없으므로 전체-요청 `temporarily_unavailable`
        + ERROR 로그가 남은 정확한 선택이다.
        """
        patchers = self._patchers()
        patchers["krx_flag"] = patch.object(
            config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True
        )
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                # ⛔ 운영자 신호도 계약이다 — debug로 격하되면 설정 결함이 조용히 지나간다.
                with self.assertLogs("exchange_rate.topic_dispatcher", level="ERROR"):
                    ws.send_json({
                        "type": "subscribe", "request_id": "gated-1",
                        "id_token": "t", "topics": ["fx:usd-krw", KRX_TOPIC],
                    })
                    msg = _receive_json_or_fail(ws, self.fail)
        self.assertEqual(msg.get("type"), "subscription_error")
        self.assertEqual(msg.get("error"), "temporarily_unavailable")
        self.assertEqual(msg.get("request_id"), "gated-1")
        self.assertIn("retry_after_seconds", msg, "§8-C 는 이 코드에 retry_after 동반을 요구한다")

    def test_subscribe_classification_uses_the_same_availability_predicate(self):
        """⛔ **양 방향을 다 잠근다.** builder 쪽만 잠그면 dispatcher 가 갈라져도 green 이다.

        실측(codex 지적 → 변이로 재현): dispatcher 가 판정기를 버리고
        `topic.startswith("fx:") and not config.FX_TOPIC_ENABLED` 를 직접 읽게 바꿨더니
        **전체 3817개가 그대로 green** 이었다 — FX-off E2E 는 답이 같아 통과하고, builder
        단일소스 테스트는 dispatcher 를 보지 않기 때문이다.

        여기서는 **flag 는 켜 두고 판정기만 False 로** patch 한다. dispatcher 가 판정기를 통하지
        않으면 topic 이 accept 되어 red 가 된다 — 즉 "같은 지식의 단일 소스"가 양쪽에서 잠긴다.
        """
        from app import topic_initial_snapshot as tis

        patchers = self._patchers()
        patchers["fx_flag"] = patch.object(config, "FX_TOPIC_ENABLED", True)
        patchers["availability_predicate"] = patch.object(
            tis, "is_snapshot_topic_enabled", return_value=False
        )
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({
                    "type": "subscribe", "request_id": "pred-1",
                    "id_token": "t", "topics": ["fx:usd-krw"],
                })
                msg = _receive_json_or_fail(ws, self.fail)
        self.assertEqual(
            msg.get("accepted_topics"), [],
            "dispatcher 가 availability 판정기를 통하지 않았다 — 지식이 두 곳으로 갈렸다",
        )
        self.assertEqual(
            msg.get("rejected_topics"),
            [{"topic": "fx:usd-krw", "error": "topic_unavailable"}],
        )

    def test_snapshot_builder_uses_the_same_availability_predicate(self):
        """⛔ subscribe 분류와 snapshot 발사가 **같은 판정기**를 써야 한다.

        ⚠️ 이 테스트가 잠그는 방식: **판정기를 patch** 해서 builder 가 그것을 따르는지 본다.
        builder 가 config flag 를 직접 읽으면 patch 가 무효가 되어 payload 가 나오고 red 가 된다 —
        즉 "단일 소스"가 AST 검사 없이 **행동으로** 잠긴다.
        (변이 실측: 이 테스트가 없을 때 builder 의 판정기 호출을 제거해도 전 스위트가 green 이었다.)
        """
        from app import topic_initial_snapshot as tis

        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(tis, "is_snapshot_topic_enabled", return_value=False):
            self.assertIsNone(
                tis._build_snapshot_sync("fx:usd-krw"),
                "builder 가 availability 판정기를 무시했다 — 분류와 발사가 갈린다",
            )

    def _subscribe_and_receive(self, *, verifier_raises=None, request_id="err-1",
                               topics=("fx:usd-krw",), extra=None, ping_after=False):
        """공통 경로 — verifier 의 side_effect 만 갈아 끼운다(새 harness 금지)."""
        patchers = self._patchers()
        if verifier_raises is not None:
            patchers["verify_token"] = patch(
                "app.main.verify_ws_subscribe_token",
                new=AsyncMock(side_effect=verifier_raises),
            )
        if extra:
            patchers.update(extra)
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                before = _registry_connection_count()
                payload = {"type": "subscribe", "request_id": request_id,
                           "id_token": "tok", "topics": list(topics)}
                ws.send_json(payload)
                msg = _receive_json_or_fail(ws, self.fail)
                pong = None
                if ping_after:
                    # ⛔ **연결 생존**이 계약이다 — §8-C 의 두 코드는 전체-요청 범위이므로
                    #    요청만 접혀야 하고 연결·registry 는 불변이어야 한다.
                    ws.send_text("ping")
                    pong = _receive_json_or_fail(ws, self.fail)
                delta = _registry_connection_count() - before
        return msg, pong, delta

    def test_revoked_token_becomes_invalid_token_and_keeps_the_connection(self):
        """⛔ 현행은 프레임 **0개 + 연결 종료**였다 — 그 차이는 소켓에서만 보인다."""
        from app.topic_wire import SubscribeAuthFailed

        msg, pong, delta = self._subscribe_and_receive(
            verifier_raises=SubscribeAuthFailed("invalid_token"), ping_after=True,
        )
        self.assertEqual(msg.get("type"), "subscription_error")
        self.assertEqual(msg.get("error"), "invalid_token")
        self.assertEqual(msg.get("request_id"), "err-1")
        self.assertNotIn(
            "retry_after_seconds", msg,
            "죽은 자격에 retry_after 를 붙이면 클라가 영구 재시도한다",
        )
        self.assertEqual(pong, {"type": "pong"}, "연결이 닫혔다 — 요청만 접어야 한다")
        self.assertEqual(delta, 0, "실패한 요청이 registry 를 바꿨다")

    def test_unavailable_verdict_carries_retry_after(self):
        from app.topic_wire import SubscribeAuthFailed

        msg, _, delta = self._subscribe_and_receive(
            verifier_raises=SubscribeAuthFailed("temporarily_unavailable", 5),
        )
        self.assertEqual(msg.get("error"), "temporarily_unavailable")
        self.assertIs(type(msg.get("retry_after_seconds")), int, "정수여야 한다")
        self.assertGreaterEqual(msg.get("retry_after_seconds"), 1)
        self.assertEqual(delta, 0)

    def test_config_fault_frame_matches_the_auth_failure_frame(self):
        """⛔ 두 경로가 **같은 생성자**를 지나는지는 두 프레임을 비교해야만 보인다."""
        from app.topic_wire import SubscribeAuthFailed

        auth_frame, _, _ = self._subscribe_and_receive(
            verifier_raises=SubscribeAuthFailed("temporarily_unavailable", 5),
            request_id="same-1",
        )
        cfg_frame, _, _ = self._subscribe_and_receive(
            request_id="same-1", topics=("fx:usd-krw", KRX_TOPIC),
            extra={"krx_flag": patch.object(
                config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True)},
        )
        self.assertEqual(
            sorted(auth_frame), sorted(cfg_frame),
            "설정 결함 프레임과 인증 실패 프레임의 키가 다르다 — 생성 경로가 갈렸다",
        )
        self.assertEqual(auth_frame["type"], cfg_frame["type"])
        self.assertEqual(auth_frame["error"], cfg_frame["error"])

    def test_malformed_id_token_is_a_request_format_error(self):
        """형식 위반은 인증 **이전** 단계다 — SDK 의 토큰-무관 ValueError 경로를 없앤다."""
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "request_id": "bad-1",
                              "id_token": 42, "topics": ["fx:usd-krw"]})
                msg = _receive_json_or_fail(ws, self.fail)
        self.assertEqual(msg.get("error"), "invalid_request")
        self.assertNotIn("retry_after_seconds", msg)

    def _subscribe_with_real_verifier(self, sdk_error, request_id="sdk-1"):
        """⛔ **verifier 를 fake 하지 않는다** — SDK 를 fake 해 실제 분류기를 경계에서 관측한다.

        `_patchers()` 는 `verify_ws_subscribe_token` 을 통째로 교체하므로 그걸로는 예외 분류가
        검증되지 않는다(그 사실은 별도 단위 테스트가 기록한다). 여기서는 verifier fake 를 빼고
        `firebase_admin.auth.verify_id_token` 이 SDK 예외를 던지게 한다.
        """
        import firebase_admin

        patchers = self._patchers()
        del patchers["verify_token"]                    # 실제 verifier 가 돈다
        patchers["fb_initialized"] = patch(
            "app.main.is_firebase_initialized", return_value=True
        )
        # ⚠️ 가드는 **DEFAULT app 과 인증 전용 app 을 함께** 본다 — 후자를 빼면 미초기화로 접힌다.
        patchers["auth_app"] = patch(
            "app.notifications.fcm.ws_auth_app", return_value=object()
        )
        patchers["fb_verify"] = patch.object(
            firebase_admin.auth, "verify_id_token", side_effect=sdk_error
        )
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                before = _registry_connection_count()
                ws.send_json({"type": "subscribe", "request_id": request_id,
                              "id_token": "tok", "topics": ["fx:usd-krw"]})
                msg = _receive_json_or_fail(ws, self.fail)
                delta = _registry_connection_count() - before
        return msg, delta

    def test_sdk_exceptions_split_by_type_not_by_parent_catch(self):
        """⛔ **broad/부모 catch 금지**를 타입으로 지켰는지 보는 유일한 테스트.

        세 케이스가 함께 있어야 의미가 있다:
        - `UserDisabledError` 는 `InvalidIdTokenError` 의 **형제**라 그 절로는 안 잡힌다.
        - `ConfigurationNotFoundError` 는 `UserNotFoundError` 의 **형제**다 — 부모
          `NotFoundError` 로 뭉치는 변이는 **이 케이스에서만** red 가 된다.
        """
        import firebase_admin

        auth = firebase_admin.auth
        # (예외, 기대 wire 코드, retry_after 동반?, 운영자 ERROR 신호 필요?)
        # ⚠️ 이름 문자열로 분기하지 않는다 — stub 클래스는 `_Stub…` 로 이름이 달라
        #    리터럴 매칭이 **한 번도 발화하지 않았다**(변이 생존으로 발견).
        cases = [
            (auth.RevokedIdTokenError("revoked"), "invalid_token", False, False),
            (auth.UserDisabledError("disabled"), "invalid_token", False, False),
            # ⛔ **자격 오류가 아니다.** 6.9.0 `_user_mgt.py:598` 은 `accounts:lookup` 응답이
            #    falsy 이거나 `users` 필드가 없기만 해도 같은 예외를 던진다 — 계정 삭제와 공급자
            #    응답 손상이 **구별 불가**다. 손상을 자격 오류로 접으면 장애 중 정상 사용자
            #    **전원**이 재인증을 요구받는다(codex High).
            (auth.UserNotFoundError("gone"), "temporarily_unavailable", True, False),
            (auth.ConfigurationNotFoundError("no cfg"), "temporarily_unavailable", True, True),
            # ⚠️ 실물 `CertificateFetchError.__init__(self, message, cause)` 는 cause 가 **필수**다
            #    (6.9.0 `_token_gen.py:431`). stub 이 그걸 그대로 반영해 내 호출 실수를 잡았다.
            (auth.CertificateFetchError("certs", None), "temporarily_unavailable", True, False),
            # ⚠️ 서비스계정 토큰 갱신 실패는 **FirebaseError 도 requests 예외도 아니라** SDK 변환
            #    그물을 통과해 날것으로 올라온다 → 별 절이 필요하다(부모 `GoogleAuthError` 를 잡아
            #    `RefreshError`·`TransportError` 를 함께 덮는다).
            (_google_auth_error("refresh failed"), "temporarily_unavailable", True, True),
            # ⛔ 재시도로 낫지 않는 **서버측** 자격 오류(401/403)는 wire 값이 같아도 **운영자
            #    신호**가 있어야 한다 — 없으면 죽은 서비스계정 키를 클라가 5초마다 재시도하는
            #    동안 서버는 아무것도 모른다.
            (_firebase_error("PermissionDeniedError", "403"), "temporarily_unavailable", True, True),
            (_firebase_error("UnauthenticatedError", "401"), "temporarily_unavailable", True, True),
        ]
        # ⛔ 설정 결함 계열은 **운영자 신호(ERROR)** 도 계약이다 — 그것이 그 절의 고유 가치다.
        #    변이 실측: NotFound 절을 지우면 verdict 는 FirebaseError 절로 흘러 **동일**해서
        #    verdict 단언만으론 생존한다. 로그를 함께 봐야 그 절이 잠긴다.
        for exc, expected_error, wants_retry, wants_error_log in cases:
            with self.subTest(exc=type(exc).__name__):
                # ⛔ 플래그는 **양방향**이다. `False` 를 "검사 안 함"으로 두면 ERROR 를 남발하는
                #    변이가 생존한다 — 실측: `UserNotFoundError` 절을 지우면 형제 절로 흘러
                #    verdict 는 **동일**하고 로그만 WARNING→ERROR 로 바뀌는데, 그건 정상 사건인
                #    계정 삭제마다 운영자에게 거짓 경보를 울리는 것이다(늑대 소년).
                if wants_error_log:
                    with self.assertLogs("exchange_rate.main", level="ERROR"):
                        msg, delta = self._subscribe_with_real_verifier(exc)
                else:
                    with self.assertNoLogs("exchange_rate.main", level="ERROR"):
                        msg, delta = self._subscribe_with_real_verifier(exc)
                self.assertEqual(msg.get("type"), "subscription_error")
                self.assertEqual(msg.get("error"), expected_error)
                self.assertEqual(
                    "retry_after_seconds" in msg, wants_retry,
                    "retry_after 동반 규칙이 코드와 맞지 않는다",
                )
                if wants_retry:
                    # ⛔ **"재시도로 낫지 않는다"고 분류한 것에 짧은 간격을 주면 안 된다.**
                    #    운영자 신호(ERROR)가 뜬 결함은 재시도로 해소되지 않으므로, 같은 5초를
                    #    주면 클라가 retry storm 을 만들고 그 요청마다 ERROR 가 쌓여 신호가
                    #    희석된다(codex Medium). 두 축은 **함께 움직여야** 한다.
                    self.assertEqual(
                        msg["retry_after_seconds"] > app_main.WS_AUTH_RETRY_AFTER_SECONDS,
                        wants_error_log,
                        "운영자 신호 여부와 재시도 간격이 어긋났다 — "
                        f"error_log={wants_error_log} retry={msg['retry_after_seconds']}",
                    )
                self.assertEqual(delta, 0, "실패가 registry 를 바꿨다")

    def test_unclassifiable_exception_is_not_swallowed(self):
        """⛔ 분류 불가를 wire 오류로 접으면 우리 버그가 조용해지고 클라가 영구 재시도한다.

        관측: 프레임이 오지 않고 연결이 끊긴다(상위 handler 가 traceback 을 남긴다).
        """
        msg_box = {}
        try:
            msg_box["msg"], _ = self._subscribe_with_real_verifier(
                ZeroDivisionError("우리 버그")
            )
        except Exception as exc:      # noqa: BLE001 — 연결 종료가 기대 동작이다
            msg_box["closed"] = type(exc).__name__
        self.assertNotIn(
            "msg", msg_box,
            f"분류 불가를 wire 프레임으로 접었다: {msg_box.get('msg')}",
        )

    def test_verifier_that_never_finishes_yields_a_transient_frame(self):
        """R1 — wire deadline 이 **dispatcher 에** 있다는 것까지 이 테스트 하나가 잠근다.

        harness 는 verifier 를 **통째로** 교체한다. deadline 이 verifier 안에 있으면 fake 에는
        없으므로 프레임이 오지 않고 hang → `_receive_json_or_fail` 이 fast-fail 한다.
        즉 "프레임이 온다"가 곧 "deadline 이 호출부에 있다"이다 — 별도 위치 단언이 필요 없다.

        ⚠️ **타이밍 단언 없음.** deadline 을 0.05 로 낮춰 하니스 join(2s)보다 훨씬 작게 만들면,
        CI 부하가 늘어도 결론이 안 바뀐다(상한이 아니라 하한만 남는다).
        """
        never = asyncio.Event()

        async def _hang(*_a, **_k):
            await never.wait()          # ⛔ `AsyncMock(side_effect=lambda: sleep())` 은 await 되지
                                        #    않아 즉시 통과한다 — 반드시 실제 코루틴이어야 한다.
        patchers = self._patchers()
        patchers["verify_token"] = patch("app.main.verify_ws_subscribe_token", new=_hang)
        patchers["deadline"] = patch.object(config, "WS_AUTH_WIRE_DEADLINE_SECONDS", 0.05)
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                before = _registry_connection_count()
                with self.assertLogs("exchange_rate.topic_dispatcher", level="WARNING"):
                    ws.send_json({"type": "subscribe", "request_id": "dl-1",
                                  "id_token": "tok", "topics": ["fx:usd-krw"]})
                    msg = _receive_json_or_fail(ws, self.fail)
                ws.send_text("ping")
                pong = _receive_json_or_fail(ws, self.fail)
                delta = _registry_connection_count() - before
        self.assertEqual(msg.get("type"), "subscription_error")
        self.assertEqual(msg.get("error"), "temporarily_unavailable")
        self.assertEqual(msg.get("retry_after_seconds"), config.WS_AUTH_RETRY_AFTER_SECONDS)
        self.assertEqual(pong, {"type": "pong"}, "deadline 이 연결을 닫았다 — 요청만 접어야 한다")
        self.assertEqual(delta, 0)

    def test_verifier_raised_timeout_is_not_reported_as_a_wire_deadline(self):
        """⛔ 검증자가 스스로 던진 `TimeoutError` 를 deadline 초과로 기록하면 **D 를 튜닝할
        telemetry 가 오염된다**(codex Medium).

        ⚠️ 이론이 아니다: 3.10+ 에서 `socket.timeout is TimeoutError` 라 SDK 내부 소켓 timeout 이
        그대로 이 타입으로 도착할 수 있다. `wait_for` 는 두 경우를 **같은 예외**로 주므로
        `asyncio.timeout` + `expired()` 로 갈라야 구분된다.

        관측: deadline 프레임이 오지 **않고**(분류 불가라 재전파) 연결이 끊긴다.
        """
        async def _raise_timeout(*_a, **_k):
            raise TimeoutError("SDK 내부 소켓 timeout")

        patchers = self._patchers()
        patchers["verify_token"] = patch("app.main.verify_ws_subscribe_token", new=_raise_timeout)
        got = {}
        try:
            with contextlib.ExitStack() as stack:
                for patcher in patchers.values():
                    stack.enter_context(patcher)
                with self.client.websocket_connect("/ws") as ws:
                    _receive_json_or_fail(ws, self.fail)
                    ws.send_json({"type": "subscribe", "request_id": "vt-1",
                                  "id_token": "tok", "topics": ["fx:usd-krw"]})
                    got["msg"] = _receive_json_or_fail(ws, self.fail, timeout=1.0)
        except Exception:      # noqa: BLE001 — 연결 종료가 기대 동작이다
            pass
        self.assertNotIn(
            "msg", got,
            f"검증자의 TimeoutError 를 wire deadline 으로 접었다: {got.get('msg')}",
        )

    def test_unprepared_auth_app_yields_a_frame_not_a_dropped_connection(self):
        """R2 — named app 미준비를 분류하지 않으면 **연결이 끊긴다**(프레임이 아니라).

        `is_firebase_initialized()` 는 DEFAULT app 전용이라 그것만 보면 이 상태를 못 잡는다.
        그 차이는 소켓에서만 보인다 — 단위로는 둘 다 "예외"라 구별되지 않는다.
        """
        import firebase_admin

        patchers = self._patchers()
        del patchers["verify_token"]                     # 실제 verifier 가 돈다
        patchers["fb_initialized"] = patch(
            "app.main.is_firebase_initialized", return_value=True
        )
        patchers["auth_app"] = patch(
            "app.notifications.fcm.ws_auth_app", return_value=None
        )
        patchers["fb_verify"] = patch.object(
            firebase_admin.auth, "verify_id_token",
            side_effect=AssertionError("app 미준비인데 검증을 시도했다"),
        )
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                with self.assertLogs("exchange_rate.main", level="ERROR"):
                    ws.send_json({"type": "subscribe", "request_id": "noapp-1",
                                  "id_token": "tok", "topics": ["fx:usd-krw"]})
                    msg = _receive_json_or_fail(ws, self.fail)
                ws.send_text("ping")
                pong = _receive_json_or_fail(ws, self.fail)
        self.assertEqual(msg.get("error"), "temporarily_unavailable")
        self.assertEqual(
            msg.get("retry_after_seconds"),
            config.WS_AUTH_PERSISTENT_FAULT_RETRY_AFTER_SECONDS,
            "미준비는 재시도로 낫지 않는다 — 짧은 간격을 주면 storm 이 된다",
        )
        self.assertEqual(pong, {"type": "pong"}, "연결이 끊겼다")

    def test_verification_is_bound_to_the_auth_app(self):
        """R3 — `app=` 를 빠뜨리면 **낮춘 httpTimeout 이 통째로 no-op** 인데 흔적이 없다.

        `httpTimeout` 은 firebase-admin 이 `app.options` 에서 **app 단위**로 읽으므로, DEFAULT
        app 으로 검증하면 상한이 120초 그대로다. 프레임·로그·타이밍 어디에도 차이가 없어
        **호출 인자만이 증거**다.

        ⚠️ 여기서 실제 app 객체의 options 를 단언하지 않는다 — conftest 가 `firebase_admin` 을
        MagicMock 으로 stub 하므로 로컬에서 영구 red 가 된다. options 는 아래 단위 테스트가 본다.
        """
        import firebase_admin

        sentinel = object()
        captured = {}

        def _capture(token, **kwargs):
            captured.update(kwargs)
            return {"uid": "uid-app"}

        patchers = self._patchers()
        del patchers["verify_token"]
        patchers["fb_initialized"] = patch(
            "app.main.is_firebase_initialized", return_value=True
        )
        patchers["auth_app"] = patch(
            "app.notifications.fcm.ws_auth_app", return_value=sentinel
        )
        patchers["fb_verify"] = patch.object(
            firebase_admin.auth, "verify_id_token", side_effect=_capture
        )
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "request_id": "app-1",
                              "id_token": "tok", "topics": ["fx:usd-krw"]})
                _receive_json_or_fail(ws, self.fail)
        self.assertIs(captured.get("app"), sentinel, "DEFAULT app 으로 검증했다 — ②가 no-op 이다")
        self.assertIs(captured.get("check_revoked"), True)

    def test_authenticated_subscribe_without_request_id_is_refused(self):
        """⛔ id 없는 인증 subscribe 가 `request_id: null` 인 **ack** 을 받고 있었다(실측 재현).

        그건 두 가지를 동시에 깬다: §8-A 의 "상태 변경 메시지는 id 를 갖고 ack 을 받는다", 그리고
        ack 을 **필수 필드**로 모델링한 클라의 디코드. 검증은 **인증 이전**이라 registry 는 불변이다.
        """
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                before = _registry_connection_count()
                ws.send_json({"type": "subscribe", "id_token": "tok",
                              "topics": ["fx:usd-krw"]})       # request_id 없음
                msg = _receive_json_or_fail(ws, self.fail)
                delta = _registry_connection_count() - before
        self.assertEqual(msg.get("type"), "subscription_error")
        self.assertEqual(msg.get("error"), "invalid_request")
        self.assertIsNone(msg.get("request_id"), "echo 할 id 가 없으면 null 이어야 한다")
        self.assertEqual(delta, 0, "거부된 요청이 registry 를 바꿨다")

    def test_untokened_subscribe_still_works_without_a_request_id(self):
        """⚠️ 반대 방향 — §E1 중간 상태의 구 클라는 id 도 토큰도 보내지 않는다.

        인증 경로의 검증을 **무토큰 경로까지** 확대하면 그 클라가 topic 을 잃는다.
        """
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                before = _registry_connection_count()
                ws.send_json({"type": "subscribe", "topics": ["fx:usd-krw"]})
                msg = _receive_json_or_fail(ws, self.fail)
                delta = _registry_connection_count() - before
        self.assertEqual(msg.get("type"), "snapshot", "무토큰 경로가 깨졌다")
        self.assertEqual(delta, 1, "무토큰 경로가 등록하지 않았다")

    def test_request_id_is_opaque_not_a_uuid(self):
        """⚠️ **UUID 를 강제하지 않는다** (2026-08-01 확정, codex Medium).

        정본 §8-C 가 한때 `invalid_request` 사유로 "UUID 오류"를 적었지만, 그 문구는 §8-A **예시**
        의 `"<uuid>"` 에서 흘러온 것이지 제약이 아니었다. 서버는 request_id 를 **echo 만** 하므로
        형식이 서버 쪽 의미를 갖지 않고, 강제하면 counter·ULID 를 쓰는 클라가 보호 효과 없이
        거부된다. 이 테스트는 그 결정을 잠근다 — UUID 검증을 넣는 변이가 여기서 죽는다.
        """
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "request_id": "counter-7",
                              "id_token": "tok", "topics": ["fx:usd-krw"]})
                msg = _receive_json_or_fail(ws, self.fail)
        self.assertEqual(msg.get("type"), "subscription_ack",
                         "UUID 가 아닌 id 가 거부됐다 — opaque 계약 위반")
        self.assertEqual(msg.get("request_id"), "counter-7", "id 가 그대로 echo 되지 않았다")

    def test_non_dict_input_is_ignored_without_a_frame(self):
        """⚠️ 비-JSON / 비-dict 입력에는 **프레임을 보내지 않는다**.

        `/ws` 는 legacy 평문도 받는 경계라, 아무 텍스트에나 오류를 쏘면 구 클라에 스팸이 된다.
        ⛔ 한때 정본이 이 경로도 "nullable request_id 오류를 받는다"고 읽히게 적었는데 **틀렸다**
        (codex Low) — 이 테스트가 실제 동작 쪽을 잠근다.

        ⚠️ "프레임이 없다"는 뒤이은 **정상 요청의 ack 이 첫 프레임**인 것으로 확인한다. 그냥
        기다리면 blocking 이라 "없음"을 관측할 수 없다.
        """
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                before = _registry_connection_count()
                ws.send_text("이건 JSON 이 아니다")
                ws.send_json(["dict 가 아닌 배열"])
                ws.send_json({"type": "subscribe", "request_id": "after-junk",
                              "id_token": "tok", "topics": ["fx:usd-krw"]})
                msg = _receive_json_or_fail(ws, self.fail)
                delta = _registry_connection_count() - before
        self.assertEqual(msg.get("request_id"), "after-junk",
                         f"쓰레기 입력이 프레임을 만들었다 — 첫 프레임: {msg!r}")
        self.assertEqual(msg.get("type"), "subscription_ack")
        self.assertEqual(delta, 1, "정상 요청이 등록되지 않았다")

    # ── 종결 프레임 불변식 ────────────────────────────────────────────────────
    #
    # ⚠️ 정확한 문장은 **"식별된 요청은 종결 프레임 1개 **또는 연결 종료**를 받는다"** 이다.
    #    "항상 프레임 1개"는 **거짓**이고, 그걸 다음 슬라이스의 큐가 믿으면 정확히 예전처럼
    #    멈춘다. 프레임 0개 + 연결 종료인 경로가 실제로 있다(적대적 검토가 찾음, 코드 확인):
    #      - 분류 불가 인증 예외 → `app/main.py` 가 그대로 재전파 → 상위 `except` → 연결 정리
    #      - 16KB 초과 메시지 → uvicorn `--ws-max-size 16384` 가 transport 에서 close 1009
    #        (정본 §8-C 도 `request_too_large` 를 "전송 계층은 close 1009"로 규정한다)
    #      - 반쯤 닫힌 소켓에서 `send_json` 자체가 실패
    #    → 클라 큐는 **disconnect 를 모든 in-flight 의 종결 신호로** 처리해야 하고 timeout 은
    #      여전히 load-bearing 이다.

    def _receive_for(self, ws, request_id, limit=8):
        """`request_id` 가 일치하는 프레임이 올 때까지 읽는다.

        ⚠️ subscribe 성공은 ack **뒤에 snapshot 프레임을 더 보낸다** — 그냥 다음 프레임을
           읽으면 그걸 집는다(실측으로 red 를 봤다).
        """
        for _ in range(limit):
            msg = _receive_json_or_fail(ws, self.fail)
            if msg.get("request_id") == request_id:
                return msg
        self.fail(f"request_id={request_id!r} 응답이 오지 않았다")

    def _receive_terminal(self, ws, limit=8):
        """`snapshot` 을 건너뛰고 **종결 프레임**(ack/error)을 읽는다.

        ⚠️ subscribe 성공은 ack **뒤에 snapshot 을 더 보낸다** — `request_id` 가 null 인
           오류를 기다릴 땐 id 로 못 거르므로 타입으로 건너뛴다.
        """
        for _ in range(limit):
            msg = _receive_json_or_fail(ws, self.fail)
            if msg.get("type") in ("subscription_ack", "subscription_error"):
                return msg
        self.fail("종결 프레임이 오지 않았다")

    def _probe_then_collect(self, ws, sent):
        """`sent` 를 보낸 뒤 **probe** 를 보내, probe 응답 **앞의** 프레임만 모은다.

        ⛔ 프레임 type 으로 경계를 판별하지 말 것 — flag-off 응답도 probe 응답도 둘 다
           `subscription_ack` 이라 "under-test 를 놓치고 probe 만 잡은" 변이가 통과한다.
           경계는 **probe 전용 request_id** 로 판별한다.
        """
        ws.send_json(sent)
        # ⛔ probe 는 **registry 를 건드리면 안 된다.** 한때 probe 를 정상 subscribe 로 뒀더니
        #    probe 자신이 등록해 카운터를 되돌렸고, "해제가 반영되지 않았다"는 **거짓 red** 가
        #    났다(실측). 빈 topics 는 종결되면서 아무것도 등록하지 않는다.
        ws.send_json({"type": "subscribe", "request_id": "PROBE", "id_token": "tok",
                      "topics": []})
        collected = []
        for _ in range(8):
            msg = _receive_json_or_fail(ws, self.fail)
            if msg.get("request_id") == "PROBE":
                return collected
            collected.append(msg)
        self.fail(f"probe 응답이 오지 않았다 — 수집: {collected!r}")

    def test_identified_unsubscribe_is_acknowledged(self):
        """⛔ unsubscribe 가 **프레임을 0개** 보내고 있었다(실측 확인).

        그건 조용한 실패이고(§8-A "조용한 실패 금지"), 클라의 in-flight 슬롯을 영영 잡아 둔다 —
        실제로 현재 iOS dev 빌드는 매 unsubscribe 마다 `pendingRequests` 항목을 누수 중이다.
        """
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                # 사전 등록 — 이게 없으면 아래 단언이 빈 registry 에서 **공허하게** 통과한다.
                ws.send_json({"type": "subscribe", "request_id": "pre", "id_token": "tok",
                              "topics": ["fx:usd-krw", "usdt:krw"]})
                pre_ack = self._receive_for(ws, "pre")
                before = _registry_connection_count()

                ws.send_json({"type": "unsubscribe", "request_id": "u-1",
                              "topics": ["usdt:krw"]})
                msg = self._receive_for(ws, "u-1")
                partial = _registry_connection_count()

                # positive control — 남은 것까지 빼면 카운터가 **실제로 움직인다**.
                ws.send_json({"type": "unsubscribe", "request_id": "u-2",
                              "topics": ["fx:usd-krw"]})
                last = self._receive_for(ws, "u-2")
                after = _registry_connection_count()

        self.assertEqual(len(pre_ack.get("accepted_topics", [])), 2, "사전 등록이 실패했다")
        self.assertEqual(msg.get("type"), "subscription_ack")
        self.assertEqual(msg.get("operation"), "unsubscribe", "같은 schema라 구분자가 필요하다")
        self.assertEqual(msg.get("request_id"), "u-1")
        self.assertEqual(msg.get("accepted_topics"), [{"topic": "usdt:krw"}])
        # §8-B Stage 2 — **남아 있는 구독의 lease 를 잃지 않는다**(클라 재인증 timer 의 입력).
        remaining = msg.get("active_subscriptions") or []
        self.assertEqual([x["topic"] for x in remaining], ["fx:usd-krw"],
                         "ack 이 연결의 최종 상태를 담지 않았다")
        self.assertEqual(
            set(remaining[0]), {"topic", "lease_id", "lease_duration_seconds"},
            "unsubscribe ack 이 남은 구독의 lease 를 잃었다 — 재인증 시점을 알 수 없게 된다",
        )
        self.assertEqual(msg.get("removed_topics"), [], "§C2 eviction 축은 Stage 1 에서 빈다")
        self.assertEqual(last.get("active_subscriptions"), [])
        self.assertEqual((before, partial, after), (1, 1, 0),
                         "positive control 실패 — 이 카운터는 부분 해제로는 안 움직인다")

    def test_unidentified_unsubscribe_still_unregisters_silently(self):
        """⚠️ 반대 방향 — §E1 구 클라는 `request_id` 를 보내지 않고 ack 도 받지 않는다.

        여기에 프레임을 보내거나 해제를 거부하면 구 클라가 구독을 못 뺀다.
        """
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "topics": ["fx:usd-krw"]})   # 무토큰 등록
                _receive_json_or_fail(ws, self.fail)                            # snapshot
                before = _registry_connection_count()
                frames = self._probe_then_collect(
                    ws, {"type": "unsubscribe", "topics": ["fx:usd-krw"]})
                after = _registry_connection_count()
        self.assertEqual(frames, [], f"구 클라의 unsubscribe 에 프레임이 나갔다: {frames!r}")
        self.assertEqual((before, after), (1, 0), "구 클라의 해제가 반영되지 않았다")

    def test_identified_requests_are_acknowledged_when_topics_are_disabled(self):
        """⛔ flag off 가 **조용히 무시**하고 있었다 — 운영이 지금 그 상태다(실측 확인).

        ⚠️ 응답은 전체-요청 오류가 아니라 **전부 rejected 된 ack** 이다: §8-C 에서
        `topics_disabled` 는 per-topic 범위이고 전체-요청 코드 4개에 없다.
        ⚠️ 이 ack 은 **인증 이전**에 나간다 → ack 수신이 인증 통과를 뜻하지 않는다.
        """
        patchers = self._patchers()
        patchers["dispatcher_flag"] = patch.object(config, "TOPIC_DISPATCHER_ENABLED", False)
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                before = _registry_connection_count()
                for payload in (
                    {"type": "subscribe", "request_id": "off-s", "id_token": "쓰레기",
                     "topics": ["fx:usd-krw", "usdt:krw"]},
                    {"type": "unsubscribe", "request_id": "off-u", "topics": ["fx:usd-krw"]},
                ):
                    ws.send_json(payload)
                    msg = _receive_json_or_fail(ws, self.fail)
                    with self.subTest(op=payload["type"]):
                        self.assertEqual(msg.get("type"), "subscription_ack")
                        self.assertEqual(msg.get("operation"), payload["type"])
                        self.assertEqual(msg.get("accepted_topics"), [])
                        self.assertEqual(
                            msg.get("rejected_topics"),
                            [{"topic": t, "error": "topics_disabled"}
                             for t in payload["topics"]],
                        )
                        self.assertEqual(msg.get("active_subscriptions"), [])
                delta = _registry_connection_count() - before
        self.assertEqual(delta, 0, "flag off 인데 registry 가 바뀌었다")

    def test_topics_payload_failures_terminate_identified_requests(self):
        """⛔ 빈 목록과 잘못된 payload 가 **침묵**이었다 — 식별된 요청이면 큐가 멈춘다.

        ⚠️ 빈 리스트는 `all([])` 가 True 라 현행 검사를 **통과**했고, 그러면 성공과 구분되지
        않는 빈 ack 이 나갔다. §8-C 는 "빈 topics" 를 `invalid_request` 로 규정한다.
        """
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                for label, payload in (
                    ("빈 목록", {"type": "subscribe", "request_id": "e-1", "id_token": "tok",
                                "topics": []}),
                    ("문자열", {"type": "subscribe", "request_id": "e-2", "id_token": "tok",
                               "topics": "fx:usd-krw"}),
                    ("비문자 원소", {"type": "unsubscribe", "request_id": "e-3",
                                  "topics": ["fx:usd-krw", 7]}),
                    ("누락", {"type": "unsubscribe", "request_id": "e-4"}),
                ):
                    ws.send_json(payload)
                    msg = _receive_json_or_fail(ws, self.fail)
                    with self.subTest(case=label):
                        self.assertEqual(msg.get("type"), "subscription_error")
                        self.assertEqual(msg.get("error"), "invalid_request")
                        self.assertEqual(msg.get("request_id"), payload["request_id"])

    def test_subscribe_with_request_id_but_no_token_is_terminated(self):
        """⛔ `{"request_id": …, "id_token": null}` 이 **미식별로 새던** 구멍이다.

        `id_token` **값** 존재만으로 판별하면 이 shape 가 무토큰 경로로 흘러 blind register +
        프레임 0개가 된다 — 클라는 id 를 발급했으므로 in-flight 슬롯이 영영 안 풀린다.
        판별자를 두 축의 **합집합**으로 두면 §8-A 위반으로 종결된다.
        """
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                before = _registry_connection_count()
                ws.send_json({"type": "subscribe", "request_id": "nt-1",
                              "id_token": None, "topics": ["fx:usd-krw"]})
                msg = _receive_json_or_fail(ws, self.fail)
                delta = _registry_connection_count() - before
        self.assertEqual(msg.get("type"), "subscription_error")
        self.assertEqual(msg.get("error"), "invalid_request")
        self.assertEqual(msg.get("request_id"), "nt-1")
        self.assertEqual(delta, 0, "종결시킨 요청이 blind register 를 했다")

    def test_unknown_type_termination_covers_both_identification_axes(self):
        """⛔ U1 의 식별은 **두 축의 합집합**이다 — 각 축을 **독립으로** 잠근다.

        한때 이 테스트가 `request_id` 와 `id_token` 을 **동시에** 넣어, request_id 축만으로
        통과했다. 그동안 `id_token` 만 실은 미지 타입은 **0 프레임**이었다(실측 재현) —
        `id_token` 을 `msg_type == "subscribe"` 일 때만 읽었기 때문이다. 즉 U1 이 문서에만
        있고 코드에는 반만 있었다.

        ⚠️ `id_token` 존재는 **신 프로토콜 클라 신호**다. 그런 클라가 인식 못 하는 타입을
        보냈으면 그 사실을 알려야 한다 — 오타 하나(`subscrbe`)로 같은 shape 가 갈리면 안 된다.
        §E1 구 클라는 토큰을 보내지 않으므로 영향 0이다.
        """
        cases = (
            ("request_id 축", {"type": "subscrbe", "request_id": "axis-r",
                               "topics": ["fx:usd-krw"]}, "axis-r"),
            ("id_token 축", {"type": "subscrbe", "id_token": "tok",
                             "topics": ["fx:usd-krw"]}, None),
        )
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                before = _registry_connection_count()
                results = []
                for _, payload, _expected in cases:
                    ws.send_json(payload)
                    results.append(_receive_json_or_fail(ws, self.fail))
                delta = _registry_connection_count() - before
        for (label, _payload, expected_id), msg in zip(cases, results):
            with self.subTest(axis=label):
                self.assertEqual(msg.get("type"), "subscription_error")
                self.assertEqual(msg.get("error"), "invalid_request")
                self.assertEqual(
                    msg.get("request_id"), expected_id,
                    "echo 할 id 가 없으면 null 이어야 한다(§8-B-stage)",
                )
        self.assertEqual(delta, 0)

    def test_unidentified_unknown_type_stays_silent(self):
        """⚠️ 세 번째 경우 — 두 축 **모두 없으면** 침묵을 유지한다(forward-compat).

        `/ws` 는 legacy 평문·잡음이 흐르는 경계이고, 그쪽엔 기다리는 요청자가 없다.
        """
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                frames = self._probe_then_collect(ws, {"type": "renew_lease",
                                                       "topics": ["fx:usd-krw"]})
        self.assertEqual(frames, [], f"미식별 미지 타입에 프레임이 나갔다: {frames!r}")

    def test_token_bearing_unsubscribe_without_request_id_is_terminated(self):
        """⚠️ **선언된 동작 변화** — 토큰을 실은 unsubscribe 도 `request_id` 를 요구한다.

        §8-A 의 unsubscribe 는 토큰이 없다. 토큰을 실었다는 것은 **신 프로토콜 클라**라는
        신호이므로 `request_id` 도 있어야 한다. 구 동작은 조용히 해제였고, 이제 거부한다 —
        echo 할 id 가 없으면 ack 을 만들 수 없고, ack 없는 unregister 가 바로 이 계약이
        삭제하는 침묵이다. §E1 구 클라는 토큰을 보내지 않으므로 영향 0이다.
        """
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "request_id": "pre-t", "id_token": "tok",
                              "topics": ["fx:usd-krw"]})
                self._receive_for(ws, "pre-t")
                before = _registry_connection_count()
                ws.send_json({"type": "unsubscribe", "id_token": "tok",
                              "topics": ["fx:usd-krw"]})
                msg = self._receive_terminal(ws)
                delta = _registry_connection_count() - before
        self.assertEqual(msg.get("type"), "subscription_error")
        self.assertEqual(msg.get("error"), "invalid_request")
        self.assertIsNone(msg.get("request_id"))
        self.assertEqual(delta, 0, "거부된 요청이 구독을 해제했다")

    def test_active_subscriptions_are_sorted(self):
        """⛔ 정렬은 **정본에 없고 코드에만 있던 계약**이라 재작성에서 조용히 사라질 뻔했다.

        입력이 `set` 이라 정렬하지 않으면 `PYTHONHASHSEED` 에 따라 wire 순서가 프로세스마다
        달라진다. 기존 ack 단언은 전부 원소 1개라 이걸 못 잡았다.
        """
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "request_id": "srt", "id_token": "tok",
                              "topics": ["usdt:krw", "fx:eur-krw", "fx:usd-krw"]})
                msg = self._receive_for(ws, "srt")
        self.assertEqual(
            [x["topic"] for x in msg["active_subscriptions"]],
            ["fx:eur-krw", "fx:usd-krw", "usdt:krw"],
            "active_subscriptions 가 사전순이 아니다 — 순서가 비결정이 된다",
        )
        self.assertEqual(
            [x["topic"] for x in msg["accepted_topics"]],
            ["usdt:krw", "fx:eur-krw", "fx:usd-krw"],
            "accepted_topics 는 **요청 순서**를 보존해야 한다 — 두 축의 정렬 정책은 다르다",
        )

    # ── §8-B Stage 2 — lease 수직 슬라이스 ──────────────────────────────────────
    #
    # ⛔ **한 E2E 에서 전 경로가 돌아야 한다.** helper 단위 테스트로는 "실제로 연결됐다"를
    #    증명하지 못한다 — 이 트랙은 소비자 없는 기계를 만들어 한 번 리셋했다.
    #    경로: /ws subscribe → 인증 → lease 발급 → ack 에 lease → registry 저장
    #          → publish 직전 재확인 → 만료 시 전송 0 → disconnect 시 제거

    def test_lease_is_issued_enforced_at_publish_and_removed_on_disconnect(self):
        from app import topic_dispatcher as dispatcher

        fake_now = {"mono": 1000.0}
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            stack.enter_context(patch.object(
                dispatcher, "lease_clock",
                lambda: SimpleNamespace(mono=lambda: fake_now["mono"],
                                        wall=lambda: None),
            ))
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "request_id": "lease-1",
                              "id_token": "tok", "topics": ["fx:usd-krw"]})
                ack = _receive_json_or_fail(ws, self.fail)

                # ① ack 이 lease 를 싣는다 (§8-B Stage 2)
                accepted = ack.get("accepted_topics") or []
                self.assertEqual(len(accepted), 1, f"수락되지 않았다: {ack!r}")
                lease_id = accepted[0].get("lease_id")
                duration = accepted[0].get("lease_duration_seconds")
                self.assertIsInstance(lease_id, str)
                self.assertTrue(lease_id, "lease_id 가 비어 있다")
                self.assertIsInstance(duration, int)
                self.assertTrue(0 < duration <= 900,
                                f"lease 는 15분 이하여야 한다 — got {duration}")
                self.assertEqual(
                    ack.get("active_subscriptions"), accepted,
                    "active_subscriptions 도 같은 lease 를 실어야 한다(§8-B-stage Stage 2)",
                )

                # ② publish 가 실제로 도달한다 (lease 유효 구간)
                sent_live = _publish_on_app_loop(ws, "fx:usd-krw", {"type": "snapshot", "t": 1})
                self.assertEqual(sent_live, 1, "lease 가 유효한데 publish 가 도달하지 않았다")

                # ③ **인가되지 않은 KRX 는 publish 0** — 애초에 등록되지 않는다
                sent_krx = _publish_on_app_loop(ws, KRX_TOPIC, {"type": "snapshot"})
                self.assertEqual(sent_krx, 0, "인가되지 않은 KRX topic 에 전송이 나갔다")

                # ④ 만료 뒤에는 **전송 0** (publish 직전 재확인, fail-closed)
                fake_now["mono"] += 901.0
                sent_expired = _publish_on_app_loop(ws, "fx:usd-krw", {"type": "snapshot", "t": 2})
                self.assertEqual(sent_expired, 0,
                                 "만료된 lease 로 publish 가 나갔다 — revoke 상한이 무의미해진다")
                after_expiry = _registry_connection_count()

            # ⑤ disconnect 시 제거
        self.assertEqual(after_expiry, 1, "만료가 registry 를 지우면 안 된다(정리는 별 축)")
        self.assertEqual(_registry_connection_count(), 0, "disconnect 후에도 registry 에 남았다")

    def test_untokened_subscribe_cannot_reach_a_per_user_gated_topic(self):
        """⛔ **유료 데이터 우회.** 무토큰 경로가 요청 topic 을 **분류 없이** 등록하면, per-user
        판정이 필요한 KRX 가 **lease 없이** 등록되고 publish 는 lease 부재를 "무제한"으로
        취급한다 → 무인증으로 유료 데이터를 받는다.

        §E1 이 보존하는 것은 **무료 topic 의 기존 동작**이지 "아무 topic 이나 무인증 허용"이 아니다.

        ⚠️ 이 테스트는 **KRX 배포 flag 를 켠 상태**에서 돈다. 끈 상태로 `publish == 0` 만 보면
        인가 로직이 하나도 없어도 통과한다 — 내가 처음 쓴 단언이 정확히 그 형태였다.
        """
        from app import topic_dispatcher as dispatcher

        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            stack.enter_context(patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True))
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                before = len(dispatcher.registry.get_subscribers(KRX_TOPIC))
                ws.send_json({"type": "subscribe", "topics": ["fx:usd-krw", KRX_TOPIC]})
                _receive_json_or_fail(ws, self.fail)        # 무료 topic snapshot
                krx_subs = len(dispatcher.registry.get_subscribers(KRX_TOPIC)) - before
                sent = _publish_on_app_loop(ws, KRX_TOPIC, {"type": "snapshot"})
                free_subs = len(dispatcher.registry.get_subscribers("fx:usd-krw"))
        self.assertEqual(krx_subs, 0, "무토큰 구독이 per-user gated topic 에 등록됐다")
        self.assertEqual(sent, 0, "무인증 연결로 유료 topic 이 전송됐다")
        self.assertGreater(free_subs, 0, "무료 topic 까지 막혔다 — §E1 이 깨졌다(반대 방향)")

    def test_untokened_resubscribe_cannot_clear_an_existing_lease(self):
        """⛔ **lease 우회**: 토큰으로 lease 를 받은 뒤 **무토큰 재구독**으로 그 lease 를 지우면
        그 구독은 legacy(무제한)가 되어 **15분 revoke 상한이 무력화**된다.

        우리 클라는 이 조합을 만들지 않지만, 그게 방어를 빼도 되는 이유는 아니다 —
        wire 는 아무나 말할 수 있다.
        """
        from app import topic_dispatcher as dispatcher

        fake_now = {"mono": 5000.0}
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            stack.enter_context(patch.object(
                dispatcher, "lease_clock",
                lambda: SimpleNamespace(mono=lambda: fake_now["mono"], wall=lambda: None),
            ))
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "request_id": "bypass-1",
                              "id_token": "tok", "topics": ["fx:usd-krw"]})
                _receive_json_or_fail(ws, self.fail)                 # ack (lease 발급)

                ws.send_json({"type": "subscribe", "topics": ["fx:usd-krw"]})   # 무토큰 재구독
                _receive_json_or_fail(ws, self.fail)                 # snapshot

                fake_now["mono"] += 901.0
                sent = _publish_on_app_loop(ws, "fx:usd-krw", {"type": "snapshot"})
        self.assertEqual(
            sent, 0,
            "무토큰 재구독이 lease 를 지웠다 — 인증된 구독이 무제한이 된다(fail-open)",
        )

    def test_lease_horizon_starts_before_the_auth_call_not_after(self):
        """⛔ 인증 I/O 가 걸린 시간만큼 **15분 경계가 늘어나면 안 된다**(fail-open).

        검증 결과가 반영하는 상태는 아무리 늦어도 **호출 시작 시점**이다. 끝난 시각을 쓰면
        느린 인증일수록 revoke 상한이 길어진다 — 정확히 반대 방향이다.

        ⚠️ 검증자가 즉시 반환하면 두 시각이 같아 이 성질이 **관측되지 않는다**(실측: 그래서
        변이가 생존했다). 그래서 검증자 안에서 fake 시계를 전진시킨다.
        """
        from app import topic_dispatcher as dispatcher

        fake_now = {"mono": 100.0}
        auth_io_seconds = 30.0

        async def slow_verify(_id_token):
            fake_now["mono"] += auth_io_seconds     # 인증 I/O 가 30초 걸렸다
            return "uid-slow"

        patchers = self._patchers()
        patchers["verify_token"] = patch("app.main.verify_ws_subscribe_token",
                                         new=AsyncMock(side_effect=slow_verify))
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            stack.enter_context(patch.object(
                dispatcher, "lease_clock",
                lambda: SimpleNamespace(mono=lambda: fake_now["mono"], wall=lambda: None),
            ))
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "request_id": "slow-1",
                              "id_token": "tok", "topics": ["fx:usd-krw"]})
                ack = _receive_json_or_fail(ws, self.fail)

        duration = (ack.get("accepted_topics") or [{}])[0].get("lease_duration_seconds")
        self.assertEqual(
            duration, int(900 - auth_io_seconds),
            "lease 지평이 인증 I/O 만큼 늘어났다 — 느린 인증일수록 revoke 상한이 길어진다",
        )

    def test_ack_reports_remaining_lease_not_the_original_duration(self):
        """⛔ ack 은 **그 시점의 남은 시간**을 실어야 한다(정본 §8-B: "topic별 lease_id + 남은
        duration"). 발급 당시 값을 되풀이하면, 900초 lease 를 받고 600초 뒤 다른 topic 을
        해제했을 때 실제 잔여 300초인데 ack 이 다시 900 을 말한다 — 클라가 그걸로 timer 를
        재설정하면 **서버 만료보다 늦게 재인증**해 데이터가 조용히 끊긴다.
        """
        from app import topic_dispatcher as dispatcher

        fake_now = {"mono": 200.0}
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            stack.enter_context(patch.object(
                dispatcher, "lease_clock",
                lambda: SimpleNamespace(mono=lambda: fake_now["mono"], wall=lambda: None),
            ))
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "request_id": "rem-1", "id_token": "tok",
                              "topics": ["fx:usd-krw", "usdt:krw"]})
                first = self._receive_for(ws, "rem-1")

                fake_now["mono"] += 600.0                  # lease 소비 600초
                ws.send_json({"type": "unsubscribe", "request_id": "rem-2",
                              "topics": ["usdt:krw"]})
                second = self._receive_for(ws, "rem-2")

                fake_now["mono"] += 400.0                  # 총 1000초 → 이미 만료
                ws.send_json({"type": "unsubscribe", "request_id": "rem-3",
                              "topics": ["not:subscribed"]})
                third = self._receive_for(ws, "rem-3")

        self.assertEqual([x["lease_duration_seconds"] for x in first["accepted_topics"]],
                         [900, 900], "발급 직후엔 전체 구간이어야 한다")
        remaining = second["active_subscriptions"]
        self.assertEqual([x["topic"] for x in remaining], ["fx:usd-krw"])
        self.assertEqual(remaining[0]["lease_duration_seconds"], 300,
                         "ack 이 발급 당시 duration 을 되풀이했다 — 클라 timer 가 만료를 넘긴다")
        self.assertEqual(
            third["active_subscriptions"][0]["lease_duration_seconds"], 0,
            "만료된 lease 는 **0** 이어야 한다 — 필드를 빼면 무토큰 구독과 구분되지 않아 "
            "클라가 무제한으로 오해한다",
        )

    def test_detailed_publisher_counts_only_lease_eligible_attempts(self):
        """⚠️ `attempted` 는 **lease 게이트 이후** 수다 — "전송 자격이 있어 실제로 시도한 대상".

        ⛔ 그래서 **등록자는 있는데 전부 만료**면 `attempted == 0` 이고 downstream 이
        `NO_SUBSCRIBERS` 로 분류한다. 운영상 "아무도 안 본다"와 "다들 재인증을 못 하고 있다"는
        다른 사건이므로, lease 가 실제로 발화하면 만료-skip 수를 따로 세야 한다.
        지금은 소비자가 없어 **의미만 고정**한다.
        """
        from app import topic_dispatcher as dispatcher

        fake_now = {"mono": 300.0}
        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            stack.enter_context(patch.object(
                dispatcher, "lease_clock",
                lambda: SimpleNamespace(mono=lambda: fake_now["mono"], wall=lambda: None),
            ))
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "request_id": "det-1", "id_token": "tok",
                              "topics": ["fx:usd-krw"]})
                self._receive_for(ws, "det-1")

                portal = ws.portal
                live = portal.call(dispatcher.publish_topic_detailed,
                                   "fx:usd-krw", {"type": "snapshot"})
                fake_now["mono"] += 901.0
                expired = portal.call(dispatcher.publish_topic_detailed,
                                      "fx:usd-krw", {"type": "snapshot"})
                still_registered = len(dispatcher.registry.get_subscribers("fx:usd-krw"))

        self.assertEqual((live.attempted, live.sent), (1, 1), "유효 lease 인데 시도가 없었다")
        self.assertEqual(
            (expired.attempted, expired.sent), (0, 0),
            "만료된 구독이 detailed publisher 의 게이트를 통과했다",
        )
        self.assertTrue(expired.enabled, "flag 는 켜져 있다 — enabled 까지 꺼지면 원인이 흐려진다")
        self.assertEqual(still_registered, 1,
                         "만료가 registry 를 지웠다 — 정리는 disconnect / 재구독 축이다")

    def test_untokened_subscription_has_no_lease_and_still_receives(self):
        """⚠️ §E1 — 무토큰 구독은 lease 가 **없고**, 그래도 발행은 도달해야 한다.

        lease 는 *인증된* 구독의 revoke 상한을 위한 것이다. 무토큰 구독은 인증된 적이 없어
        철회할 자격도 없다 — 여기에 lease 를 요구하면 구 클라가 조용히 데이터를 잃는다.
        (enforcement 와 capability 의 분리 — §E1.)
        """
        from app import topic_dispatcher as dispatcher

        patchers = self._patchers()
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "topics": ["fx:usd-krw"]})
                self.assertEqual(_receive_json_or_fail(ws, self.fail).get("type"), "snapshot")
                sent = _publish_on_app_loop(ws, "fx:usd-krw", {"type": "snapshot"})
        self.assertEqual(sent, 1, "무토큰 구독이 발행을 못 받는다 — §E1 이 깨졌다")

    def test_auth_stages_share_one_absolute_deadline(self):
        """⛔ 단계마다 새 timeout 을 시작하면 상한이 **단계 수만큼 곱해진다**(identity 10s +
        gated 10s = 20s). config 는 이 값을 "호출자 대기를 끊는 상한"(**단수**)으로 정의한다.

        ⚠️ **sleep 으로 재현하지 않는다**(codex): "0.5초 예산 / 각 단계 0.35초" 식 누적 테스트는
        CI 부하로 **첫 단계만 예산을 넘겨도 통과**해 거짓 green 이 된다 — 공유 여부를 전혀
        검증하지 못한다. 대신 `timeout_at` 을 passthrough 로 감싸 **두 호출이 정확히 같은
        `when` 을 받는지** 본다. 그게 "공유 절대 deadline"의 정의 그 자체다.
        """
        from app import topic_dispatcher as dispatcher

        seen_when = []
        real_timeout_at = asyncio.timeout_at

        def recording_timeout_at(when):
            seen_when.append(when)
            return real_timeout_at(when)

        patchers = self._patchers()
        patchers["revenuecat_must_not_be_called"] = patch(
            "app.subscription.fetch_revenuecat_result",
            new=AsyncMock(return_value=SimpleNamespace(is_premium=True)),
        )
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            stack.enter_context(patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True))
            stack.enter_context(patch.object(
                dispatcher, "authorize_gated_subscription",
                new=AsyncMock(return_value=_granted_verdict()),
            ))
            stack.enter_context(patch.object(dispatcher.asyncio, "timeout_at",
                                             recording_timeout_at))
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "request_id": "dl-share",
                              "id_token": "tok", "topics": ["fx:usd-krw", KRX_TOPIC]})
                self._receive_for(ws, "dl-share")

        self.assertEqual(len(seen_when), 2,
                         f"인증 단계가 2회 timeout 을 열지 않았다: {seen_when!r}")
        self.assertEqual(
            seen_when[0], seen_when[1],
            "두 단계가 **서로 다른** deadline 을 열었다 — 상한이 단계 수만큼 곱해진다",
        )

    def test_client_message_handling_is_awaited_not_fire_and_forget(self):
        """⛔ **클라가 이 성질에 의존한다** — ack 을 받은 순서대로 상태를 수렴시킨다.

        서버가 메시지를 fire-and-forget 으로 처리하면 느린 요청의 ack 이 뒤늦게 도착해 최신 상태를
        되돌릴 수 있다. 현행 루프는 `await handle_client_message(...)` 라 그 역전이 **구조적으로
        불가능**하다.

        ⚠️ **왜 행동 테스트가 아니라 구조 검사인가**: 타이밍으로 잡으려 했더니 `create_task` 로
        바꾼 변이가 **생존**했다(실측) — TestClient 는 다른 스레드에서 메시지를 보내므로 "첫
        핸들러가 진입한 뒤"를 결정적으로 만들 수 없고, gate 를 너무 일찍 풀면 인터리빙 창 자체가
        안 생긴다. 시간에 기대는 단언은 CI 부하에서 흔들리기도 한다. 그래서 **AST 로** 본다 —
        이 성질에 대해서는 그것이 유일하게 결정적인 관측이다.
        """
        import ast as _ast
        import inspect

        from app import main as _main

        source = inspect.getsource(_main.websocket_endpoint)
        tree = _ast.parse(source.lstrip())
        awaited = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Await) and isinstance(node.value, _ast.Call):
                func = node.value.func
                if isinstance(func, _ast.Attribute):
                    awaited.add(func.attr)
        calls = {
            n.func.attr
            for n in _ast.walk(tree)
            if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
        }
        self.assertIn(
            "handle_client_message", calls,
            "핸들러 호출이 사라졌다 — 이 검사가 공허해졌다(자기검사)",
        )
        self.assertIn(
            "handle_client_message", awaited,
            "메시지 처리가 await 되지 않는다 — ack 역전이 가능해지고 클라의 순서 수렴 전제가 깨진다",
        )


class TestWsSubscribeTokenVerifier(unittest.IsolatedAsyncioTestCase):
    """`verify_ws_subscribe_token` **본문** — E2E 는 이 함수를 통째로 fake 하므로 미검증이다.

    실측(codex): 본문을 `return "uid"` 로 바꿔도 위 E2E 는 통과한다. **seam 배선**과
    **검증자 본문**은 다른 주장이므로 따로 잠근다.

    ⚠️ 이 테스트가 잠그지 **않는** 것: Firebase 예외 → wire 오류 코드 매핑(`invalid_token` /
    `temporarily_unavailable`). 그건 다음 슬라이스이고, 그때 REST 쪽 분류와의 중복을 함께 정리한다
    (codex Medium: 지금은 SDK 호출 한 줄만 겹치지만 분류가 들어오면 진짜 중복이 된다).
    """

    async def test_returns_the_uid_from_the_decoded_token(self):
        import firebase_admin

        with patch("app.main.is_firebase_initialized", return_value=True), \
             patch("app.notifications.fcm.ws_auth_app", return_value=object()), \
             patch.object(
                 firebase_admin.auth, "verify_id_token", return_value={"uid": "uid-9"}
             ) as verify:
            uid = await app_main.verify_ws_subscribe_token("tok-9")
        self.assertEqual(uid, "uid-9")
        # ⛔ `check_revoked=True` 가 계약이다 — False 면 revoke 된 토큰이 통과해 accept 된다.
        self.assertEqual(verify.call_args.args, ("tok-9",))
        self.assertIs(verify.call_args.kwargs["check_revoked"], True)
        self.assertIn("app", verify.call_args.kwargs, "인증 전용 app 이 전달되지 않았다")

    async def test_verification_runs_off_the_event_loop_thread(self):
        """⛔ 동기 SDK 호출이 loop 스레드에서 돌면 **그 프로세스의 모든 연결이 함께 멈춘다**.

        ⚠️ **시간으로 재지 않는다.** 부하 아래서 흔들리기 때문이다 — 대신 검증이 실제로 어느
        스레드에서 돌았는지 본다(결정적). 이 테스트 본문은 loop 스레드에서 돌므로 그것과
        비교하면 오프로드 여부가 정확히 갈린다.
        """
        import firebase_admin

        loop_thread = threading.current_thread()
        seen = []

        def _record(token, **kwargs):
            seen.append(threading.current_thread())
            return {"uid": "uid-off"}

        with patch("app.main.is_firebase_initialized", return_value=True), \
             patch("app.notifications.fcm.ws_auth_app", return_value=object()), \
             patch.object(firebase_admin.auth, "verify_id_token", side_effect=_record):
            uid = await app_main.verify_ws_subscribe_token("tok-off")

        self.assertEqual(uid, "uid-off")
        self.assertEqual(len(seen), 1, "검증이 정확히 1회 돌지 않았다")
        self.assertIsNot(
            seen[0], loop_thread,
            "검증이 event loop 스레드에서 돌았다 — 인증서 조회가 걸리면 전 연결이 멈춘다",
        )

    async def test_refuses_when_firebase_is_not_initialized(self):
        """⛔ 미초기화를 통과시키면 검증 없는 uid 가 흐른다.

        ⚠️ 계약이 바뀌었다: 구 버전은 bare `RuntimeError` 였다. 이제 **판정 불가**로 분류돼
        `SubscribeAuthFailed("temporarily_unavailable", …)` 이다 — 자격 오류가 아니라 우리 쪽
        상태이므로 클라에 재인증을 지시하면 안 된다.
        """
        from app.topic_wire import SubscribeAuthFailed

        with patch("app.main.is_firebase_initialized", return_value=False):
            with self.assertRaises(SubscribeAuthFailed) as caught:
                await app_main.verify_ws_subscribe_token("tok")
        self.assertEqual(caught.exception.error, "temporarily_unavailable")
        self.assertGreaterEqual(caught.exception.retry_after_seconds, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
