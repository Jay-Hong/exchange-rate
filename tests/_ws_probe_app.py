"""WS 메시지 상한 하니스용 **최소 ASGI 앱** (ADR-039 §8.1 harness 선행 (4)).

실앱(`app.main:app`)을 subprocess로 띄우지 않는 이유: 검증 대상은 **메시지 상한 = uvicorn의 책임**
이지 앱 로직이 아니다. 실앱은 import 시 테이블 초기화까지 하고 `/ws` 연결 직후 Redis·DB 경로를
타는데, `tests/conftest.py`의 sys.modules stub과 sqlite 강제는 **pytest 프로세스에만** 적용돼
subprocess에는 전달되지 않는다 → 불필요한 실패 표면만 늘어난다.

"우리 배포가 실제로 그 플래그를 준다"는 연결은 `test_ws_frame_limit.py`의 Dockerfile trip-wire가
담당하고, 하니스는 **그 CMD 토큰을 그대로 재사용**해 플래그를 따로 하드코딩하지 않는다.
"""


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
        return

    if scope["type"] != "websocket":
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})
        return

    await send({"type": "websocket.accept"})
    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            return
        if message["type"] != "websocket.receive":
            continue
        text = message.get("text")
        if text is None:
            payload = message.get("bytes") or b""
            await send({"type": "websocket.send", "text": f"bytes:{len(payload)}"})
        else:
            # 상한은 **바이트** 기준이라 UTF-8 길이를 돌려준다 (테스트가 ASCII만 보내므로 동일).
            await send({"type": "websocket.send", "text": f"text:{len(text.encode())}"})
