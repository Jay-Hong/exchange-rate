# REALTIME_V2_CLIENT_GUIDE — topic 구독 클라이언트 계약 (신규 iOS/Android 앱)

> **상태**: Proposed/Draft (2026-06-25, codex 019efdf0+019efe0b 검토 반영). 신규 topic-consuming 앱 출시용 **단일 핸드오프 계약**.
> 초기 OPEN 2건 모두 해소: usdt:krw REST bootstrap(§3, `/api/v2/topics/snapshot`) + USDT/KRX same-bucket ordering(§5, `rate_changed_at` 노출). 서버 측 계약 closed — 잔여는 client 구현 + live enable(별도 GO).
> 서버 코드 구현 완료(snapshot-on-subscribe + wire e2e), **prod에서는 flag-off dormant**
> (`TOPIC_DISPATCHER_ENABLED`/`FX_TOPIC_ENABLED` default false) — live 활성은 별도 GO.
> 이 문서가 topic 계약의 **authoritative source**. [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md)
> "Topic API" 섹션은 본 문서로 supersede(구현에 사용 금지).

## 0. 범위 (실제 구현 기준)

| topic | 의미 | 상태 |
| --- | --- | --- |
| `fx:usd-krw` | USD/KRW 은행(≤9, BANK_DISPLAY_ORDER) + Investing reference | ✅ 구현 |
| `fx:jpy-krw` | JPY/KRW 은행 + reference | ✅ 구현 |
| `fx:eur-krw` | EUR/KRW 은행 + reference | ✅ 구현 |
| `usdt:krw` | 테더 탭 (USDT 5거래소 + USD/KRW 은행[kb,hana] + reference + KRX 선물 optional) | ✅ 구현 |

**범위 밖 (topic publisher 미구현 — 구독해도 데이터 안 옴)**:
- **DXY / news / graph**: 독립 topic 없음. DXY는 legacy broadcast `data.indices.dxy`, news/graph는 REST.
- **KRX 미국달러선물**: 독립 `krx:*` topic 없음. `usdt:krw` payload 안 `data.usd_krw_futures` optional group으로만(`KRX_TOPIC_INCLUDE=true`일 때).
- 미지원 topic을 subscribe하면 registry에는 등록되나 **snapshot은 오지 않음**(조용히 skip).

## 1. 연결 + 구독 프로토콜

```text
URL:         wss://fxi.kr/ws            (legacy broadcast와 동일 endpoint 공유)
Subscribe:   {"type": "subscribe",   "topics": ["fx:usd-krw", "usdt:krw"]}
Unsubscribe: {"type": "unsubscribe", "topics": ["usdt:krw"]}
Keep-alive:  "ping" (raw text) → 서버 {"type": "pong"}
```

### 서버 → 클라이언트 메시지 종류 (⚠️ /ws는 legacy와 공유)

| `type` | 언제 | 처리 |
| --- | --- | --- |
| `"rates"` | **연결 직후 1건 자동** (legacy 초기 payload) + legacy broadcast cycle | **무시** (topic 클라이언트 대상 아님) |
| `"snapshot"` | subscribe 직후 + 값 변경 시 | **처리** — `payload["topic"]`으로 분기 |
| `"pong"` | `"ping"` 응답 | keep-alive |

> **🔴 필수**: 신규 앱은 **`type=="snapshot"` 이고 `topic` 필드가 있는 메시지만** topic payload로 decode한다.
> 연결 직후 오는 `type=="rates"` legacy payload(main.py가 모든 `/ws` 연결에 자동 전송)를 snapshot으로
> 파싱하면 첫 메시지에서 깨진다. `type`으로 먼저 분기할 것.

### 활성 조건 (서버 flag)

- `TOPIC_DISPATCHER_ENABLED=true` 필요(전 topic). **현 prod는 false → subscribe 무시·snapshot 없음**.
- FX topic(`fx:*`)은 추가로 `FX_TOPIC_ENABLED=true` 필요. `usdt:krw`는 `TOPIC_DISPATCHER_ENABLED`만(+ KRX 포함은 `KRX_TOPIC_INCLUDE`).
- live 활성 = 별도 운영 GO (출시 직전).

### ack / error / auth

- **subscribe 성공/실패 ack 없음**. invalid JSON / unknown type / invalid topics / FF-off / 미지원 topic 모두 **조용히 무시**(error 응답 없음). **snapshot 수신 자체가 성공 신호**.
- **현재 `/ws` topic 구독에 인증 없음**(Firebase 검증 X). 알림 API와 달리 공개 read-only 스트림.

## 2. Payload schema (version=1)

### 2.1 top-level (모든 topic 공통)

```jsonc
{
  "type": "snapshot",   // v1은 snapshot-only (delta/seq 없음 — §8)
  "version": 1,         // schema version. 비호환 변경 시 bump
  "topic": "fx:usd-krw",// 송신 topic (multi-구독 분기용, publisher 주입)
  "data": { /* topic별 그룹 (§2.3/§2.4) */ }
}
```

### 2.2 entry shape (모든 그룹 공통)

```jsonc
{
  "source": "kb",                            // string — 데이터 공급자
  "asset": "usd-krw",                        // string — 통화쌍/상품
  "rate": 1385.5,                            // float — KRW
  "timestamp": "2026-06-25T15:00:00+09:00"   // ISO8601 KST — ⚠️ source별 의미 상이 (§5)
}
```

- Entry 식별자 = **`(source, asset)` tuple**. 같은 source가 다른 asset 가능.
- 서버는 표시명/아이콘/색상/정렬 **미전송**. 단말이 `(source, asset)`로 자체 registry lookup. 새 source 추가 시 단말 registry 갱신.
- **`rate_changed_at`(optional, ISO8601 KST)**: `usdt_krw` 거래소 + `usd_krw_futures`(KRX) entry의 정밀 변경 시각(Redis-served 시). 이들 `timestamp`는 `seen_at`(5초 bucket)일 수 있어 merge ordering은 이 필드를 우선(§5). bank/investing/FX entry엔 없음(`timestamp`가 이미 정밀). client는 항상 `rate_changed_at ?? timestamp` 사용.

### 2.3 `fx:<asset>` data

```jsonc
"data": {
  "banks": [ {entry}, ... ],     // 은행 ≤9 (BANK_DISPLAY_ORDER, 미등록 source 자동 제외)
  "reference": {entry}           // Optional — source="investing". 없으면 key 누락
}
```

### 2.4 `usdt:krw` data

```jsonc
"data": {
  "usdt_krw": [ {entry + "rate_changed_at"}, ... ],  // USDT 5거래소 — entry에 rate_changed_at 추가(§5)
  "usd_krw_banks": [ {entry}, ... ],  // 은행 (kb, hana)
  "usd_krw_reference": {entry},       // Optional — source="investing", asset="usd-krw"
  "usd_krw_futures": {entry (+"rate_changed_at" when Redis-served)}  // Optional — source="krx", asset="usd-krw-futures" (KRX_TOPIC_INCLUDE 시만)
}
```

**snapshot 크기(레이아웃 참고)**: `fx:*` = 은행 ≤9 + reference 1. `usdt:krw` = 거래소 5 + 은행 2 + reference 1 + futures 1 = ≤9 entry. 작음.

## 3. Bootstrap (초기 상태 획득)

**PRIMARY (권장) = WS connect + subscribe → 즉시 snapshot**:
1. `wss://fxi.kr/ws` 연결 → (연결 직후 오는 `type="rates"` legacy payload는 무시, §1)
2. subscribe 메시지 전송
3. **서버가 해당 topic 현재 전체 snapshot 1건 즉시 푸시**(snapshot-on-subscribe, 구현됨) → 첫 화면 렌더
4. 이후 값 변경 시 추가 snapshot

→ 정상 경로 **REST bootstrap 불필요**. 빈 시간대(주말/조용한 통화)에도 구독 즉시 현재값 수신.

**REST bootstrap (WS 미연결/실패 시 권장 fallback)** — v2 endpoint:

```text
GET /api/v2/topics/snapshot?topic=<topic>     // topic ∈ {fx:usd-krw, fx:jpy-krw, fx:eur-krw, usdt:krw}
```

- 응답 = **WS snapshot과 동일 schema**(`{type:"snapshot", version:1, topic, data}` + `usdt_krw`/`usd_krw_futures`의 `rate_changed_at` + KRX optional). 같은 builder 공유 → client는 REST/WS 동일 merge 로직(`rate_changed_at ?? timestamp`).
- 권장 흐름: **REST bootstrap(즉시 렌더) → WS subscribe → snapshot/live merge**(REST 응답을 §5 merge로 흡수, WS snapshot이 자연 갱신).
- `Cache-Control: no-store`. 인증 없음(WS topic과 일관).
- 응답 코드: 200(payload) / 404 `topics_disabled`(TOPIC_DISPATCHER_ENABLED off=출시 전) / 404 `unknown_topic`(+supported_topics) / 404 `topic_unavailable`(지원 topic이나 현재 미제공, 예 FX_TOPIC_ENABLED off).
- legacy `/api/rates/{usdt-krw|usd-krw-futures}`는 여전히 410 Gone(use_topic) — 신규 앱은 위 v2 bootstrap 사용. FX legacy `/api/rates/{asset}`(legacy shape)도 v2 bootstrap으로 대체 권장.

> ✅ (구 OPEN — usdt:krw REST bootstrap 부재)는 본 endpoint로 **해소**(2026-06-25). 전 topic(fx:*+usdt:krw) 통일 bootstrap.

## 4. 수신 규칙 (snapshot 처리)

- 모든 topic 메시지는 `type="snapshot"` = **해당 topic 현재 전체 상태 dump**. delta/증분 없음(§8).
- 수신 시 **`(source, asset)` 기준 merge**(§5) — 같은 키만 갱신, 전체 교체 불필요.
- **Optional 그룹 누락 = 삭제 아님(v1 tombstone 없음)**:
  - *최초 부재*(키가 한 번도 안 온 경우): 미표시.
  - *중도 부재*(이전엔 왔는데 이번 메시지에 없음): **이전 값 유지**(v1엔 삭제 신호 없음).
  - ⚠️ 운영 함의: `KRX_TOPIC_INCLUDE`를 끄면(`usd_krw_futures` 미전송) **이미 연결된 앱에서 KRX가 즉시 사라지지 않고 마지막 값이 남는다**. 즉시 제거가 필요하면 별도 신호 필요(v1 미지원).

## 5. ⚠️ snapshot ↔ live race → merge 규칙 (필수 계약)

서버는 구독 snapshot과 직후 live publish의 **순서 역전**이 가능(per-message seq 없음). 클라이언트 merge 규칙:

> **각 `(source, asset)`에 대해 `merge_at = rate_changed_at ?? timestamp`를 비교해, `merge_at`이 더 최신인 entry만 반영. 더 오래된 `merge_at`으로 기존 값을 덮지 않는다(같으면 기존 유지 → 회귀 방지).**

- **FX(은행/investing)**: `timestamp`=실제 값 변경 시각(정밀). `rate_changed_at` 없음 → `merge_at = timestamp`. merge 정확.
- **USDT 5거래소(`usdt_krw` 그룹)**: `timestamp`=`seen_at`(5초 bucket alias)지만 **`rate_changed_at`(정밀 변경 시각)이 entry에 포함됨** → `merge_at = rate_changed_at`로 same-bucket 연속 변경도 정밀 ordering. (서버: Redis path `get_latest_usdt_rate_from_sync_job` + DB fallback `get_latest_source_rates_for_topic` 양쪽 노출.)
- **KRX(`usd_krw_futures`)**: Stage E tick writer(`KRX_REDIS_TICK_WRITE_ENABLED`, 운영 활성)가 USDT와 동일한 5-field schema를 써서 `timestamp`=`seen_at`(5초 bucket)일 수 있음 → **Redis-served entry에 `rate_changed_at`(정밀) 포함**(USDT와 대칭). DB fallback 시엔 `timestamp`가 정밀이라 `rate_changed_at` 생략 → `merge_at = rate_changed_at ?? timestamp`로 일관 처리.

> 한계: `rate_changed_at`은 ms 해상도라 5초 bucket 문제는 해소되나, 진정한 total order(동일 ms 동시 변경)는 per-message `seq`가 필요(v1 미지원, 실질 영향 없음).

## 6. reconnect / resync + 클라이언트 정책

- 끊김 → 재연결 → **재구독 필수**(서버는 connection별 registry, 끊기면 구독 소실).
- 재구독 시 §3대로 **현재 snapshot 다시 수신**(resync). 별도 resync 프로토콜 불요. 서버측 snapshot 캐시/증분 resync는 v1 미구현.
- **권고 클라이언트 정책**: 재연결 exponential backoff(예 1s→2s→…→30s cap), keep-alive `"ping"` 주기(예 30s) — 서버는 raw `"ping"`에 `{"type":"pong"}` 응답만.

## 7. versioning / forward-compat

- `version=1` 고정. 비호환 변경 시에만 bump(단말 release 동기화 후 lock-in).
- 클라이언트는 **모르는 필드/그룹 무시**(forward-compat). 서버가 그룹/필드 추가해도 안 깨지게.
- `type`이 미래값(`delta` 등)이면 v1 단말은 무시 가능.

## 8. v1 범위 밖 (단말이 기대하면 안 되는 것)

REALTIME_ARCHITECTURE_PLAN.md §5에 설계로 적혀 있으나 **현재 서버 미구현** — 계약 제외:
- **hello handshake**(protocol_version/supported_topics 협상): 없음. 단말은 고정 topic 목록(§0) 사용.
- **delta / seq**: 없음. 항상 full snapshot. dedup/순서는 §5 merge(`rate_changed_at ?? timestamp`)로 단말이 처리.
- **서버측 stale 신호**: payload에 stale 필드 없음(timestamp만). 휴장/주말 stale UI는 단말 정책(권고: 마지막 값 유지).

## 9. 참조

- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md) §5 (서비스 계약 SoT)
- [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md) (Topic API 섹션 — 본 문서로 supersede)
- [DECISIONS.md](DECISIONS.md) ADR-028 (Topic-only Tether/KRX + dual-emit FX)
- 서버 구현: `app/fx_topic_payload.py`, `app/usdt_topic_payload.py`, `app/topic_dispatcher.py`, `app/topic_initial_snapshot.py`(snapshot-on-subscribe)
