# USDT Exchange WebSocket Guide

> Status: implementation guide
> Verified: 2026-04-28
> Scope: 국내 거래소 5종의 `USDT/KRW` 현재가(ticker)를 백엔드에서 WebSocket으로 수집하기 위한 가이드
> Related: [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md), [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md), [USDT_TAB_PROPOSAL.md](USDT_TAB_PROPOSAL.md)

## 1. 결론

5개 거래소 모두 public WebSocket ticker로 USDT/KRW 가격을 받을 수 있다.

| source | endpoint | 인증 | USDT 단독 ticker 구독 | 가격 필드 |
| --- | --- | --- | --- | --- |
| `upbit` | `wss://api.upbit.com/websocket/v1` | 불필요 | 가능: `KRW-USDT` | `trade_price` |
| `bithumb` | `wss://ws-api.bithumb.com/websocket/v1` | 불필요 | 가능: `KRW-USDT` | `trade_price` |
| `coinone` | `wss://stream.coinone.co.kr` | 불필요 | 가능: `KRW` + `USDT` topic | `data.last` |
| `korbit` | `wss://ws-api.korbit.co.kr/v2/public` | 불필요 | 가능: `usdt_krw` | `data.close` |
| `gopax` | `wss://wsapi.gopax.co.kr` | 불필요 | ticker는 전체 구독만 가능 | `last` |

권장 구조:

- 백엔드가 거래소 5종 WebSocket을 long-running collector로 유지한다.
- collector는 거래소별 메시지를 `source + asset + rate + exchange_ts + received_at` 표준 모델로 정규화한다.
- 앱은 거래소별 WebSocket에 직접 붙지 않고 FXi WebSocket만 구독한다.
- 고팍스는 전체 ticker를 받아 서버에서 `USDT-KRW`만 필터링한다.
- 기존 REST polling은 collector 장애 시 fallback으로 유지한다.

## 2. 표준 정규화 모델

collector 내부에서는 거래소별 원본 메시지를 아래 모델로 변환한다.

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class UsdtTick:
    source: str              # upbit | bithumb | coinone | korbit | gopax
    asset: str               # usdt-krw
    rate: float              # KRW per USDT
    exchange_ts_ms: int | None
    received_at: datetime    # server receive time, UTC
    raw: dict[str, Any]
```

정규화 규칙:

- `asset`은 항상 `usdt-krw`로 고정한다.
- `rate <= 0`이면 폐기한다.
- 거래소 timestamp가 없거나 신뢰하기 어려운 경우에도 `received_at`은 반드시 기록한다.
- broadcast SLO는 `received_at` 기준으로 잡는다.
- DB 저장 시점은 별도 정책으로 제한한다. 모든 tick을 `source_rates`에 INSERT하지 않는다.

## 3. Upbit

공식 문서:

- [WebSocket 사용 및 에러 안내](https://docs.upbit.com/kr/reference/websocket-guide)
- [현재가 Ticker](https://docs.upbit.com/kr/reference/websocket-ticker)

### 연결

```text
wss://api.upbit.com/websocket/v1
```

public quotation endpoint는 인증이 필요 없다. private 자산/주문 endpoint만 인증이 필요하다.

### 구독

```json
[
  {"ticket": "fxi-usdt-upbit"},
  {"type": "ticker", "codes": ["KRW-USDT"]},
  {"format": "DEFAULT"}
]
```

### 응답 파싱

```json
{
  "type": "ticker",
  "code": "KRW-USDT",
  "trade_price": 1486,
  "trade_timestamp": 1777370239843,
  "timestamp": 1777370240080,
  "stream_type": "REALTIME"
}
```

파싱:

```python
rate = float(message["trade_price"])
exchange_ts_ms = int(message.get("trade_timestamp") or message.get("timestamp"))
```

### 연결 유지

문서상 idle timeout이 있으며, client PING frame 전송을 권장한다. 구현에서는 30초 주기로 WebSocket ping frame을 보내고, 수신 메시지가 일정 시간 없으면 재연결한다.

## 4. Bithumb

공식 문서:

- [기본 정보](https://apidocs.bithumb.com/reference/%EA%B8%B0%EB%B3%B8-%EC%A0%95%EB%B3%B4.md)
- [현재가 Ticker](https://apidocs.bithumb.com/reference/%ED%98%84%EC%9E%AC%EA%B0%80-ticker)

### 연결

```text
wss://ws-api.bithumb.com/websocket/v1
```

public endpoint는 인증이 필요 없다. 문서 기준 WebSocket 연결 요청은 IP 기준 초당 10회 제한이다.

### 구독

빗썸 v1 WebSocket ticker는 업비트 호환 형태로 동작한다.

```json
[
  {"ticket": "fxi-usdt-bithumb"},
  {"type": "ticker", "codes": ["KRW-USDT"]},
  {"format": "DEFAULT"}
]
```

### 응답 파싱

2026-04-28 실연결에서 확인한 형태:

```json
{
  "type": "ticker",
  "code": "KRW-USDT",
  "trade_price": 1486,
  "trade_timestamp": 1777370239843,
  "timestamp": 1777370240080,
  "stream_type": "REALTIME"
}
```

파싱:

```python
rate = float(message["trade_price"])
exchange_ts_ms = int(message.get("trade_timestamp") or message.get("timestamp"))
```

### 연결 유지

공식 기본 정보 문서에는 public endpoint와 rate limit은 명시되어 있지만, ticker heartbeat 세부 규칙은 별도 확인이 필요하다. 구현에서는 업비트와 동일하게 30초 주기 WebSocket ping frame + 수신 timeout 기반 재연결로 시작한다.

## 5. Coinone

공식 문서:

- [Public 웹소켓](https://docs.coinone.co.kr/reference/public-websocket-1)
- [티커 응답 TICKER](https://docs.coinone.co.kr/reference/public-websocket-ticker)

### 연결

```text
wss://stream.coinone.co.kr
```

public WebSocket은 인증 없이 수신 가능하다. IP당 최대 20개 연결 제한이 있고, `request_type`, `channel`, `format` 값은 대문자로 입력해야 한다.

### 구독

```json
{
  "request_type": "SUBSCRIBE",
  "channel": "TICKER",
  "topic": {
    "quote_currency": "KRW",
    "target_currency": "USDT"
  }
}
```

단축 필드가 필요하면 `"format": "SHORT"`를 추가할 수 있다. 초기 구현은 가독성을 위해 DEFAULT 포맷을 권장한다.

### 응답 파싱

DEFAULT 포맷:

```json
{
  "response_type": "DATA",
  "channel": "TICKER",
  "data": {
    "quote_currency": "KRW",
    "target_currency": "USDT",
    "timestamp": 1777370268046,
    "last": "1485",
    "ask_best_price": "1486",
    "bid_best_price": "1485"
  }
}
```

파싱:

```python
data = message["data"]
if data.get("quote_currency") == "KRW" and data.get("target_currency") == "USDT":
    rate = float(data["last"])
    exchange_ts_ms = int(data["timestamp"])
```

SHORT 포맷을 쓸 경우:

```python
data = message["d"]
rate = float(data["la"])
exchange_ts_ms = int(data["t"])
```

### 연결 유지

문서상 마지막 PING 요청 이후 30분이 지나면 유휴 연결로 판단한다. 구현에서는 5분마다 아래 메시지를 보내는 방식으로 충분하다.

```json
{"request_type": "PING"}
```

운영에서는 모든 거래소 collector에 공통 watchdog을 두고, `last_received_at`이 오래되면 재연결한다.

## 6. Korbit

공식 문서:

- [Korbit API Docs](https://docs.korbit.co.kr/index_en.html)

### 연결

```text
wss://ws-api.korbit.co.kr/v2/public
```

public ticker는 인증 없이 구독한다.

### 구독

```json
[
  {
    "requestId": 1,
    "method": "subscribe",
    "type": "ticker",
    "symbols": ["usdt_krw"]
  }
]
```

`requestId`는 선택이지만, 성공/실패 확인을 명확히 하려면 넣는 편이 좋다. 코빗 문서는 성공 응답을 확인하려면 `requestId`를 설정하라고 설명한다.

### 응답 파싱

```json
{
  "type": "ticker",
  "timestamp": 1700000027754,
  "symbol": "usdt_krw",
  "snapshot": true,
  "data": {
    "close": "1490",
    "bestAskPrice": "1491",
    "bestBidPrice": "1489",
    "lastTradedAt": 1700000010022
  }
}
```

파싱:

```python
if message.get("type") == "ticker" and message.get("symbol") == "usdt_krw":
    data = message["data"]
    rate = float(data["close"])
    exchange_ts_ms = int(data.get("lastTradedAt") or message["timestamp"])
```

### 연결 유지

공식 문서에서 ticker heartbeat 세부 규칙은 명확히 드러나지 않는다. 구현에서는 30초 주기 WebSocket ping frame + 수신 timeout 기반 재연결로 시작한다.

## 7. GOPAX

공식 문서:

- [Gopax WebSocket API](https://gopax.github.io/wsapi/)

### 연결

```text
wss://wsapi.gopax.co.kr
```

문서 상단에는 인증 예제가 있지만, 공개 API 섹션은 인증 없이 사용할 수 있다고 명시한다. `SubscribeToTickers`는 공개 API다.

### 구독

```json
{"n": "SubscribeToTickers", "o": {}}
```

주의: 고팍스 ticker는 특정 거래쌍 하나만 지정하지 못한다. 전체 ticker를 구독한 뒤 서버에서 `USDT-KRW`만 필터링한다.

### 응답 파싱

초기 응답은 전체 ticker 목록이다.

```json
{
  "n": "SubscribeToTickers",
  "o": {
    "data": [
      {
        "tradingPairName": "USDT-KRW",
        "last": 1490,
        "lastTraded": 1777112971993
      }
    ]
  }
}
```

delta는 1초 단위로 묶여 온다.

```json
{
  "i": -1,
  "n": "TickerEvent",
  "o": {
    "USDT-KRW": {
      "tradingPairName": "USDT-KRW",
      "last": 1490,
      "lastTraded": 1777112971993
    }
  }
}
```

파싱:

```python
def parse_gopax(message: dict) -> tuple[float, int] | None:
    if message.get("n") == "SubscribeToTickers":
        for item in message.get("o", {}).get("data", []):
            if item.get("tradingPairName") == "USDT-KRW":
                return float(item["last"]), int(item.get("lastTraded", 0) or 0)

    if message.get("n") == "TickerEvent":
        item = message.get("o", {}).get("USDT-KRW")
        if item:
            return float(item["last"]), int(item.get("lastTraded", 0) or 0)

    return None
```

### 연결 유지

고팍스는 Primus 스타일 ping 문자열을 보낸다. 문자열 전체가 JSON string 형태로 들어올 수 있으므로 prefix 처리에 주의한다.

수신:

```text
"primus::ping::1777112968768"
```

응답:

```text
"primus::pong::1777112968768"
```

Python 처리 예:

```python
if raw.startswith('"primus::ping::'):
    await ws.send(raw.replace("::ping::", "::pong::"))
    continue
```

문서상 서버는 30초마다 ping을 보내며, 30초 안에 pong을 받지 못하면 연결을 끊을 수 있다.

## 8. Collector 인터페이스 권장안

거래소별 구현은 다르지만 외부로 내보내는 인터페이스는 같게 둔다.

```python
class ExchangeWebSocketCollector:
    source: str

    async def run(self) -> None:
        """Connect, subscribe, parse, and reconnect forever."""

    async def stop(self) -> None:
        """Gracefully close the WebSocket."""
```

공통 run loop:

```python
async def run_forever(collector):
    delay = 1
    while not collector.stopped:
        try:
            await collector.connect_and_consume()
            delay = 1
        except Exception:
            logger.exception("exchange websocket collector failed", extra={"source": collector.source})
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
```

tick 처리:

```python
async def handle_tick(tick: UsdtTick):
    await redis.set(
        f"tick:usdt:{tick.source}",
        json.dumps({
            "source": tick.source,
            "asset": tick.asset,
            "rate": tick.rate,
            "exchange_ts_ms": tick.exchange_ts_ms,
            "received_at": tick.received_at.isoformat(),
        }),
        ex=10,
    )
    await usdt_tick_queue.put(tick)
```

## 9. Broadcast / Alert / DB 분리

거래소 WebSocket 도입 시 수신 tick을 그대로 DB와 WebSocket broadcast에 묶지 않는다.

권장 분리:

```text
exchange WS tick
  └─ normalize UsdtTick
      ├─ Redis latest state update
      ├─ Broadcast router signal (debounced, e.g. 1s)
      ├─ Alert evaluator (all valid ticks)
      └─ DB writer (policy-based sample / changed / 1s latest)
```

정책:

- Broadcast: 유저 화면용. 1초 debounce 또는 event-driven.
- Alert: 조건 평가용. 모든 valid tick 평가를 원칙으로 하되, 동일 가격 반복은 source별 debounce 가능.
- DB: 그래프/히스토리용. 모든 tick INSERT 금지. 최소 `changed` 또는 `1초 last` 정책을 별도로 둔다.

## 10. REST Fallback

기존 REST fetcher는 제거하지 않고 fallback으로 유지한다.

| source | REST fallback |
| --- | --- |
| `upbit` | `GET https://api.upbit.com/v1/ticker?markets=KRW-USDT` |
| `bithumb` | `GET https://api.bithumb.com/v1/ticker?markets=KRW-USDT` |
| `coinone` | `GET https://api.coinone.co.kr/public/v2/ticker_utc_new/KRW/USDT` |
| `korbit` | `GET https://api.korbit.co.kr/v2/tickers?symbol=usdt_krw` |
| `gopax` | `GET https://api.gopax.co.kr/trading-pairs/USDT-KRW/ticker` |

fallback 트리거 예:

- WebSocket 연결 실패가 3회 이상 연속 발생
- 마지막 valid tick 수신 후 10초 이상 경과
- parse error가 일정 횟수 이상 발생
- 거래소가 maintenance/error control message를 반환

fallback은 source별로 독립 동작해야 한다. 한 거래소 장애가 나머지 4개 수집을 막으면 안 된다.

## 11. 로컬 Smoke Test

Node.js가 있으면 `wscat`으로 빠르게 확인할 수 있다.

### Upbit

```bash
npx -y wscat -c wss://api.upbit.com/websocket/v1
```

```json
[{"ticket":"fxi-test"},{"type":"ticker","codes":["KRW-USDT"]},{"format":"DEFAULT"}]
```

### Bithumb

```bash
npx -y wscat -c wss://ws-api.bithumb.com/websocket/v1
```

```json
[{"ticket":"fxi-test"},{"type":"ticker","codes":["KRW-USDT"]},{"format":"DEFAULT"}]
```

### Coinone

```bash
npx -y wscat -c wss://stream.coinone.co.kr
```

```json
{"request_type":"SUBSCRIBE","channel":"TICKER","topic":{"quote_currency":"KRW","target_currency":"USDT"}}
```

### Korbit

```bash
npx -y wscat -c wss://ws-api.korbit.co.kr/v2/public
```

```json
[{"requestId":1,"method":"subscribe","type":"ticker","symbols":["usdt_krw"]}]
```

### GOPAX

```bash
npx -y wscat -c wss://wsapi.gopax.co.kr
```

```json
{"n":"SubscribeToTickers","o":{}}
```

`TickerEvent`의 `o.USDT-KRW.last`를 확인한다.

## 12. 구현 체크리스트

- [ ] `app/realtime/` 또는 `app/collectors/` 아래 거래소별 collector 모듈 추가
- [ ] 공통 `UsdtTick` 모델 추가
- [ ] source별 parser unit test 작성
- [ ] source별 smoke/integration test 스크립트 추가
- [ ] Redis latest state key 설계 적용: `tick:usdt:{source}`
- [ ] stale 감지 및 REST fallback 연결
- [ ] alert evaluator를 REST crawler 흐름에서 tick 흐름으로 분리
- [ ] broadcast router를 `usdt:krw` 토픽 기반으로 분리
- [ ] DB writer 정책 결정: changed only vs 1초 last vs OHLC bucket
- [ ] 24시간 연결 안정성 측정 후 운영 파라미터 확정

## 13. 주의사항

- 거래소 API는 endpoint와 payload가 바뀔 수 있다. 구현 직전과 배포 직전에 공식 문서를 다시 확인한다.
- 고팍스는 전체 ticker를 수신하므로 앱 단말 직접 연결에 부적합하다. 서버에서 필터링한다.
- 앱 알림 기준은 서버 수신 tick이어야 한다. 단말 직접 WebSocket 값으로 알림을 판단하지 않는다.
- `received_at`과 거래소 timestamp를 분리해서 저장한다. 운영 SLO와 stale 판단은 `received_at` 기준이 더 안전하다.
- 단일 collector 장애가 전체 realtime pipeline을 멈추지 않도록 source별 task와 fallback을 격리한다.
