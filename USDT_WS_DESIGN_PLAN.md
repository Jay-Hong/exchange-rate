# USDT WebSocket Primary Design Plan

> 📅 **작성일**: 2026-05-14
> 🏷️ **상태**: Phase A draft — 결정 대기 항목 포함, 구현 미진입
> 📋 **문서 성격**: USDT 5거래소 REST polling을 WebSocket primary로 전환하기 위한 design plan. KRX fanout refactor 패턴(LivenessMonitor/RestFallbackController/RedisLatestWriter/DbWriter) 재사용 검토 + 결정 대기 항목 명시. **구현 계획 아님** — 최종 구현 GO는 별도.

## 0. Scope & Status

본 문서는 *USDT WebSocket primary 전환 + REST fallback*의 설계 산출물. Phase A 결과:

- Phase A.0 — 로컬 문서/코드 확인 ✓ ([USDT_EXCHANGE_WEBSOCKET_GUIDE.md](USDT_EXCHANGE_WEBSOCKET_GUIDE.md) + [app/crawlers/usdt_sources.py](app/crawlers/usdt_sources.py))
- Phase A.1 — 구조 결정 (분리 axis 4종 + fanout 패턴 재사용)
- Phase A.2 — canary / REST fallback / 알림 기준 / feature flag 옵션
- Phase A.3 — **OHLC 1차 제외 (deferred)** 명시

**범위 안**:
- 5거래소 WebSocket primary + REST fallback 구조 설계
- KRX 패턴 (LivenessMonitor/RestFallbackController/RedisLatestWriter/DbWriter) 재사용 검토
- 결정 대기 항목 + 추천안

**범위 밖**:
- 구현 코드 변경 (별 GO 흐름)
- OHLC window aggregate 저장 ([§9](#9-ohlc-1차-제외--미래-재검토-조건))
- USDT 탭 productization (iOS/Android client work — 백엔드 작업과 분리)
- KRX 동일 패턴 적용 (별 phase)

**병행**:
- 5/18 KRX 만기 baseline 관찰 ([KRX_CANARY.md](KRX_CANARY.md))

## 1. 현재 상태

### 1.1 REST 수집 흐름 ([app/crawlers/usdt_sources.py](app/crawlers/usdt_sources.py))

- **`_fetch_*` 5종** (`_fetch_upbit/_fetch_bithumb/_fetch_coinone/_fetch_korbit/_fetch_gopax`): per-source REST endpoint + ticker price 필드 파싱
- **`collect_usdt_rates()`**: `ThreadPoolExecutor`로 5거래소 병렬 fan-out, per-source 2초 timeout, 실패 격리
- 스케줄: APScheduler cron 10s
- 변경 감지 + DB insert: `crud.insert_source_rate_if_changed`
- Redis direct write: `_mirror_changed_source_to_redis` (ADR-029 — DB insert 성공 시 best-effort)
- 알림: `_process_source_alerts_safe` → `process_source_rate_alerts` (changed_rates 기반)

### 1.2 한계

- **REST polling 10초 → 가격 변동 1초 단위 추적 불가**: 짧은 시간 안 임계값 통과 후 회복 시 알림 누락 가능 (changed_rates DB row 기반)
- **per-source rate limit**: 거래소 REST API rate limit 의식적 관리 필요 (현 2초 timeout × 10s 주기 → 거의 무관하지만 빈도 단축 시 issue)
- **장애 격리는 fan-out 단위**: WebSocket이면 connection 단위 격리 가능 (per-source long-running)

### 1.3 변경 후 목표 흐름 (KRX fanout 패턴 재사용)

```text
WebSocket tick (per-exchange long-running)
  ↓ normalize UsdtTick (source/asset/rate/exchange_ts/received_at)
  ↓ fanout
  ├─ UsdtRedisLatestWriter   # every tick → Redis latest
  ├─ UsdtAlertEvaluator      # every tick → 알림 평가
  └─ UsdtDbWriter            # 기존 insert_source_rate_if_changed 유지 (1차)

별도 cycle:
  UsdtLivenessMonitor          # WebSocket silence / reconnect / parse error tracking
  UsdtRestFallbackController   # WebSocket 장애 시 REST probe
```

## 2. 기존 가이드 기반 WebSocket 스펙 요약

> ✅ **Phase B.0 재확인 완료 (2026-05-14)**: 5거래소 공식 docs 재확인 결과 **spec 변경 없음**. 상세는 [USDT_EXCHANGE_WEBSOCKET_GUIDE.md §1.1](USDT_EXCHANGE_WEBSOCKET_GUIDE.md) Phase B.0 재확인 표 참조. Codex critical 3 항목(Bithumb Upbit 호환 / Bithumb/Korbit heartbeat 명시 / Gopax 단일 pair) 모두 기존 상태 유지 — design plan §3/§5 영향 없음, §10 결정 그대로 Phase B.1 진입 가능.
>
> 이전 검증: [USDT_EXCHANGE_WEBSOCKET_GUIDE.md](USDT_EXCHANGE_WEBSOCKET_GUIDE.md) 2026-04-28 검증본.

| Source | Endpoint | Subscribe 단일 KRW-USDT | Price 필드 | Heartbeat |
|---|---|---|---|---|
| **upbit** | `wss://api.upbit.com/websocket/v1` | ✓ `["KRW-USDT"]` | `trade_price` | WS ping 30s |
| **bithumb** | `wss://ws-api.bithumb.com/websocket/v1` | ✓ `["KRW-USDT"]` (Upbit 호환) | `trade_price` | 미명시 (provisional 30s) |
| **coinone** | `wss://stream.coinone.co.kr` | ✓ TICKER channel (`KRW`+`USDT`) | `data.last` | App PING 5분 (서버 30분 idle) |
| **korbit** | `wss://ws-api.korbit.co.kr/v2/public` | ✓ `usdt_krw` | `data.close` | 미명시 (provisional 30s) |
| **gopax** | `wss://wsapi.gopax.co.kr` | ✗ 전체 ticker 구독 (서버 필터) | `last` | Primus `::ping::` 30s |

**Reconnect**: 거래소 공통 exponential backoff (1s → 2s → 4s ... 30s cap)

**Phase B.0 재확인 완료 (2026-05-14)** — 7 spec 항목 모두 기존 상태 유지:

- endpoint 도메인/path 변경 없음 ✓
- subscribe payload 형식 변경 없음 ✓
- price 필드 (이름/타입) 변경 없음 ✓
- heartbeat 규칙 변경 없음 ✓ (Bithumb/Korbit는 여전히 공식 미명시 → provisional 30s 유지)
- rate limit 변경 없음 ✓ (Gopax 동시 연결 20/IP, 연결 시도 20/sec/IP 등 [guide §14](USDT_EXCHANGE_WEBSOCKET_GUIDE.md#14-약관정책-1차-검증) 표 동일)
- 재연결 권장 방식 변경 없음 ✓

→ 본 design doc은 2026-04-28 guide 기준 + 2026-05-14 Phase B.0 재확인. **구현이 장기간 지연(>1-2개월)되면 진입 GO 직전 한 번 더 재확인 권장** (5거래소 docs는 빈도 낮지만 endpoint/heartbeat 변경 이력 있음).

## 3. USDT용 fanout 구조 (KRX 패턴 재사용)

### 3.1 분리 axis 4종 (KRX_FANOUT_REFACTOR_PLAN과 일관)

| Axis | USDT 적용 | 비고 |
|---|---|---|
| **WS tick → Redis latest** | 모든 tick (`UsdtRedisLatestWriter`) | KRX `set_latest_krx_rate_from_sync_job` 패턴 → `set_latest_usdt_rate_from_sync_job` 재사용 (이미 존재, [ADR-029](DECISIONS.md)) |
| **WS tick → 알림 평가** | 모든 tick (`UsdtAlertEvaluator`) | §6 알림 기준 변화 옵션 결정 영역 |
| **WS tick → DB** | 기존 `insert_source_rate_if_changed` (1차 유지) | OHLC 1차 제외 (§9) |
| **REST → WS 장애 fallback** | `UsdtRestFallbackController` | §5 fallback 정책 옵션 결정 영역 |

### 3.2 컴포넌트 매핑

| 미래 컴포넌트 | KRX 대응 | 비고 |
|---|---|---|
| `UsdtWsClient` (per-exchange) | `KisFuturesClient` | 거래소별 5 인스턴스 (subscribe/parse 거래소별 차이 큼) |
| `UsdtLivenessMonitor` (per-exchange) | `KrxLivenessMonitor` | frame counters / age timestamps / gap buckets |
| `UsdtRestFallbackController` (per-exchange) | `KrxRestFallbackController` | eligibility evaluator + REST probe lifecycle |
| `UsdtRedisLatestWriter` | `KrxRedisLatestWriter` | tick → Redis SET (per-source key `latest:source:{source}:usdt-krw`) |
| `UsdtAlertEvaluator` | 미존재 (KRX는 stub만) | §6 옵션 |
| `UsdtDbWriter` | `KrxDbWriter` | 1차 기존 insert_source_rate_if_changed |

### 3.3 1 vs 5 인스턴스 결정

**옵션 A — per-exchange 5 인스턴스** (추천):
- 거래소별 WS protocol 차이 큼 (Upbit/Bithumb 호환, Coinone/Korbit 별도, Gopax Primus ping)
- 거래소별 장애 격리 자연 (한 connection 죽어도 다른 4개 영향 X)
- 거래소별 reconnect/backoff 독립
- 거래소별 metric/telemetry 자연

**옵션 B — 단일 manager + 5 connection**:
- 중앙 lifecycle 관리 (start/stop 통합)
- 단점: 거래소별 protocol 분기 manager 안에 집중 → 복잡

→ **A 추천** (KRX `KisFuturesClient` 단일 인스턴스 패턴과 다름 — 거래소 수 + protocol 다양성 차이).

## 4. Canary 거래소 선정 근거

**추천: Upbit**

| 기준 | Upbit | Bithumb | Coinone | Korbit | Gopax |
|---|---|---|---|---|---|
| 공식 문서 명확성 | 우수 (subscribe+heartbeat+reconnect 모두) | 양호 (heartbeat 미명시) | 양호 (PING 명시) | 양호 (heartbeat 미명시) | 양호 (Primus 30s 명시) |
| Subscribe 단일 pair | ✓ | ✓ | ✓ | ✓ | ✗ (전체 + 서버 필터) |
| Heartbeat 정책 | WS ping 30s | 미명시 (Upbit 호환 가정) | App PING 5분 | 미명시 (WS ping 30s 추정) | Primus `::ping::` 30s |
| Subscribe 호환성 | 원본 | Upbit 호환 (코드 재사용) | 별도 protocol | 별도 protocol | Primus 별도 |
| 코드 기존 REST adapter | OK | OK | OK | OK | OK |

> ⚠️ **거래량/유동성 순위는 의식적으로 제외** — 공개 집계가 시점별로 크게 바뀌고, 검증 없이 단정 시 잘못된 결정 근거가 됨. canary 선정은 *프로토콜 단순성 + 문서 명확성*에 의존.

**Upbit canary 이유** (객관 기준):
1. 공식 문서 가장 명확 (subscribe + heartbeat + reconnect 모두 명시)
2. Subscribe 단일 pair 가능 (대역폭 최소, 필터 로직 불요)
3. Subscribe payload가 표준 형태 (Bithumb이 호환 → Upbit 성공 시 즉시 확장)
4. WS ping/reconnect 패턴이 가장 단순 (KRX KIS 패턴과 유사)

**Bithumb 2순위**: Upbit canary 안정 후 즉시 확장 (subscribe payload 호환). ★ "Upbit 호환" 자체도 구현 진입 직전 공식 문서 재확인 대상 (Bithumb 측 spec 변경 가능성). §11 Phase B.0 항목.

**Coinone/Korbit 3-4순위**: 별도 protocol → 각자 별 PR.

**Gopax 마지막**: 전체 ticker + 서버 필터링 + Primus ping → 가장 다른 구조, 검증 마지막에.

→ **결정 대기**: Upbit 1차 canary 채택? (추천) 또는 다른 선호?

## 5. REST fallback 역할 옵션

**옵션 A — Continuous polling (현 구조 유지)**:
- REST를 10초마다 계속 polling (현재 그대로)
- WebSocket 도입 후에도 REST가 *백업 latest source*
- 장점: 안전 (WS 장애 영향 0), 기존 코드 재사용
- 단점: REST API 부하 + rate limit 압박 + WebSocket 가치 일부 상쇄

**옵션 B — WebSocket silent 시 probe만**:
- WS 정상 시 REST X
- **WS *frame/heartbeat silence* 감지 시 REST 1회 probe** → 결과 반영
- 핵심 nuance: *frame silence* ≠ *price tick silence*. 저유동성 구간에서 ticker update가 뜸한 건 정상 — 가격 변경 없음이 fallback 트리거가 되면 false positive 폭주. KRX 패턴 일관: `last_frame_age_sec` (any frame, heartbeat 포함) 기준이지 `last_trade_frame_age_sec` (가격 활동) 기준 X.
- 거래소별 frame silence threshold (★ 모두 *provisional* — 구현 진입 직전 §11 Phase B.0 공식 문서 재확인 후 확정):
  - Upbit/Bithumb/Korbit: WS ping/pong 또는 ticker frame 30s+ silence (Bithumb/Korbit heartbeat 미명시라 추정값)
  - Coinone: App PING 5분 사이클이라 더 긴 threshold
  - Gopax: Primus ping 30s 기준
- 장점: REST API 부하 최소, WebSocket가 primary
- 단점: source-specific threshold 결정 + WS 라이브러리 frame event 추적 필요

**옵션 C — Hybrid (continuous reduced + silent escalate)**:
- WS 정상 시 REST 5분/10분에 1회 (sanity check)
- WS frame silence 시 옵션 B 동일
- 장점: 균형
- 단점: 2 trigger 관리 복잡

**추천**: **옵션 B** (KRX ADR-027 패턴과 일관, *primary가 정상이면 REST 무관*).

→ **결정 대기**:
- A/B/C 선택?
- B 채택 시: 거래소별 frame silence threshold (source-specific 권장 — Coinone은 5분, 나머지는 30-60s)

REST 결과 반영 범위 (옵션 B 채택 시 추가 결정):
- Redis latest 갱신: ✓ (정합 유지)
- DB INSERT: 옵션 (insert_source_rate_if_changed 적용 시 자연 deduplication)
- 알림 evaluation: 옵션 (REST 결과도 tick과 동등 처리 vs probe 결과는 분류만)

## 6. 알림 기준 변화 — B1 채택 (장기 알림 입력 모델 변경)

**현재**: `_process_source_alerts_safe(db, changed_rates)` — DB INSERT 발생한 *changed_rates*만 평가.

### 6.1 옵션 세분화

**옵션 A — DB row 기반 유지** (변경 X):

- 장점: 기존 코드 재사용, 회귀 위험 최소
- 단점: WebSocket tick 빈도 ↑ + DB insert-if-changed 적용 시 *짧은 시간 안 임계값 통과 + 회복* 시 알림 누락

**옵션 B — Tick/observation 기반** (3 sub-option):

- **B1**: tick/observation 평가 + 기존 1회성 `triggered/disabled` semantics 유지
  - 임계값 통과 즉시 발화 + 자동 비활성
  - 사용자 재활성화 전까지 추가 발화 X (현 spec 동일)
  - 정밀도 ↑ (10s 사이 spike 캡쳐). **단, 같은 설정의 반복 발화는 기존 1회성 semantics로 막지만, 기존에 누락되던 crossing이 발화될 수 있어 총 발화 건수는 증가 가능** (Codex 정정 — *"알림 빈도 변화 없음"은 부정확*)
- **B2 — 사용자 설정 반복 간격** (Codex/사용자 합의 reframing — *"cooldown 반복"은 implementer-centric 표현, 제품 spec은 사용자 설정 반복 간격*):
  - 조건이 만족되는 동안 *사용자가 선택한 간격*으로 반복 알림을 보낸다.
  - 지원 후보: `once`, 1m, 5m, 10m, 30m, 1h, 2h, 4h, 6h, 12h, 1d
  - schema: `repeat_interval_sec` 필드 추가 (**`null = once`**, 정수 = 초 단위 간격)
  - **`null = once` 설계**: 기존 알림 설정과 자연 호환 (마이그레이션 시 default null → 현 1회성 그대로 동작)
  - 1차 Upbit canary에서는 **구현하지 않고**, 별도 ADR/PR에서 `repeat_interval_sec` 필드를 추가하는 알림 설정 모델 확장으로 처리. 은행/Investing/USDT/KRX/비교 알림 **공통 기능**으로 다룬다.
- **B3 — Threshold direction crossing 반복**:
  - 발화 후 가격이 임계값 반대편으로 → 다시 통과 시 재발화
  - schema: `last_side`, `last_crossed_at` 등 추가 가능
  - B2보다 정교, 더 복잡한 별도 기능 — 별 ADR/PR (B2 안정 후)

### 6.2 1차 결정 — B1 채택 (Codex 권장 wording)

> **1차 결정: B1 채택.**
> 알림 평가는 DB INSERT 이벤트가 아니라 tick/observation을 입력으로 받는다.
> 다만 기존 1회성 `triggered/disabled` semantics는 유지한다.
>
> 이 결정은 USDT 전용이 아니라 **장기 알림 입력 모델 변경**이다.
> USDT Upbit canary에서 먼저 검증하고, 이후 KRX, 은행/Investing 가격 알림,
> 비교 알림으로 확장한다.
>
> 단, Phase B.1 구현에서는 **공통 interface만 정의하고,
> 실제 구현은 Upbit canary에 필요한 최소 범위**로 제한한다.
> B2 사용자 설정 반복 간격, B3 direction-crossing 반복은 별도 사용자 요구와 ADR/PR 전까지 구현하지 않는다.

### 6.3 구조 결정

- **Evaluator 입력**: `AlertObservation(source, asset, rate, timestamp, kind)` data model — DB row가 아닌 *observation*
- **source/asset normalization** (구현자 재해석 여지 차단):
  - USDT 거래소: `source ∈ {"upbit", "bithumb", "coinone", "korbit", "gopax"}`, `asset = "usdt-krw"`
  - KRX: `source = "krx"`, `asset = "usd-krw-futures"` (만기 contract는 별도 metadata)
  - 은행: `source ∈ {"kb", "hana", "shinhan", ...}` (기존 `notification_settings.bank` 필드 매핑), `asset ∈ {"usd-krw", "jpy-krw", "eur-krw"}` (기존 `currency` 매핑)
  - Investing: `source = "investing"` (기존 `bank="investing"` 매핑), `asset` 동일
  - 비교 알림: `kind="comparison"` + source/asset 페어는 evaluator 내부 정책 (multi-source 평가 detail은 별 phase)
- **`kind` field 의미**: observation 분류 (예: `"trade"` / `"quote"` / `"snapshot"` / `"rest_probe"`) — alert evaluator 자체는 가격 평가만, kind는 metric/log/필터 등 보조 용도
- **DB insert는 evaluator 입력이 아니라 별도 side effect** — fanout에서 독립 handler
- **Interface는 source-neutral**: USDT/KRX/은행/Investing/비교 알림 공통 형태 (재발명 회피)
- **구현 범위는 canary 최소**: Upbit observation → UsdtAlertEvaluator 1개만. 다른 source는 별 phase.
- **반복 간격 설계 (B2 확장 여지)**: evaluator interface가 *once-only를 hardcode 하지 말 것*. setting에서 `repeat_interval_sec` 읽고 분기 — 현 단계에서는 `null = once`만 분기 (1회성 spec), 미래 B2 구현 시 정수 값 분기 추가. 즉 Phase B.1에서 `repeat_interval_sec is None` 분기만 처리해도 미래 호환.
- **B3 확장 여지**: interface에서 direction state 덧붙일 수 있도록 — *지금은 구현 X*.

### 6.4 확장 순서 (이번 doc 범위 외)

1. USDT Upbit canary (Phase B.1) — interface 정립 + 최소 구현 (B1 + `repeat_interval_sec is None` 분기만)
2. USDT 5거래소 확장 (Phase B.2~B.4) — 같은 evaluator 재사용
3. KRX (Phase C 후보) — 같은 패턴 적용
4. 은행/Investing 가격 알림 — 같은 패턴 (운영 사용자 N명, 알림 정밀도 ↑ 체감 변화)
5. **알림 설정 모델 `repeat_interval_sec` 확장** — 별 ADR/PR. schema (`notification_settings` + `source_notification_settings`) + API (POST/PUT request 필드) + iOS/Android client (간격 선택 UI). 4 source 공통 적용. 신규 기능 — 사용자 가치 ↑.
6. 비교 알림 (Phase 3 후보) — interface 위에 추가 평가
7. B3 (direction crossing) — 별 ADR/PR (B2 안정 후, 더 복잡한 기능)

## 7. DB 저장 정책 — 기존 helper + 1초 window 적용

[USDT_EXCHANGE_WEBSOCKET_GUIDE.md §9](USDT_EXCHANGE_WEBSOCKET_GUIDE.md) 권장: *"모든 tick INSERT 금지. 최소 changed 또는 1초 last 정책"*.

**결정 (PR5 구현 확정, 2026-05-14)**: 기존 `insert_source_rate_if_changed` 그대로 호출하되, WS tick은 **1초 window debounce 후 last tick만 helper에 전달**.

- WebSocket tick 빈도 (Upbit 초당 다수) ≫ 10s REST polling
- 모든 tick INSERT 금지 — DB 폭증 위험
- helper 자체는 가격 변경 시만 INSERT → 자연 deduplication
- 추가로 1초 window debounce → 동일 가격 연속 tick에서도 SELECT 비교 부담까지 절감
- 구현: `UpbitDbWriter._flush_after_window` (KRX `KrxDbWriter` 패턴 mirror, [app/crawlers/usdt_ws/upbit.py](app/crawlers/usdt_ws/upbit.py))
- close 시 1초 window 기다리지 않고 마지막 pending tick 즉시 flush — shutdown 마지막 tick 보장
- helper signature / schema 변경 없음 (timestamp 자동 생성, exchange timestamp 저장은 별 PR)

## 8. Feature flag / Rollback / Smoke 기준

### 8.1 Feature flag

KRX 패턴 (`KRX_FUTURES_ENABLED`) 일관:

- **`USDT_WS_<EXCHANGE>_ENABLED`** per-exchange (예: `USDT_WS_UPBIT_ENABLED`)
- 각 거래소 독립 토글 — canary 1개부터 활성, 안정 후 확장
- Default: `false` (배포 후 점진 활성)
- 미활성 시: 기존 REST polling 그대로 동작

### 8.2 Rollback

- env false 전환 + `docker compose restart fastapi`
- 또는 `git revert` (per-exchange commit 분리 시 깔끔)

### 8.3 Smoke 기준 (per canary)

- WebSocket 연결 성공 (subscribe 응답 OK)
- Tick 수신 (60초 안 첫 tick)
- Redis latest 갱신 확인 (per-source key, mirrored_at age 정상)
- **DB INSERT rate 허용 범위 내 확인** (WS tick 빈도가 REST 10s polling보다 ↑ — `insert_source_rate_if_changed` 적용해도 가격 변경 빈도에 따라 INSERT 증가 가능):
  - source_rates growth가 30일 보관 정책 안 (per-source row 수 / 일 추정)
  - tick-to-insert 비율 측정 (대부분 tick은 동일 가격 → INSERT skip 정상)
  - 운영 DB write throughput / replication lag 등 baseline 비교
- usdt:krw topic builder Redis-first hit (DB query 미호출)
- B1 (tick/observation 평가) 채택 — 알림 빈도/정밀도 baseline 측정. tick 기반으로 이전 polling에서 누락되던 crossing이 잡혀 *총 발화 건수 증가 가능* (§6.1 정정 참조). B2 (사용자 설정 반복 간격)는 canary 범위 외라 baseline 측정 항목 X.

## 9. OHLC 1차 제외 + 미래 재검토 조건

### 9.1 1차 제외 결정

> **1차 USDT WebSocket 전환에서는 OHLC 저장을 도입하지 않는다.**
>
> 이유는 OHLC가 최신값 표시/알림 정확도/REST fallback 문제를 해결하지 않고, 현재 명확한 차트·분석 소비자가 없기 때문이다.
>
> DB는 기존 `source_rates` close/last 저장 정책을 유지한다.
> OHLC window 저장은 차트/분석 요구가 명확해질 때 `source_rate_windows` 새 테이블 + 별도 ADR/PR로 재검토한다.

### 9.2 핵심 가치 영역 분리

| 영역 | 해결 메커니즘 | OHLC 기여 |
|---|---|---|
| 최신값 표시 | Redis latest (ADR-026/030) | 0 |
| 알림 정확도 | tick/observation 기반 fanout (§6) | 0 |
| REST fallback | §5 옵션 | 0 |
| DB 저장량 | insert-if-changed (§7) | 작음 |
| **차트/분석** | **OHLC window aggregate** | **유일하게 OHLC가 가치 제공** |

### 9.3 재검토 트리거 (미래)

다음 중 하나라도 충족 시 OHLC 재검토:
- USDT 탭 차트 client 출시 결정 (iOS/Android)
- USDT/KRX historical 분석 API 요구 (예: 거래량 시각화)
- 1초 미만 가격 변동 데이터 보존 요구 (백테스트, 알림 정밀도 분석 등)

재검토 시 산출물 — *별 ADR*:
- 새 테이블 `source_rate_windows` 스키마 (open/high/low/close/sample_count/window_start/window_end/open_ts/high_ts/low_ts/close_ts)
- 중복 제거 정책 (row-level skip)
- USDT/KRX 공통 WindowOhlcWriter 가능성
- 보관 정책 (30일 vs 다른 주기)
- read path 분리 (latest는 Redis, 분석은 새 테이블)
- 마이그레이션 전략 (기존 source_rates 영향 X)

## 10. 현재 결정 (7 항목 모두 확정)

Phase A 결과로 모든 결정 잠금. Phase B 구현 진입 시 본 표가 spec.

| # | 항목 | 결정 |
|---|---|---|
| 1 | Canary 거래소 | **Upbit** (§4) |
| 2 | REST fallback 정책 | **옵션 B (silent probe)** + source-specific frame/heartbeat silence threshold (§5) — 거래소별 provisional, Phase B.0 재확인 후 확정 |
| 3 | 알림 기준 변화 | **B1 채택** — tick/observation 기반 평가 + 기존 1회성 `triggered/disabled` semantics 유지 (§6). 장기 알림 입력 모델 변경. USDT canary에서 검증 후 KRX/은행/Investing/비교 알림 확장. **공통 interface만 정의, 구현은 Upbit canary 필요 최소 범위**. **B2 (사용자 설정 반복 간격, `repeat_interval_sec` 필드)**: 4 source 공통 schema/API/client 확장 — 별 ADR/PR. **B3 (direction crossing)**: B2 안정 후 별 ADR/PR. |
| 4 | DB 저장 정책 | **기존 `insert_source_rate_if_changed` 유지** (§7) |
| 5 | Feature flag | **per-exchange `USDT_WS_<EXCHANGE>_ENABLED`** (§8) |
| 6 | OHLC 도입 | **Deferred** — 1차 미도입, 차트/분석 요구 명확화 시 `source_rate_windows` 새 테이블 + 별 ADR/PR (§9) |
| 7 | 공식 WS 문서 재확인 trigger | **Phase B.0 (구현 진입 직전)** — 별 task (§2, §11) |

## 11. 구현 진입 순서 (참고 — 본 doc 범위 외)

설계 GO 후 implementation phase 흐름:

1. **Phase B.0**: 공식 WS 문서 재확인 (§2 + Codex 7 spec checklist). 특히 *Bithumb subscribe payload Upbit 호환성* (§4)과 *Bithumb/Korbit heartbeat 정책 명시 여부* (§5 provisional threshold) 확정 필요.
2. **Phase B.1**: Upbit canary 구현 — **interface 공통 + 구현 최소 범위**. 상세 PR 분할은 [§12 Phase B.1 PR 분할](#12-phase-b1-pr-분할-7-pr) 참조:
   - **`AlertObservation(source, asset, rate, timestamp, kind)` data model** 신설 (source-neutral, 4 source 호환 가능 shape)
   - **base Evaluator interface** 정의 (`evaluate_observation(obs: AlertObservation)`) — B2/B3 확장 여지(repeat interval state / direction state 덧붙임)는 *interface 차원만* 검토, 구현 X
   - `UsdtWsClient` + `UsdtLivenessMonitor` + `UsdtRestFallbackController` skeleton
   - `UsdtRedisLatestWriter` (기존 `set_latest_usdt_rate_from_sync_job` 재사용)
   - **`UsdtAlertEvaluator` (B1 최소 구현)** — `AlertObservation` 받음, 1회성 spec 유지, source_notification_settings 평가, 기존 `process_source_rate_alerts`와 호환 wrapper 유지 (점진 마이그레이션)
   - `USDT_WS_UPBIT_ENABLED=false` default
   - Unit tests + smoke 기준
3. **Phase B.2**: Canary 활성화 + 24h 운영 관찰
4. **Phase B.3**: Bithumb 확장 (Upbit 호환)
5. **Phase B.4**: Coinone / Korbit / Gopax 순차 확장
6. **Phase B.5**: 5거래소 모두 활성 + 기존 REST polling 격하/제거

**Phase C 후보 (별 PR, 본 doc 범위 외)**:

- KRX `KrxAlertEvaluator` 신설 + 기존 KRX fanout에 부착 (KRX_FANOUT_REFACTOR_PLAN 5.1.D)
- 은행/Investing 가격 알림을 `AlertObservation` 기반 evaluator로 전환 (운영 사용자 N명 — 알림 정밀도 ↑ 체감 변화 영역)
- **알림 설정 모델 `repeat_interval_sec` 확장 (B2 구현)** — 별 ADR/PR. schema (`notification_settings` + `source_notification_settings`) + API (POST/PUT 필드 추가) + iOS/Android client (간격 선택 UI). 4 source 공통 적용. `null = once`라 기존 알림 호환. 신규 기능 — 사용자 가치 ↑.
- 비교 알림 (`comparison_alerts`) — 같은 evaluator 위에 multi-source 평가 추가
- B3 (direction crossing) — B2 안정 후 별 ADR/PR
- **Post-Upbit 구조 refactor 후보**: PR4~PR7은 현재 `app/crawlers/usdt_ws/upbit.py` 위치를 유지한다. Upbit canary Stage 2 안정 후, Bithumb 확장 전에 `app/market_data/{usdt,krx,banks,investing}/...` 도메인 구조로 이동하는 별도 refactor PR을 검토한다. 이 refactor는 기능 변경 없이 import/path 정리만 수행하며, KRX/은행/Investing은 한 번에 옮기지 않고 단계적으로 이동한다.

## 12. Phase B.1 PR 분할 (7 PR)

Phase B.1 implementation을 7 PR로 분할. 각 PR은 default OFF feature flag 아래 land. Codex/Claude 합의 (2026-05-14).

### 12.1 Guardrail 4종 (전 PR 공통 잠금)

1. **PR4-PR5 divergence 의도적**: PR4 land 후 ~ PR5 land 전 window 동안 WS는 Redis만, 기존 REST polling ([app/crawlers/usdt_sources.py](app/crawlers/usdt_sources.py))은 DB만 write. canary OFF default 라 운영 영향 0. 코드 리뷰 시 "WS가 DB write 안 함" 의문 발생 시 본 단서 인용.
2. **additive only**: PR1~PR7 어디서도 기존 REST polling 제거/격하 금지. polling은 baseline 안전망 유지. polling 격하/교체는 별 PR (canary Stage 4, §12.3).
3. **per-PR smoke 기준 필수**: 표의 "Smoke 기준" column이 다음 PR code merge 진입의 1차 게이트. 단 **PR2 24h dev soak**는 *Stage 1 운영 활성화 (`USDT_WS_UPBIT_ENABLED=true`)* 진입 직전 충족 — PR3~PR7 code merge는 PR2 unit test + 단기 connect 검증으로 충분 (PR3 LivenessMonitor는 PR2 leak/race 진단 도구 역할도 가능). 24h soak의 목적은 운영 진입 전 leak/race 감지이지 dev 리듬 차단 아님.
4. **AlertObservation source-neutral interface + Upbit-only 구현**: PR6 범위는 `AlertObservation(source, asset, rate, timestamp, kind)` dataclass + base `Evaluator` interface + `UsdtAlertEvaluator` (Upbit-only). bank/investing/KRX adapter는 PR6 범위 외 (Phase C 별 PR).

### 12.2 7 PR scope/non-scope/flag/rollback/smoke

| PR | Scope | Non-scope | Flag default | Rollback | Smoke 기준 |
|---|---|---|---|---|---|
| **PR1** | feature flag env 도입 + process lifecycle scaffolding (bootstrap/start/stop) + scheduler hook. 외부 connection 없음. | WS connect, DB/Redis write, alert, fallback | `USDT_WS_UPBIT_ENABLED=false` | env unset | flag=false 무동작 unit test + flag=true skeleton lifecycle log emit (no network) |
| **PR2** | flag ON 시 WS connect/subscribe/parse + log only. raw tick → 정규화까지 검증. | Redis/DB write, alert, liveness, fallback | `USDT_WS_UPBIT_ENABLED=false` | env=false | **24h dev soak with flag=true**: no leak, no panic, parse rate normal vs REST polling 비교 |
| **PR3** | LivenessMonitor + reconnect. frame/heartbeat silence detection + gap bucket structured metrics. fallback action 없음. | REST fallback trigger, Redis/DB write, alert | `USDT_WS_UPBIT_ENABLED=false` | env=false | 강제 disconnect 시 gap bucket log 발생 + reconnect 후 정상 복귀, gauge stable |
| **PR4** | Redis latest writer. `set_latest_usdt_rate_from_sync_job` 재사용 (ADR-029 direct write). `latest:source:upbit:usdt-krw` 갱신. legacy_policy allowlist 통과 여부 확인 (정책 변경 없음). | DB write, alert, fallback, **broadcast read path / legacy_policy 변경** (기존 Z-2 infra 그대로 사용) | `USDT_WS_UPBIT_ENABLED=false` | env=false (Redis stale 데이터는 mirror_age로 자연 만료) | flag=true 시 `latest:source:upbit:usdt-krw` 값/타임스탬프 갱신 확인 + `mirror_age` 5초 이내 안정. broadcast end-to-end 반영은 Stage 1 별도 stage check (PR4 단독 범위 밖) |
| **PR5** | DB writer. `insert_source_rate_if_changed` 호출 (§7 정책 유지). REST polling은 그대로 (additive). | alert, fallback, polling 제거 | `USDT_WS_UPBIT_ENABLED=false` | env=false (DB write만 멈춤, schema/data 보존) | DB row 증가율 baseline 측정 — 기존 REST polling row 대비 변화량 (insert-if-changed 정책 충실 검증) |
| **PR6** | AlertObservation dataclass + base Evaluator interface (source-neutral) + UsdtAlertEvaluator (B1 최소, Upbit-only). 기존 1회성 `triggered/disabled` semantics 유지. 기존 `process_source_rate_alerts` 호환 wrapper. | KRX/bank/investing adapter, B2 (`repeat_interval_sec`), B3 (direction crossing) | `USDT_WS_UPBIT_ENABLED=false` | env=false (evaluator 호출 안 됨, 기존 REST polling이 alert 처리) | 동일 threshold/source/asset에서 WS-based trigger와 기존 REST-based trigger 결과 일치 (1회성 발화 보존) |
| **PR7** | REST fallback controller (silent probe 옵션 B). liveness signal 기반 trigger. Redis/DB write 범위 명시 잠금. | polling 격하/제거, KRX/bank/investing fallback | `USDT_WS_UPBIT_ENABLED=false` | env=false (fallback 비활성, 기존 REST polling이 baseline) | WS 강제 종료 시 fallback probe → Redis latest stale 시간 ≤ §5 거래소별 threshold |

### 12.3 Canary stage 매핑

| Stage | 진입 조건 | 행동 | 운영 영향 |
|---|---|---|---|
| **Stage 0** | PR1~PR3 land 후 | flag default false. dev/soak에서만 flag=true. | 0 |
| **Stage 1** | PR4~PR5 land + `USDT_WS_UPBIT_ENABLED=true` | WS → Redis (PR4) + DB (PR5) write. 기존 REST polling 유지 (additive). | 1주 운영 관찰 후 다음 단계 |
| **Stage 2** | PR6 land + `USDT_WS_UPBIT_ENABLED=true` | alert evaluator를 WS observation으로 전환 (Upbit 한정). 1회성 trigger semantics 보존. | 알림 발화 dedup 확인 |
| **Stage 3** | PR7 land + `USDT_WS_UPBIT_ENABLED=true` | REST fallback 활성. WS dead 시 silent probe로 Redis/DB 보전. | freshness 정책 검증 |
| **Stage 4** | 별 PR (본 doc 범위 외) | Bithumb 확장 → Coinone/Korbit/Gopax → 기존 REST polling 격하/제거. | Phase B.3~B.5 |

### 12.4 Rollback 정책 공통

- **1차 수단**: env toggle `USDT_WS_UPBIT_ENABLED=false`. 모든 PR에서 즉시 격리 (lifecycle 격리 패턴, KRX `KRX_FUTURES_ENABLED` 검증 완료).
- **2차 수단**: 코드 revert. lifecycle/skeleton 의존성 깨질 위험 있어 1차 실패 시 한정.
- **PR5 (DB writer) 특이**: rollback 시 `source_rates` schema/data 보존 (write만 멈춤). 별도 마이그레이션 불필요.
- **PR6 (AlertObservation) 특이**: rollback 시 기존 `process_source_rate_alerts` REST polling이 alert 처리 baseline 유지.

## 13. 참조

- [USDT_EXCHANGE_WEBSOCKET_GUIDE.md](USDT_EXCHANGE_WEBSOCKET_GUIDE.md): 거래소별 WS spec (2026-04-28 검증)
- [USDT_TOPIC_MIGRATION_PLAN.md](USDT_TOPIC_MIGRATION_PLAN.md): Z-2 series, topic protocol 마이그레이션 history
- [KRX_FANOUT_REFACTOR_PLAN.md](KRX_FANOUT_REFACTOR_PLAN.md): 재사용할 fanout 패턴 (A/B/C/A-pre 완료)
- [DECISIONS.md ADR-029](DECISIONS.md): USDT direct write + read-path DB fallback
- [DECISIONS.md ADR-031](DECISIONS.md): KRX Redis 통합 (유사 1차 부채 해소 패턴)
- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md): "알림 모든 tick, DB window close" 원칙
- [app/crawlers/usdt_sources.py](app/crawlers/usdt_sources.py): 현재 REST 수집 구조
- [app/crawlers/krx_kis.py](app/crawlers/krx_kis.py): 재사용할 KRX fanout 구현
