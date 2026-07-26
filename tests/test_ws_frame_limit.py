"""WebSocket **메시지** 상한 계약 (ADR-039 §8.1 D7 / harness 선행 (4)).

## 왜 "frame"이 아니라 "message"인가

`--ws-max-size`는 **incoming message** 크기 상한이고, 분할 전송된 frame들의 **합산**에 적용된다
(websockets `legacy/protocol.py`). 계획 문서가 "frame 상한"이라 부르던 것을 여기서는 message로 적는다.

## 계약이 서버가 아니라 **클라이언트** 관측인 이유 (실측)

lock 정확 버전(uvicorn 0.44.0 / websockets 16.0)에서 재현:

| impl | 클라이언트 | ASGI 앱 |
| --- | --- | --- |
| `websockets` (Dockerfile 고정) | close **1009** | **1006**, reason `''` |
| `websockets-sansio` | 1009 | 1009 + reason |
| `wsproto` (0.44.0) | 정상 1000 | **상한 무시, 전량 수신** |

legacy impl의 `fail_connection`이 피어의 close 프레임을 파싱하지 않아 앱은 1006(=일반 네트워크
단절과 **같은 코드**)을 본다. 따라서 "ASGI에서 1009 확인"은 원리적으로 불가능하고, 서버 로그만으로는
oversize 남용과 평범한 단절을 구분할 수 없다(알려진 한계, ADR-039에 기록).

## `--ws websockets`를 함께 고정하는 이유

`--ws`가 없으면 `auto`인데, `auto`는 **버전에 따라 다른 impl로 귀결**한다:
- 현재(0.44.0): websockets 설치 시 legacy → 상한 적용
- websockets 미설치 시: wsproto로 내려가고 **0.44.0의 wsproto는 `max_size`를 무시**한다
  (0.46.0부터 지원). wsproto는 lock에 이미 있어 기동 실패조차 하지 않는다 = **조용한 fail-open**
- uvicorn 0.50.0(2026-07-04)부터: `auto` 기본이 `websockets-sansio`로 바뀌고 legacy는 deprecated

명시 고정하면 websockets 없는 env에서 uvicorn이 **기동 자체를 실패**한다(fail-fast, 실측).
⚠️ **업그레이드 게이트**: uvicorn을 0.50.0+로 올릴 때 `websockets-sansio` 전환을 검토하고
(앱이 1009+reason을 봐 관측 공백이 해소된다), 전환 시 Dockerfile CMD와 이 파일의 기대값을 함께 바꾼다.
"""
import json
import os
import re
import socket
import subprocess
import sys
import unittest
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
WS_MAX_SIZE = 16384
WS_IMPL = "websockets"


def _dockerfile_cmd_tokens() -> list[str]:
    """Dockerfile의 마지막 exec-form `CMD`를 **구조 파싱**한다.

    단순 substring 검사는 주석·구 CMD·중복 옵션에 속는다(codex).
    """
    text = (REPO_ROOT / "Dockerfile").read_text()
    matches = re.findall(r"^CMD\s+(\[.*\])\s*$", text, re.MULTILINE)
    if not matches:
        raise AssertionError("Dockerfile에 exec-form CMD가 없다")
    return json.loads(matches[-1])


def _option_value(tokens: list[str], flag: str) -> str:
    occurrences = [i for i, t in enumerate(tokens) if t == flag]
    if len(occurrences) != 1:
        raise AssertionError(f"{flag}가 정확히 1번 있어야 한다 (실제 {len(occurrences)}번)")
    index = occurrences[0]
    if index + 1 >= len(tokens):
        raise AssertionError(f"{flag} 뒤에 값이 없다")
    return tokens[index + 1]


class TestDeploymentTripWire(unittest.TestCase):
    """하니스는 자체 서버를 띄우므로, **프로덕션 설정이 조용히 갈라지는 것**을 여기서 막는다.

    이 trip-wire가 없으면 아래 동작 테스트는 green인데 배포엔 상한이 없는 상태가 성립한다.
    """

    def test_dockerfile_pins_impl_and_limit(self):
        tokens = _dockerfile_cmd_tokens()
        self.assertEqual(tokens[0], "uvicorn")
        self.assertEqual(_option_value(tokens, "--ws"), WS_IMPL,
                         "auto는 버전·설치상태에 따라 상한을 무시하는 impl로 귀결한다")
        self.assertEqual(_option_value(tokens, "--ws-max-size"), str(WS_MAX_SIZE))

    def test_compose_does_not_override_command(self):
        """CMD를 고정해도 compose가 덮어쓰면 무의미하다."""
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        service = re.search(r"^  fastapi:\n(.*?)(?=^  \S|\Z)", compose, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(service, "compose에 fastapi 서비스가 없다")
        body = service.group(1)
        for key in ("command:", "entrypoint:"):
            self.assertNotIn(key, body, f"fastapi 서비스가 {key}로 CMD를 덮어쓴다")

    def test_docker_md_reproduces_the_real_cmd(self):
        """`DOCKER.md`가 Dockerfile CMD를 **통째로 복제**한다 — 드리프트하면 따라 만든 배포에서
        상한이 조용히 사라진다(실제로 이번에 어긋나 있었다). 두 CMD 줄이 정확히 같아야 한다.
        """
        docker_md = (REPO_ROOT / "DOCKER.md").read_text()
        cmd_lines = [ln.strip() for ln in docker_md.splitlines() if ln.startswith("CMD [")]
        self.assertEqual(len(cmd_lines), 1, "DOCKER.md의 CMD 복제본이 1개여야 한다")
        real = [ln.strip() for ln in (REPO_ROOT / "Dockerfile").read_text().splitlines()
                if ln.startswith("CMD [")]
        self.assertEqual(cmd_lines[0], real[-1], "DOCKER.md 복제본이 Dockerfile과 어긋났다")

    def test_pinned_impl_is_importable(self):
        """`--ws websockets` 고정은 이 모듈이 있어야 성립한다(없으면 uvicorn이 기동 실패)."""
        __import__("uvicorn.protocols.websockets.websockets_impl")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _ProbeServer:
    """Dockerfile CMD 토큰을 **재사용**해 uvicorn을 띄운다 — 플래그를 따로 하드코딩하지 않는다."""

    def __init__(self):
        self.port = _free_port()
        self.proc: subprocess.Popen | None = None

    def argv(self) -> list[str]:
        tokens = list(_dockerfile_cmd_tokens())
        tokens[tokens.index("app.main:app")] = "tests._ws_probe_app:app"
        tokens[tokens.index("--port") + 1] = str(self.port)
        tokens[tokens.index("--host") + 1] = "127.0.0.1"
        return [sys.executable, "-m"] + tokens

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.argv(), cwd=str(REPO_ROOT), env=dict(os.environ),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        finally:
            self.proc = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"


class _WsLimitBase(unittest.IsolatedAsyncioTestCase):
    server: _ProbeServer

    @classmethod
    def setUpClass(cls):
        cls.server = _ProbeServer()
        try:
            cls.server.start()
        except Exception:
            cls.server.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    async def asyncSetUp(self):
        # readiness = TCP가 아니라 **WS handshake + 앱 응답**까지 (TCP만으론 앱 준비를 모른다)
        import asyncio
        last = None
        for _ in range(100):
            try:
                async with websockets.connect(self.server.url, open_timeout=1) as ws:
                    await ws.send("r")
                    if await asyncio.wait_for(ws.recv(), timeout=2) == "text:1":
                        return
            except Exception as exc:                      # noqa: BLE001 - 기동 대기 폴링
                last = exc
                await asyncio.sleep(0.1)
        out = ""
        if self.server.proc and self.server.proc.stdout:
            self.server.proc.kill()
            out = self.server.proc.stdout.read()[-2000:]
        raise AssertionError(f"probe 서버 준비 실패: {last}\n--- 서버 출력 ---\n{out}")

    async def _send_expect_close(self, payload) -> int | None:
        """oversize 전송 후 **클라이언트가 관측한** close code."""
        try:
            async with websockets.connect(self.server.url, open_timeout=5) as ws:
                await ws.send(payload)
                await ws.recv()
        except websockets.exceptions.ConnectionClosed as exc:
            # `exc.code`는 13.1에서 deprecated — 수신한 close 프레임의 코드를 직접 읽는다.
            # (여기서는 서버가 1009를 보내므로 rcvd가 항상 채워지지만, 방어적으로 None을 허용한다.)
            return exc.rcvd.code if exc.rcvd is not None else None
        return None


class TestWsMessageLimit(_WsLimitBase):
    async def test_exact_limit_is_accepted(self):
        """정확히 상한이면 통과 — positive control.

        "작은 payload가 통과한다"로 두면 상한을 8 KiB로 **잘못 낮춰도** 계속 green이다(codex).
        """
        async with websockets.connect(self.server.url, open_timeout=5) as ws:
            await ws.send("x" * WS_MAX_SIZE)
            self.assertEqual(await ws.recv(), f"text:{WS_MAX_SIZE}")

    async def test_one_byte_over_limit_closes_1009(self):
        """정확히 상한+1에서 끊긴다 — 경계를 잠근다."""
        self.assertEqual(await self._send_expect_close("x" * (WS_MAX_SIZE + 1)), 1009)

    async def test_oversize_binary_closes_1009(self):
        """앱 계약이 text JSON이어도 transport 통제는 binary에도 적용돼야 한다."""
        self.assertEqual(await self._send_expect_close(b"x" * (WS_MAX_SIZE + 1)), 1009)

    async def test_fragmented_message_sums_toward_limit(self):
        """분할 전송해도 **합산**이 상한을 넘으면 끊긴다 — message 상한이지 frame 상한이 아니다.

        각 조각은 상한 이하라, frame 단위로만 보는 구현이면 여기서 red.
        """
        half = WS_MAX_SIZE // 2 + 1
        fragments = [b"a" * half, b"b" * half]
        self.assertEqual(await self._send_expect_close(fragments), 1009)

    async def test_server_survives_oversize_and_serves_new_connection(self):
        """1009로 끊은 뒤 서버가 살아 있어야 한다 — 프로세스까지 죽는 회귀 방지."""
        self.assertEqual(await self._send_expect_close("x" * (WS_MAX_SIZE + 1)), 1009)
        async with websockets.connect(self.server.url, open_timeout=5) as ws:
            await ws.send("ok")
            self.assertEqual(await ws.recv(), "text:2")


if __name__ == "__main__":
    unittest.main()
