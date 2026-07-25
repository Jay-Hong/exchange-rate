# REALTIME_V2_CLIENT_GUIDE — topic 구독 클라이언트 계약 (신규 iOS/Android 앱)

> **상태**: Proposed/Draft (2026-06-25, codex 019efdf0+019efe0b 검토 반영). 신규 topic-consuming 앱 출시용 **단일 핸드오프 계약**.
> 초기 OPEN 2건 모두 해소: usdt:krw REST bootstrap(§3, `/api/v2/topics/snapshot`) + USDT/KRX same-bucket ordering(§5, `rate_changed_at` 노출). 서버 측 계약 closed — 잔여는 client 구현 + live enable(별도 GO).
> 서버 코드 구현 완료(snapshot-on-subscribe + wire e2e). ⚠️ **prod 현재 OFF** — 구 "prod LIVE"(2026-06-27
> `TOPIC_DISPATCHER_ENABLED`/`FX_TOPIC_ENABLED` ON)는 2026-07-22 route auth 감사에서 무인증 누수 완화로
> `TOPIC_DISPATCHER_ENABLED=false`로 되돌렸다(2026-07-25 재확인: `topics/snapshot` → 404 `topics_disabled`).
> 재활성화 선행 3조건(§1): ①**E3**(REST twin 인증 게이트, 2026-07-25 land) ②WS 인증(1C)
> ③**클라 bootstrap 3종의 인증 transport 이관** — 그 뒤 별도 운영 GO.
> KRX는 2026-07-08부터 독립 topic
> `krx:usd-krw-futures`(ADR-038 D2 — 구 `KRX_TOPIC_INCLUDE` env 제거). 잔여 = **client release gate**
> (iOS `RealtimeV2Config` build-config gate `TOPIC_V2_RELEASE_ON`; 절차는 iOS repo `TOPIC_V2_RELEASE_RUNBOOK.md`).
> 이 문서가 topic 계약의 **authoritative source**. [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md)
> "Topic API" 섹션은 본 문서로 supersede(구현에 사용 금지).

## 0. 범위 (실제 구현 기준)

| topic | 의미 | 상태 |
| --- | --- | --- |
| `fx:usd-krw` | USD/KRW 은행(≤8, **Citi 제외** — `FX_TOPIC_BANK_ORDER`) + Investing reference | ✅ 구현 |
| `fx:jpy-krw` | JPY/KRW 은행(≤8, Citi 제외) + reference | ✅ 구현 |
| `fx:eur-krw` | EUR/KRW 은행(≤8, Citi 제외) + reference | ✅ 구현 |
| `usdt:krw` | 테더 탭 (USDT 5거래소 + USD/KRW 은행[kb,hana] + reference) — **KRX 선물 미포함**(ADR-038 D2) | ✅ 구현 |
| `krx:usd-krw-futures` | KRX 미국달러선물 단독 (ADR-038 D2 독립 topic — `KRX_CLIENT_DISTRIBUTION_EFFECTIVE`[G2∧G3] on일 때만 발행/snapshot) | ✅ 구현 (2026-07-08) |

**범위 밖 (topic publisher 미구현 — 구독해도 데이터 안 옴)**:
- **DXY / news / graph**: 독립 topic 없음. DXY는 legacy broadcast `data.indices.dxy`, news/graph는 REST.
- 미지원 topic을 subscribe하면 registry에는 등록되나 **snapshot은 오지 않음**(조용히 skip).
  `krx:usd-krw-futures`도 게이트 off(`KRX_CLIENT_DISTRIBUTION_EFFECTIVE=false`)면 동일하게 조용히 skip.

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

- `TOPIC_DISPATCHER_ENABLED=true` 필요(전 topic). ⚠️ **현 prod는 OFF** — 2026-07-22 route auth 감사에서
  무인증 누수 완화로 되돌렸다(2026-07-25 재확인: `topics/snapshot` → 404 `topics_disabled`).
  구 "현 prod는 ON(2026-06-27 확인)"은 폐기. **재활성화 선행 조건**: ①서버 REST twin 인증 게이트(E3, 2026-07-25 land)
  ②WS 인증(1C) ③**클라 bootstrap 3종의 인증 transport 이관** — ③ 없이 켜면 클라 cold-start bootstrap이 401로 조용히 사라진다.
- FX topic(`fx:*`)은 추가로 `FX_TOPIC_ENABLED=true` 필요. `usdt:krw`는 `TOPIC_DISPATCHER_ENABLED`만. `krx:usd-krw-futures`는 추가로 `KRX_CLIENT_DISTRIBUTION_EFFECTIVE`(=`KRX_FUTURES_ENABLED`∧`KRX_CLIENT_DISTRIBUTION_ENABLED`, ADR-038 G2·G3) 필요.
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
- **`rate_changed_at`(optional, ISO8601 KST)**: `usdt_krw` 거래소 + `krx:usd-krw-futures` topic entry의 정밀 변경 시각(Redis-served 시). 이들 `timestamp`는 `seen_at`(5초 bucket)일 수 있어 merge ordering은 이 필드를 우선(§5). bank/investing/FX entry엔 없음(`timestamp`가 이미 정밀). client는 항상 `rate_changed_at ?? timestamp` 사용.

### 2.3 `fx:<asset>` data

```jsonc
"data": {
  "banks": [ {entry}, ... ],     // 은행 ≤8 (FX_TOPIC_BANK_ORDER = Citi 제외, 미등록 source 자동 제외)
  "reference": {entry}           // Optional — source="investing". 없으면 key 누락
}
```

### 2.4 `usdt:krw` data

```jsonc
"data": {
  "usdt_krw": [ {entry + "rate_changed_at"}, ... ],  // USDT 5거래소 — entry에 rate_changed_at 추가(§5)
  "usd_krw_banks": [ {entry}, ... ],  // 은행 (kb, hana)
  "usd_krw_reference": {entry}        // Optional — source="investing", asset="usd-krw"
}
```

> ADR-038 D2 (2026-07-08): 구 `usd_krw_futures` optional group은 **제거** — KRX는 §2.5 독립 topic.

### 2.5 `krx:usd-krw-futures` data (ADR-038 D2, 2026-07-08)

```jsonc
"data": {
  "usd_krw_futures": {entry (+"rate_changed_at" when Redis-served)}  // source="krx", asset="usd-krw-futures"
}
```

- entry shape는 구 usdt:krw group과 **동일**(TopicSourceEntry 하위호환) — group 키 이름도 유지.
- 발행/snapshot 조건: `KRX_CLIENT_DISTRIBUTION_EFFECTIVE`(G2∧G3) on. off면 발행 중단 + REST 404 + WS snapshot skip.
- per-user 노출 — **경로별로 다르다**:
  - **REST** `/api/v2/topics/snapshot`: **서버가 강제**(2026-07-25~, ADR-039 §8.1 E3). 비-entitled에겐 404 unknown_topic.
  - **WS**: 아직 무인증이라 **클라 `krx_visible` gate 담당**(GET /api/entitlements, ADR-038 Decision 3).
    1C(WS 인증) land 시 서버 강제로 전환 예정 — 그때까지 클라 gate를 제거하면 안 된다.

**snapshot 크기(레이아웃 참고)**: `fx:*` = 은행 ≤8(Citi 제외) + reference 1. `usdt:krw` = 거래소 5 + 은행 2 + reference 1 = ≤8 entry. `krx:*` = 1 entry. 작음.

## 3. Bootstrap (초기 상태 획득)

**PRIMARY (권장) = WS connect + subscribe → 즉시 snapshot**:
1. `wss://fxi.kr/ws` 연결 → (연결 직후 오는 `type="rates"` legacy payload는 무시, §1)
2. subscribe 메시지 전송
3. **서버가 해당 topic 현재 전체 snapshot 1건 즉시 푸시**(snapshot-on-subscribe, 구현됨) → 첫 화면 렌더
4. 이후 값 변경 시 추가 snapshot

→ 정상 경로 **REST bootstrap 불필요**. 빈 시간대(주말/조용한 통화)에도 구독 즉시 현재값 수신.

**REST bootstrap (WS 미연결/실패 시 권장 fallback)** — v2 endpoint:

```text
GET /api/v2/topics/snapshot?topic=<topic>     // topic ∈ {fx:usd-krw, fx:jpy-krw, fx:eur-krw, usdt:krw} (+ krx:usd-krw-futures — G2∧G3 on **이고 그 사용자에게 entitlement가 있을 때만**, 아니면 404 unknown_topic)
Authorization: Bearer <Firebase ID token>     // 필수 (2026-07-25~)
```

- 응답 = **WS snapshot과 동일 schema**(`{type:"snapshot", version:1, topic, data}` + usdt/krx tick entry의 `rate_changed_at`). 같은 builder 공유 → client는 REST/WS 동일 merge 로직(`rate_changed_at ?? timestamp`).
- 권장 흐름: **REST bootstrap(즉시 렌더) → WS subscribe → snapshot/live merge**(REST 응답을 §5 merge로 흡수, WS snapshot이 자연 갱신).
- `Cache-Control: no-store` — 이 endpoint가 **직접 만드는 응답 전부**(200 · 404 3종 · DB 순단 503).
  401/403/PENDING 503은 `HTTPException` 공통 예외 경로라 헤더가 없다 — 세 코드 모두 휴리스틱 캐시
  대상이 아니라(RFC 9110 §15.1 목록에 부재) 준수 캐시는 저장 자체를 못 한다.
- ⚠️ **인증·권한 필수 (2026-07-25 변경 — ADR-039 §8.1 E3)**: 구 "인증 없음"은 **폐기**.
  이 endpoint는 WS topic의 REST twin이라 같은 권한 매트릭스를 따른다 — **Firebase 인증 + premium**,
  KRX는 **+ entitlement**. 클라는 토큰 없이 호출하면 안 된다(1B 인증 transport 경유).
  entitlement 없는 사용자에게 krx topic은 `unknown_topic` 404이고 `supported_topics` 에코에도
  나타나지 않는다 — **미지원 topic과 구분 불가**(KRX 존재 비노출 계약).
- 응답 코드: 200(payload) / **401**(토큰 없음·무효) / **403**(premium 아님) /
  **503** — **세 원인, 전부 재시도 대상**(인증 실패로 처리하지 말 것). 구분은 **본문**으로 한다:

  | 원인 | 본문 | `Retry-After` | 재시도 감각 |
  |---|---|---|---|
  | 구독 판정 PENDING | `{"detail": "Subscription status pending..."}` | **있음**(5) | 초 단위 |
  | 인증 인프라 장애(Firebase 미초기화/네트워크) | `{"detail": "..."}` | 없음 | 클라 backoff |
  | **DB 순단**(entitlement 조회·snapshot 빌드) | `{"error": "temporarily_unavailable"}` | 없음 | 클라 backoff, **분 단위**(RDS failover) |

  ⚠️ 인증 인프라 장애는 **토큰이 없어도** 401보다 먼저 나올 수 있다(초기화 확인이 헤더 검사보다 앞).
  ⚠️ `temporarily_unavailable`은 WS `subscription_error`의 같은 이름과 **같은 의미**(판정 불가)지만,
  REST 쪽은 `Retry-After`를 주지 않는다 — 5초 재시도를 지시하면 DB failover 동안 storm이 된다.
  **헤더가 없다는 사실만으로는 storm이 막히지 않으므로 클라 재시도를 계약으로 고정한다.**

  ⚠️ 전제: 이 endpoint는 **best-effort 가속기**지 데이터 경로의 정본이 아니다(정본은 §3 PRIMARY의
  WS snapshot). 그래서 재시도는 **짧고 유한**해야 한다 — 긴 backoff는 의미가 없다. 수 초 뒤엔 WS
  snapshot이 이미 도착해 있을 가능성이 높고, 그때 도착한 REST 응답은 §5 merge에서 구값으로 버려진다.

  | 항목 | 계약 |
  |---|---|
  | 총 시도 | **3회 이내**(첫 시도 포함). 소진하면 **포기하고 WS snapshot에 맡긴다** — 무기한 재시도 금지 |
  | backoff | 0.5s → 1.5s(±20% jitter). 마지막 값 이후는 재사용 |
  | 조기 종료 | 그 topic의 snapshot을 이미 받았으면(WS/다른 경로) 남은 시도 취소 |
  | 취소 | 앱 background 전환 · 연결 generation 변경 · 게이트/권한 변경 시 즉시 |
  | 동시성 | **topic별 독립 타이머** — cold-start 4~5건이 같은 시각에 재시도하지 않도록 |
  | 재시도 **대상 아님** | 401 · 403 · 404 3종(상태가 바뀌어야 해소된다) |

  ℹ️ 참조 구현: iOS `ExchangeRateViewModel.bootstrapKrxWithRetry`(`krxBootstrapMaxAttempts = 3`,
  `krxBootstrapBackoffsSeconds = [0.5, 1.5]`, WS-wins revision 체크 + 취소 4조건).
  tether/fx bootstrap은 현재 **재시도 없음**(1회 시도 후 WS에 위임) — 그것도 이 계약을 만족한다.

  **404 세 종류** — 전부 재시도 무의미(상태가 바뀌어야 해소된다):
  `topics_disabled`(`TOPIC_DISPATCHER_ENABLED` off = 출시 전) /
  `unknown_topic`(+`supported_topics`. **미지원 topic과 미인가 KRX가 동일 응답**) /
  `topic_unavailable`(지원 topic이나 현재 미제공, 예 `FX_TOPIC_ENABLED` off).

  **순서 = dormant flag → 인증 → premium → topic 판정**이므로, flag off면 미인증이어도 404이고,
  미인증이면 unknown topic이어도 401이다(미인증자는 topic 목록을 열거할 수 없다).
- legacy `/api/rates/{usdt-krw|usd-krw-futures}`는 여전히 410 Gone(use_topic) — 신규 앱은 위 v2 bootstrap 사용. FX legacy `/api/rates/{asset}`(legacy shape)도 v2 bootstrap으로 대체 권장.

> ✅ (구 OPEN — usdt:krw REST bootstrap 부재)는 본 endpoint로 **해소**(2026-06-25). 전 topic(fx:*+usdt:krw) 통일 bootstrap.

## 4. 수신 규칙 (snapshot 처리)

- 모든 topic 메시지는 `type="snapshot"` = **해당 topic 현재 전체 상태 dump**. delta/증분 없음(§8).
- 수신 시 **`(source, asset)` 기준 merge**(§5) — 같은 키만 갱신, 전체 교체 불필요.
- **Optional 그룹 누락 = 삭제 아님(v1 tombstone 없음)**:
  - *최초 부재*(키가 한 번도 안 온 경우): 미표시.
  - *중도 부재*(이전엔 왔는데 이번 메시지에 없음): **이전 값 유지**(v1엔 삭제 신호 없음).
  - ⚠️ 운영 함의: `krx:usd-krw-futures` 게이트를 끄면(발행 중단) **이미 연결된 앱에서 KRX가 즉시 사라지지 않고 마지막 값이 남는다**(v1 tombstone 없음). 클라 측 즉시 제거는 `krx_visible`(GET /api/entitlements) refresh가 담당 — 서버 push 신호는 v1 미지원.

## 5. ⚠️ snapshot ↔ live race → merge 규칙 (필수 계약)

서버는 구독 snapshot과 직후 live publish의 **순서 역전**이 가능(per-message seq 없음). 클라이언트 merge 규칙:

> **각 `(source, asset)`에 대해 `merge_at = rate_changed_at ?? timestamp`를 비교해, `merge_at`이 더 최신인 entry만 반영. 더 오래된 `merge_at`으로 기존 값을 덮지 않는다(같으면 기존 유지 → 회귀 방지).**

- **FX(은행/investing)**: `timestamp`=실제 값 변경 시각(정밀). `rate_changed_at` 없음 → `merge_at = timestamp`. merge 정확.
- **USDT 5거래소(`usdt_krw` 그룹)**: `timestamp`=`seen_at`(5초 bucket alias)지만 **`rate_changed_at`(정밀 변경 시각)이 entry에 포함됨** → `merge_at = rate_changed_at`로 same-bucket 연속 변경도 정밀 ordering. (서버: Redis path `get_latest_usdt_rate_from_sync_job` + DB fallback `get_latest_source_rates_for_topic` 양쪽 노출.)
- **KRX(`krx:usd-krw-futures` topic)**: Stage E tick writer(`KRX_REDIS_TICK_WRITE_ENABLED`, 운영 활성)가 USDT와 동일한 5-field schema를 써서 `timestamp`=`seen_at`(5초 bucket)일 수 있음 → **Redis-served entry에 `rate_changed_at`(정밀) 포함**(USDT와 대칭). DB fallback 시엔 `timestamp`가 정밀이라 `rate_changed_at` 생략 → `merge_at = rate_changed_at ?? timestamp`로 일관 처리.

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
