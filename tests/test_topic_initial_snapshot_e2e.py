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
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

# conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 설정.
from app import config, models
from app.database import engine
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
        (상수/다른 uid로 판정하면 남의 권한으로 서빙된다)."""
        premium = AsyncMock(return_value=True)
        krx = MagicMock(return_value=True)
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch("app.main.verify_firebase_token",
                   new=AsyncMock(return_value="uid-from-token")), \
             patch("app.main.require_premium", new=premium), \
             patch("app.entitlements.compute_krx_visible", new=krx), \
             patch("app.topic_initial_snapshot._build_snapshot_sync",
                   return_value=_canned_snapshot("usdt:krw")):
            r = self._get()
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
                   return_value=_canned_snapshot("usdt:krw")):
            self._get()
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
