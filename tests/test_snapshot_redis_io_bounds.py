"""LOAD-S2 Redis 후속 — snapshot sync Redis I/O와 pool 반환의 실행형 계약."""

from __future__ import annotations

import asyncio
import ast
import pathlib
import socket
import threading
import time
import unittest
from unittest.mock import patch

from app import latest_rates_cache as lrc
from app import topic_initial_snapshot as snapshot


REPO = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOT_REDIS_PATHS = (
    REPO / "app" / "topic_initial_snapshot.py",
    REPO / "app" / "fx_topic_payload.py",
    REPO / "app" / "usdt_topic_payload.py",
    REPO / "app" / "krx_topic_publisher.py",
)


class _StalledRespServer:
    """CLIENT SETINFO에는 답하고 GET 응답만 멈추는 최소 RESP 서버."""

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.get_seen = threading.Event()
        self.stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @staticmethod
    def _read_command(conn: socket.socket, buffered: bytes) -> tuple[list[bytes] | None, bytes]:
        while b"\r\n" not in buffered:
            chunk = conn.recv(4096)
            if not chunk:
                return None, b""
            buffered += chunk
        header, buffered = buffered.split(b"\r\n", 1)
        if not header.startswith(b"*"):
            return None, buffered

        args: list[bytes] = []
        for _ in range(int(header[1:])):
            while b"\r\n" not in buffered:
                buffered += conn.recv(4096)
            length, buffered = buffered.split(b"\r\n", 1)
            size = int(length[1:])
            while len(buffered) < size + 2:
                buffered += conn.recv(4096)
            args.append(buffered[:size])
            buffered = buffered[size + 2:]
        return args, buffered

    def _serve(self) -> None:
        conn: socket.socket | None = None
        try:
            conn, _ = self._listener.accept()
            conn.settimeout(2.0)
            buffered = b""
            while not self.stop.is_set():
                command, buffered = self._read_command(conn, buffered)
                if command is None:
                    return
                if command[0].upper() == b"GET":
                    self.get_seen.set()
                    self.stop.wait(2.0)
                    return
                conn.sendall(b"+OK\r\n")
        finally:
            if conn is not None:
                conn.close()
            self._listener.close()

    def close(self) -> None:
        self.stop.set()
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=0.1).close()
        except OSError:
            pass
        self._thread.join(timeout=1.0)


class TestSnapshotRedisClientConfiguration(unittest.TestCase):
    def setUp(self) -> None:
        self._client = lrc._sync_client
        lrc._sync_client = None

    def tearDown(self) -> None:
        lrc._sync_client = self._client

    def test_factory_binds_all_finite_bounds_to_a_blocking_pool(self) -> None:
        pool = object()
        client = object()
        url = "redis://bounded-test.invalid:6379/0"
        password = "test-password"
        with patch.object(lrc.config, "REDIS_URL", url), \
             patch.object(lrc.config, "REDIS_PASSWORD", password), \
             patch.object(lrc.redis_sync.BlockingConnectionPool, "from_url", return_value=pool) as make_pool, \
             patch.object(lrc.redis_sync, "Redis", return_value=client) as make_client:
            self.assertIs(lrc._get_sync_client(), client)

        make_pool.assert_called_once_with(
            url,
            password=password,
            decode_responses=False,
            max_connections=lrc.SYNC_REDIS_MAX_CONNECTIONS,
            timeout=lrc.SYNC_REDIS_POOL_WAIT_TIMEOUT_SECONDS,
            socket_timeout=lrc.SYNC_REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=lrc.SYNC_REDIS_SOCKET_TIMEOUT_SECONDS,
            retry_on_timeout=False,
        )
        make_client.assert_called_once_with(connection_pool=pool)
        self.assertEqual(lrc.SYNC_REDIS_MAX_CONNECTIONS, 50)
        self.assertGreater(lrc.SYNC_REDIS_POOL_WAIT_TIMEOUT_SECONDS, 0)
        self.assertGreater(lrc.SYNC_REDIS_SOCKET_TIMEOUT_SECONDS, 0)

    def test_snapshot_call_graph_has_no_raw_or_async_redis_bypass(self) -> None:
        imported_modules: set[str] = set()
        called_by_file: dict[str, set[str]] = {}
        for path in SNAPSHOT_REDIS_PATHS:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            called_by_file[path.name] = {
                node.func.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules.add(node.module)
                    # ⛔ `from app import cache` 는 node.module 이 "app" 뿐이라 위 한 줄만으로는
                    #    "app.cache" 를 절대 만들지 않는다 — 가드가 그 형태를 통째로 놓쳤다
                    #    (변이 실측: 그 import 를 주입해도 green). alias 를 붙여 dotted 이름을
                    #    함께 기록한다. 이 리포는 `from app import latest_rates_cache, ...` 형태를
                    #    실제로 쓰므로 가정 가능한 형태다.
                    imported_modules.update(
                        f"{node.module}.{alias.name}" for alias in node.names
                    )

        self.assertFalse(
            {name for name in imported_modules if name == "redis" or name.startswith("redis.")},
            "snapshot 경로가 bounded sync client를 우회해 raw Redis client를 만들었다",
        )
        self.assertNotIn("app.cache", imported_modules, "snapshot 경로가 async redis_cache로 갈라졌다")
        expected_calls = {
            "topic_initial_snapshot.py": {
                "load_and_build_fx_topic_payload",
                "load_and_build_tether_tab_payload",
                "load_krx_topic_entry",
            },
            "fx_topic_payload.py": {
                "get_latest_bank_rate_from_sync_job",
                "get_latest_investing_rate_from_sync_job",
            },
            "usdt_topic_payload.py": {
                "get_latest_usdt_rate_from_sync_job",
                "get_latest_bank_rate_from_sync_job",
                "get_latest_investing_rate_from_sync_job",
            },
            "krx_topic_publisher.py": {"get_latest_krx_rate_from_sync_job"},
        }
        for filename, expected in expected_calls.items():
            self.assertTrue(
                expected <= called_by_file[filename],
                f"snapshot Redis 호출 그래프가 갈렸다: {filename} missing={expected - called_by_file[filename]}",
            )


class TestBypassGuardCollectsBothImportForms(unittest.TestCase):
    """가드가 **두 import 형태를 모두** 기록하는지 잠근다.

    `from app.cache import redis_cache` 만 잡고 `from app import cache` 를 놓치면,
    가드는 통과하는데 우회는 실재한다 — 이 세션에서 변이로 실측된 구멍이다.
    """

    @staticmethod
    def _modules(source: str) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
        return found

    def test_both_forms_yield_the_dotted_module_name(self) -> None:
        for label, source in (
            ("from app.cache import", "from app.cache import redis_cache\n"),
            ("from app import", "from app import cache\n"),
            ("import app.cache", "import app.cache\n"),
        ):
            with self.subTest(form=label):
                self.assertIn("app.cache", self._modules(source))

    def test_unrelated_app_import_is_not_flagged(self) -> None:
        self.assertNotIn("app.cache", self._modules("from app import crud, models\n"))


class TestSnapshotRedisCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_caller_does_not_claim_early_release_and_slot_returns_after_timeout(self) -> None:
        server = _StalledRespServer()
        original_client = lrc._sync_client
        lrc._sync_client = None
        worker_finished = threading.Event()

        try:
            with patch.object(lrc.config, "REDIS_URL", f"redis://127.0.0.1:{server.port}/0"), \
                 patch.object(lrc.config, "REDIS_PASSWORD", None), \
                 patch.object(lrc, "SYNC_REDIS_MAX_CONNECTIONS", 1), \
                 patch.object(lrc, "SYNC_REDIS_POOL_WAIT_TIMEOUT_SECONDS", 0.2), \
                 patch.object(lrc, "SYNC_REDIS_SOCKET_TIMEOUT_SECONDS", 0.2):
                client = lrc._get_sync_client()
                self.assertIsNotNone(client)

                def stalled_builder(_topic: str):
                    try:
                        client.get("snapshot:redis:release-probe")
                    finally:
                        worker_finished.set()

                with patch.object(snapshot, "_build_snapshot_sync", side_effect=stalled_builder):
                    task = asyncio.create_task(snapshot.build_snapshot_observed("fx:usd-krw"))
                    deadline = time.monotonic() + 1.0
                    while not server.get_seen.is_set() and time.monotonic() < deadline:
                        await asyncio.sleep(0.01)
                    self.assertTrue(server.get_seen.is_set(), "양성대조 실패: GET read가 실제로 막히지 않았다")

                    pool = client.connection_pool
                    self.assertEqual(pool.pool.qsize(), 0, "GET 중인데 pool slot이 이미 반환됐다")
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertFalse(
                        worker_finished.is_set(),
                        "to_thread caller 취소가 sync worker까지 멈춘 것처럼 보인다",
                    )
                    self.assertEqual(pool.pool.qsize(), 0, "worker 실행 중 socket을 반환했다고 오판했다")

                    deadline = time.monotonic() + 1.0
                    while not worker_finished.is_set() and time.monotonic() < deadline:
                        await asyncio.sleep(0.01)
                    self.assertTrue(worker_finished.is_set(), "socket read 상한 뒤에도 worker가 끝나지 않았다")
                    self.assertEqual(pool.pool.qsize(), 1, "timeout 뒤 connection이 pool로 반환되지 않았다")
        finally:
            if lrc._sync_client is not None:
                lrc._sync_client.connection_pool.disconnect()
            lrc._sync_client = original_client
            server.close()
