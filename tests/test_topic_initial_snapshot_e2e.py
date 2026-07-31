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
import contextlib
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
        self.assertEqual(msg.get("accepted_topics"), [{"topic": "fx:usd-krw"}])
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
            msg.get("active_subscriptions"), [{"topic": "fx:usd-krw"}],
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
                subs = topic_dispatcher.registry.get_subscriptions(ws)
        return msg, pong, subs

    def test_revoked_token_becomes_invalid_token_and_keeps_the_connection(self):
        """⛔ 현행은 프레임 **0개 + 연결 종료**였다 — 그 차이는 소켓에서만 보인다."""
        from app.topic_wire import SubscribeAuthFailed

        msg, pong, subs = self._subscribe_and_receive(
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
        self.assertEqual(subs, set(), "실패한 요청이 registry 를 바꿨다")

    def test_unavailable_verdict_carries_retry_after(self):
        from app.topic_wire import SubscribeAuthFailed

        msg, _, subs = self._subscribe_and_receive(
            verifier_raises=SubscribeAuthFailed("temporarily_unavailable", 5),
        )
        self.assertEqual(msg.get("error"), "temporarily_unavailable")
        self.assertIs(type(msg.get("retry_after_seconds")), int, "정수여야 한다")
        self.assertGreaterEqual(msg.get("retry_after_seconds"), 1)
        self.assertEqual(subs, set())

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
        patchers["fb_verify"] = patch.object(
            firebase_admin.auth, "verify_id_token", side_effect=sdk_error
        )
        with contextlib.ExitStack() as stack:
            for patcher in patchers.values():
                stack.enter_context(patcher)
            with self.client.websocket_connect("/ws") as ws:
                _receive_json_or_fail(ws, self.fail)
                ws.send_json({"type": "subscribe", "request_id": request_id,
                              "id_token": "tok", "topics": ["fx:usd-krw"]})
                msg = _receive_json_or_fail(ws, self.fail)
                subs = topic_dispatcher.registry.get_subscriptions(ws)
        return msg, subs

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
                        msg, subs = self._subscribe_with_real_verifier(exc)
                else:
                    with self.assertNoLogs("exchange_rate.main", level="ERROR"):
                        msg, subs = self._subscribe_with_real_verifier(exc)
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
                self.assertEqual(subs, set(), "실패가 registry 를 바꿨다")

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
             patch.object(
                 firebase_admin.auth, "verify_id_token", return_value={"uid": "uid-9"}
             ) as verify:
            uid = await app_main.verify_ws_subscribe_token("tok-9")
        self.assertEqual(uid, "uid-9")
        # ⛔ `check_revoked=True` 가 계약이다 — False 면 revoke 된 토큰이 통과해 accept 된다.
        verify.assert_called_once_with("tok-9", check_revoked=True)

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
