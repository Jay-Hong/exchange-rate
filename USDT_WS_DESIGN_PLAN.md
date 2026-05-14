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

> ⚠️ **본 절은 [USDT_EXCHANGE_WEBSOCKET_GUIDE.md](USDT_EXCHANGE_WEBSOCKET_GUIDE.md) (2026-04-28 검증본) 요약이며, 본 design doc에서 새로 공식 문서를 재검증하지 않음**. 최신 공식 확인표는 *Phase B.0 산출물* (구현 진입 직전 web 재확인 task).

| Source | Endpoint | Subscribe 단일 KRW-USDT | Price 필드 | Heartbeat |
|---|---|---|---|---|
| **upbit** | `wss://api.upbit.com/websocket/v1` | ✓ `["KRW-USDT"]` | `trade_price` | WS ping 30s |
| **bithumb** | `wss://ws-api.bithumb.com/websocket/v1` | ✓ `["KRW-USDT"]` (Upbit 호환) | `trade_price` | WS ping 30s |
| **coinone** | `wss://stream.coinone.co.kr` | ✓ TICKER channel (`KRW`+`USDT`) | `data.last` | App PING 5분 (서버 30분 idle) |
| **korbit** | `wss://ws-api.korbit.co.kr/v2/public` | ✓ `usdt_krw` | `data.close` | WS ping 30s |
| **gopax** | `wss://wsapi.gopax.co.kr` | ✗ 전체 ticker 구독 (서버 필터) | `last` | Primus `::ping::` 30s |

**Reconnect**: 거래소 공통 exponential backoff (1s → 2s → 4s ... 30s cap)

**검증 필요 (구현 진입 시 web 재확인)**:
- 모든 endpoint 도메인/path 변경 없음 확인
- subscribe payload 형식 변경 없음 확인
- price 필드 (이름/타입) 변경 없음 확인
- heartbeat 규칙 변경 없음 확인
- rate limit 변경 없음 확인 (특히 connection 수)
- 재연결 권장 방식 변경 없음 확인

→ 본 design doc은 2026-04-28 guide 기준 작성. **구현 진입 GO 직전에 공식 문서 재확인 step 필수** (별도 task, web search 사용).

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

## 6. 알림 기준 변화 옵션

**현재**: `_process_source_alerts_safe(db, changed_rates)` — DB INSERT 발생한 *changed_rates*만 평가.

**옵션 A — DB row 기반 유지** (변경 X):
- 장점: 기존 코드 재사용, 회귀 위험 최소
- 단점: WebSocket tick 빈도 ↑ + DB insert-if-changed 적용 시 *짧은 시간 안 임계값 통과 + 회복* 시 알림 누락

**옵션 B — Tick/observation 기반**:
- 모든 WS tick에서 알림 evaluator 호출 (DB insert 여부 무관)
- 장점: threshold crossing 정밀도 ↑ (모든 임계값 통과 캡쳐)
- 단점: 알림 빈도 증가 가능성, 중복 알림 방지 로직 강화 필요 (cooldown / triggered flag 등)

**옵션 C — Hybrid (per-tick 평가 + 가격 변화 임계값)**:
- Tick 기반 평가 + 마지막 평가 가격과 비교
- 미세한 가격 변동 (예: 1원 미만)은 skip
- 옵션 B 정밀도 + 노이즈 감소

**추천**: **옵션 B** (KRX/REALTIME_ARCHITECTURE_PLAN "알림은 모든 tick" 원칙과 일관). 단, **이는 사용자 가치 변화** — 알림 빈도/정밀도 trade-off.

→ **결정 대기 (사용자 가치 영역)**:
- A/B/C 선택?
- B 채택 시 cooldown 정책 (per-setting / per-rate change / time-based)?

## 7. DB 저장 정책 — 기존 close/last 유지

[USDT_EXCHANGE_WEBSOCKET_GUIDE.md §9](USDT_EXCHANGE_WEBSOCKET_GUIDE.md) 권장: *"모든 tick INSERT 금지. 최소 changed 또는 1초 last 정책"*.

**1차 결정**: 기존 `insert_source_rate_if_changed` **그대로 유지**.

- WebSocket tick 빈도 (Upbit 초당 다수) ≫ 10s REST polling
- 모든 tick INSERT 금지 — DB 폭증 위험
- 기존 `insert_source_rate_if_changed`는 가격 변경 시만 INSERT → 자연 deduplication
- WS tick에 적용 시: 동일 가격 연속 tick → INSERT 0회 (기존 동작)

→ **결정 대기**: 기존 유지 (추천)? 또는 1초 last writer 별도 도입?

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
- 알림 옵션 B 채택 시: 알림 빈도/정밀도 baseline (cooldown 정책 효과 확인)

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

## 10. 결정 대기 항목 요약

본 doc 작성 시점에 *옵션 + 추천*만 제시. 구현 GO 전 사용자 결정 영역:

| # | 항목 | 옵션 | 추천 |
|---|---|---|---|
| 1 | Canary 거래소 | Upbit / Bithumb / Coinone / Korbit / Gopax | **Upbit** (§4) |
| 2 | REST fallback 정책 | A (continuous), B (silent probe), C (hybrid) | **B** + silent threshold (§5) |
| 3 | 알림 기준 변화 | A (DB row 유지), B (tick 기반), C (hybrid) | **B** + cooldown 정책 (§6) ★ **사용자 가치 영역** |
| 4 | DB 저장 정책 | 기존 유지 / 1초 last writer 별도 | **기존 유지** (§7) |
| 5 | Feature flag 명명 | `USDT_WS_<EXCHANGE>_ENABLED` per-exchange | per-exchange (§8) |
| 6 | OHLC 1차 도입 여부 | 도입 / **deferred** | deferred (§9) |
| 7 | 공식 WS 문서 재확인 trigger | Phase A 끝 / 구현 진입 직전 | **구현 진입 직전** (§2) |

★ #3 (알림 기준)은 *사용자 가치 trade-off*라 사용자 결정 영역.

## 11. 구현 진입 순서 (참고 — 본 doc 범위 외)

설계 GO 후 implementation phase 흐름:

1. **Phase B.0**: 공식 WS 문서 재확인 (§2 + Codex 7 spec checklist). 특히 *Bithumb subscribe payload Upbit 호환성* (§4)과 *Bithumb/Korbit heartbeat 정책 명시 여부* (§5 provisional threshold) 확정 필요.
2. **Phase B.1**: Upbit canary 구현
   - `UsdtWsClient` + `UsdtLivenessMonitor` + `UsdtRestFallbackController` skeleton
   - `UsdtRedisLatestWriter` (기존 `set_latest_usdt_rate_from_sync_job` 재사용)
   - `UsdtAlertEvaluator` (옵션 B 결정 시)
   - `USDT_WS_UPBIT_ENABLED=false` default
   - Unit tests + smoke 기준
3. **Phase B.2**: Canary 활성화 + 24h 운영 관찰
4. **Phase B.3**: Bithumb 확장 (Upbit 호환)
5. **Phase B.4**: Coinone / Korbit / Gopax 순차 확장
6. **Phase B.5**: 5거래소 모두 활성 + 기존 REST polling 격하/제거

## 12. 참조

- [USDT_EXCHANGE_WEBSOCKET_GUIDE.md](USDT_EXCHANGE_WEBSOCKET_GUIDE.md): 거래소별 WS spec (2026-04-28 검증)
- [USDT_TOPIC_MIGRATION_PLAN.md](USDT_TOPIC_MIGRATION_PLAN.md): Z-2 series, topic protocol 마이그레이션 history
- [KRX_FANOUT_REFACTOR_PLAN.md](KRX_FANOUT_REFACTOR_PLAN.md): 재사용할 fanout 패턴 (A/B/C/A-pre 완료)
- [DECISIONS.md ADR-029](DECISIONS.md): USDT direct write + read-path DB fallback
- [DECISIONS.md ADR-031](DECISIONS.md): KRX Redis 통합 (유사 1차 부채 해소 패턴)
- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md): "알림 모든 tick, DB window close" 원칙
- [app/crawlers/usdt_sources.py](app/crawlers/usdt_sources.py): 현재 REST 수집 구조
- [app/crawlers/krx_kis.py](app/crawlers/krx_kis.py): 재사용할 KRX fanout 구현
