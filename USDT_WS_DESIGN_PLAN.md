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
- KRX 동일 패턴 적용 (별도 phase)

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
- 단점: 거래소별 protocol 분기가 manager 안에 집중 → 복잡

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

**Coinone/Korbit 3-4순위**: 별도 protocol → 각자 별도 PR.

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
  - 비교 알림: `kind="comparison"` + source/asset 페어는 evaluator 내부 정책 (multi-source 평가 detail은 별도 phase)
- **`kind` field 의미**: observation 분류 (예: `"trade"` / `"quote"` / `"snapshot"` / `"rest_probe"`) — alert evaluator 자체는 가격 평가만, kind는 metric/log/필터 등 보조 용도
- **DB insert는 evaluator 입력이 아니라 별도 side effect** — fanout에서 독립 handler
- **Interface는 source-neutral**: USDT/KRX/은행/Investing/비교 알림 공통 형태 (재발명 회피)
- **구현 범위는 canary 최소**: Upbit observation → UsdtAlertEvaluator 1개만. 다른 source는 별도 phase.
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
- timestamp: §12.9.8 ③ **super-lite (2026-06-11)** — DB writer가 tick의 exchange 체결/이벤트 시각을 기존 `insert_source_rate_if_changed(timestamp=)` param(`crud.event_ms_to_utc_naive`)으로 전달. **schema 변경 없음** (기존 `timestamp DESC` 쿼리가 latest 결정 → out-of-order stale이 latest 못 덮음). 원래 "exchange timestamp 저장은 별도 PR(schema 변경 영역)"로 보류했으나 무-migration 해소. DB writer 경유 전체(WS tick + REST probe fanout) 적용 — 직접 crud 호출(KRX/legacy)만 미적용.

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

**Phase C 후보 (별도 PR, 본 doc 범위 외)**:

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
2. **additive only**: PR1~PR7 어디서도 기존 REST polling 제거/격하 금지. polling은 baseline 안전망 유지. polling 격하/교체는 별도 PR (canary Stage 4, §12.3).
3. **per-PR smoke 기준 필수**: 표의 "Smoke 기준" column이 다음 PR code merge 진입의 1차 게이트. 단 **PR2 24h dev soak**는 *Stage 1 운영 활성화 (`USDT_WS_UPBIT_ENABLED=true`)* 진입 직전 충족 — PR3~PR7 code merge는 PR2 unit test + 단기 connect 검증으로 충분 (PR3 LivenessMonitor는 PR2 leak/race 진단 도구 역할도 가능). 24h soak의 목적은 운영 진입 전 leak/race 감지이지 dev 리듬 차단 아님.
4. **AlertObservation source-neutral interface + Upbit-only 구현**: PR6 범위는 `AlertObservation(source, asset, rate, timestamp, kind)` dataclass + base `Evaluator` interface + `UsdtAlertEvaluator` (Upbit-only). bank/investing/KRX adapter는 PR6 범위 외 (Phase C 별도 PR).

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
| **Stage 4** | 별도 PR (본 doc 범위 외) | Bithumb 확장 → Coinone/Korbit/Gopax → 기존 REST polling 격하/제거. | Phase B.3~B.5 |

### 12.4 Rollback 정책 공통

- **1차 수단**: env toggle `USDT_WS_UPBIT_ENABLED=false`. 모든 PR에서 즉시 격리 (lifecycle 격리 패턴, KRX `KRX_FUTURES_ENABLED` 검증 완료).
- **2차 수단**: 코드 revert. lifecycle/skeleton 의존성 깨질 위험 있어 1차 실패 시 한정.
- **PR5 (DB writer) 특이**: rollback 시 `source_rates` schema/data 보존 (write만 멈춤). 별도 마이그레이션 불필요.
- **PR6 (AlertObservation) 특이**: rollback 시 기존 `process_source_rate_alerts` REST polling이 alert 처리 baseline 유지.

### 12.5 Phase B.3 — Bithumb WS 확장 (Upbit 패턴 작은 복제)

> 📅 **작성일**: 2026-05-17
> 🏷️ **상태**: 계획 잠금 (Stage U1) — 구현 (Stage U2-U7) 진입 전 외부 검토 통과

#### 12.5.1 Scope

**Phase B.3 = Bithumb 단독.** Coinone/Korbit/Gopax는 후속 phase 목록만 명시 (별도 phase 진입 시점 결정).

**근거 — Bithumb 1순위 선정**:

- Bithumb WS는 **Upbit-compatible** (USDT_EXCHANGE_WEBSOCKET_GUIDE §4 + 2026-05-17 Python websockets smoke 재확인):
  - Endpoint만 다름: `wss://ws-api.bithumb.com/websocket/v1`
  - Subscribe message 동일: `[{ticket}, {type=ticker, codes=["KRW-USDT"]}, {format=DEFAULT}]`
  - Payload format 동일: `{type, code, trade_price, trade_timestamp, timestamp, stream_type}`
  - 파싱 동일: `float(message["trade_price"])` + `int(message.get("trade_timestamp") or message.get("timestamp"))`
  - 인증 없음 (public)
  - Heartbeat 공식 미명시 → provisional 30s 유지 (§2.1)
- **이식 비용 가장 낮음** — Upbit 패턴 거의 1:1 복사 + URL/ticket 명만 변경
- Coinone/Korbit (별도 protocol) / Gopax (전체 ticker + 서버 필터) 는 Bithumb 검증 후 단계적 진입

#### 12.5.2 Stage U1-U7 분할

> **Codex/Claude 합의 (2026-05-17)**: KRX close finalizer 큰 PR (~1100 lines) → 3 High findings → revert + 분할 재시작 학습 적용. Phase B.3은 작은 단위 stage 분할로 진입.
>
> **2026-05-18 보강**: 당초 6 stage에서 U7을 신설하여 7 stage로 분할. alert evaluator wiring을 U6의 DB writer / REST fallback / helper 안정 후 별도 stage로 분리 (PR 크기 관리 + U6 리뷰 표면 축소).

| Stage | Scope | 변경 파일 | Tests |
|---|---|---|---|
| **U1** | 본 §12.5 Phase B.3 section 신설 (계획 잠금) | `USDT_WS_DESIGN_PLAN.md` | — (docs) |
| **U2** | `USDT_WS_BITHUMB_ENABLED=false` env + `app/crawlers/usdt_ws/bithumb.py` lifecycle skeleton + scheduler hook | `app/config.py`, `app/crawlers/usdt_ws/bithumb.py` (신규), `app/scheduler.py` | skeleton lifecycle tests (flag=false 무동작 + flag=true skeleton log emit) |
| **U3** | Bithumb WS connect/subscribe/parse + log only | `bithumb.py` | parse/connect tests |
| **U4** | `UsdtLivenessMonitor` 재사용 + reconnect | `bithumb.py` | reconnect tests + gap bucket |
| **U5** | `BithumbRedisWriter` + topic trigger 자동 발화 (`request_tether_topic_trigger` source-neutral) | `bithumb.py` | redis writer tests |
| **U6** | `BithumbDbWriter` + `BithumbRestFallbackController` + **`fetch_bithumb_usdt_tick()` normalized REST helper 추가** (`_fetch_bithumb` rate-only 보강). **AlertObservation 미schedule** (alert wiring은 U7) | `bithumb.py`, `app/crawlers/usdt_sources.py` | db writer + fallback tests |
| **U7** | Bithumb alert evaluator wiring (`UsdtAlertEvaluator` 재사용). WS tick 및 REST probe 성공 결과를 `AlertObservation(source="bithumb", asset="usdt-krw", kind="tick"\|"rest_probe")`로 schedule. U6의 DB/REST/helper 안정 후 별도 stage로 진입. | `bithumb.py` | alert wiring tests + close/drain order + flag=false invariant |

**Stage 재매핑 (Phase B.1 7 PR → Phase B.3 7 stage)** 정당화:

- Phase B.1 PR3 (LivenessMonitor) 신설 → Phase B.3 U4 재사용 (작업량 감소)
- Phase B.1 PR4 (Redis writer) + PR3 (LivenessMonitor)을 Phase B.3에서는 U4 (reconnect) / U5 (Redis writer) 분리 유지 (작은 단위)
- Phase B.1 PR6 (AlertObservation) — `AlertObservation` / `UsdtAlertEvaluator` interface 자체는 source-neutral로 이미 구현됨. Bithumb client wiring (`_alert_evaluator` attribute + WS tick / REST probe path schedule)은 **U7 별도 stage**로 분할 (KRX 1100 lines → revert 학습 mirror). U6 scope에서는 `AlertObservation` 미schedule — DB writer + REST fallback + REST helper 보강만.

**U6 보강 — normalized REST helper**:

- 현재 `_fetch_bithumb()` (`app/crawlers/usdt_sources.py:85`)는 `float(data[0]["trade_price"])` rate-only 반환
- Upbit `fetch_upbit_usdt_tick()` normalized tick helper와 비대칭 → WS path와 fallback 정규화 결과 불일치 위험
- U6에서 `fetch_bithumb_usdt_tick()` 신설 — `{source, asset, rate, timestamp_ms}` 형태 표준 정규화 (Upbit `fetch_upbit_usdt_tick()` `app/crawlers/usdt_sources.py:40-73` shape 일치)
- 기존 `_fetch_bithumb()`는 호환성 유지 (`fetch_bithumb_usdt_tick()` wrapping 또는 별도)

#### 12.5.3 Canary 활성화 조건

> ⚠️ **운영 활성화는 KRX 5/18~5/19 첫 실측 + 7일 telemetry 안정 후 별도 GO.**

- `USDT_WS_BITHUMB_ENABLED=false` default — Stage U2-U7 코드 land + push 누적 시 운영 영향 0
- **KRX 첫 실측 완료 조건**:
  - 2026-05-18 (월) 15:45 KST CF close finalizer 정상 동작 (Redis flag SET + DB row + 로그)
  - 2026-05-19 (화) 06:00 KST CM close finalizer 정상 동작
  - 5/19~5/26 7일 telemetry 측정 (case A/B/C 분포)
- KRX 안정 + 별도 deploy GO 후 `USDT_WS_BITHUMB_ENABLED=true` canary 진입
- canary 운영 1주 관찰 후 다음 단계 Phase B.4 (Coinone) / B.5 (Korbit) / B.6 (Gopax) 순차 진입. 공통화 검토는 5개 거래소 전부 land 후 별도 시점에 진행.

#### 12.5.4 Rollback 정책 (Phase B.1 §12.4 패턴 재사용)

- **1차 수단**: env toggle `USDT_WS_BITHUMB_ENABLED=false`. lifecycle 격리 (Upbit/KRX 검증 완료 패턴).
- **2차 수단**: 코드 revert. lifecycle 의존성 깨질 위험 있어 1차 실패 시 한정.
- **process 재생성 필수**: KRX_CANARY.md 핵심 원칙 5 — `docker compose restart`는 env_file 변경 반영 안 됨, **`docker compose up -d --force-recreate fastapi`** 사용.

#### 12.5.5 후속 phase 목록 (Phase B.3 범위 외)

| Phase | Scope | 비고 |
|---|---|---|
| **Phase B.4** (예정) | Coinone WS 확장 | 별도 protocol — Upbit 패턴 비호환 (subscribe + payload 형식 다름) |
| **Phase B.5** (예정) | Korbit WS 확장 | 별도 protocol — symbol notation 다름 (`usdt_krw`) |
| **Phase B.6** (예정) | Gopax WS 확장 | 전체 ticker 구독 + 서버 필터링 + Primus `::ping::` 30s (가장 다른 구조) |
| **공통화 검토** | 거래소 base class / shared lifecycle | **Phase B.3 + B.4 + B.5 + B.6 모두 land 후 (5개 거래소 전부)** 중복 명확해진 시점에 판단. 2개만 보고 base class를 결정하면 Korbit/Gopax의 별도 protocol 차이 (symbol notation, 전체 ticker 구독 등)가 반영되지 않아 추상화가 다시 흔들릴 위험. 선제 abstraction 금지 (KRX close finalizer 큰 PR 학습). |

후속 phase 진입 시점 + 순서는 Phase B.3~B.6 운영 안정 측정 후 결정.

### 12.6 Phase B.4 — Coinone WS 확장 (별도 protocol, 2-signal 분리)

> 📅 **작성일**: 2026-05-18
> 🏷️ **상태**: 계획 잠금 (Stage C1) — 구현 (Stage C2-C7) 진입 전 외부 검토 통과
> 🔍 **사전 검증**: Stage C0 ad-hoc smoke 완료 (2026-05-18 21:16~21:46 KST, 30분, n=501 DATA frame). 결과는 §12.6.2 인용.

#### 12.6.1 Scope

**Phase B.4 = Coinone 단독.** Korbit/Gopax는 후속 phase 목록만 명시 (§12.6.7).

**근거 — Coinone 2순위 선정 (Phase B.3 Bithumb 다음)**:

- Coinone WS는 **Upbit/Bithumb 비호환 별도 protocol** ([USDT_EXCHANGE_WEBSOCKET_GUIDE.md §5](USDT_EXCHANGE_WEBSOCKET_GUIDE.md)):
  - Endpoint: `wss://stream.coinone.co.kr` (인증 없음, public, IP당 20 connection 제한)
  - Subscribe message form: `{request_type, channel, topic}` (Upbit/Bithumb의 `[{ticket}, {type, codes}]` 와 다름)
  - Payload format: `data.last` (string) / `data.timestamp` (int ms) (Upbit/Bithumb의 `trade_price` 와 다름)
  - Heartbeat: **공식 idle 30분 방지** → 5분 PING 권장 (안전 마진 6×)
  - 대문자 enum 필수 (`request_type=SUBSCRIBE`, `channel=TICKER`)
- **이식 비용**: Phase B.3 (Bithumb URL/ticket 단순 mirror) 대비 큼 — 별도 protocol parser + 2-signal 분리 (§12.6.3) 신규 작업

#### 12.6.2 Stage C0 smoke 결과 (실측, 2026-05-18 21:16~21:46 KST)

> ⚠️ **시간대 caveat**: 저녁 21시 거래 활발 시간대 sample. 새벽/주말 sparse-time silence 패턴은 §12.6.7 후속 보충 smoke 영역.

| 항목 | 결과 |
|---|---|
| Endpoint connect | `wss://stream.coinone.co.kr` 성공 |
| Subscribe 수락 | `TICKER` / `KRW-USDT` 정상 (SUBSCRIBED frame 수신) |
| Frame total | 508 (CONNECTED 1 + SUBSCRIBED 1 + DATA 501 + PONG 5) |
| Frame rate | 16.7 frame/min average (DATA only) |
| DATA interval | median 1.40s / p95 13.34s / **max 30.00s** |
| PING / PONG | 5분 cycle 5회 송신, 5회 수신, 누락 0 |
| PONG latency | min 11.96ms / median 13.07ms / max 13.59ms (분포 폭 ~1.6ms) |
| `data.last` 타입 | string (예: `"1487"`) |
| `data.timestamp` 타입 | int ms (Unix epoch) |
| ERROR frame | 0 (정상 운영 구간) |
| CONNECTED `data.session_id` | UUID-like 형식 |

산출물: `/tmp/coinone_ws_smoke/{raw.jsonl, summary.json, run.log}` (스크립트 `/tmp/coinone_ws_smoke.py`, untracked).

#### 12.6.3 Coinone-specific 결정 사항 (Phase B.3 Bithumb 대비 추가 5개)

1. **CONNECTED 프레임 처리** — handshake 직후 `{response_type: "CONNECTED", data: {session_id}}` 자동 수신. 운영 parser에서 무시(skip)하지 말고 **session_id를 logger에 첨부**해 reconnect 분석/debugging 활용.
2. **5분 PING + PONG timeout 5s** — `{request_type: "PING"}` 송신 → `{response_type: "PONG"}` 응답. 공식 idle 30분 방지에 대한 안전 마진(6× 빈도). PONG timeout 5s는 실측 latency 13ms × ~400 margin (보수적). timeout 초과 시 reconnect 트리거.
3. **2-signal 분리 (connection liveness vs price freshness)** — Phase B.4 핵심 설계 결정:
   - **connection alive signal**: PONG 기반. 5분 PING cycle, timeout 5s 내 PONG 없으면 reconnect.
   - **price freshness signal**: DATA frame age. soft threshold ~60s (provisional — sparse-time smoke 후 조정 가능) 이상 silence → REST fallback probe만 트리거, **reconnect 안 함**.
   - 근거: C0 smoke max silence 30.00s (활발 시간대) + Coinone TICKER가 "값 변경 시 전송" 구조 → 거래 뜸할 때 DATA silence 정상 발생 가능. 단일 frame silence threshold로 reconnect 트리거 시 false reconnect 폭주 risk. (Upbit/Bithumb은 frame 빈도 잦아 통합 모델 무리 없었던 영역.)
4. **DEFAULT format 유지** — 가독성 우선. SHORT format(`data.d` / `la` / `t`)은 Phase B.4 범위 밖 (미래 bandwidth 최적화 검토 대상).
5. **REST fallback contract rollover check 불필요** — KRX와 달리 Coinone USDT/KRW는 만기/contract rollover 개념 없음 (source/asset 검증은 별개 — multi-pair subscribe 시 운영 parser에서 처리). REST fallback은 price freshness degradation 신호에만 사용 (`fetch_coinone_usdt_tick()` normalized helper, C6 stage 신규).

#### 12.6.4 Stage 분할 — C1-C7 (Phase B.3 U1-U7 패턴 mirror)

> Stage **C0 (ad-hoc smoke, 완료)**는 본 plan의 input. Stage **C1~C7** 7-stage가 작업 단위 (Phase B.3 U1~U7과 대칭).

| Stage | Scope | 변경 파일 | Tests |
|---|---|---|---|
| **C0** | ad-hoc Coinone WS smoke 30분 (완료 2026-05-18) | `/tmp/coinone_ws_smoke.py` (untracked) | — (관찰) |
| **C1** | 본 §12.6 Phase B.4 section 신설 (계획 잠금) | `USDT_WS_DESIGN_PLAN.md` | — (docs) |
| **C2** | `USDT_WS_COINONE_ENABLED=false` env + `app/crawlers/usdt_ws/coinone.py` lifecycle skeleton + scheduler hook | `app/config.py`, `app/crawlers/usdt_ws/coinone.py` (신규), `app/scheduler.py` | skeleton lifecycle tests (flag=false 무동작 + flag=true skeleton log emit) |
| **C3** | Coinone WS connect/subscribe/parse + log only — **별도 protocol parser** (`request_type=SUBSCRIBE`, `data.last`/`data.timestamp` 파싱) + CONNECTED.session_id 캡처 + DEFAULT format | `coinone.py` | parse/connect tests, response_type 분기 검증 (CONNECTED/SUBSCRIBED/DATA/PONG/ERROR) |
| **C4** | **Connection liveness** (`UsdtLivenessMonitor` **source-neutral 재사용** — `last_activity_at = max(tick, heartbeat)` 이미 2-signal 의식, 확장 없음) + **application-level PING/PONG event-based** (Coinone 별도 protocol — `{"request_type":"PING"}` send + `_pong_event` Event clear→send→wait_for, 5분 cycle + 5s timeout) + **2 status 분리** (`_connection_status` / `_ticker_freshness_status`) + ticker freshness telemetry (60s warning, transition 기반 1회 log, action X) + reconnect loop (Bithumb mirror) | `coinone.py` | ping_loop event sequence / PONG timeout / status 전이 + log throttle / ticker silence no reconnect / scope guard / constants tests |
| **C5** | `CoinoneRedisWriter` + topic trigger 자동 발화 (`request_tether_topic_trigger` source-neutral hook 재사용) | `coinone.py` | redis writer tests |
| **C6** | `CoinoneDbWriter` + `CoinoneRestFallbackController` (price freshness signal trigger) + **`fetch_coinone_usdt_tick()` normalized REST helper** (`_fetch_coinone` rate-only 보강 — Phase B.3 U6 패턴 mirror) | `coinone.py`, `app/crawlers/usdt_sources.py` | db writer + fallback tests |
| **C7** | Coinone alert evaluator wiring (`UsdtAlertEvaluator` 재사용, `AlertObservation(source="coinone", asset="usdt-krw", kind="tick"\|"rest_probe")`) | `coinone.py` | alert wiring tests + close/drain order + flag=false invariant |

**Stage 정당화**:

- Phase B.3 U1-U7 7-stage 패턴 mirror (C1-C7). C0 smoke는 stage 명명에서 빼서 Bithumb과 대칭 유지.
- **C4 신규성 정확화 (2026-05-19 C4 구현 시 정정)**: `UsdtLivenessMonitor`의 `last_activity_at = max(tick, heartbeat)` + `is_stale` 패턴이 이미 2-signal 의식이라 **2-signal 분리 자체는 재사용 가능** (확장 없음). C4 신규 작업은 (1) **application-level PING/PONG event-based** (Coinone 별도 protocol — `{"request_type":"PING"}` send + `_pong_event` Event synchronization, Bithumb의 WS protocol `ws.ping()`과 다름), (2) **ticker freshness telemetry** (`ticker_update_age_sec` warning transition + log throttle by transition, action 미진입), (3) **2 status 분리 명시** (`_connection_status` / `_ticker_freshness_status` — Upbit/Bithumb의 통합 `_status`와 의미 충돌 회피), (4) **per-source threshold module-level constants** (`PING_INTERVAL_SEC=300` / `PING_TIMEOUT_SEC=5` / `STALE_AFTER_SEC=360` / `TICKER_FRESHNESS_WARNING_SEC=60`). Phase B.3 → B.4 이식 비용은 Bithumb U3 (Upbit 호환 단순 mirror) 대비 큼이나 monitor 자체는 재사용.
- **C3 = 별도 protocol parser**. Phase B.3 U3 (Upbit 호환 parse) 와 별도 코드 작성. CONNECTED response_type 처리 명시적 분기 추가.
- **C5/C6/C7 = Phase B.3 U5/U6/U7 거의 1:1 mirror** — Bithumb 후 정착된 source-neutral hook (`request_tether_topic_trigger`, `UsdtAlertEvaluator`, `AlertObservation`) 재사용.

#### 12.6.5 Canary 활성화 조건 + 운영 status

> 📅 **활성화 시점**: 2026-05-19 18:42:16 KST (선행 조건 일부 완화하여 선활성화)

**원래 선행 조건** (계획 잠금 시점):

- `USDT_WS_COINONE_ENABLED=false` default — Stage C2-C7 코드 land + push 누적 시 운영 영향 0
- 2026-05-19 (화) 06:00 KST KRX CM close finalizer 정상 동작
- 2026-05-19 ~ 2026-05-26 KRX close finalizer 7일 telemetry 안정 (case A/B/C 분포 확인)
- Phase B.4 C2~C7 코드 land + 외부 검토 통과
- canary 운영 1주 관찰 후 다음 단계 Phase B.5 (Korbit) / B.6 (Gopax) 순차 진입. 공통화 검토는 5개 거래소 전부 land 후 별도 시점에 진행.

**활성화 결정 (2026-05-19 사용자 판단)** — KRX 5/26 telemetry 완료 전 선활성화:

- ✅ 2026-05-19 06:00 KST CM finalizer 통과 (KRX_CANARY 참조)
- ✅ Phase B.4 C2~C7 land + push 완료 (C2 `13b6daf` → C7 `9da2beb`, 5/19 17:00~18:30 KST)
- ⏳ 5/26 telemetry 진행 중 (Day 1/7)
- **선활성화 근거**: (1) Coinone WS는 KRX와 axis 독립 (2) 사용자 앱 영향 0 — 앱 테더 탭 미배포, backend canary side effect (Redis/DB/Alert)는 허용 범위 (3) 이상 시 즉시 rollback 가능 (`env=false` + `--force-recreate`, §12.6.6) (4) sparse-time 데이터를 별도 smoke 없이 자연 누적 가능

**활성화 lesson** (CLAUDE.md `## Docker 배포 및 관리` cross-reference):

- `git pull` + `docker compose up -d --force-recreate fastapi`만으론 image rebuild 안 됨 → 새 모듈 (`coinone.py`) 부재 → `ModuleNotFoundError` 사고 발생
- **`docker compose build fastapi` 선행 필수** (CLAUDE.md 변경 종류별 절차 표 참조)
- 신규 모듈 검증: `docker exec exchange-rate-app python -c "from app.crawlers.usdt_ws.coinone import CoinoneWsClient"` import check

**활성화 후 ~2시간 운영 status** (18:42 → 20:38 KST):

- container Up 2h (healthy), reconnect 0
- 이상 징후 0건: `DB write failed` / `Redis write returned False` / `connection_status normal → reconnecting` / `ticker_freshness_status` transition / `coinone.fallback` probe / `ping failed` / `ERROR`/`WARNING` 모두 0
- DB throughput: coinone 1.82 rows/min (REST polling 단독 ~0.9 대비 ~2× ↑ — WS effect 확인). upbit 4.72/min / bithumb 5.65/min과 동일 axis에서 정상 동작
- Redis mirror tick-level fresh (`mirrored_at` lag <1s)
- DB row gap (가격 변경 저장 간격) 1h 분석: median 20.1s / max 137.5s — **DB 저장 관점 보조 지표**이며 실제 DATA frame freshness와 동일하지 않음 (호가/거래량 변경도 frame trigger이나 가격 무변동이면 DB INSERT skip)
- 실제 ticker freshness 검증은 운영 로그상 `ticker_freshness_status` transition 0 + `fallback` 0 사실로만 — **저녁 활발 시간대 한정, sparse-time (새벽/주말) 데이터는 24h+ 자연 누적 예정**

**남은 작업**:

- 24h+ 운영 관찰 (sparse-time 자연 누적)
- sparse-time max ticker_update_gap 결과로 `TICKER_FRESHNESS_DEGRADED_SEC` / `FALLBACK_COOLDOWN_SEC` provisional 300s 정확값 확정 — 필요 시 별도 작은 commit (상수 1~2줄 수정)
- Coinone 24h+ 자연 누적 관찰과 **병행하여** Phase B.5 (Korbit) 준비/진입 가능. 공통화 검토는 Phase B.6까지 5개 거래소 전부 land 후 별도 시점에 진행 (2개만 보고 base class 결정 시 Korbit/Gopax의 별도 protocol 차이 미반영 위험).

**Coinone canary 운영 누적 관찰 (2026-05-21 ~07:28 KST 시점 갱신)**:

- 활성화 5/19 18:42 → 2026-05-21 ~07:28 시점에 **누적 ~36h, 24h+ 기준 통과**
- 24h 운영 log grep (transition / fallback probe / ping failed / DB write failed / Redis write returned False) Coinone 자체 이상징후 0건 관찰
- Redis `mirrored_at` fresh (직접 측정 시점 기준 ~수십 ms 이내), 24h DB rows ~2900건 + last_gap ~144s (가격 변경 기반 보조 지표)
- **Upbit 부수 관찰 — Redis write queue saturation 반복 발생 확인 (서비스 영향 없음, guard 정상 작동)**:
  - 1차 2026-05-20 10:09:07 KST: 13건 burst (~1초)
  - 2차 2026-05-21 01:20:42 KST: 4건 burst (~1초), 1차 대비 약 15h 간격
  - `MAX_PENDING_WRITES=20` PR4 guard 작동, tick skip + WARNING log (ERROR/Traceback 아님 — 의도된 보호 동작), 측정 시점 사용자 앱 영향 없음 (테더 탭 미배포 + last-write-wins로 latest 유지)
  - 반복 발생 확인 (1차/2차 모두 새벽 시간대) — 원인/패턴 (sparse-time / 일정 cadence / Redis latency burst 등) 추가 telemetry 누적으로 관찰 필요. summary log 부재로 saturation count historical 산출 제한 (별도 후속 PR 영역, 본 §12.7.5 lesson 참조).
  - Coinone과 직접 인과 없음 (Upbit Redis writer 별도 instance), 같은 fastapi process 운영 시점 부수 관찰로 기록

#### 12.6.6 Rollback 정책 (Phase B.3 §12.5.4 패턴 재사용)

- **1차 수단**: env toggle `USDT_WS_COINONE_ENABLED=false`. lifecycle 격리 (Upbit/Bithumb/KRX 검증 완료 패턴).
- **2차 수단**: 코드 revert. lifecycle 의존성 깨질 위험 있어 1차 실패 시 한정.
- **process 재생성 필수**: `docker compose up -d --force-recreate fastapi` (env_file 변경 반영, `restart`로 부족).

#### 12.6.7 후속 phase 목록 + 보충 smoke (Phase B.4 범위 외)

**보충 smoke / 운영 관찰 (Phase B.4 land 후 자연 진행)**:

- **새벽/주말 sparse-time 관찰** — 별도 ad-hoc smoke 대신 2026-05-19 18:42 canary 활성화 후 운영 데이터로 자연 누적 (§12.6.5 참조). DATA silence 패턴 검증 + threshold 정확값 확정 input:
  - `TICKER_FRESHNESS_WARNING_SEC = 60s` (warning state, log only — provisional)
  - `TICKER_FRESHNESS_DEGRADED_SEC = 300s` (REST probe trigger — provisional)
  - `FALLBACK_COOLDOWN_SEC = 300s` (degraded threshold와 동일 — provisional)
  - 둘 다 sparse-time 운영 데이터 (특히 5분 이상 silence 발생 여부)로 정확값 확정. 필요 시 상수 1~2줄 수정 별도 commit.
- **ERROR shape smoke** (분리 실행, ~10분, 선택사항) — 의도적 잘못된 subscribe로 ERROR response shape 캡처. C3 (parse)에 unknown safe handler 이미 구현되어 있어 운영 영향 없음. 별도 검증 가치는 낮음.

**후속 phase**:

| Phase | Scope | 비고 |
|---|---|---|
| **Phase B.5** (예정) | Korbit WS 확장 | 별도 protocol — symbol notation 다름 (`usdt_krw` 소문자, [USDT_EXCHANGE_WEBSOCKET_GUIDE.md §6](USDT_EXCHANGE_WEBSOCKET_GUIDE.md)) |
| **Phase B.6** (예정) | Gopax WS 확장 | 전체 ticker 구독 + 서버 필터링 + Primus `::ping::` 30s (가장 다른 구조) |
| **공통화 검토** | 거래소 base class / shared lifecycle | **Phase B.3 + B.4 + B.5 + B.6 모두 land 후 (5개 거래소 전부)** 중복 명확해진 시점 판단. Coinone의 2-signal 분리 + per-source threshold 분기는 큰 input이지만, Korbit/Gopax의 별도 protocol 차이 (symbol notation, 전체 ticker 구독, Primus `::ping::`)도 base class 결정의 핵심 input. 2개만 보고 결정 시 추상화 재흔들림 위험. 선제 abstraction 금지 (KRX close finalizer 큰 PR 학습). |

후속 phase 진입 시점 + 순서는 Phase B.4 운영 안정 측정 + B.5/B.6 land 후 결정.

### 12.7 Phase B.5 — Korbit WS 확장 (Bithumb-Coinone 혼합 패턴)

> 📅 **작성일**: 2026-05-19
> 🏷️ **상태**: 계획 잠금 (Stage K1) — 구현 (Stage K2-K7) 진입 전 외부 검토 통과
> 🔍 **사전 검증**: Stage K-2 smoke 완료 (2026-05-19 21:43~22:13 KST, 30분 valid + ~30s invalid). 결과는 §12.7.2 인용.

#### 12.7.1 Scope

**Phase B.5 = Korbit 단독.** Gopax는 후속 phase 목록만 명시 (§12.7.7).

**근거 — Korbit 3순위 선정 (Phase B.3 Bithumb / Phase B.4 Coinone 다음)**:

- Korbit WS는 **Bithumb-Coinone 혼합 패턴** ([USDT_EXCHANGE_WEBSOCKET_GUIDE.md §6](USDT_EXCHANGE_WEBSOCKET_GUIDE.md)):
  - Endpoint: `wss://ws-api.korbit.co.kr/v2/public` (인증 없음, public)
  - Subscribe wire format: **list-wrap** `[{requestId, method:"subscribe", type:"ticker", symbols:["usdt_krw"]}]` — Bithumb (`{op,args}`)와도 Coinone (`{request_type,channel,topic}`)와도 다른 신규 wire format
  - Ticker payload: `{type, timestamp, symbol, snapshot, data:{close, lastTradedAt, ...}}` — Coinone과 유사하게 **별도 `data` 객체** 구조이나 필드명 다름 (`close` vs Coinone `last`, `lastTradedAt` vs Coinone `timestamp`)
  - Heartbeat: 공식 미명시. K-2 smoke로 **WS protocol `ws.ping()` 정상 동작 확인** → Bithumb 패턴 mirror 가능 (Coinone의 app-level event-based PING 불필요)
  - Symbol notation: `usdt_krw` 소문자 underscore (WS/REST 일관)
- **이식 비용**: K4 liveness는 Bithumb mirror 1:1 (작음). K3 parser는 Coinone 구조 유사 + 신규 wire format. K6b helper는 Coinone과 다른 source (`lastTradedAt`). 전체 K2-K7 7-stage는 Coinone C2-C7 패턴 mirror, 단 K4가 Bithumb mirror로 축소

#### 12.7.2 Stage K-2 smoke 결과 (실측, 2026-05-19 21:43~22:13 KST)

> ⚠️ **시간대 caveat**: 화요일 저녁 21:43~22:13 거래 활발 시간대 sample. 새벽/주말 sparse-time silence 패턴은 §12.7.7 후속 보충 smoke 영역 (canary 자연 누적).

**Valid run (30분)**:

| 항목 | 결과 |
|---|---|
| Endpoint connect | `wss://ws-api.korbit.co.kr/v2/public` 성공 |
| Subscribe ACK | `{status:"success", requestId:1}` — 별도 frame (ticker 직전 1회) |
| Frame total | 214 (ACK 1 + ticker 213) |
| `snapshot:true` first frame | 1회, offset 0.85s |
| `snapshot` key in subsequent ticker | **누락** (key 자체 부재, false 명시 안 함) |
| DATA interval (ticker frame) | median **10.02s** / p95 10.04s / max 10.13s / min 0.49s (snapshot 전환 직후) — **관찰 사실, 공식 보장 미확인** |
| `data.lastTradedAt` 존재 | 213/213 (100%, missing 0건) |
| `ws.ping()` pong_waiter | 5회 모두 정상, latency median 14.64ms (13.44~15.52ms 범위) |
| `ws.ping()` timeout | 0 |
| `data.close` 타입 | string (예: `"1488"`) |
| `data.lastTradedAt` 타입 | int ms (Unix epoch) |
| ERROR frame in valid run | 0 |
| Decode failure | 0 |

**Invalid run (별도 connection, ~30s)**:

| 항목 | 결과 |
|---|---|
| Invalid subscribe payload | `[{requestId:999, method:"subscribe_INVALID_xyz", type:"unknown_xyz", symbols:["nonexistent_pair_xyz"]}]` |
| Response frame | 1회: `{status:"fail", code:"INVALID_REQUEST", message:"unknown_type", requestId:999}` |
| Connection close by server | **false** (서버가 ERROR 후 연결 유지) |

**REST sample** (smoke 시작 시 1회 GET `https://api.korbit.co.kr/v2/tickers?symbol=usdt_krw`):

- Top-level keys: `["success", "data"]`
- `data[0]` keys: `symbol, open, high, low, close, prevClose, priceChange, priceChangePercent, volume, quoteVolume, bestBidPrice, bestAskPrice, lastTradedAt`
- **Top-level `timestamp` 필드 없음** (`rest_has_timestamp: false`)
- `lastTradedAt: 1779194593306` (epoch ms) 존재 — REST normalized helper의 timestamp source로 사용

산출물: `/tmp/korbit_ws_smoke/{raw.jsonl, summary.json, run.log}` + `/tmp/korbit_ws_smoke_invalid/{raw.jsonl, summary.json, run.log}` (스크립트 `/tmp/korbit_ws_smoke.py` + `/tmp/korbit_ws_smoke_invalid.py`, untracked).

#### 12.7.3 Korbit-specific 결정 사항 (Bithumb-Coinone 혼합 패턴 5개)

1. **K4 heartbeat = Bithumb mirror (WS protocol `ws.ping()`)** — K-2 smoke `ws.ping()` 5회 모두 정상 동작 확인 (median latency 14.64ms, timeout 0). Coinone의 app-level event-based PING/PONG (`{request_type:"PING"}` + `_pong_event` synchronization) **불필요**. K4 작업량이 Coinone C4 대비 대폭 축소.
2. **K3 parser — unified ACK/ERROR shape via `status` 분기**:
   - Subscribe ACK: `{status:"success", requestId}`
   - Error: `{status:"fail", code, message, requestId}`
   - Parser 분기: `if "status" in frame: handle_ack_or_error(frame)` 단일 path (CONNECTED/SUBSCRIBED/DATA 별도 frame 분기였던 Coinone과 다름)
   - Ticker: `if frame.get("type") == "ticker" and frame.get("symbol") == "usdt_krw": parse_ticker(frame["data"])` → rate = `float(data["close"])`, timestamp = `int(data.get("lastTradedAt") or frame["timestamp"])`
   - `snapshot` 필드는 첫 frame만 `true`, 이후 key 자체 누락 → **분기 불필요** (가격은 동일 추출)
3. **2-signal 분리 결정 (Coinone §12.6.3 패턴 재사용)** — connection liveness vs price freshness 분리는 source-neutral 결정이므로 Korbit에도 동일 적용. 단 absolute threshold는 다름:
   - **connection alive signal**: `ws.ping()` pong_waiter 기반. **기본값 `PING_INTERVAL_SEC=300` (Coinone/Bithumb 통일)**, `PONG_TIMEOUT_SEC=5`. canary에서 조정 가능. Korbit은 10s ticker가 꾸준하면 ticker 자체가 liveness signal 역할도 하므로 PING은 백업 성격 (ticker-stalled 상황에서만 reconnect 판단의 주요 근거).
   - **price freshness signal**: DATA frame age. K-2 관찰 결과 **약 10초 간격 publish** (median 10.02s, p95 10.04s, max 10.13s — 30분 활발 시간대). 공식 보장 미확인이므로 provisional thresholds:
     - `TICKER_FRESHNESS_WARNING_SEC = 30s` (10s × 3, 3회 연속 누락 시 warning, log only)
     - `TICKER_FRESHNESS_DEGRADED_SEC = 120s` (10s × 12, REST probe trigger)
   - **Coinone (60s/300s)와 다른 절대값**: Coinone은 "값 변경 시 push" 모델 (median 1.4s, max 30s)이라 60s/300s가 적정. Korbit은 활발 시간대 관찰상 throttled publish로 보여 더 짧은 threshold가 자연. canary sparse-time 자연 누적 후 정확값 확정.
4. **K6b REST normalized helper — Coinone/Bithumb과 동일 contract** — REST 응답에 top-level `timestamp` 없음 (`rest_has_timestamp: false`)이지만 normalized tick contract는 source-neutral 유지. `fetch_korbit_usdt_tick()` shape: `{"source": "korbit", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}`. 내부 구현에서 `rate = float(data["data"][0]["close"])`, `timestamp_ms = int(data["data"][0]["lastTradedAt"])` (timestamp source는 `lastTradedAt` — Coinone의 top-level `timestamp` field와 source가 다르지만 contract는 동일). C6b/C7 fanout에서 source-specific 분기 회피.
5. **`data.lastTradedAt` single source + fallback 보존** — K-2 활발 시간대 sample 213/213 모두 존재 (missing ratio 0.0). 그러나 sparse-time에서 거래 없는 동안 `lastTradedAt`이 stale 또는 null 가능성 미확정 → parser는 single source 사용하되 `int(data.get("lastTradedAt") or frame["timestamp"])` 보수적 fallback 유지.

#### 12.7.4 Stage 분할 — K1-K7 (Phase B.4 C1-C7 패턴 mirror, K4 축소)

> Stage **K-2 (ad-hoc smoke, 완료)**는 본 plan의 input. Stage **K1~K7** 7-stage가 작업 단위 (Phase B.4 C1~C7과 대칭).

| Stage | Scope | 변경 파일 | Tests |
|---|---|---|---|
| **K-2** | ad-hoc Korbit WS smoke 30분 valid + ~30s invalid (완료 2026-05-19) | `/tmp/korbit_ws_smoke.py` + `/tmp/korbit_ws_smoke_invalid.py` (untracked) | — (관찰) |
| **K1** | 본 §12.7 Phase B.5 section 신설 (계획 잠금) | `USDT_WS_DESIGN_PLAN.md` | — (docs) |
| **K2** | `USDT_WS_KORBIT_ENABLED=false` env + `app/crawlers/usdt_ws/korbit.py` lifecycle skeleton + scheduler hook | `app/config.py`, `app/crawlers/usdt_ws/korbit.py` (신규), `app/scheduler.py` | skeleton lifecycle tests (flag=false 무동작 + flag=true skeleton log emit) |
| **K3** | Korbit WS connect/subscribe/parse + log only — **list-wrap subscribe build** + ticker parser (`type=="ticker"` + `symbol="usdt_krw"` + `data.close` + `data.lastTradedAt`) + unified ACK/ERROR `status` 분기 + snapshot 무시 처리 | `korbit.py` | parse/connect tests, status 분기 검증 (success/fail), ACK shape, snapshot key 누락 처리 |
| **K4** | **Connection liveness (`UsdtLivenessMonitor` source-neutral 재사용)** + **`ws.ping()` WS protocol heartbeat (Bithumb U4 mirror)** + **2 status 분리** (`_connection_status` / `_ticker_freshness_status`, Coinone C4 패턴 재사용) + ticker freshness telemetry (30s warning, transition 기반 1회 log, action X) + reconnect loop | `korbit.py` | ws.ping() pong_waiter / status 전이 + log throttle / ticker silence no reconnect / scope guard / constants tests |
| **K5** | `KorbitRedisWriter` + topic trigger 자동 발화 (`request_tether_topic_trigger` source-neutral hook 재사용) | `korbit.py` | redis writer tests |
| **K6** | `KorbitDbWriter` + 1s window debounce + `KorbitRestFallbackController` (price freshness signal trigger, cooldown 120s) + **`fetch_korbit_usdt_tick()` normalized REST helper** (`_fetch_korbit` rate-only 보강 — Coinone C6 패턴 mirror, contract `{source, asset, rate, timestamp_ms}` 동일 유지, 내부 timestamp source는 `data["data"][0]["lastTradedAt"]`) | `korbit.py`, `app/crawlers/usdt_sources.py` | db writer + fallback tests + REST helper shape |
| **K7** | Korbit alert evaluator wiring (`UsdtAlertEvaluator` 재사용, `AlertObservation(source="korbit", asset="usdt-krw", kind="tick"\|"rest_probe")`) | `korbit.py` | alert wiring tests + close/drain order + flag=false invariant |

**Stage 정당화**:

- Phase B.4 C1-C7 7-stage 패턴 mirror (K1-K7). K-2 smoke는 stage 명명에서 빼서 Coinone과 대칭 유지.
- **K4 신규성 정확화**: Coinone C4 대비 **대폭 축소** — application-level PING/PONG event-based 코드 (`_pong_event` synchronization, ping send/wait_for) 불필요. K-2 smoke 검증 결과 `ws.ping()` pong_waiter 5회 정상 (Bithumb U4 패턴 1:1 mirror 가능). 2-signal 분리 자체는 Coinone C4 패턴 재사용 (`UsdtLivenessMonitor`의 `last_activity_at = max(tick, heartbeat)` 이미 2-signal 의식 — 확장 없음). per-source threshold module-level constants는 Korbit 절대값으로 (`PING_INTERVAL_SEC=300` / `PING_TIMEOUT_SEC=5` / `STALE_AFTER_SEC=360` / `TICKER_FRESHNESS_WARNING_SEC=30` / `TICKER_FRESHNESS_DEGRADED_SEC=120` / `FALLBACK_COOLDOWN_SEC=120`).
- **K3 = unified ACK/ERROR via `status` 분기 + list-wrap subscribe build**. Coinone C3 (CONNECTED/SUBSCRIBED 별도 frame 분기) 와 다른 단순한 path. wire format은 신규 작성 (list-wrap, Bithumb dict/Coinone dict 모두 비호환).
- **K5/K6/K7 = Coinone C5/C6/C7 거의 1:1 mirror** — source-neutral hook (`request_tether_topic_trigger`, `UsdtAlertEvaluator`, `AlertObservation`) 재사용. K6의 REST normalized helper는 contract (`{source, asset, rate, timestamp_ms}`) Coinone/Bithumb과 동일 유지 — 내부 timestamp source만 `lastTradedAt` (Coinone top-level `timestamp` 대신).

#### 12.7.5 Canary 활성화 조건 + 운영 status

> 📅 **활성화 시점**: 2026-05-20 04:15:18 KST (Coinone canary 24h+ 미달 ~10h 시점, 2-step deploy 패턴 적용하여 선행 안정 검증 후 활성화)

**원래 선행 조건** (계획 잠금 시점):

- `USDT_WS_KORBIT_ENABLED=false` default — Stage K2-K7 코드 land + push 누적 시 운영 영향 0
- Phase B.4 Coinone canary 운영 안정 (5/19 18:42 활성화 후 24h+ 자연 누적, sparse-time max ticker_update_gap 관찰 — §12.6.5)
- Phase B.5 K2~K7 코드 land + 외부 검토 통과
- 활성화 후 24h+ 자연 누적 → sparse-time DATA silence 패턴으로 `TICKER_FRESHNESS_DEGRADED_SEC` / `FALLBACK_COOLDOWN_SEC` provisional 120s 정확값 확정 (필요 시 별도 작은 commit)
- 1주 운영 안정 검증 후 Phase B.6 (Gopax) 진입. 공통화 검토는 Phase B.6까지 5개 거래소 전부 land 후 별도 시점에 진행 (§12.6.5/§12.6.7 결정 mirror).

**활성화 결정 (2026-05-20 사용자 판단)** — Coinone 24h+ 미달이지만 검증 단계 통과 후 활성화:

- ✅ Step A — Coinone 10h+ 누적 운영 안정 (container Up 9h healthy / error/reconnect/transition 0건 / Redis fresh / DB last gap 46s 정상)
- ✅ Step B — K2~K7 코드 prod deploy (git pull `9da2beb..afc6978` → docker build → force-recreate **flag=false 유지**). Import OK / ERROR 0건 / 기존 Upbit/Bithumb/Coinone 재attach 정상 / Korbit `skip start` 1줄만 (10분 안정 관찰 ERROR 0건)
- ✅ Step C — Korbit `USDT_WS_KORBIT_ENABLED=true` 활성화 + force-recreate
- **선활성화 근거**: (1) 2-step deploy (flag=false → flag=true) 패턴으로 import/startup regression 사전 차단 (Coinone canary 활성화 lesson 적용) (2) Coinone 10h sparse-time 안정 + Step B 10분 추가 안정 검증으로 환경 영향 0 확인 (3) 사용자 앱 영향 0 — 앱 테더 탭 미배포, backend canary side effect는 허용 범위 (4) 이상 시 즉시 rollback 가능 (`env=false` + `--force-recreate`, §12.7.6) (5) Korbit publish ~10s 모델로 sparse-time 자연 누적 가능

**활성화 lesson** (Coinone §12.6.5 lesson + 2-step deploy 추가):

- **2-step deploy 패턴** (Codex 권장, Coinone Canary lesson 발전):
  1. Step B: `USDT_WS_KORBIT_ENABLED=false` 유지 + `docker compose build fastapi` + `docker compose up -d --force-recreate fastapi` — 신규 모듈 import/startup regression 사전 차단 (Coinone canary 활성화 시 `ModuleNotFoundError` 사고 패턴 회피)
  2. 10분 안정 관찰 — 기존 Upbit/Bithumb/Coinone 재attach 정상, Korbit `skip start` 1줄만 확인
  3. Step C: `.env`에 `USDT_WS_KORBIT_ENABLED=true` 추가 + `docker compose up -d --force-recreate fastapi`
- **CLAUDE.md `## Docker 배포 및 관리` 변경 종류별 절차 표 일관**: 코드 변경 + env 변경 양쪽 모두 build → force-recreate
- **Coinone canary lesson 발전형**: Coinone은 `git pull` + `force-recreate`만 시도하여 `ModuleNotFoundError` 사고 발생 → `build` 누락 발견. Korbit는 2-step deploy로 build 누락도 사전 차단.

**활성화 후 초기 운영 status** (04:15:18 → ~04:16:23, ~65초):

- container Up after recreate, healthy
- Korbit sequence 1초 내 완료: KorbitWsClient task 시작 → start (K4 reconnect loop) → connected url=`wss://ws-api.korbit.co.kr/v2/public` → subscribed symbol=usdt_krw requestId=1 → subscribe ACK → first tick (K-2 smoke 시나리오 1:1 재현)
- Coinone 재attach 정상: CoinoneWsClient task 시작 → C4 → connected (`wss://stream.coinone.co.kr`) + subscribed channel=TICKER topic=KRW/USDT + CONNECTED session_id `5317f0a9-...` (새 session) + first tick 04:15:24
- ERROR/Traceback/ModuleNotFoundError 0건
- Redis 4 source 모두 fresh:
  - upbit: 1490.0 @ 04:16:22 KST (mirror_lag ~170ms)
  - bithumb: 1489.0 @ 04:16:18 KST (mirror_lag ~250ms)
  - coinone: 1490.0 @ 04:16:19 KST (mirror_lag ~8ms)
  - **korbit: 1490.0 @ 04:16:13 KST (mirror_lag ~10s — K-2 관찰된 ~10초 publish cadence와 일관)**
- Korbit DB rows last 2m: 0건 — 가격 stable (1490 고정) + `insert_source_rate_if_changed` 정상 동작. §12.6.5 Coinone lesson과 동일 (가격 미변경 시 INSERT skip)

**남은 작업** (24h+ 운영 관찰 영역):

- sparse-time max ticker_update_gap 결과로 `TICKER_FRESHNESS_DEGRADED_SEC` / `FALLBACK_COOLDOWN_SEC` provisional 120s 정확값 확정 — 필요 시 별도 작은 commit (상수 1~2줄 수정)
- Korbit 10초 publish 모델이 sparse-time에도 유지되는지 검증 (K-2 30분 활발 시간대 sample base, sparse-time guarantee X — Codex 강조 톤 유지)
- reconnect/transition log monitoring (Coinone 12h 0건 패턴 mirror)
- 1주 운영 안정 검증 후 Phase B.6 (Gopax) 진입 결정. 공통화 검토는 Phase B.6까지 5개 거래소 전부 land 후 별도 시점에 진행 (§12.6.5/§12.6.7 결정 mirror).

**활성화 직후 진단 lesson (2026-05-20)** — Redis JSON 해석 오진 사례:

활성화 ~1h 30분 시점 진단에서 Korbit Redis의 `timestamp` 필드를 "WS freshness 지표"로 잘못 해석하여 rollback/restart 권장 (오진). 실제로는 정상 운영. Codex 정정으로 발견.

**원인 — Redis JSON 필드 의미가 source-specific**:

- Korbit `timestamp`는 `data.lastTradedAt` 기반이라 sparse-time 체결 없는 동안 정지할 수 있다 (마지막 체결 시각 유지).
- Coinone `timestamp`는 `data.timestamp` (frame publish 시각에 가까움, `mirrored_at`과 거의 동일).
- `mirrored_at`은 우리 system이 Redis SET한 시각 — **모든 source 일관** (WS frame 도착 시점).

**WS health 진단 기준 (cross-source 일관)**:

- WS health는 source별 `timestamp`가 아니라 cross-source 일관 필드인 `mirrored_at` 갱신 패턴으로 판단한다.
- 15초 간격 GET 두 번으로 `mirrored_at` 갱신 확인 (Codex 권장 진단 명령):

  ```bash
  docker compose exec -T redis sh -lc 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" GET "latest:source:korbit:usdt-krw"'
  sleep 15
  docker compose exec -T redis sh -lc 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" GET "latest:source:korbit:usdt-krw"'
  ```
- **`docker compose exec`은 fastapi container 안에서 별도 Python process로 실행** — `from app import scheduler; scheduler.usdt_ws_korbit_client`로 globals 직접 확인 시도해도 새 process라 globals 초기 (None) 상태. **운영 중인 FastAPI process 상태와 무관**. introspection은 logs/Redis/DB로만 가능.

**관찰 사례 (2026-05-20 ~05:51, 활성화 후 ~1h 30분)**:

- `mirrored_at`: 05:51:45 → 05:51:55 → 05:52:14 (약 10~20초 간격으로 갱신되어 WS frame 수신과 Redis write가 정상임을 확인) ✅
- `timestamp`: 05:51:04 → 05:51:04 (체결 없음, 정지) → 05:52:14 (체결 발생 시점 갱신) ✅
- ERROR/transition/reconnect log 0건 ✅

**추가 정상 패턴 (오진 회피)**:

- `_ticker_freshness_status` transition log 0건 = frame receive 기준 freshness 정상 (lastTradedAt 정지와 무관, K4 `_liveness.observe_tick(time.time())`은 frame receive time 기준).
- DB rows 0건 = 가격 stable + `insert_source_rate_if_changed` skip (Coinone §12.6.5 lesson과 동일 패턴).

**Korbit canary 운영 누적 관찰 (2026-05-21 ~07:28 KST 시점 갱신)**:

- 활성화 5/20 04:15 → 2026-05-21 ~07:28 시점에 **누적 ~27h, 24h+ 기준 통과**
- **Korbit reconnect 2회 모두 자동 회복 — K4 reconnect loop + backoff sequence 운영 정상 작동 검증**:
  - 1차 2026-05-20 12:01:20 KST:
    - log: `connection_status normal → reconnecting (reconnect_attempt=1 max_gap=10.60s)` → `connection closed (attempt 1): no close frame received or sent — backoff 1.0s` → 1초 후 `connection_status reconnecting → normal`
    - close reason: "no close frame received or sent" (server-side abrupt close 추정)
  - 2차 2026-05-21 01:31:47 KST (1차 대비 ~13h 30분 간격):
    - log: `connection_status normal → reconnecting (reconnect_attempt=2 max_gap=10.82s)` → `connection closed (attempt 2): received 1001 (going away) CloudFlare WebSocket proxy restarting; then sent 1001 (going away) — backoff 2.0s` → 2초 후 `connection_status reconnecting → normal`
    - close reason: server-side / CloudFlare WebSocket proxy restart로 관찰 (close code 1001 "going away"). 정기성 / cadence는 2회 관찰 만으로 단정 X — 추가 누적 필요.
  - 두 case 모두 backoff sequence `[1, 2, 4, 8, 16, 30]` 정상 적용 (1차 1.0s, 2차 2.0s = `reconnect_attempt` index 따라). 회복 시간 모두 backoff sec 직후 1 iteration.
  - max_gap 두 case 유사 범위 (10.60s / 10.82s) — backoff 적용 직전 마지막 frame age. summary log 부재로 reconnect 외 시점 historical max_gap 산출 제한 (별도 후속 PR 영역).
- **Bithumb 단기 sample 관찰 (참고만, 통계 결론 보류)**:
  - 5/20 ~19:57 시점 15초 간격 5-sample 중 1회 ~20초대 `mirrored_at` gap 관찰. 같은 sample 구간 transition / error 0건.
  - 5-sample은 통계 의미 약함. "정상 범위" 단정 X. summary log 누적으로 통계 산출 필요.
- Coinone/Upbit는 같은 sample 구간 `mirrored_at` ~10ms~수초 cadence 갱신, transition / error 0건 관찰.

**운영 관찰 lesson + summary log 부재 한계**:

- 관찰 기준 5개 (cross-source 일관, 운영에서 계속 봐야 할 항목):
  1. `mirrored_at` source별 예상 cadence 안에서 갱신되는지
  2. `ticker_freshness_status` warning/degraded 전이 발생 여부 + 빈도
  3. reconnect 발생 + 자동 회복 여부 + 반복 패턴
  4. Redis write queue saturation 단발 vs 반복
  5. REST fallback probe 발생 빈도 (과다 여부)
- **한계 — historical max frame/tick gap 산출 제한**:
  - USDT WS에는 KRX kis_ws `_summary_log_loop` ([app/crawlers/krx_kis.py](app/crawlers/krx_kis.py))같은 분당 metric emit이 **부재**.
  - `_liveness.max_frame_gap_sec` 등 process memory에는 누적되지만 외부 노출 (logs/admin api/Redis) 없음.
  - `docker compose exec ... python -c "..."`은 별도 process 진입이라 globals 초기 (None) 상태 — 운영 중인 FastAPI process introspection 불가 (§12.7.5 진단 lesson 참조).
  - 현재 가용 max gap 데이터는 transition log 발생 시점에 한정 (예: 위 Korbit reconnect 시점 `max_gap=10.60s` — 운영 기간 전체 max 아님).
  - DB row gap은 `insert_source_rate_if_changed` 특성으로 "가격 변경 저장 간격"이지 "tick gap"이 아님 (§12.6.5 lesson 동일).
- **후속 PR 필요성**:
  - KRX kis_ws `_summary_log_loop` 패턴 기반 source별 summary log 추가 — 분당 1회 `[usdt_ws.<source>] metrics frames_per_min=N max_frame_gap=Ns last_tick_age=Ns reconnect_attempts=N status_transitions=... redis_saturation=N fallback_probe=N` emit.
  - 단계적 적용 권장 (Codex): 하나 source (Korbit 또는 Coinone)에 먼저 검증 → Upbit/Bithumb 확장. 4 source 일괄은 review 부담 + bug risk.
  - 운영 영향 0 (log emit만 추가). threshold 조정 input 자연 누적 + reconnect/saturation 패턴 monitoring + admin api endpoint 확장 input.

#### 12.7.6 Rollback 정책 (Phase B.4 §12.6.6 패턴 재사용)

- **1차 수단**: env toggle `USDT_WS_KORBIT_ENABLED=false`. lifecycle 격리 (Upbit/Bithumb/Coinone/KRX 검증 완료 패턴).
- **2차 수단**: 코드 revert. lifecycle 의존성 깨질 위험 있어 1차 실패 시 한정.
- **process 재생성 필수**: `docker compose up -d --force-recreate fastapi` (env_file 변경 반영, `restart`로 부족 — CLAUDE.md 변경 종류별 절차 표 참조).

#### 12.7.7 후속 phase 목록 + 보충 smoke (Phase B.5 범위 외)

**보충 smoke / 운영 관찰 (Phase B.5 land 후 자연 진행)**:

- **새벽/주말 sparse-time 관찰** — 별도 ad-hoc smoke 대신 Phase B.5 canary 활성화 후 운영 데이터로 자연 누적. DATA silence 패턴 검증 + threshold 정확값 확정 input:
  - `TICKER_FRESHNESS_WARNING_SEC = 30s` (provisional, 10s × 3)
  - `TICKER_FRESHNESS_DEGRADED_SEC = 120s` (provisional, 10s × 12)
  - `FALLBACK_COOLDOWN_SEC = 120s` (degraded threshold와 동일 — provisional)
  - 둘 다 sparse-time 운영 데이터로 정확값 확정. 필요 시 상수 1~2줄 수정 별도 commit.
- **공식 ticker publish 모델 검증** — K-2 smoke에서 "약 10초 간격 publish" 관찰됐으나 공식 보장 미확인. canary 24h+ 자연 누적에서 평일/주말 모두 동일 패턴이면 운영 안정성 강화. 만약 sparse-time에서 silence가 길어지면 threshold 조정.

**후속 phase**:

| Phase | Scope | 비고 |
|---|---|---|
| **Phase B.6** (예정) | Gopax WS 확장 | 전체 ticker 구독 + 서버 필터링 + Primus `::ping::` 30s (가장 다른 구조) |
| **공통화 검토** | 거래소 base class / shared lifecycle | **Phase B.3 + B.4 + B.5 + B.6 모두 land 후 (5개 거래소 전부)** 중복 명확해진 시점 판단. Korbit의 unified `status` 분기 + 10s throttled publish + list-wrap subscribe는 Bithumb/Coinone과 또 다른 axis. Gopax (전체 ticker 구독 + Primus protocol) 까지 본 후 base class 결정. 선제 abstraction 금지 (KRX close finalizer 큰 PR 학습).

후속 phase 진입 시점 + 순서는 Phase B.5 운영 안정 측정 + B.6 land 후 결정.

### 12.8 Post-5-source 정책 표준화 backlog (2026-05-21 추가)

**컨텍스트 / 분리 원칙**:

현재 진행 중인 source별 summary log + counter 작업은 **관찰 인프라 구축**이다.
Upbit/Bithumb의 1-dim `_status` 모델과 Coinone/Korbit의 2-dim
`_connection_status` + `_ticker_freshness_status` 모델 사이의 비대칭 해소,
그리고 REST fallback 정책 표준화는 자동 진행하지 않는다. 5 source 관찰 데이터가
누적된 뒤 본 backlog의 결정 절차에 따라 별도 후속 작업으로 진행 여부를 결정한다.

**Entry 조건**:

- Upbit summary log + `redis_saturation_count` 운영 적용
- Korbit summary log + `fallback_probe_scheduled_count` 운영 적용
- Bithumb summary log + `redis_saturation_count` + `fallback_probe_scheduled_count` 운영 적용
- Coinone summary log + 필요한 counter 운영 적용
- Gopax WS land 후 동일 telemetry 적용 여부 결정 및 5 source 관찰 인프라 완성
- 5 source 모두 안정 운영 후 24~72h 관찰 누적

**분석 input**:

- observed frame gap 분포 (`max_frame_gap`, 시간대별)
- status transition 빈도 (`status_transitions`)
- `redis_saturation_count` 발생 패턴
- `fallback_probe_scheduled_count` 발화 빈도
- `reconnect_attempts` 빈도

**결정 항목**:

1. Upbit/Bithumb의 단일 `_status` 모델을 Coinone/Korbit식
   `_connection_status` + `_ticker_freshness_status` 2-signal 모델로 전환할지 결정.
2. REST fallback trigger를 connection stale 기준에서
   `ticker_freshness_status=degraded` 기준으로 표준화할지 결정.
3. warning/degraded/cooldown 임계값을 observed frame gap 분포 기반으로 재산정할지 결정.

**결정 원칙**:

- 진행 / 미진행 둘 다 합법 결론이다.
- 표준화는 운영 일관성 목적이며, 기능 부재 해소가 아니다.
- 비용이 가치보다 크면 source-specific 정책을 유지한다.
- 선제 abstraction 금지 원칙을 따른다. 관찰 데이터로 정당화된 변경만 진행한다.
- 이 원칙은 §12.6 "공통화 검토"의 선제 abstraction 금지 lesson (KRX close finalizer
  큰 PR 학습)과 정렬된다.

**결정 시 적용 절차**:

- 결정 yes: 별도 PR로 적용한다.
- 결정 no: 데이터 분석 요약과 함께 close한다.
- 결정 보류: 추가 관찰 기간과 재검토 시점을 명시한다. 무기한 보류하지 않는다.

#### 12.8.1 Stage 6 Close (2026-05-23)

**관찰 기간**: container 1 (14h baseline, §12.9.6) + container 2 (29h+ 실측, 2026-05-22 13:27 ~ 2026-05-23 19:06 KST) = **두 운영 구간 합산 참고 ~43h**. 동일 컨테이너 연속 24h 누적은 아니지만, 분석 close에 충분한 근거로 사용.

**snapshot 핵심** (snapshot 시점 2026-05-23 19:06 KST):

- Upbit: status=normal, reconnect=0, transitions all 0, saturation 25회 (§12.8.2 참조)
- Bithumb: status=normal, reconnect 1회 자기 회복, transitions all 0
- Coinone: status=normal, reconnect=0, transitions all 0
- Korbit: status=normal, reconnect 6회 자기 회복 (합산 참고), ticker transitions all 0
- Gopax: ticker normal:82/warning:82/degraded:41, REST fallback **두 운영 구간 합산 참고 61회 scheduled, failure 0 관측** (확인된 probe log 53/53/0), worst max_frame_gap=5406s (90분), connection reconnect 1회 자기 회복 (container 2 시작 직후, 1초 내 reconnecting → normal 복귀)

**결정 항목별 close**:

##### 결정 항목 1 — Upbit/Bithumb 2-signal 전환

- **분류: 더 관찰 (결정 보류)**
- **근거**: 24h+ 합산 참고에서 Upbit/Bithumb의 1-dim `status`로 reconnect (Bithumb 1회) 정확 capture. 1-dim에서 분해 못 한 case (연결 문제 vs ticker freshness 분리 필요 사례) 0건.
- **본질**: 2-signal은 장애 원인 분해력 향상 (연결 문제 vs ticker freshness 분리). 표준화 목적만으로는 코드 변경 대비 운영 이득 작음.
- **재검토 조건**: 1-dim status로 원인 분해가 어려운 실제 장애/운영 신호 발생 시 또는 추가 72h 관찰 후 동일 패턴 유지 시 결정 close.

##### 결정 항목 2 — REST fallback trigger 표준화 (stale → ticker_freshness_status=degraded)

- **분류: source-specific 정책 공식화 (결정 no)**
- **근거**: 5 source 모두 REST fallback 보유 (Upbit는 `normal → stale` 전이 trigger / Bithumb·Coinone·Korbit·Gopax는 fallback controller 보유). Gopax는 두 운영 구간 합산 참고 fallback 61회 scheduled, failure 0 관측 (`ticker_freshness_status=degraded` trigger). Upbit·Bithumb·Coinone·Korbit는 이번 관찰 구간에서 fallback 발화 신호 없음 (Upbit는 stale 전이 0, 다른 source는 fallback counter 0). 5 source 모두 ~43h 안정 운영.
- **본질**: source별 거래량 특성 차이 (sparse vs dense traffic). Gopax 저유동성 = REST fallback이 의미 있는 보완 경로. 다른 source는 dense traffic으로 fallback 의미 약함.
- **source-specific 정책 영구 기록**:
  - **Gopax**: REST fallback `ticker_freshness_status=degraded` trigger (2-signal model), 운영 가치 검증됨 (probe 61회 scheduled / failure 0 관측)
  - **Upbit**: REST fallback `normal → stale` 전이 trigger (1-dim model, `UpbitRestFallbackController`). 이번 관찰 구간에서 stale 전이 0 — 발화 신호 없음, 안전망으로 보유
  - **Bithumb/Coinone/Korbit**: REST fallback 보유, 이번 관찰 구간에서 counter 0 — 안전망 성격으로 유지

##### 결정 항목 3 — warning/degraded/cooldown threshold 재산정

- **분류: source-specific 정책 공식화 (결정 no)**
- **근거**: ~43h 합산 참고에서 source별 max_frame_gap 분포 극심 차이 (10s ~ 5406s). Gopax 300/600 (post-activation 실측 기반 tuning, commit `758c21c`) 적합 검증됨 (DEGRADED=600s가 90분 무tick 시 fallback trigger 정상 동작). 다른 source threshold도 각자 거래량 특성에 맞춤.
- **본질**: threshold는 source별 거래량/heartbeat 패턴 함수. 통합 시 trade-off (저유동성 source false positive vs 고유동성 missed signal).
- **source-specific 정책 영구 기록**:
  - **Gopax**: `WARNING=300s / DEGRADED=600s` (저유동성, sparse traffic 적합)
  - **Coinone**: `WARNING=60s / DEGRADED=300s`
  - **Korbit**: `WARNING=30s / DEGRADED=120s`
  - **Bithumb / Upbit**: 각자 거래량 특성에 맞춤 정책

##### 참고 note — fallback failure alert

- **분류: future alert 후보 (§12.8 공식 항목 아님)**
- **근거**: 합산 참고 Gopax probe 61회 scheduled / failure 0 관측. alert 트리거 신호 없음.
- **재검토 조건**: Gopax failure rate > 0% 발생 시 alert 트리거 검토. 다른 source에서 fallback 활성화 후 failure 발생 시 동일 검토.

#### 12.8.2 Follow-up Backlog — Upbit redis_saturation_count 분석 (2026-05-23 추가)

**컨텍스트**: §12.8의 분석 input #3 (`redis_saturation_count` 발생 패턴) 영역에 ~43h 관찰 중 **Upbit saturation 25회** 발견 (snapshot 시점 2026-05-23 19:06 KST). §12.8 공식 결정 3 항목 분류는 유지하되, 별도 follow-up backlog로 추적.

📌 **상위 anchor**: 본 follow-up의 중기 구조 개선(freshness metadata 분리 + 의미 있는 payload/state 변화 시 trigger)은 [REALTIME_ARCHITECTURE_PLAN.md §4.1 "All-source observation fanout contract"](REALTIME_ARCHITECTURE_PLAN.md)에 anchored된 통합 phase에 속한다. 용어(`rate_changed_at` / `seen_at` / `mirrored_at`)는 §4.1.3 정의를 따른다.

**관측 패턴 (총 6 burst, ~43h)**:

- 2026-05-22 23:21 KST: 5회 (fpm 289까지 상승)
- 2026-05-23 03:37 KST: 8회
- 2026-05-23 03:46 KST: 3회
- 2026-05-23 04:30 KST: 5회 (fpm 1297까지 상승)
- 2026-05-23 16:46 KST: 4회
- 모든 burst가 dense traffic spike와 정확한 상관. **status=normal, reconnect=0, transitions all 0, error 0** — 운영 안정성 영향 없음
- 다른 source 영향 없음 (Upbit 단독)

**원인**: Upbit는 모든 tick 수신 구조. 짧은 순간 거래/호가 변동 burst 시 Redis write queue (MAX_PENDING_WRITES=20)가 saturation. skip된 tick의 latest path는 후속 tick/direct write 및 기존 mirror/warmup 경로가 보완할 수 있으나, skip tick 자체의 최신성 영향은 follow-up에서 확인.

**follow-up 후보 (단기 / 중기 구조 분리)**:

**단기 — pending write 임계값 재산정**:

- `MAX_PENDING_WRITES=20` 상향 검토 (예: 50 또는 100)
- 메모리 vs 부하 trade-off 평가
- 빠른 적용 가능, 구조 변경 없음

**중기 — Redis write dedup + freshness metadata 분리**:

같은 rate 반복 tick을 Redis에 반복 SET하는 현재 구조는 dense traffic source의 saturation burst 원인이지만, 단순 dedup ("rate 같으면 Redis 안 쓴다")만으로는 timestamp 의미가 "마지막 관측 시각"에서 "마지막 가격 변경 시각"으로 변질되어 단말/payload에서 source가 살아 있음에도 오래된 데이터처럼 보일 위험이 있다. 따라서 freshness metadata 분리가 함께 필요하다.

권장 분리 구조 ([REALTIME_ARCHITECTURE_PLAN.md §4.1.3](REALTIME_ARCHITECTURE_PLAN.md) 단일 정의 anchor 준수):

- **`rate_changed_at`**: 가격 실제 변경 시각
- **`seen_at` / `last_tick_at`**: 소스 살아 있음을 마지막 확인 시각 (같은 rate라도 매 tick 갱신)
- **`mirrored_at`**: Redis/topic 반영 시각

단계별 정책:

- tick freshness 관찰: 매 tick 기준 유지
- alert evaluator: 현재 tick observation 기반 정책 유지
- DB writer: 기존 debounce/change 정책 유지
- **Redis latest write: rate 변경 또는 의미 있는 timestamp/state 변화에만 update**
- **topic trigger: payload에 의미 있는 변화가 있을 때만 발화** (freshness 회복 / stale 복구 같은 state 변화 포함)

**All-source freshness metadata + fanout contract phase와 연결**:

본 follow-up의 중기 구조 개선 방향(`rate_changed_at` / `seen_at` / `mirrored_at` 분리 + 의미 있는 payload/state 변화 시 trigger)은 [REALTIME_ARCHITECTURE_PLAN.md §4.1 "All-source observation fanout contract"](REALTIME_ARCHITECTURE_PLAN.md) 아래 통합 phase의 일부다. 다음 source들이 같은 설계 축을 공유한다:

- **Bank/Investing**: [USDT_TOPIC_MIGRATION_PLAN.md §6.6](USDT_TOPIC_MIGRATION_PLAN.md) β 옵션 (DB-first monolithic → observation-based fanout)
- **KRX 미국달러선물**: [KRX_FANOUT_REFACTOR_PLAN.md §5.2 Stage E](KRX_FANOUT_REFACTOR_PLAN.md) (`KrxRedisLatestWriter` DB-insert-bound → tick-level 전환)

5 source + Bank/Investing + KRX 모두 같은 freshness metadata 정책 통합 시점에 함께 진입할 가치 있다.

**진입 조건**:

- 단기: 운영 위험성 평가 후 즉시 검토 가능 (현재 status normal 유지라 긴급도 낮음)
- 중기: All-source freshness metadata + fanout contract phase로 진입 (5 source + Bank/Investing + KRX 통합 — REALTIME §4.1 anchor 기반)

#### 12.8.3 Follow-up implementation decision — price alert coalescing (2026-05-24 합의)

**컨텍스트**: §12.8.2 follow-up의 implementation phase 진입 합의. (5a) coalescer 설계 + (5b) USDT 5 source alert path 적용 PR scope 결정. 본 항목은 §12.8.2의 후속 implementation decision으로, [REALTIME_ARCHITECTURE_PLAN.md §4.1 "All-source observation fanout contract"](REALTIME_ARCHITECTURE_PLAN.md) 안의 단일 가격 alert 영역. Redis freshness grain (정책 D — `seen_at` 5s wall-clock grain rounding)은 결정에는 포함하되 *별 sub-PR*로 분리 (§12.8.3.3 참조) — Redis value schema 변경 + topic payload/read path 영향이라 blast radius 분리.

📌 **상위 anchor**: terminology는 [REALTIME_ARCHITECTURE_PLAN.md §4.1.3](REALTIME_ARCHITECTURE_PLAN.md) 단일 정의 따른다. 본 항목은 USDT 5 source 단일 가격 alert path만 다룬다 — 비교 알림(Phase E)과 KRX/Bank/Investing 측 적용은 별도.

##### 12.8.3.1 12 결정 사항 (확정)

| # | 영역 | 확정 |
| --- | --- | --- |
| 1 | Redis write coalescing | 정책 D — `seen_at` 5s wall-clock grain rounding *(별 sub-PR — §12.8.3.3 참조, blast radius 분리)* |
| 2 | Alert window pattern | A-3 — wall-clock 5s grain (`floor(now/5s)*5s`). **Flush timing은 tick-driven** — 다음 tick의 bucket boundary 넘김 또는 `close()`에서 flush. 별도 5초 timer cron 없음 (§13.10 "5초마다 검사" 아니라 "5초 안 관측 묶어 1회 평가" 정합). 희소 source(예: Gopax fpm≈2) alert 평가가 다음 tick 도래까지 지연될 수 있음 — source-specific window 정밀화는 후속 결정 영역. |
| 3 | Window summary 필드 | `min_rate` / `max_rate` / `last_rate` / `window_start/end` / `tick_count` |
| 4 | Coalescer 위치 | 별도 모듈 (`app/notifications/price_alert_coalescer.py`) + UsdtAlertEvaluator composition (호출부 무변경) |
| 5 | Source-specific window 값 | USDT/KRX = 5s, Bank/Investing = 0 (pass-through) |
| 6 | Tick 없으면 평가 | skip (B1 once-only); B2 due path만 예외 |
| 7 | 비교 알림 | (5a) 영역 외 — Phase E 별도 evaluator (interface 흔적 X) |
| 8 | 반복 알림 (B2) | (5a) interface forward-compat만 (`Literal["price_window", "repeat_due"]`만 허용, `comparison_snapshot` 제외) |
| 9 | Evaluator input model | `PriceAlertEvaluationInput` dataclass + 단일 `schedule(observation)` entry (input_kind dispatch 내부) |
| 10 | condition_matches 분리 | `condition_matches_observation` (raw observation용 보존) + `condition_matches_price_input` (window용 신규) |
| 11 | triggered_rate 산출 | `matched_triggered_rate(setting, price_input) -> Decimal \| None` 통합 함수 (above=max_rate, below=min_rate) |
| 12 | `rest_probe` 처리 | Coalescer pass-through 즉시 평가 ("상태 변화/복구" 성격이라 burst 최적화 대상 외) |

##### 12.8.3.2 (5b) PR scope — USDT 5 source alert path 적용 (6 항목)

> ⚠️ Evaluator 내부 composition 방식이라 *5 source 자동 동시 적용*. Upbit-only 점진 도입이 필요하면 source-specific coalescer config로 별도 gating 해야 한다. 결정 #5 USDT/KRX = 5s 통일 정합.

1. `PriceAlertEvaluationInput` + `PriceAlertCoalescer` 신규 모듈 (`app/notifications/price_alert_coalescer.py`)
2. UsdtAlertEvaluator 내부 composition으로 coalescer 적용 (5 source 호출부 변경 없음)
3. `kind="tick"`만 5s wall-clock grain window coalesce
4. `kind="rest_probe"`는 pass-through 즉시 평가 (window 우회)
5. window 평가 시 `max_rate`/`min_rate` 기준 condition match, `triggered_rate` 분리 기록 (`_send_one()` + `_build_fcm_payload()` 시그니처 갱신)
6. 기존 5 source 호출부 무변경 — `schedule(AlertObservation)` public API 유지

##### 12.8.3.3 명시적 범위 외 (별 PR/ADR)

- **Redis freshness grain (정책 D `seen_at` 5s grain)**: 별 sub-PR — `latest_rates_cache.py` USDT helper schema 변경 (`rate_changed_at` / `seen_at` / `mirrored_at` 필드 추가) + topic payload/read path 영향. alert coalescing PR과 분리해 blast radius 축소. 결정표 #1과 함께 잠금.
- **비교 알림 (Phase E)**: `ComparisonAlertEvaluationInput` + `ComparisonAlertEvaluator`는 별도 evaluator로 도입. (5a) `PriceAlertEvaluationInput`에 *interface 슬롯 X*.
- **B2 `repeat_interval_sec` 실제 구현**: interface forward-compat만 (Literal type slot). 실제 schema/API/iOS/Android UI는 별 ADR.
- **B3 direction crossing**: B2 안정 후 별 ADR.
- **Redis ZSET threshold index**: §13.4 Phase 3 영역.
- **Bank/Investing β 옵션 적용**: [USDT_TOPIC_MIGRATION_PLAN.md §6.6](USDT_TOPIC_MIGRATION_PLAN.md) — DB-first monolithic → observation fanout 재설계 + freshness metadata 정렬 + main.py legacy hook 격하 가능성까지 포함하는 **대규모 refactor**. coalescer `window=0` pass-through는 본 phase 본체의 작은 하위 측면일 뿐 (≠ 본 phase scope). §6.6 본문 참조.
- **KRX Stage E 적용**: [KRX_FANOUT_REFACTOR_PLAN.md §5.2 Stage E](KRX_FANOUT_REFACTOR_PLAN.md) — KRX Redis DB-insert-bound → tick-level 전환 + freshness metadata 정렬. coalescer `window=5s` 적용은 본 phase 안의 하나의 측면.

##### 12.8.3.4 4 forward-compat 원칙 정합 (cross-ref)

본 결정은 §13.10 condition evaluation coalescing 정책의 USDT 5 source 측 implementation phase 진입. 4 원칙 매핑:

1. AlertObservation / evaluator interface를 단일 source 전용으로 굳히지 않기 — ✅ `AlertObservation` 그대로 + `PriceAlertEvaluationInput` 신규 (`kind="comparison"` 추가 X)
2. coalescing 구현을 단일 가격 전용 컴포넌트로 분리 — ✅ `PriceAlertCoalescer` 별 모듈
3. 미래 `ComparisonAlertEvaluator`는 Redis latest snapshot 기반 별도 evaluator — ✅ Phase E 범위 외 명시
4. "same-rate coalescing" 명칭은 단일 가격 내부 정책으로만, 상위 abstract = condition evaluation coalescing — ✅ §13.10 anchor 유지

### 12.9 Phase B.6 — Gopax WS 확장 (Primus protocol, 전체 ticker 구독)

Gopax는 기존 4 source 중 가장 다른 구조다. 전체 ticker 구독 + 클라이언트측
USDT-KRW 필터링 + Primus `::ping::` 30s heartbeat 때문에 WS protocol 신규성이 크다.
Phase B.6은 §12.6 (Coinone) / §12.7 (Korbit) 패턴을 참고하되, Gopax-specific
protocol 차이를 별도 stage로 분리해 진행한다.

#### 12.9.1 G1 scope (skeleton only)

**진입 범위**:

- GopaxWsClient minimal lifecycle (`_stop_event` / `_running` 만 보유 — Codex
  최종 권고로 `_connection_status` / `_ticker_freshness_status` /
  `_reconnect_attempt_count` / `_ws` 등 G2~G4 attribute는 본 stage 제외하여
  G4 설계 선반영 회피)
- USDT_WS_GOPAX_ENABLED feature flag (default false)
- scheduler.py start/shutdown helpers + globals (Bithumb/Coinone/Korbit 패턴 mirror)
- main.py lifespan 호출 추가
- tests/test_usdt_ws_gopax_skeleton.py 신규 (flag invariant + lifecycle skeleton +
  scope guard 검증)

**G1 acceptance**:

- USDT_WS_GOPAX_ENABLED=false 시 GopaxWsClient 생성 X + task 생성 X + network connect X
- flag=true 시 G1 placeholder lifecycle은 stop_event 대기만 (외부 network 호출 0)
- production 영향 0 — 다른 4 source lifecycle 변경 없음, default false로 deploy 안전

**명시적 제외 (G2~G7 + 별도 stage)**:

- G2: SubscribeToTickers + 2종 응답 parse (initial `SubscribeToTickers` array +
  delta `TickerEvent` dict) + USDT-KRW 클라이언트 필터링
- G3: Primus `::ping::` raw text matching + `::pong::` replacement 응답
- G4: UsdtLivenessMonitor 통합 + reconnect loop + status 차원 결정
  (Codex 권장: §12.8 backlog 정합 위해 2-signal 시작 — G4 단계에서 확정)
- G5-G7: GopaxRedisWriter / GopaxDbWriter / RestFallbackController / Alert evaluator
- PR 2e (옵션): Telemetry (summary log + saturation + probe counter)

#### 12.9.2 Gopax-specific 차이 (기존 4 source 대비)

| 항목 | Upbit/Bithumb | Coinone/Korbit | Gopax |
| --- | --- | --- | --- |
| Subscribe | 명시 코드 구독 | 별도 protocol, 명시 코드 | 전체 ticker `{"n":"SubscribeToTickers","o":{}}` (pair 지정 불가) |
| 필터링 | 서버측 | 서버측 | 클라이언트측 (recv 후 USDT-KRW 매칭) |
| Heartbeat 방향 | client-initiated `ws.ping()` | client-initiated App PING | **server-initiated** Primus `::ping::` 30s |
| Pong 응답 | WS frame | App PONG message | raw text replacement (`"primus::ping::"` → `"primus::pong::"`) |
| Heartbeat 미회신 영향 | client retry | client retry | **server disconnect** (30s 안 pong 미회신) |
| REST helper 현황 | normalized tick | normalized tick | `_fetch_gopax()` **rate-only** — G6b 진입 시 `fetch_gopax_usdt_tick()` 신규 작성 필요 |

#### 12.9.3 Canary 활성화 조건

Korbit/Coinone canary 24h+ 안정 + G2~G7 land 안정 후 별도 deploy GO. G1 단독은
production 영향 0이라 즉시 deploy 가능 (default false 유지).

#### 12.9.4 Rollback 정책

- **env toggle**: USDT_WS_GOPAX_ENABLED=false → lifecycle 즉시 비활성 (코드 변경 없이)
- **코드 issue**: `git revert <G1 commit>` + re-deploy
- **격리 보장**: 다른 4 source lifecycle 영향 0 (scheduler globals 분리)

#### 12.9.5 Phase B.6 완료 상태 (2026-05-22 갱신)

G1~G7 + PR 2e + activation + threshold tuning + heartbeat state cleanup까지 모두 land.

| Stage | Commit | 설명 |
| --- | --- | --- |
| G1 skeleton | `1ea4c74` | flag + scheduler + lifecycle (default false) |
| G2 Subscribe/Parse | `2b79a38` | `SubscribeToTickers` + USDT-KRW client-side filter |
| G3 Primus pong | `4ff528c` | server-initiated heartbeat raw text replacement |
| G4 reconnect/liveness/2-signal | `dd3e077` | `UsdtLivenessMonitor` 통합 + reconnect loop + connection/ticker_freshness 2-signal status |
| G5 Redis writer | `072ab0e` | tick-level fan-out + `_saturation_count` |
| G6a DB writer | `fa29bdc` | 1s window debounce + `insert_source_rate_if_changed` |
| G6b REST fallback | `fbf590d` | degraded→`schedule_probe` + 300s cooldown + `_scheduled_probe_count` |
| G7 Alert evaluator | `7047feb` | tick + REST probe → `AlertObservation` schedule + finally close drain |
| PR 2e Telemetry | `9c2b456` | 60s summary log emit, 10 fields (Coinone PR 2d mirror) |
| activation | (env) 2026-05-21 22:04 KST | `USDT_WS_GOPAX_ENABLED=true` 운영 토글 |
| threshold tuning | `758c21c` | post-activation 실측 기반 `WARNING=60→300` / `DEGRADED=300→600` (Gopax-specific, 거래량 최저 source) |
| heartbeat state cleanup | `1e8e184` | G3 `_last_heartbeat_at` 죽은 필드 제거 — heartbeat 추적은 `_liveness.last_heartbeat_at`로 일원화 |

#### 12.9.6 운영 baseline 스냅샷

**캡처 시각**: 2026-05-22 13:14 KST (container uptime 약 14h 31min, `758c21c` 배포 2026-05-21 22:43 KST 이후)

**5 source 운영 상태** (단일 summary cycle 캡처, `fpm`은 1분 window 측정치라 cycle마다 변동):

| Source | fpm | reconnect | status transitions | counters | max_frame_gap |
| --- | --- | --- | --- | --- | --- |
| Upbit | 88 | 0 | normal:0/reconnecting:0/stale:0 (1-dim) | saturation:0 | 48.3s |
| Bithumb | 19 | 0 | normal:0/reconnecting:0/stale:0 (1-dim) | saturation:0 / probe:0 | 98.5s |
| Coinone | 8 | 0 | conn 0/0/0 + ticker 0/0/0 (2-signal 6 keys) | saturation:0 / probe:0 | 30.2s |
| Korbit | 7 | **3** | conn normal:3/reconnecting:3/stale:0 + ticker 0/0/0 | probe:0 | 10.6s |
| Gopax | 2 | 0 | conn 0/0/0 + ticker normal:47/warning:47/**degraded:20** | saturation:0 / **probe:20** | **2437s** |

**Gopax fallback 운영 통계 (캡처 시점 누적, ~14h 30min)**:

- `scheduled_count` = `probe_start` = `probe_success` = ticker_degraded transitions = **20**
- `probe_failure / timeout / none / error` = **0** (REST fallback success rate 100%)
- `max_frame_gap` = 2437s ≈ 40분 무tick 구간 발생했으나 새 `DEGRADED=600s` + fallback이 freshness 보강 — 의도된 보완 경로 정상 동작
- 거래량 매우 적은 시간대(Gopax는 5 source 중 최저 fpm)에 정기적 fallback trigger — sparse traffic 보완 가치 검증

**Korbit 자기 회복**: 14h 운영 중 3회 reconnect 모두 `reconnecting → normal` 정상 복귀 (평균 4.7h 간격, alert 불필요한 자연스러운 disconnection 패턴)

**나머지 3 source (Upbit/Bithumb/Coinone)**: 전이 0, counter 0, 완전 안정.

#### 12.9.7 후속 작업 후보

- 5 source 24h+ 자연 누적 관찰 (다음 baseline 시점은 `1e8e184` cleanup 배포 후로 reset 예정)
- §12.8 Post-5-source 정책 표준화 backlog 진입 검토 (5 source 모두 land + activation 완료된 시점부터 가능)
- Gopax fallback failure rate trend tracking (현재 0% — future failure 발생 시 alert 또는 threshold 재조정 검토)
- 공통화 검토 (5 source 모두 land 후 별도 시점, §12.6 / §12.7 패턴 mirror — 선제 abstraction 금지 원칙 유지)

**우선순위 합의 (2026-05-25 추가)**: 5b/5d-a/legacy polling disable land 완료 후 [REALTIME_ARCHITECTURE_PLAN.md §4.1](REALTIME_ARCHITECTURE_PLAN.md) "All-source observation fanout contract" 통합 phase 진입 순서 합의 — **(1) [KRX Stage E](KRX_FANOUT_REFACTOR_PLAN.md) → (2) [Bank/Investing β](USDT_TOPIC_MIGRATION_PLAN.md) → (3) USDT 공통화 검토**. KRX Stage E 먼저 진행 이유: 단일 source라 scope 좁고 (5b-bis freshness metadata + 5d-a SET-only topic trigger) USDT 검증 패턴을 KRX에 먼저 이식하여 패턴 안정성 확보. USDT 공통화는 (1)+(2) land 후 3 도메인 검증 데이터 기반으로 진입 (선제 abstraction 금지 원칙 정렬).

#### 12.9.8 Silent-session 사고 postmortem + `c4a51e5` fix + deferred follow-ups (2026-06-03)

**Incident**: 2026-05-28 이후 Gopax USDT/KRW가 stale 고착 (~5일). 운영 실측(2026-06-03 캡처): Redis latest·공개 `usdt:krw` topic = `5/28 15:52:50 KST / 1475.0`, DB `source_rates` latest = `5/28 15:42 KST / 1475.0` — 셋 다 5/28 동결 (DB는 변경 시에만 INSERT라 Redis tick/probe write보다 ~10분 older, rate는 동일 1475.0).

- 원인: WS task는 **살아 있었으나**(metrics 계속 emit) tick·heartbeat frame이 **모두 정지**했고 `ConnectionClosed`가 발생하지 않은 **silent WS session**. 운영 로그 실측: `frames_per_min=0` / `last_tick_age ≈ last_heartbeat_age ≈ 427,600s` / `connection_status=stale` / `reconnect_attempts=1`(freeze 이후 0).
- 핵심 결함: `connection_status=stale`은 감지됐으나 **label-only no-op** — 재연결이 `ConnectionClosed` 예외에만 묶여 있어 half-open/silent 소켓은 영구 고착.
- Gopax 고유성: 다른 4 source(Upbit/Bithumb/Coinone/Korbit)는 `_ping_loop`로 능동 `ws.close()` → reconnect 보유. Gopax만 서버발 Primus heartbeat 수동 응답에 의존해 능동 종료 경로 부재.
- 복구: fastapi restart로 즉시 회복. end-user 영향 0(테더 탭 미배포), canary/topic 데이터 품질 이슈.

**Fixed in `c4a51e5` (PR #1, deploy 2026-06-03)**:

- no-first-valid-USDT-tick timeout (`FIRST_TICK_TIMEOUT_SEC=30`, valid tick 기준 — heartbeat-only 세션 포착) → `_SilentSessionError` → reconnect
- mid-session stale (tick·heartbeat silence > `STALE_AFTER_SEC=360`) → `_SilentSessionError` → reconnect (기존 no-op 대체)
- 첫 valid tick에 consecutive backoff 리셋 (`_consecutive_reconnect_attempts`, lifetime `_reconnect_attempt_count` metric과 분리)
- backoff 구간 bounded REST probe (`run_backoff_probe`: 병렬 / backoff 종료 시 cancel / cancel 시 cooldown 미소비 / session-scoped probe와 독립 in-flight·cooldown)
- USDT Redis writer `<` 역행 가드 (`SKIPPED_REGRESSION` enum + `direct_write_regression_skipped` counter + throttled warning)
- 테스트 `tests/test_gopax_silent_session_fix.py` 16 + 관련 suite 765 passed, **migration 없음**

**c4a51e5가 못 덮는 잔여 모드 → deferred follow-ups (미구현, 비긴급, 필요 시 별 PR)**:

| 실패 모드 | c4a51e5 | 후속 |
| --- | --- | --- |
| tick·heartbeat 모두 정지 / 첫 tick 없음 / Redis out-of-order | ✅ | — |
| **heartbeat 살아있는데 ticker만 정지** | ❌ | ① |
| **WS task 자체 종료(크래시/취소)** | ❌ | ② |
| **DB out-of-order write** | ❌ (Redis만) | ③ |

**① heartbeat-alive · ticker-dead 복구** — [우선순위 1 / 작업량 M / migration 없음]

> ✅ **완료** (2026-06-11, commit `ef99c66` + 배포 live): Gopax-only. `fetch_gopax_last_traded_ms`(/tickers, 신규·기존 fetch 무접촉) + controller 별 task detector(REST `lastTraded` > WS 저장값 → reconnect flag → recv loop `_SilentSessionError("ticker_dead")`) + race guard(valid tick flag clear) + metrics `ticker_dead_check_count`. 14 단위 테스트. 배포 후 live(normal, 평상시 발화 0 정상). 5-source 공통화는 후속.

- 문제: heartbeat는 오는데 target ticker만 정지. 시장은 거래 중인데 우리 데이터만 dead.
- 현 fix 미커버: `is_stale = max(tick,heartbeat) silence`라 heartbeat fresh면 not-stale → 재연결 안 됨. first-tick guard는 세션 시작 때만.
- 탐지 신호: ticker degraded 지속 시 REST `lastTraded`(체결 시각)와 **저장된 WS last `lastTraded`** 비교 → REST가 앞서면 WS 데이터 dead 확정 → reconnect.
- 건드릴 파일: `app/crawlers/usdt_sources.py` (현 `fetch_gopax_usdt_tick`은 `/trading-pairs/USDT-KRW/ticker`의 `time`=ticker update time 사용 → **`/tickers`의 `lastTraded`=trade time으로 교체/추가, 시맨틱 일치 필수**) / `gopax.py` (WS tick `lastTraded` instance 저장 + degraded 비교 + `_SilentSessionError` 재사용) / race 가드(probe 중 WS 회복).
- 스코프: **Gopax-only 먼저**. 5 source 공통 위험이나(다른 4 ping loop도 *데이터* 죽음 미탐지) REST 비교 5개 확장은 M→L. Gopax 검증 후 공통화 검토.
- 테스트: REST>WS→reconnect / REST==WS(조용한 시장)→무동작 / race.

**② 5-source 공통 task supervisor** — [우선순위 2 / 작업량 M / migration 없음]

> ✅ **완료** (2026-06-11, commit `db8461b` + 배포 + 활성): 30s watchdog(`_usdt_ws_supervisor_tick`) — done() task 감지 → `shutdown_*`(idempotent teardown) → `start_*`(fresh, dedup 가드 재사용). `USDT_WS_SUPERVISOR_ENABLED` env(default false → job 미등록, 배포 ≠ 동작 변화) + main.py shutdown race guard flag(stop()/task await 구간 globals not-None window 차단) + per-source backoff(consec 2부터 60·120·240… cap 300s, **영구 포기 X**, 생존 5분+ reset — 단일-tick-alive 아님) + per-source 격리. 16 단위 테스트. 활성 후 5 source 전수 감시 + restart 0(healthy). ⚠️ **KRX는 ② 1차 제외**(bootstrap+client 2-task 구조 상이)였으나 **task-death gap은 별 분기로 land**(2026-06-11): `_reconcile_krx_futures_contract`에 `client 비None AND task.done()` 분기(shutdown_krx→bootstrap(resolved) + post-condition) + ②의 shutdown flag를 `_collector_shutdown_initiated`로 일반화(supervisor+reconcile 공유) + enum `task_dead_restarted`/`task_dead_restart_error` + backoff 없음(5분 cron throttle) + 6 tests. **일반 배포**(보류 불필요 — 본 분기는 dead task에만 발화해 6/15 rollover[live task]와 직교: rollover 분기 미변경 + 회귀 잠금; 발화 시 `last_result=task_dead_restarted` telemetry 귀속. 15:35~15:50 CF close finalizer 창만 회피). 6/15 관찰 시 reconcile `last_result`로 분기 구분. 실제 잔존은 ③만.

- 문제: WS *연결* 죽음이 아니라 **collector task 자체**(asyncio task)가 종료(크래시/취소)되는 경우 — 되살릴 장치 없음. (legacy REST polling 안전망 5/25 비활성.)
- 현 fix 미커버: 이번 fix는 *살아있는 task 안*에서 reconnect.
- 설계: `app/scheduler.py`의 5개 `start_usdt_ws_*_client`/`_run_usdt_ws_*_client`(현재 crash 시 log-only)에 공용 supervisor — `task.done()` 감지 또는 주기 watchdog → 재기동 + shutdown race / 중복 task 방지 / backoff. KRX wrapper도 동일 log-only 구조라 함께 검토 가능. all-source 안전망.

**③ DB exchange-ts 정합** — [우선순위 3 / 가장 비긴급]

> ✅ **super-lite first PR land** (2026-06-11, 세 리뷰어 수렴): 아래 설계(신규 `exchange_ts` 컬럼 + migration)는 **불필요로 판명**. 기존 `insert_source_rate_if_changed(timestamp=)` param + `timestamp DESC` 쿼리가 이미 받을 수 있어, USDT 5 WS DB writer가 tick의 exchange event ts(`crud.event_ms_to_utc_naive`)를 그 param으로 전달 → **out-of-order stale이 latest로 오판되지 않음 (migration/watermark/read-path 0)**. DB writer 경유 전체(WS tick + REST probe fanout) 적용, 직접 crud 호출(KRX/legacy)만 미적용. 핵심 근거 = **DB는 history store**라 stale도 제 ts로 보존(Redis latest-only는 skip이 맞지만 DB는 다름). Redis 가드(seen_at)와 동일 `timestamp_ms` 사용이라 시맨틱 일관(5 source 전수 확인). full(영속 컬럼)은 추가 가치(재시작 직후 첫 write 가드)가 plumbing 비용 대비 약해 **보류·폐기**. KRX는 KIS frame ts 시맨틱 검토 후 별 PR. 아래는 **super-lite 전 원 설계 기록**(역사 보존):

- 문제: out-of-order write 시 stale value가 늦게 도착해 '최신'으로 기록 가능. Redis는 c4a51e5 `<` 가드로 차단, **DB 미차단**.
- 현 fix 미커버: DB writer가 save-time(now())만 기록 → 거래 시각 역행 미구분. latest 쿼리 `timestamp DESC, id DESC`(`crud.py`).
- 현재 완화: lifecycle ordering(죽은 세션 flush 후 새 세션 write)으로 *사실상* 방지 → robustness/defense-in-depth.
- 설계: `source_rates`에 **신규 `exchange_ts` 컬럼(migration)** + insert 시 역행 거부. ⚠️ `crud.insert_source_rate_if_changed`는 **KRX 공유**(`krx_kis.py` 2곳) — 전역 수정 신중(opt-in param). `timestamp` 컬럼 의미 변경은 graph/cleanup 등 광범위 영향이라 **신규 컬럼이 안전**. 5 WS DB writer는 현재 timestamp 미전달 → plumbing 필요.

**추천 순서**: ① → ② → ③ (서로 독립, 임의 순서 가능). **①② land 완료** (2026-06-11, `ef99c66`·`db8461b` 배포) + **KRX task-death gap도 별 분기로 land** (2026-06-11, 일반 배포 — rollover 직교, 위 ② blockquote 참조). 실제 남은 것 = **③**(migration + 이미 lifecycle ordering으로 완화 → 가장 비긴급)뿐.

**참고**: fix commit `c4a51e5` / PR #1(merged, branch 삭제됨) / 진단 근거는 운영 metrics 로그(2026-06-03 캡처).

## 13. Long-term alert scaling roadmap (PR6 follow-up 2)

PR6 + follow-up 1 (CRUD invalidation) 완료 후 미래 작업 방향 명시. 사용자 우려
(2026-05-14, 사용자/Codex/Claude 합의 누적: 30k 알림 시나리오, 비교 알림 폭주,
multi-process 확장, history retention)와 합의된 phase plan을 영구 기록 — PR7
이후 같은 설계 재논의 비용 차단.

### 13.1 핵심 원칙 (모든 phase 공통)

- **DB는 source of truth** (settings / triggered state / log / history). 가격값
  조회용 X.
- **Redis는 cache/index/latest** (가격 latest / settings cache / 발송 lock /
  threshold index). 원본 X.
- **알림은 이벤트 보존** (Redis writer "latest 보존"과 다름). observation drop
  금지, 짧은 threshold crossing 누락 차단.
- **격리 원칙**: cache/DB/FCM 실패는 WS session 영향 X.

### 13.2 Phase 1 (현재 — PR6 + Follow-up 1 완료)

- **In-process AlertSettingsCache** (TTL=10s, `(source, asset)` bucket)
- **CRUD invalidation** (POST/PUT/DELETE endpoint hook, [main.py](app/main.py))
- **In-flight setting guard** (in-process atomic claim)
- **Per-key loading guard** (TTL 만료 thundering herd 차단)
- **Refetch source/asset/condition/threshold 재확인** (cache stale 최종 방어)
- **DB session 3-step 분리** (refetch → FCM no-session → mark/log)
- **Cache singleton, evaluator runtime per-instance** ([alert_evaluator.py](app/notifications/alert_evaluator.py))

**적용 범위**: Upbit USDT only. 처리 규모: ~수십~수백 settings, single process.

### 13.3 Phase 2 — multi-process 대비 Redis pub/sub invalidation

**Trigger**: process 다중화 (worker 분리 또는 horizontal scaling).

**문제**: in-process cache singleton은 process 별 독립 → CRUD invalidation이 다른
process의 cache에 미적용 → 사용자가 알림 끄거나 변경한 후에도 다른 process는
옛 cache로 평가.

**해결**:
- CRUD endpoint이 Redis pub/sub channel에 `(source, asset)` invalidation 메시지
  발행
- 각 process가 채널 구독 → 메시지 수신 시 local cache invalidate
- pattern: `alert_settings_cache:invalidate:<source>:<asset>`

**Non-scope until trigger**: Phase 1 (single-process) 동안은 도입 X.

### 13.4 Phase 3 — Redis ZSET threshold index (대량 settings)

**Trigger**: 사용자 ↑ + per-user alert ↑ 시점 (예: 500 user × 60 alert =
30,000 settings).

**문제**: tick마다 `(source, asset)` bucket을 in-memory iterate (현재 Phase 1).
30k 중 ~2k이 같은 (source, asset) bucket이면 매 tick 2k 비교. 비교 자체는
빠르지만 누적 부담.

**해결**:
- Redis Sorted Set으로 threshold index 구성:
  ```text
  ZSET alerts:price:upbit:usdt-krw:above  (score=threshold, value=setting_id)
  ZSET alerts:price:upbit:usdt-krw:below  (score=threshold, value=setting_id)
  ```
- tick 도착 시 `ZRANGEBYSCORE` 로 threshold 조건 매칭만 fetch:
  - above: `ZRANGEBYSCORE alerts:...:above -inf <current_price>`
  - below: `ZRANGEBYSCORE alerts:...:below <current_price> +inf`
- O(log N + M), M = 매칭 수 (30k → 매칭 ~10-100개)

**Non-scope until trigger**: Phase 1/2 동안 in-memory bucket iterate로 충분.

### 13.5 Phase 4 — Alert worker process 분리

**Trigger**: WS process가 alert 평가 부담으로 throughput 영향 받는 시점.

**해결**:
- AlertObservation을 queue/Redis stream으로 alert worker에 전달
- WS process는 enqueue만 (fire-and-forget)
- Alert worker process가 dequeue → 평가 → FCM
- 가로 확장 가능 (worker N대)

**Non-scope until trigger**: 현재 fire-and-forget background task로 충분.

### 13.6 알림 기능 확장 roadmap (Phase 1과 별도 축)

| 시점 | 작업 |
|---|---|
| Phase B.1 PR6 (완료) | Upbit-only B1 once 정책 |
| Phase C | KRX 알림 새 구조 적용 (USDT 검증 후) |
| Phase D | 은행/Investing/달러/엔/유로 알림 migration (§13.7 shadow eval 참조) |
| Phase E | 비교 알림 (`comparison_alerts`) — **Redis latest snapshot 기반 multi-source** (서로 다른 source의 현재값 동시 비교 필요). **단일 가격 알림**은 §6 B1 채택대로 **tick/observation 입력** — 두 입력 모델은 별도. "Redis 중심"이 가격 latest read로 오해되지 않도록 명시. |
| 별 ADR | B2 `repeat_interval_sec` 실제 구현 (schema + API + iOS/Android UI) |
| 별 ADR | B3 direction crossing |

### 13.7 기존 알림 migration 패턴 — shadow evaluation

**원칙**: 기존 노출 기능 (달러/엔/유로 가격도달 알림)은 새 구조로 즉시 교체 X.
사용자 영향 큼.

**Migration step**:
1. 기존 알림 로직은 그대로 실제 발송 (production 동작 보존)
2. 새 alert_evaluator 구조로 같은 입력 평가하되 **발송 X, 결과만 log**
3. 며칠~몇 주 두 결과 비교
4. 일치 확인 후 새 구조로 전환 (feature flag 단계적 ramp)

### 13.8 History retention cleanup (별도 commit)

**Trigger**: `source_notification_logs` (그리고 미래 `comparison_notification_logs`)
row 누적.

**정책**:
- 90일 또는 180일 보관 (사용자가 단말에서 조회)
- 그 이후 cleanup job (`scheduler.py`의 `cleanup_old_*` 패턴)
- 사용자 직접 삭제 API는 future enhancement

### 13.9 사용자 우려 기록 (2026-05-14 합의 근거)

| 우려 | Phase 1 처리 | 확장 시점 |
|---|---|---|
| tick마다 DB query 부담 | TTL=10s in-process cache | Phase 2/3 |
| 30k 알림 처리 가능? | Redis 저장 자체는 무문제, access 패턴이 중요 | Phase 3 ZSET index |
| 비교 알림 폭주 | Phase 1 non-scope | Phase E + Phase 3 (source/asset 인덱스 → 영향 rule만 평가) |
| 사용자 알림 끄기 즉시 반영 | Follow-up 1 CRUD invalidation (single process) | Phase 2 (multi-process Redis pub/sub) |
| 반복 알림 확장성 | `delivery_allowed` 분리 (interface 확장 자리 마련) | 별 ADR B2 |
| 기존 알림 영향 | PR6에서 건드리지 X (additive) | Phase D shadow eval |
| 히스토리 사용자 조회 | `source_notification_logs` 활용 | §13.8 retention cleanup |
| 동일 가격 반복 frame 평가 부담 | §13.10 same-rate evaluation coalescing 정책 (가격/비교 알림 공통) | Phase 3 ZSET과 별개 (보완 관계) |

### 13.10 Alert evaluation coalescing — 가격 알림 + 비교 알림 공통

**배경 (2026-05-18 사용자 우려 + 코덱스/Claude 검증)**: Upbit/Bithumb WS는 `type=ticker`
구독만 하지만, ticker frame은 체결가가 변하지 않아도 들어올 수 있다. 현재 구현은
`trade_price` 동일 여부와 무관하게 모든 valid ticker frame을 Redis latest /
liveness / DB writer / alert evaluator에 전달한다. 500 user × 60 alert = 30k
settings 시점이 도래하면 동일 가격 반복 frame에서 condition 비교가 누적 부담이 됨.

**핵심 정책 (가격 알림 + 비교 알림 공통)**:

- 알림은 DB 저장 이벤트가 아니라 **observation/state change 기반**으로 평가한다.
- Redis latest / liveness / DB writer 입력은 **모든 valid ticker frame**을 유지한다
  (down-stream 정책 분리 원칙).
- 단, 알림 evaluator는 *"모든 raw frame"*이 아니라 **알림 결과가 달라질 수 있는
  meaningful observation/state change**를 평가 대상으로 삼는다. 동일 값 반복으로
  결과가 변하지 않는 경우 **condition evaluation coalescing** 적용 가능 (= same-rate
  evaluation skip).

**입력 모델 분리**:

- **단일 가격 알림**: 개별 source의 tick/observation 입력 (§6 B1)
- **비교 알림** (Phase E): Redis latest snapshot 기반 multi-source state 입력
  - 비교 알림의 multi-source observation trigger 방식 (어느 source 변경이 trigger인지,
    scheduler 기반 vs event-driven, snapshot 조합 기준)은 **Phase E 시점에 결정**.
    현 §13.10에서는 "동일 원칙 적용" 잠금만 명시.

**생략 금지 조건 (가격/비교 알림 공통, 같은 값/같은 snapshot이어도 평가)**:

1. evaluator 시작 후 첫 observation
2. settings cache miss / TTL refresh 이후
3. settings CRUD invalidation 이후 (POST/PUT/DELETE)
4. 새 알림 / 변경된 알림이 반영된 이후
5. `repeat_interval_sec` due (미래 B2 — 가격 동일해도 반복 발송 시점 도래 시 재평가)
6. direction crossing state 전이 가능 구간 (미래 B3 — 상태 전이 가능성 있으면 재평가)

**용어 주의**: "debounce"는 broadcast/UI/DB writer 영역에서는 시간 기반 throttle로
유효하게 사용되지만, 알림 정책에서는 **same-rate evaluation skip / condition
evaluation coalescing / meaningful observation**으로 표기한다. 알림에 "debounce"
표현은 시간 기반 throttle 오해 위험.

**Phase 3 ZSET threshold index와의 관계**: 두 최적화는 별개 + 보완 관계.

- §13.4 Phase 3 ZSET: **후보 settings 수**를 줄임 (`ZRANGEBYSCORE`로 임계값 매칭만 fetch)
- §13.10 coalescing: **동일 가격 반복 frame**의 재평가 자체를 줄임

같은 tick에서 ZSET fetch 후에도 동일 가격 연속이면 evaluator 진입 전 coalescing 가능.

**구현 PR 결정 (별도 stage, plan 잠금 후 결정)**:

- (α) Phase 1.5 즉시 적용 — 30k 도달 전 선제 방어, 단순 last-rate memoize는 위험
  (생략 금지 조건 6개 모두 처리 필요)
- (β) Phase 3 ZSET과 묶음 — Phase 1 단순성 유지, 30k 도달 시점에 함께 도입
- (γ) Phase 1.5 + Phase 3 분리 land — coalescing 먼저 + ZSET은 별도
- **(δ) 구현 방식 결정 deferred (현재 결정, 2026-05-18 사용자/코덱스 합의)** — 아래
  trigger 중 하나가 먼저 도달하면 α/β/γ 재검토:
  - 비교 알림 (Phase E) 실제 구현 진입
  - 테더탭/WebSocket refactor 이후 자산별 알림 패턴 안정
  - Bithumb canary 및 후속 거래소 telemetry 확보
  - 알림 settings 규모가 사전 최적화 검토 기준에 도달 (예: 10k+ settings)

  근거: 비교 알림 입력/trigger 방식이 Phase E 시점에 결정될 예정 + Bithumb canary
  운영 후 실제 frame 빈도 측정 가능 + 현재 진행 작업 (테더탭/WebSocket refactor/
  KRX rollover) 우선순위 정합. 추측 기반 결정 회피 + 실측 데이터 기반 결정.

source-neutral 설계 (Upbit / Bithumb / 향후 Coinone / Korbit / Gopax + KRX + 비교
알림 공통 helper). Bithumb (Phase B.3)도 동일 정책 자동 적용 — Upbit pattern 복제
이므로 구현 시점에 두 source 동시 적용.

## 14. Phase B.2 — Tether topic publish trigger 분리

Phase B.1 (PR1~PR7 + follow-ups) 완료 후 후속 작업. 현재 main.py
`broadcast_rates_once`의 `is_changed` piggyback hook을 **임시 위치**에서
정식 위치(USDT/KRX writer success 시점 + topic 단위 coalesce)로 이동.

**진입 조건**: Phase B.1 완료 + Stage 0 Dev Smoke 통과 (`17471f7` + `1b621bf`).
**Codex/Claude 합의 (2026-05-15, 3 라운드)**.

### 14.1 문제 정의

[main.py:677-696](app/main.py):
```python
new_json = json.dumps(payload, ensure_ascii=False)
is_changed = new_json != cached_json   # legacy rates payload 기준
if is_changed:
    ...
    await tether_topic_publisher.safe_publish_tether_tab_snapshot(...)
```

- `payload`는 **legacy rates** (USD/JPY/EUR + 은행 + Investing + DXY)
- USDT 5거래소 / KRX 미국달러선물 변경은 `is_changed`에 반영 X
- 결과: Upbit WebSocket이 Redis sub-second 갱신해도 단말 도달은 legacy 변경 시점에만
- 야간 (은행 변경 거의 없음): 단말 갱신 거의 없음 (사용자 체감)
- 코드 주석 명시: "임시 위치, USDT WebSocket/Redis-first 전환 후 mirror/topic
  pipeline으로 이동 예정" ([tether_topic_publisher.py:21](app/tether_topic_publisher.py),
  [USDT_TOPIC_MIGRATION_PLAN.md:155-156](USDT_TOPIC_MIGRATION_PLAN.md))

### 14.2 핵심 결정

**Mode-based feature flag** (boolean 두 개 조합 회피, 운영 실수 방지):

```text
TETHER_TOPIC_TRIGGER_MODE = "legacy_piggyback" | "dual_shadow" | "direct_coalesced"
```

| Mode | 동작 |
|---|---|
| `legacy_piggyback` (default) | 현재 — `main.py is_changed` 안에서만 publish. controller request_trigger 수신해도 noop (coalesce timer도 시작 안 함) |
| `dual_shadow` | trigger 수집 → **coalesce window 실제 진행** (direct 모드와 동일 timing) → flush 시점에 publish call **직전 단계까지** → publish skip + telemetry 기록. **production 영향 0 + direct 전환 시 publish 빈도 정확 예측 가능** (Codex review). |
| `direct_coalesced` | trigger 수집 → coalesce window 진행 → flush 시점에 `safe_publish_tether_tab_snapshot()` 호출. **실 publish** |

**핵심**: `dual_shadow`가 단순 counter만 올리면 actual publish 빈도(coalesce 효과 반영)를 측정 못 함. coalesce timer까지 mirroring해야 direct 전환 후 실제 publish 빈도 예측 가능 (telemetry baseline 의미 확보).

원안 전환 흐름: `legacy_piggyback` → `dual_shadow` (실측 검증) → `direct_coalesced` (canary) → 안정 시 `legacy_piggyback` 격하/제거.

2026-05-16 운영 결정: `dual_shadow`를 건너뛰고 `direct_coalesced`로 직접 진입. 운영 App Store 단말에는 테더 탭이 없고, iOS dev 단말로 직접 검증 가능하며, direct 모드에서도 baseline counter 측정이 가능했기 때문이다. 상세 결과는 §14.11.

### 14.3 TetherTopicTriggerController (`app/tether_topic_trigger.py` 신규)

**책임 분리** (Phase B.1 single-responsibility 패턴):
- `tether_topic_publisher.py` (기존): **어떻게** publish — snapshot build / schema / subscriber guard / 격리
- `tether_topic_trigger.py` (신규): **언제** publish — coalesce window / debounce / mode 분기 / telemetry

**API**:

```python
class TetherTopicTriggerController:
    def request_trigger(self, source: str, asset: str, reason: str) -> None:
        """sync, USDT/KRX writer success 시점에 호출.
        
        mode=legacy_piggyback이면 즉시 return (timer 시작 X).
        그 외 mode: _pending_topic_trigger 갱신 + coalesce window timer 시작/유지.
        """

    async def _flush_after_window(self) -> None:
        """coalesce window 만료 → mode 분기:
        - dual_shadow: publish call **직전 단계까지** 진행 → publish skip + telemetry
        - direct_coalesced: safe_publish_tether_tab_snapshot() 호출 + telemetry
        
        coalesce timer는 dual_shadow도 동일하게 진행 — direct 전환 시 publish 빈도
        정확 예측 baseline 확보 (Codex review).
        """

    async def close(self, timeout: float = 1.0) -> None:
        """shutdown drain-first (PR4 Redis writer 패턴 mirror).
        
        pending flush task 완료까지 대기, timeout 후 cancel. 단일 timer라
        cleanup 단순 — gather 1개 + return_exceptions=True.
        """
```

**Coalesce window 확정값**: `TETHER_TOPIC_TRIGGER_COALESCE_MS=500` (PR1 default). iOS 단말 latency 영향 ~500ms 추가 — sub-second 도달은 보존, 사용자 체감 빠름.

**Singleton**: process-wide. topic 단위 coalesce 위해 필수. main.py lifespan에서 lifecycle 관리.

**Env (PR1 확정)**:
- `TETHER_TOPIC_TRIGGER_MODE=legacy_piggyback` (default)
- `TETHER_TOPIC_TRIGGER_COALESCE_MS=500`

### 14.4 PR 분할 (4 PR)

**진행 status (2026-05-16)**:

- ✅ PR1 완료 — `f16d908` (skeleton + env + lifespan close hook)
- ✅ PR2 완료 — `2d6cece` (Upbit hook + telemetry + GC strong reference 보강)
- ✅ PR3 완료 — `7c67e67` (KRX Redis writer hook)
- ✅ PR4 Step A 완료 — `direct_coalesced` 운영 활성화 (`2026-05-16 15:16 KST`). `dual_shadow`는 건너뜀.
- ⏳ PR4 Step B 대기 — main.py legacy hook fallback 격하/완전 제거 결정

| PR | Scope | Non-scope | Flag default | Rollback | Smoke 기준 |
|---|---|---|---|---|---|
| **PR1** ✅ `f16d908` | 설계 doc 누적 (§14) + env 2개 (`TETHER_TOPIC_TRIGGER_MODE`, `TETHER_TOPIC_TRIGGER_COALESCE_MS`) + `app/tether_topic_trigger.py` 신규 (`TetherTopicTriggerController` skeleton). mode=legacy_piggyback이면 `request_trigger` 즉시 return (timer 시작 X). dual_shadow / direct_coalesced 모드는 coalesce timer 모두 진행 (publish call만 차이). main.py 변경 X. lifespan close hook. | trigger 연결, USDT/KRX hook | `legacy_piggyback` | env 그대로 | (1) legacy: timer/publish 모두 0 (2) **dual_shadow: 여러 trigger → 1번 flush coalesce, publish call 0** (3) **direct: 여러 trigger → 1번 publish coalesce** (4) close: pending flush drain (5) **invalid mode / coalesce_ms 0 또는 음수 → safe default fallback 또는 validation error** |
| **PR2** ✅ `2d6cece` | `UpbitRedisWriter._write_async`에서 `set_latest_usdt_rate_from_sync_job` True 반환 시점 → **lock 밖**에서 `request_tether_topic_trigger("upbit", "usdt-krw", TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS)` 호출 (성공 정보 local var로 lock 안에서 저장 후 lock 밖에서 호출 — 책임 분리, Codex review). **Trigger reason은 `app/tether_topic_trigger.py`에 상수로 중앙화** (e.g., `TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS = "usdt_ws_redis_write_success"`) — reason taxonomy 단일 진실 소스, 향후 KRX/Bithumb 등 reason 추가도 같은 위치. Writer/Test는 상수 import만. Redis-backed telemetry 추가: 기존 `topic:tether:stats` hash + `trigger_*` prefix. 구현 시 보강된 실측 fields는 아래 "**PR2 실측 telemetry field**" 단락 참조 (counter 10 + last/HSET 7). best-effort 격리 (circuit_breaker 미오염, publisher 패턴 동일). | KRX hook (PR3), legacy 격하 (PR4), **DB session during publish refactor (PR4 진입 시 재검토)** | `legacy_piggyback` | env 그대로 | (1) helper True → trigger 호출 (2) helper False → trigger 미호출 (3) helper exception → trigger 미호출 + writer 격리 (4) default legacy mode에서도 hook 호출 안전 (controller noop) (5) trigger 예외가 Redis writer/WS에 전파 X — 모두 dual_shadow mode 활성 시 telemetry 갱신 확인 (`trigger_request` + `trigger_coalesced` + `trigger_last_window_ms`, publish 0) |
| **PR3** ✅ `7c67e67` | KRX Redis write success → `request_tether_topic_trigger("krx", "usd-krw-futures", TETHER_TRIGGER_REASON_KRX_REDIS_WRITE_SUCCESS)`. 호출 위치: `KrxDbWriter._flush_after_window`의 finally 블록 **밖** (race-prevention timer 재예약 책임과 분리, PR2 lock 밖 패턴 mirror). 시그니처 변경 2곳: (a) `KrxRedisLatestWriter.write_after_db_insert` → `bool` return (`set_latest_krx_rate_from_sync_job` 결과 propagate). (b) `_sync_db_write` → `bool` return (inserted=True **AND** redis_write_success=True). reason 상수 `TETHER_TRIGGER_REASON_KRX_REDIS_WRITE_SUCCESS = "krx_redis_write_success"` 추가 (USDT와 달리 "ws" prefix 미포함 — KRX REST fallback이 동일 DB path 공유 가능성 + 미래 tick-level Redis writer 도입 시에도 reason 그대로 유지). **현재 hook은 DB insert 후 Redis write success 반환 지점, 미래 KRX tick-level Redis writer 도입 시 hook 위치 이동 예정 (reason 이름 유지)**. 동일 controller 공유. | legacy 격하 (PR4), KRX tick-level Redis writer (별도 phase, ADR-031 Stage C 영역) | `legacy_piggyback` | env 그대로 | (1) inserted=True AND redis_ok=True → trigger 호출 + 인자 검증 (2) inserted=False → trigger 미호출 (3) inserted=True AND redis_ok=False → trigger 미호출 (4) `_sync_db_write` exception → trigger 미호출 + writer loop 격리 (5) trigger 예외 → writer loop 영향 X (6) legacy_piggyback mode에서 hook 호출 안전 (controller noop) |
| **PR4** ✅ Step A | `dual_shadow`를 건너뛰고 `direct_coalesced` 직접 진입. 근거: 운영 App Store 단말에는 테더 탭이 없고, iOS dev 단말로 직접 검증 가능하며, direct 모드에서도 baseline counter 측정 가능. Step A는 mode 전환 + 비구독/구독 path 검증. Step B는 main.py 임시 hook 격하/제거 결정. | iOS 클라이언트 변경, main.py hook 즉시 제거, 모든 source trigger 확대 | `direct_coalesced` | env 환원 `legacy_piggyback` | (1) 비구독: `trigger_publish_called == hook_called == skipped_no_subscribers` (2) 구독: `trigger_publish_success` / `built` / `publish_called` / `publish_sent_total` 증가 (3) `trigger_skipped_legacy` 정지 (4) iOS dev 단말 실시간 체감 확인 |

**PR2 실측 telemetry field (구현 시 보강)**:

- Counter (10, `trigger_` prefix): `request`, `skipped_legacy`, `coalesced`, `flush_dual_shadow`, `flush_direct`, `publish_called`, `publish_success`, `publish_skipped_shadow`, `no_loop`, `error`
- Last/HSET fields: `last_result`, `last_reason`, `last_source`, `last_asset`, `last_window_ms`, `last_at_kst`, `last_error`
- 초안 (12 field, `trigger_count` / `trigger_coalesced_count` / `trigger_last_mode` 등) 대비 일부 명칭 단순화 + `skipped_legacy` / `no_loop` / `error` / `last_result` / `last_at_kst` / `last_error` 추가 (운영 진단 보강)
- GC strong reference 보강: module-level `_telemetry_tasks` set + `add_done_callback(discard)` (asyncio docs 권고, fire-and-forget task GC 회피)

### 14.5 Telemetry 누적 계획

**Storage**: 기존 `topic:tether:stats` Redis hash 활용 (별도 key X — admin 운영 확인 포인트 단일 유지). 기존 publisher field와 명시적 구분을 위해 **모든 신규 trigger field는 `trigger_` prefix** (Codex 합의).

**도입 시점**: PR1은 in-process Stats만 (process restart 시 손실). **PR2 진입 시 Redis-backed 보강** (dual_shadow 운영 prerequisite).

**실측 telemetry fields** (구현 후 갱신, 옛 12 field 초안 대비 일부 명칭 단순화 + 운영 진단 보강):

**Counter fields (10개, `trigger_` prefix, `hincrby`)**:

- `trigger_request` — request_trigger 호출 횟수 (옛 `trigger_count`에서 단순화)
- `trigger_skipped_legacy` — legacy_piggyback mode strict noop 카운트 (운영 진단 보강)
- `trigger_coalesced` — coalesce window 안에 dedup 된 trigger 수 (옛 `trigger_coalesced_count`에서 단순화)
- `trigger_flush_dual_shadow` — dual_shadow mode flush 진입 (publish skip 직전)
- `trigger_flush_direct` — direct_coalesced mode flush 진입 (publish 호출 직전)
- `trigger_publish_called` — direct mode에서 publish 호출
- `trigger_publish_success` — publish 성공 (sent > 0)
- `trigger_publish_skipped_shadow` — dual_shadow에서 publish skip 한 횟수 (flush_dual_shadow와 동일 값)
- `trigger_no_loop` — running event loop 부재로 trigger skip 한 횟수 (sync test/startup 경로 진단)
- `trigger_error` — flush task 예외 격리 카운트 (`circuit.record_failure` 미호출, best-effort 보존)

**Last/HSET fields (7개, `trigger_` prefix, `hset`)**:

- `trigger_last_result` — 최근 결과 분류 (e.g., `publish_success`, `skipped_legacy`, `publish_skipped_shadow`, `publish_zero`, `error`)
- `trigger_last_reason` — 최근 trigger reason (e.g., `usdt_ws_redis_write_success`, `krx_redis_write_success`)
- `trigger_last_source` — 최근 trigger source (e.g., `upbit`, `krx`)
- `trigger_last_asset` — 최근 trigger asset (e.g., `usdt-krw`, `usd-krw-futures`)
- `trigger_last_window_ms` — 최근 coalesce window 실측 ms
- `trigger_last_error` — 최근 flush 예외 문자열 (길이 500 cap)
- `trigger_last_at_kst` — 최근 telemetry 기록 시각 (KST ISO)

옛 plan 초안의 `trigger_last_mode`는 process config라 매 trigger마다 변하지 않아 실측에서 제외. `trigger_count` / `trigger_coalesced_count`는 prefix 안의 `_count` suffix 중복이라 단순화.

**격리 원칙** (publisher 패턴 동일):

- best-effort: Redis 실패 시 silent + `logger.debug`
- `circuit_breaker.record_failure()` 호출 X (broadcast/mirror Redis path 오염 회피)
- Writer hot path 영향 0

**핵심 비교**: `trigger_flush_dual_shadow` ↔ `trigger_flush_direct`는 같은 timing 동작 → dual_shadow → direct 전환 시 publish 빈도 정확 예측 (Codex review baseline). `trigger_last_window_ms`로 coalesce window 실측.

`/admin/api/topic-status` endpoint 확장 (기존 활용).

### 14.6 iOS 단말 latency 측정

**Phase B.2 minimum scope에서 server-side만**:
- `publish_called_at` timestamp 측정
- USDT Redis write timestamp ↔ publish_called_at 차이 = "trigger latency"

**클라이언트 latency 측정은 별도 phase**:
- iOS / Android 코드 변경 필요 (recv timestamp 기록 + 서버로 echo)
- topic protocol schema 확장 (server publish_ts 필드 추가)
- Phase B.2 안정 후 별도 PR

### 14.7 Legacy piggyback 격하 정책

main.py 임시 hook은 즉시 삭제 X. PR4 Step B에서 다음 중 선택:

- **Option A — 완전 제거**: `direct_coalesced` mode에서 안정 검증 후 main.py hook 삭제. emergency rollback은 `legacy_piggyback` mode 환원 + 새 controller가 처리.
- **Option B — fallback 격하**: main.py hook은 mode 분기 안에서만 발화 (`legacy_piggyback` 또는 `direct_coalesced` 실패 fallback). 코드 유지 + 안전망.

**2026-05-16 합의**: 장기적으로 Option A가 목표. 단, 완전 제거 조건은 **테더 탭에 표시되는 모든 자산이 Redis latest write-through 성공 지점 기반 topic publish trigger를 갖춘 뒤**로 둔다 (여기서 trigger는 broadcast/topic publish trigger를 의미 — alert evaluator trigger와는 별개이며, alert는 §6 B1대로 tick/observation 입력을 사용한다). 그 전에는 Option B fallback 격하가 더 안전하다.

**Trigger 위치 일반 원칙**:

- Trigger는 DB insert 자체가 아니라 **canonical Redis latest write-through 성공 지점** 이후에 둔다.
- USDT/KRX처럼 direct write source는 writer success 직후 hook이 적절하다.
- 은행/Investing처럼 mirror cycle 기반 source는 trigger 위치를 별도로 설계해야 한다. 단순 DB insert 직후 trigger는 Redis latest와 publish payload 시점이 어긋날 수 있다.

**2026-05-22 보강 — PR4 Step B 보류 (Bank/Investing 구조 재설계 phase 이후 재검토)**:

main.py legacy topic hook은 단순 fallback이 아니라 **은행(kb/hana) + Investing USD/KRW 변경을 fx:* 및 usdt:krw topic으로 발사하는 primary bridge** 역할을 포함한다. 코드 검증 ([app/main.py:716-735](app/main.py#L716-L735)) — `is_changed` 분기 안에서 `safe_publish_tether_tab_snapshot` + `safe_publish_all_fx_snapshots` 두 publish 모두 호출.

은행/Investing은 현재 mirror cycle 기반 구조 — DB SELECT 후 중복 검사 → DB INSERT commit 후 helper로 Redis direct write ([app/crud.py:102-153](app/crud.py#L102-L153)). source-level topic trigger router는 부재. 특히 kb/hana/investing usd-krw는 fx:usd-krw와 usdt:krw 양쪽 topic에 영향을 주므로 단순 tether trigger 추가가 아니라 **수집/저장/알림/topic publish 전체 구조 재설계**가 필요하다.

**미래 구조 옵션** (확정 보류, 별도 phase에서 검토):

- **α USDT 패턴 그대로**: 매 fetch → Redis SET + trigger (단순, coalesce 흡수 의존). 같은 값 반복 publish 우려.
- **β 절충 (현재 유력 후보)**: Redis는 매 fetch에서 seen_at/mirrored_at 갱신, rate/timestamp는 값 변경 시만 갱신. **topic trigger는 기본적으로 rate 변경 또는 topic payload에 의미 있는 state 변화(예: freshness 회복, stale 복구)가 있을 때 발화**. successful fetch 자체는 seen_at/mirrored_at 갱신으로 기록하되, 같은 rate 반복 fetch가 항상 topic publish를 유발하지는 않는다. observation freshness 정확 + publish 효율적.
- **γ 보수**: Redis SET/trigger 모두 값 변경 시만, freshness는 별도 추적.

DB 쿼리 최소화 input: 현재 DB INSERT 전 중복 검사는 DB SELECT. 미래 구조에서는 **Redis 값 기반 dedup**으로 DB SELECT 회피 가능 ([REALTIME_ARCHITECTURE_PLAN.md §1](REALTIME_ARCHITECTURE_PLAN.md) "DB를 실시간 전송 경로에서 분리, latest는 Redis에서 관리" 방향 일관).

각 옵션의 timestamp 분리 정책 (`rate_changed_at` vs `seen_at` vs `mirrored_at`)은 별도 phase에서 확정. 자세한 내용은 [USDT_TOPIC_MIGRATION_PLAN.md §6.6](USDT_TOPIC_MIGRATION_PLAN.md) 참조.

따라서 main.py hook 완전 제거(Option A)는 Bank/Investing 구조 재설계 phase 이후 재검토한다. Option B (fallback 격하)도 "main.py hook이 은행/Investing primary path 역할을 포함"한다는 사실과 어긋나 현재 상황에는 부적합 — 현재 상태 그대로 유지가 가장 정확한 표현이다.

### 14.8 Non-scope (잠금)

- **FX topic trigger 변경**: `fx:usd-krw` 등 별도 publisher 그대로 (FX는 legacy data 변경과 동기화 의미 있음 — 분리 보류)
- ~~**REST polling 제거**~~ **상태 변경 (2026-05-25, `ed0885c`)**: USDT 상시 REST polling cron(`collect_usdt_rates` 매분 6회)을 `USDT_LEGACY_REST_POLLING_ENABLED=false` default로 비활성화. 5b/5d series로 WS fanout이 Redis(tick path + 5s grain coalescing) / DB(1초 window writer) / Alert(coalescer) 책임 처리 + source-specific REST fallback probe가 stale 시 동일 fanout(`fetch_*_usdt_tick` helper) 재사용 — 상시 polling 중복. `app/crawlers/usdt_sources.py`의 `fetch_*_usdt_tick` helper는 WS fallback이 재사용하므로 **함수/모듈 보존**. Rollback: env `USDT_LEGACY_REST_POLLING_ENABLED=true` + `docker compose up -d --force-recreate fastapi`로 cron 복원. additive guardrail 단계는 통과, polling 제거 (flag-toggled) 단계 진입.
- **iOS/Android 클라이언트 변경**: Phase B.2 minimum scope 외 (별도 phase)
- **Bithumb~Gopax WebSocket 확장**: Phase B.3 영역
- **legacy piggyback 즉시 삭제**: PR4에서 telemetry 검증 후 결정
- **Multi-process atomic claim** (Redis pub/sub coalesce): future Phase (Phase Z-2 Phase 2 영역)

**참고 (2026-05-22)**: 본 non-scope는 PR4 Step B minimum scope 한정 표현이다. §14.7 2026-05-22 보강의 Bank/Investing 구조 재설계 phase에서는 fx:* trigger routing도 함께 재검토한다 ([USDT_TOPIC_MIGRATION_PLAN.md §6.6](USDT_TOPIC_MIGRATION_PLAN.md) 참조).

### 14.9 24h Soak 처리

- Phase B.1 24h Soak는 background로 계속 유지 (Stage 0 활성화 2026-05-15 01:01 KST 이후)
- PR1은 mode default `legacy_piggyback`이라 단말 publish 영향 0 → 즉시 진행 가능
- PR2/PR3 default `legacy_piggyback`:
  - **단말 publish 영향 0** (controller strict noop early return, publish 미발화)
  - **단, trigger telemetry Redis HSET은 발생** (`topic:tether:stats` hash의 `trigger_skipped_legacy` counter + `trigger_last_*` fields). best-effort 격리 (`circuit.record_failure` 미호출, hot path 보호) — Redis 장애 시 silent skip.
  - 빈도: PR2 USDT Upbit tick-level → 매 100~500ms (~수만/일 추정), PR3 KRX 1초 window + 변경 시 → 매 1초 이하 (~수천/일 추정).
  - 의도: dual_shadow → direct_coalesced 전환 안전성 baseline 측정 (trigger 빈도 + coalesce 효율 사전 관찰).
- PR4 Step A는 24h Soak 통과 후 `dual_shadow` 없이 `direct_coalesced`로 직접 진입. 이유: 운영 App Store 단말에는 테더 탭이 없고, iOS dev 단말로 직접 검증 가능하며, direct 모드에서 baseline counter 측정이 가능했기 때문.

### 14.10 진입 GO 조건 (사용자 결정)

위 plan으로 Phase B.2 PR1 진입. Codex 추가 보정 round 후 PR1 commit/push → EC2 배포 → PR2 진입 cycle.

**진행 update (2026-05-16)**:

- PR1 `f16d908`, PR2 `2d6cece`, PR3 `7c67e67` commit/push + EC2 배포 완료.
- `2026-05-16 15:16 KST`: `TETHER_TOPIC_TRIGGER_MODE=direct_coalesced` 운영 활성화.
- `dual_shadow`는 건너뜀. 운영 단말에는 테더 탭이 없고, iOS dev 단말로 직접 검증 가능하며, direct 모드에서도 coalesce/publish baseline counter를 측정할 수 있었기 때문.

### 14.11 운영 활성화 결과 (2026-05-16)

**Step A — 비구독 path 검증**:

- iOS dev 단말을 테더 topic 비구독 상태로 두고 `direct_coalesced` 전환.
- `trigger_skipped_legacy` 정지 확인 (`12970` 유지).
- `trigger_publish_called == hook_called == skipped_no_subscribers` 확인.
  - 15:18 → 15:29 delta: `511 == 511 == 511`
- `built`, `publish_called`, `publish_sent_total`, `trigger_publish_success` 변화 없음.
- `trigger_last_window_ms=500.01`, coalesce 효율 약 63%, publish flush rate 약 0.77/sec.
- warning/error 없음.

**Step B — 구독 path 검증**:

- iOS dev 단말에서 테더 탭 진입 후 `usdt:krw` topic 구독.
- `trigger_publish_success`, `built`, `publish_called`, `publish_sent_total` 동시 증가 확인.
  - 15:29 → 15:33 delta: `trigger_publish_success +43`, `built +43`, `publish_called +43`, `publish_sent_total +43`
- `last_result=sent`, `trigger_last_result=publish_success`.
- iOS dev 단말에서 Upbit 가격 갱신이 거의 실시간으로 체감됨.

**해석 기준**:

- `trigger_publish_called`는 controller가 publisher를 호출한 횟수.
- publisher `hook_called`는 `safe_publish_tether_tab_snapshot` 진입 횟수이며 `trigger_publish_called`와 대응.
- publisher `publish_called`와 `trigger_publish_success`는 실제 `usdt:krw` 구독자가 있을 때만 증가한다.

### 14.12 장기 로드맵 합의 (2026-05-16)

**공통 목표**: 신규 단말 앱은 topic 구독 모델만 사용하고, broadcast cycle은 구버전 호환용으로 유지한 뒤 장기적으로 deprecate한다.

| 단계 | 목표 | 비고 |
|---|---|---|
| 현재 | `direct_coalesced` 활성, Upbit + KRX trigger, REST polling 유지, main.py legacy hook 유지 | iOS dev 단말 실시간 체감 검증 완료 |
| 중기 | Bithumb/Coinone/Korbit/Gopax WS 확장, 은행/Investing trigger path 설계, main.py hook Option B fallback 격하 | 모든 테더 탭 표시 자산이 trigger source를 갖추는 방향 |
| 장기 | 모든 표시 자산 trigger 완성 후 main.py legacy hook Option A 완전 제거. REST polling은 WS primary + REST fallback 전용으로 격하. 신규 앱은 topic-only, broadcast는 구버전 호환 후 deprecate | 완전 제거는 rollback 안전망과 구버전 단말 통계 확인 후 |

**Trigger source 분류**:

| Source 유형 | 현재 Redis write 패턴 | Trigger 상태 / 방향 |
|---|---|---|
| Upbit WS | tick-level direct SET | 구현 완료 (PR2) |
| KRX | `KrxDbWriter` direct SET | 구현 완료 (PR3) |
| Bithumb/Coinone/Korbit/Gopax REST | REST polling direct SET | 장기적으로 WS 전환 후 writer success hook. REST fallback 전용 전환 전까지는 별도 검토 |
| Investing USD/KRW | mirror cycle 3초 | trigger 위치 재설계 필요 |
| 은행 USD/KRW (KB/Hana 등) | mirror cycle 3초 | trigger 위치 재설계 필요 |

## 15. 참조

- [USDT_EXCHANGE_WEBSOCKET_GUIDE.md](USDT_EXCHANGE_WEBSOCKET_GUIDE.md): 거래소별 WS spec (2026-04-28 검증)
- [USDT_TOPIC_MIGRATION_PLAN.md](USDT_TOPIC_MIGRATION_PLAN.md): Z-2 series, topic protocol 마이그레이션 history
- [KRX_FANOUT_REFACTOR_PLAN.md](KRX_FANOUT_REFACTOR_PLAN.md): 재사용할 fanout 패턴 (A/B/C/A-pre 완료)
- [DECISIONS.md ADR-029](DECISIONS.md): USDT direct write + read-path DB fallback
- [DECISIONS.md ADR-031](DECISIONS.md): KRX Redis 통합 (유사 1차 부채 해소 패턴)
- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md): "알림 모든 tick, DB window close" 원칙
- [app/crawlers/usdt_sources.py](app/crawlers/usdt_sources.py): 현재 REST 수집 구조
- [app/crawlers/krx_kis.py](app/crawlers/krx_kis.py): 재사용할 KRX fanout 구현
