# 실시간 아키텍처 마이그레이션 플랜 (v0.9 draft)

> 🔖 **단일 진리 source 명시 (2026-05-06)**:
>
> 본 문서는 **서비스 출시 계약**에 대한 단일 진리 source다. 본문 §2 "현재 구조"는 **현재 구현된 백엔드 경로**를 묘사하고, §4 이후 "목표 구조" / §5 Topic 설계 / §11 dual-emit 전략 등은 **서비스 출시 계약**을 정의한다. 둘은 **서로 다른 계층**이므로 본 문서를 읽을 때 항상 "현재 구현(legacy compat)" vs "목표 계약(topic-only/dual-emit)" 구분 필요.
>
> **핵심 운영 사실 (2026-05-06)**:
> - 운영 앱(iOS 2026-01-21 / Android 2026-03-13 출시)에는 **테더 탭 없음**
> - 백엔드 USDT 5거래소 / KRX 미국달러선물 수집은 운영 진행 중
> - **현재 구현은 USDT를 legacy `rates` 배열에 포함**시킨 상태 — 이는 iOS dev/test 단계의 임시 모델 (test-era compatibility 경로)
> - **서비스 출시 계약 = topic-only Tether/KRX + legacy FX dual-emit** ([DECISIONS.md ADR-028](DECISIONS.md))
>
> **계약 요약**:
> - legacy `rates` 채널 = USD/JPY/EUR + Investing/은행 9개 한정. **테더 탭 데이터(USDT/KRX) 미포함**
> - 새 topic 채널 = `fx:*` / 테더 탭 토픽 / `krx:*` / `dxy` / `graph:*` / `news`
> - dual-emit 범위 = 환율 탭 데이터에만 (legacy + topic 둘 다). 테더/KRX는 topic만 발사
>
> 관련 ADR: [ADR-026](DECISIONS.md) (Redis-first hot path) / [ADR-027](DECISIONS.md) (KRX REST/stale 정책) / [ADR-028](DECISIONS.md) (Topic-only Tether/KRX + legacy FX dual-emit)
>
> 📝 **상태 (v0.9 draft, 2026-05-12)**: PR6 Stage 1 canary 진행 중 (`KRX_FUTURES_ENABLED=true`로 DB 저장만 활성, legacy 노출은 Z-2d legacy_policy allowlist로 차단). PR6e 운영 보강 적용. ADR-028 신설로 topic-only/dual-emit 계약 고정. Z-2d cleanup(2026-05-12)으로 `KRX_BROADCAST_INCLUDE` env 제거 — legacy 노출 정책 단일화.
> 📝 **이전 상태 (v0.8 draft, 2026-05-04)**: PR3-PR5 구현/배포/24h+IN mode 측정 완료 — broadcast hot path DB-free 달성. ADR-026 / CHANGELOG / 본 문서 PR3 섹션에 최종 수치 반영 완료. 후속은 영업시간 Redis read jitter 진단과 PR6(KRX futures / USDT WebSocket) 영역.
> 🎯 **목적**: 1초 단위 실시간화 + 거래소 WebSocket + 구독 기반 라우팅으로의 단계별 전환을 위한 합의 문서
> 🔄 **변경 이력**: v0.8 — PR3 Step 1-5 + PR3.5 (계측) + PR4 (MGET) + PR5 (DXY mirror) 시퀀스 완료 (2026-05-03). broadcast hot path에서 rates + DXY spot DB SELECT 제거. PR5 24h 관측(n=31495)에서 rates/DXY Redis hit 100%, dxy_query_ms 0건, payload_build_ms p99 30.25ms 확인. IN mode 30분 관측(n=1801)은 p99 75.97ms로 영업시간 Redis read wall-clock jitter 증가를 확인했지만 DB fallback/DXY fallback은 0건. ADR-026 / CHANGELOG / 본문 PR3 섹션에 최종 수치 반영 완료.
> 🔄 **이전 변경**: v0.7 — 24h fast window PoC(2026-05-01 15:53~) 진행 중. Performance Insights + EXPLAIN ANALYZE 분석에서 16:00 spike 확대 구간(8분 PI window) 기준 wait event가 CPU 단일로 관측되고 LWLock/Lock/IO:WalSync wait 0건 확인. 같은 구간 PI Top SQL에서 bank+source latest SELECT가 부하의 대부분(bank 0.21 + source 0.15 AAS = 0.36)을 차지. Phase 2 작업 순서 재배열: **PR3(Redis-first broadcast)를 첫 PR로 격상**. PR3 설계 완전 합의(env / Redis schema / stale 판정 / fallback reason / 모듈 구조 / metric). cache.py·crud.py 변경 0, 신규 `app/latest_rates_cache.py` 모듈 분리. 자세한 설계는 12.Phase 2 섹션의 PR3 서브섹션 참고.
> 🔄 **이전 변경**: v0.6 — Phase 1 운영 진입 + PR2 window 임시 PoC 성공 (2026-04-30 20:00-20:17 KST). payload_build_ms p99 302ms / send timeout 0건 / misfire 0건. Investing 403 플래핑은 PR2 무관(외부 변동성)으로 분리.

---

## 1. 목표와 비목표

### 목표

- **거래소(USDT) 탭의 실시간성 강화** — 거래소 tick은 실시간 수신하되, 사용자 화면은 서버 수신 후 1초 이내 반영 (김치프리미엄 시나리오)
- **WebSocket Broadcasting 주기 단축** (10초 → 1초) 또는 event-driven 전환
- **구독 기반 토픽 라우팅** — 탭별로 필요한 데이터만 전송, 불필요한 대역폭 제거
- **거래소 데이터 수집 방식 전환** (REST polling → 공식 WebSocket ticker)
- **DB를 실시간 전송 경로에서 분리** — tick을 모두 INSERT하지 않고 latest state는 Redis에서 관리
- **All-source observation fanout contract** — 모든 source(USDT 5거래소 / Bank 9개 / Investing / KRX 미국달러선물)가 동일한 observation fanout 계약을 따르되, source-specific 정책 값만 분기. 통합 계약은 Redis latest / DB writer / Alert evaluator / Topic trigger 4갈래 fanout과 freshness metadata semantics를 정의. 상세는 §4.1

### 비목표 (이번 마이그레이션에서 다루지 않음)

- 은행 크롤링 주기 1초 단축 — 은행 고시 빈도 자체가 분 단위라 비용 대비 이득 적음
- 기존 그래프 API의 전면 재설계 — 실시간 tick과 분리된 저빈도 topic으로 충분
- ms 단위 tick의 직접 노출 — 사람 인지 한계(~100ms)로 의미 없음
- DB 스키마 전면 재설계 — 알림/그래프 보관 정책만 조정

---

## 2. 현재 구조 요약

```text
[수집]
크롤러(11개) + USDT(5개)
  └─ APScheduler cron job (10초 단위)
      └─ HTTP request → BeautifulSoup parse → DB INSERT (변경 시에만)

[전송]
Broadcasting cron job (매분 00,10,20,30,40,50초)
  └─ build_rates_payload (DB SELECT 30 row + USDT legacy adapter)
  └─ Redis BROADCAST_CACHE_KEY와 JSON diff
  └─ 변경 시에만 → manager.broadcast (순차 send_json) → 전체 클라이언트

[알림]
USDT 수집 완료 후 → changed_rates 기반 → 같은 트랜잭션에서 process_source_rate_alerts
```

### 핵심 코드 참조

- 크롤러 cron: [app/scheduler.py:591](app/scheduler.py#L591) (investing), [app/scheduler.py:1428](app/scheduler.py#L1428) (USDT)
- Broadcasting: [app/main.py:347](app/main.py#L347) `broadcast_rates_once`, [app/main.py:201](app/main.py#L201) `build_rates_payload`
- Connection 관리: [app/main.py:155](app/main.py#L155) `ConnectionManager`, [app/main.py:171](app/main.py#L171) 순차 broadcast
- Graph 결합: [app/main.py:360-364](app/main.py#L360-L364) (broadcast 변경 시 graph_buckets 결합)
- Snapshot 캐시: [app/main.py:422](app/main.py#L422) 단일 BROADCAST_CACHE_KEY
- USDT 알림: [app/crawlers/usdt_sources.py:149](app/crawlers/usdt_sources.py#L149) `changed_rates`, [app/crawlers/usdt_sources.py:189-202](app/crawlers/usdt_sources.py#L189-L202) DB 트랜잭션 내 동기 호출
- Investing TLS 우회: [app/crawlers/investing.py:102](app/crawlers/investing.py#L102) curl_cffi `safari17_0` (ADR-018)

---

## 3. 문제점

| # | 문제 | 영향 |
|---|------|------|
| 1 | REST polling이 10초 단위 — 거래소 가격 급변동 시 평균 5초 지연 | USDT 김치프리미엄 시나리오에 부족 |
| 2 | 전체 broadcast 단일 채널 — 모든 클라이언트에 동일한 전체 payload | 탭별 차등 전송 불가, 대역폭 낭비 |
| 3 | `manager.broadcast` 순차 전송 ([main.py:175-180](app/main.py#L175-L180)) | 1초 broadcast + 다수 연결 시 직렬 await가 병목 |
| 4 | `--disable-javascript`가 SELENIUM_OPTIONS에 박혀 있음 ([constants.py:49](app/crawlers/constants.py#L49)) | Investing 자동 업데이트 감시용 long-running browser 시도 시 별도 옵션 필요 |
| 5 | broadcast마다 graph_buckets DB 조회 ([main.py:360](app/main.py#L360)) | 1초 broadcast로 가면 매초 DB 조회 — 부담 |
| 6 | snapshot 캐시가 단일 `BROADCAST_CACHE_KEY` ([main.py:422](app/main.py#L422)) | 토픽 분리 후에도 snapshot이 분리되지 않으면 재연결 시 비효율 |
| 7 | 알림 평가가 DB INSERT 흐름에 강결합 ([usdt_sources.py:189-202](app/crawlers/usdt_sources.py#L189-L202)) | tick → Redis 흐름으로 가면 알림 판단 경로가 끊김 |
| 8 | tick을 모두 INSERT하면 `source_rates`가 폭증 | 5 거래소 × 초당 N tick × 10일 보관 시 수백만~수천만 row |

---

## 4. 목표 구조

```text
[수집]
거래소 5종 WebSocket collector  ─┐
Investing live channel (조사 중) ─┤
은행 크롤러 (기존 유지)          ─┤
                                  ▼
                       [Latest State Store]
                       (Redis tick + memory hot path)
                                  │
                ┌─────────────────┼──────────────────┐
                ▼                 ▼                  ▼
         [Broadcast Router]  [Alert Evaluator]  [DB Writer]
         (debounced)         (meaningful obs.)  (정책 기반)
                │                 │                  │
                ▼                 ▼                  ▼
          Topic Subscriber   FCM 발송          source_rates
          (탭별 라우팅)                         (1초 last 또는 OHLC)
```

핵심 원칙:
- **DB는 실시간 전달 경로에서 빠짐** — Redis latest state가 진짜 hot path
- **Broadcast / Alert / DB는 독립 흐름** — 각자 다른 정책
- **클라이언트는 토픽 구독으로 필요한 것만 받음**
- **legacy 전체 broadcast는 dual-emit으로 일정 기간 병행**

### 4.1 All-source observation fanout contract

> 📌 **목적**: USDT 5거래소 / Bank 9개 / Investing / KRX 미국달러선물 — 모든 source가 같은 fanout 계약을 따르도록 구조 통일. 정책 값(임계값/debounce/fallback 조건)은 source-specific으로 유지.
> 📌 **단일 진리 위치**: 본 sub-section은 freshness metadata 용어 + observation type의 정의 anchor. 다른 문서는 본 절을 cross-ref하고 별도 정의 금지.
> 📌 **Status는 phase docs 책임**: 본 anchor 문서는 *계약/원칙*만. source별 현재 상태와 phase 진척은 KRX_FANOUT / USDT_WS / USDT_TOPIC 등 phase docs에서 관리 (anchor drift 방지).

#### 4.1.1 핵심 원칙

1. **Observation fanout 4갈래** — 모든 source에서 observation 수신 → ① Redis latest write / ② DB writer / ③ Alert evaluator / ④ Topic trigger 4갈래로 fanout
2. **계약 구조는 통일, 정책 값은 source-specific** — fanout 갈래는 모든 source 공통, 임계값/debounce window/fallback trigger 조건은 source별로 분기
3. **Source-specific lifecycle은 계약 외부에 잔존** — 거래소별 세션/만기/contract rollover/close finalizer scheduling 등은 fanout 계약과 직교

#### 4.1.2 Observation type

모든 source는 다음 type 중 하나 이상을 발생:

- `tick` — WS frame 또는 REST polling으로 수신되는 정상 가격 observation
- `rest_probe` — stale-based REST fallback observation (USDT 5거래소 RestFallbackController / KRX KrxRestFallbackController Stage B+)
- `close_snapshot` — session boundary observation (현재 KRX-only, CF 15:45 / CM 06:00). 다른 source 추가 시 동일 type으로 흡수

#### 4.1.3 Freshness metadata terminology (단일 정의 anchor)

각 source의 latest state는 다음 3개 timestamp를 함께 기록:

| 용어 | 의미 | 갱신 시점 |
| --- | --- | --- |
| `rate_changed_at` | 가격 값이 마지막으로 *변경*된 시점 | observation이 dedup 통과 (값 != 이전 latest) |
| `seen_at` | observation을 마지막으로 *수신*한 시점 (dedup 무관) | 매 observation |
| `mirrored_at` | Redis latest mirror가 마지막으로 *갱신*된 시점 | Redis write 성공 |

**왜 분리하는가** — 현재 일부 source(KRX ADR-031 1차)는 "값 동일 → Redis write skip" 정책이라 Redis timestamp가 *stale*해 보이는 false positive 발생. `seen_at` 기록으로 *데이터는 살아있는데 값만 안 바뀜* 상태를 명확히 표현 가능. `mirrored_at`은 mirror cycle 자체의 health 측정용 (ADR-026 PR5 `mirror_age_ms`와 정합).

#### 4.1.4 Contract-common vs source-specific 경계

| 영역 | 분류 | 예시 |
| --- | --- | --- |
| Observation 수신 인터페이스 | contract-common | tick / rest_probe / close_snapshot type 통일 |
| Redis latest write 책임 | contract-common | `latest_rates_cache` helper 경유 |
| DB writer 책임 | contract-common | window debounce + insert-if-changed 패턴 |
| Alert evaluator 책임 | contract-common | source-neutral helper (`UsdtAlertEvaluator` 패턴) |
| Topic trigger 책임 | contract-common | `request_*_topic_trigger` 호출 |
| Freshness metadata fields | contract-common | `rate_changed_at` / `seen_at` / `mirrored_at` 모두 기록 |
| Dedup 정책 (값 변경만 vs 매 tick) | source-specific | USDT 매 tick Redis write / KRX DB-insert-bound (Stage E에서 전환 예정) |
| Liveness 임계값 | source-specific | Gopax 300/600, Coinone 60/300, Korbit 30/120 |
| REST fallback trigger 조건 | source-specific | Upbit normal→stale / Gopax `ticker_freshness=degraded` / KRX 60s+ silence (Stage A counter only) |
| Session 경계 / 만기 / contract rollover | source-specific 외부 | KRX-only (CF/CM session, 만기 swap, `_last_tick_at` reset) |
| Close finalizer scheduling | source-specific 외부 | KRX-only (CF 15:45 / CM 06:00 boundary, captured flag, REST fallback) |

#### 4.1.5 현재 주요 deviation 요약 (anchor 수준, 상세는 phase docs)

본 계약과의 현재 deviation 요약(2026-05-24 기준). 상세 status / Stage 진척은 각 phase doc에서 관리:

- **USDT 5거래소** — 계약 정합도가 가장 높음. 5 source 모두 RestFallbackController 보유 (trigger 조건만 source-specific). 매 tick → Redis write, source-neutral `UsdtAlertEvaluator` 운영 중. 남은 follow-up: Upbit saturation + freshness metadata 분리.
- **Bank 9개 / Investing** — DB-first monolithic 경로(crud.py 안에서 DB → Redis write helper → `process_rate_alerts` 직렬). Redis write와 alert는 crud.py에서 직접 처리, **Topic trigger만 main.py broadcast diff hook 경유**. β 옵션(observation-based fanout) 검토 중.
- **KRX 미국달러선물** — ADR-031 1차로 Redis write가 **DB-insert-bound**(tick-level 아님). Runtime alert evaluator 미연결 (`KrxAlertEvaluator`는 Stage D/F 후보). Stage E에서 tick-level Redis 전환 예정.

#### 4.1.6 통합 phase 진입 조건 및 cross-reference

본 계약의 전면 적용은 **multi-PR phase**로 진행. source별 작업분해는 다음 phase docs에서:

- USDT 측: [USDT_WS_DESIGN_PLAN.md §12.8.2](USDT_WS_DESIGN_PLAN.md)
- Bank/Investing 측: [USDT_TOPIC_MIGRATION_PLAN.md §6.6](USDT_TOPIC_MIGRATION_PLAN.md)
- KRX 측: [KRX_FANOUT_REFACTOR_PLAN.md §5.2 E~G](KRX_FANOUT_REFACTOR_PLAN.md)
- close_snapshot observation type 경계: [KRX_CLOSE_SNAPSHOT_PLAN.md](KRX_CLOSE_SNAPSHOT_PLAN.md)
- 관련 ADR: [ADR-027](DECISIONS.md) (KRX REST/stale) / [ADR-031](DECISIONS.md) (KRX Redis 통합 1차)

---

## 5. Topic 설계 (v1 minimal)

### 토픽 네임스페이스

```text
fx:<pair>           # 외환 — fx:usd-krw, fx:jpy-krw, fx:eur-krw
usdt:krw            # ⚠️ 잠정 명칭. 실제로는 multi-source 테더 탭 토픽
                    #   (5개 거래소 USDT + Investing/KB/Hana reference + Phase 2 KRX 달러선물)
                    #   정확한 토픽 이름과 snapshot/delta schema는 Phase 2 PR에서 확정 (미정 F)
dxy                 # ⚠️ 잠정 명칭. 달러지수 보조지표.
                    #   현물(`dxy`)은 달러 탭, 선물(`dxy_futures`)은 테더 탭 그래프 보조지표.
                    #   토픽 분리(`dxy:spot`/`dxy:futures`) vs 단일 토픽 instrument 분기는
                    #   Phase 2 PR에서 결정 (미정 F).
graph:<pair>:<range>  # 그래프 (저빈도) — graph:usd-krw:1d 등
news                # 뉴스 피드
```

### 메시지 종류 (v1)

- `hello` — 연결 직후 서버 → 클라이언트 (능력 광고)
- `subscribe` / `unsubscribe` — 클라이언트 → 서버
- `snapshot` — 토픽 구독 직후 또는 재연결 시 서버 → 클라이언트 (전체 상태)
- `delta` — 변경 발생 시 서버 → 클라이언트 (변경분만)
- `error` — 프로토콜/구독 오류

### v1 minimal 메시지 포맷

> ⚠️ 아래 메시지 예시는 v1 minimal 형식을 설명하기 위한 단순 예시다. 실제 테더 탭은 multi-source payload(5거래소 + Investing/KB/Hana + Phase 2 KRX)이며, 정확한 토픽 이름과 schema는 Phase 2 PR에서 확정한다 (미정 항목 F).

서버 → 클라이언트 hello:
```json
{
  "type": "hello",
  "protocol_version": 1,
  "supported_topics": ["fx:*", "usdt:krw", "dxy", "graph:*", "news"],
  "server_time": "2026-04-26T13:45:00+09:00"
}
```

클라이언트 → 서버 subscribe:
```json
{
  "type": "subscribe",
  "topics": ["usdt:krw", "fx:usd-krw"],
  "protocol_version": 1
}
```

서버 → 클라이언트 snapshot:
```json
{
  "type": "snapshot",
  "topic": "usdt:krw",
  "seq": 1,
  "data": {
    "exchanges": {
      "upbit": {"rate": 1472.5, "timestamp": "..."},
      "bithumb": {"rate": 1471.0, "timestamp": "..."}
    }
  }
}
```

서버 → 클라이언트 delta:
```json
{
  "type": "delta",
  "topic": "usdt:krw",
  "seq": 2,
  "changes": [
    {"source": "upbit", "rate": 1473.0, "timestamp": "..."}
  ]
}
```

### 프로토콜 버전 정책

- v1은 **minimal** — `protocol_version`, `supported_topics`만 핵심
- per-feature `topic_capabilities` 매트릭스는 **v2 이후 실제 필요 시 추가** (지금 만들면 over-engineering)
- 서버가 클라이언트 `protocol_version`을 못 받으면 **legacy mode** (subscribe 무시 + 기존 전체 broadcast 발사)

### 재연결 정책

- 클라이언트 재연결 시 구독 토픽들에 대해 **무조건 snapshot 재전송**
- v1에서는 seq 기반 incremental resync 미지원 (단순함 우선) — v2 검토 항목
- snapshot 캐시는 토픽별로 분리: `snapshot:fx:usd-krw`, `snapshot:usdt:krw`, ...

---

## 6. 거래소 WebSocket Collector 설계

### 기본 원칙

- 5개 거래소 각각 별도 long-running asyncio task
- 거래소별 끊김 시 자동 재연결 (exponential backoff: 1s → 2s → 5s → 10s → 30s)
- 재연결 실패 누적 시 REST fallback (기존 polling 코드를 보조 경로로 유지)
- Heartbeat / ping-pong 거래소별 정책 준수
- tick 수신 → Redis latest state 갱신 → broadcast/alert evaluator에게 신호

### Latest State Schema (Redis)

```text
tick:usdt:upbit  → {rate, timestamp, received_at}
tick:usdt:bithumb → ...
...
```

- `received_at`은 **우리 서버가 WS tick을 수신한 시점** (SLO 기준점)
- TTL은 짧게 (예: 10초) — stale 감지 + 자동 정리

---

## 7. 거래소 WS Endpoint 검증 체크리스트

> ⚠️ **이 섹션은 구현 직전에 별도 검증 필요**. 거래소 API는 자주 변경됨.

### 검증 항목 (거래소 5종 각각)

- [ ] 공식 public WebSocket endpoint URL 확인
- [ ] 인증 필요 여부 (public ticker 채널은 보통 무인증, 거래소별 차이)
- [ ] USDT/KRW 직접 ticker 채널 존재 여부 (없으면 orderbook → mid price 계산 필요)
- [ ] 메시지 포맷 (JSON / binary, 필드명, 시간 단위)
- [ ] Rate limit / 동시 연결 제한
- [ ] Heartbeat / ping-pong 주기 및 형식
- [ ] 끊김 빈도 SLA (24시간 모니터링 후 기록)
- [ ] 무료 tier 변경 가능성 (이용약관 확인)

### 거래소 5종 (1차 문서 검증 완료, 2026-04-28)

> v0.4: 공식 문서 + 일부 실연결 smoke test 기반 1차 검증. 상세 구현 가이드는 [USDT_EXCHANGE_WEBSOCKET_GUIDE.md](USDT_EXCHANGE_WEBSOCKET_GUIDE.md)를 따른다. **24시간 실 연결 SLA 측정은 Phase 2 직전에 별도** (코드 작업 동반).

| 거래소 | 상태 | endpoint | symbol | subscribe | heartbeat | ticker price 필드 | REST fallback |
|---|---|---|---|---|---|---|---|
| 업비트 (upbit) | ✅ 1차 검증 | `wss://api.upbit.com/websocket/v1` | `KRW-USDT` | `[{"ticket":"<uuid>"},{"type":"ticker","codes":["KRW-USDT"]}]` | 명시 미발견 (실무: idle ~120초, 주기 PING 권장) | `trade_price` | `GET https://api.upbit.com/v1/ticker?markets=KRW-USDT` |
| 빗썸 (bithumb) | ✅ 실연결 smoke test | `wss://ws-api.bithumb.com/websocket/v1` | `KRW-USDT` | `[{"ticket":"<uuid>"},{"type":"ticker","codes":["KRW-USDT"]},{"format":"DEFAULT"}]` | 미확인 (주기 PING 권장) | `trade_price` | `GET https://api.bithumb.com/v1/ticker?markets=KRW-USDT` |
| 코인원 (coinone) | ✅ 실연결 smoke test | `wss://stream.coinone.co.kr` | `quote_currency=KRW, target_currency=USDT` | `{"request_type":"SUBSCRIBE","channel":"TICKER","topic":{"quote_currency":"KRW","target_currency":"USDT"}}` | `{"request_type":"PING"}` | `data.last` | `GET https://api.coinone.co.kr/public/v2/ticker_utc_new/KRW/USDT` |
| 코빗 (korbit) | ✅ 1차 검증 | `wss://ws-api.korbit.co.kr/v2/public` | `usdt_krw` | `[{"method":"subscribe","type":"ticker","symbols":["usdt_krw"]}]` | 명시 미발견 (REST는 50 req/s, WS 별도) | `close` | `GET https://api.korbit.co.kr/v2/tickers?symbol=usdt_krw` |
| 고팍스 (gopax) | ✅ 1차 검증 | `wss://wsapi.gopax.co.kr` | `USDT-KRW` | `{"n":"SubscribeToTickers","o":{}}` | primus `"primus::ping::<ts>"` 30초 주기 (서버→클라), pong 응답 30초 내 필수 | `last` | `GET https://api.gopax.co.kr/trading-pairs/USDT-KRW/ticker` |

### 거래소 검증 요약

- **인증**: 5종 모두 public ticker 채널 무인증 ✓
- **USDT/KRW 단독 구독**: 업비트/빗썸/코인원/코빗 가능. 고팍스 ticker는 전체 구독 후 `USDT-KRW` 필터링 필요.
- **가격 산출**: 5종 모두 ticker 현재가 필드가 있어 orderbook mid price 계산 불필요.
- **메시지 포맷**: 모두 JSON 텍스트
- **표준 편차 — heartbeat**: 고팍스만 정확한 30초 ping/pong 명세, 코인원은 명시적 PING command, 나머지(업비트/빗썸/코빗)는 미명시 → Phase 2 PR에서 keep-alive 정책 별도 결정
- **연결 한계 (코덱스 권고 검증 항목)**: 고팍스 동시 연결 20개/IP 명시. 나머지는 미명시 (Phase 2 PR에서 24시간 모니터링)

### 잔여 검증 (Phase 2 직전 또는 Phase 2 PR 안에서)

- [x] **빗썸 WebSocket 실제 연결 smoke test** — 한국 빗썸(`ws-api.bithumb.com`)에서 `KRW-USDT` snapshot/realtime `trade_price` 수신 확인 (2026-04-28)
- [ ] **업비트 / 빗썸 / 코빗 heartbeat 정책** — 공식 명세 없으면 5~30초 client PING 보내며 idle timeout 파악
- [x] **약관/정책 1차 검증 완료** (2026-04-28) — 상세 표는 [USDT_EXCHANGE_WEBSOCKET_GUIDE.md §14](USDT_EXCHANGE_WEBSOCKET_GUIDE.md#14-약관정책-1차-검증). 시장 관행상 5종 모두 비교 서비스 운영 가능 확인
- [ ] 5종 약관 페이지(SPA) 직접 방문 + 미확인 항목(출처 표기·사전 승인) 보강 — Phase 2 PR 시점
- [ ] **24시간 실 연결 SLA 모니터링** — 끊김 빈도, 재연결 latency, 메시지 누락률 측정 (Phase 2 코드 작업 시점)
- [x] **코인원 ticker WS payload 필드명** — `wss://stream.coinone.co.kr`, DEFAULT 포맷 `data.last` 수신 확인 (2026-04-28)

---

## 8. Investing 실시간화 후보

### 우선순위

1. **WebSocket / SSE 채널 발견 시도** (DevTools Network 탭 분석)
   - `stream.forexpros.com` 류 endpoint 가능성
   - 발견 시 별도 long-running collector
   - **리스크**: 비공개 API, 변경 가능성, Cloudflare 추가 적용 가능
2. **Persistent JS-enabled headless browser**
   - SELENIUM_OPTIONS의 `--disable-javascript` 별도 우회 옵션 필요
   - Cloudflare 우회 별도 작업 (`undetected-chromedriver` 등)
   - 메모리 비용 (~200-400MB 상시) — t3.small 여유분 내에서 가능
3. **현재 curl_cffi cron 유지 + 주기 단축** (5초 정도)
   - 가장 안전한 fallback

### 결정 기준

PoC 단계에서 1번 → 2번 → 3번 순으로 시도. 모든 단계에서 **기존 curl_cffi cron을 fallback으로 유지**.

---

## 9. DB 저장 / 그래프 / 알림 정책

### DB 저장 정책 (실시간 분리 후)

| 데이터 | 현재 | 목표 |
|--------|------|------|
| 거래소 USDT tick | 변경 시 INSERT (10초 polling) | 1초당 최대 1 row 또는 1초 OHLC 압축 (정책 미정 → 미정 항목) |
| Investing 환율 | 변경 시 INSERT | 동일 (변경 빈도 낮아 그대로) |
| 은행 환율 | 변경 시 INSERT | 동일 |
| DXY 현물 (`instrument='dxy'`) | realtime + hourly + daily rollup | 동일 (달러 탭 그래프 보조지표) |
| DXY 선물 (`instrument='dxy_futures'`) | realtime 저장만 (rollup 없음) | **rollup 추가 필요** — 테더 탭 그래프 보조지표 노출 전 dxy_futures hourly/daily rollup + 조회 API 작업 (Phase 2 작업 항목) |

### 그래프 정책

- 실시간 tick과 분리된 **별도 토픽** (`graph:<pair>:<range>`)
- Push 빈도는 분 단위 (정확한 수치는 미정 항목)
- 기존 `build_graph_buckets` 결합 ([main.py:360-364](app/main.py#L360-L364)) 제거 — 1초 broadcast에 graph가 따라붙으면 매초 DB 조회

### 알림 정책

→ 11번 섹션 참조

---

## 10. 알림 경로 재설계

### 현재 흐름 (DB INSERT 동기 결합)

```text
USDT 수집 → DB INSERT (changed_rates) → 같은 트랜잭션에서 process_source_rate_alerts (동기)
```

코드 위치: [app/crawlers/usdt_sources.py:189-202](app/crawlers/usdt_sources.py#L189-L202), [app/crud.py:2064](app/crud.py#L2064)

### 목표 흐름 (Redis tick 기반 비동기)

```text
WS tick 수신
  ├→ Redis latest state 갱신 (즉시)
  ├→ Broadcast Router에 dirty 신호 (debounce 200~500ms)
  └→ Alert Evaluator (asyncio task, 별도 코루틴)
        ├→ 임계값 비교 + last_notified_at 체크 (Redis)
        ├→ 통과 시 FCM 발송
        └→ DB notification_logs 기록 (사후, 별도 트랜잭션)
```

### 핵심 원칙

- **Broadcast는 debounce하지만 알림은 meaningful observation을 누락하지 않는다**
  - 사용자가 임계값 통과 시점에 정확히 알림 받아야 함
  - debounce 윈도우(200ms) 안에 임계값을 한 번 통과 후 되돌아오면 UI에는 안 보이지만 알림은 잡아야 함
  - 단, 생략 금지 조건에 해당하지 않는 동일 가격 반복 frame은 결과가 변하지 않으므로 same-rate evaluation skip 가능
    (가격 알림 + 비교 알림 공통 정책 — 생략 금지 조건은 [USDT_WS_DESIGN_PLAN.md §13.10](USDT_WS_DESIGN_PLAN.md) 참조)
- **last_notified_at 윈도우 정책 미정** — 동일 설정에 대한 재발송 차단 시간 (예: 5분 / 30분 / 1시간) → 미정 항목
- **1회성 알림 동작 유지** — 발송 후 `triggered=true` 자동 비활성화, 사용자 토글 ON 시 재초기화 (현재 동작 그대로)

---

## 11. 클라이언트(iOS/Android) 마이그레이션 영향 + dual-emit 전략

### 운영 중인 앱 현황

- **iOS** 출시: 2026-01-21
- **Android** 출시: 2026-03-13
- 양쪽 모두 운영 중 — 첫 출시부터 신 프로토콜 옵션 불가

### Dual-emit 원칙

서버는 일정 기간 동안 **legacy 전체 broadcast + 새 topic delta를 동시 발사**.

#### 채널별 데이터 범위

| 채널 | 데이터 범위 |
|---|---|
| legacy `rates` (구버전 앱 호환) | USD/JPY/EUR + Investing/은행 9개만. **테더 탭 데이터(USDT 거래소 / KRX 달러선물)는 미포함** |
| 새 topic delta (신 프로토콜 앱 전용) | `fx:*`, 테더 탭 토픽(잠정 `usdt:krw`, multi-source), `dxy`, `graph:*`, `news` 등 모든 토픽 |

#### 발사 흐름 (예시 — FX 환율 변경 시)

```text
환율 tick 수신 → Redis 상태 갱신
  ├→ legacy: build_rates_payload (USD/JPY/EUR + 은행 9개) 전체 broadcast (구버전 앱)
  └→ topic: fx:usd-krw delta 등 신 프로토콜 클라이언트에만 전송 (신버전 앱)
```

#### 발사 흐름 (예시 — 거래소 USDT tick 시)

```text
거래소 USDT tick 수신 → Redis 상태 갱신
  ├→ legacy: 발사하지 않음 (구버전 앱은 USDT 인지 안 됨, 죽은 데이터 회피)
  └→ topic: 테더 탭 토픽 delta로 신 프로토콜 클라이언트에만 전송
```

- 구버전 앱: subscribe 메시지 안 보냄 → 서버가 legacy mode로 fallback → USD/JPY/EUR + 은행 9개만 수신
- 신버전 앱: hello 후 subscribe → 토픽별 delta만 받음 (테더 탭 포함)

#### 원천 데이터 재사용

같은 원천 데이터는 여러 topic payload에 재사용될 수 있다. 예를 들어 KB의 usd-krw 환율 데이터(source=kb, asset=usd-krw)는 legacy 달러 탭 호환 payload에도 포함되고, 테더 탭 topic의 비교 기준(reference)으로도 포함될 수 있다. 이는 **데이터 저장 경로 공유이지, legacy 채널에 테더 탭 데이터를 추가한다는 의미가 아니다.**

### 레거시 제거 기준

| 조건 | 액션 |
|------|------|
| iOS, Android 양쪽 모두 활성 구버전 < 1% **그리고** 최소 6개월 경과 | 레거시 broadcast 제거 후보 |
| 1~5% | 지원 종료 공지 + 업데이트 유도, 제거 보류 |
| 5% 이상 | 레거시 유지 |

→ **양쪽 플랫폼이 모두 1% 미만일 때만** 제거. 한쪽만 1% 미만이면 보류.

### Phase 2 이후 검토 항목

> ⚠️ 아래는 Phase 1 진입 minimum bar 밖. 미정 항목 B에서 후속 합의 대상으로 분류.

- iOS background에서 WebSocket 유지 정책 (iOS는 background WS가 까다로움 — push-only fallback?) — Phase 2 이후 검토
- 1초 broadcast의 모바일 배터리 영향 — Phase 2 신 프로토콜 PoC부터 본격 측정
- 앱 cold start 시 snapshot 수신 흐름 (현재 단일 캐시 → 토픽별 분리) — Phase 2 토픽 라우팅 도입 시 함께 결정
- 최소 지원 버전 정책 도입 여부 ("v2.0 이상만 지원" 라인 그어 레거시 부담 감축) — 별도 정책 결정

---

## 12. 단계별 마이그레이션

### Phase 0 — 계획 및 합의 (완료)

- ✅ 본 문서 (REALTIME_ARCHITECTURE_PLAN.md) v0.1 초안 → v0.2 합의본
- ✅ Phase 1 진입 종료 조건 합의 (미정 항목 A — minimum bar 결론형)
- ✅ Phase 1 PoC 평가 범위 minimum bar 합의 (미정 항목 B — foreground 한정)
- ✅ Phase 1 측정 메트릭 + 의사결정 절차 합의 (미정 항목 C — 정량 임계값 제외)
- ⏳ 거래소 WS endpoint 검증 (7번 섹션 체크리스트, Phase 2 직전)
- ⏳ Phase 2 도입 시점에 새 미정 항목 F 합의 (테더 탭 topic 이름 + schema)

> v0.2 commit 시점부터 **Phase 1 코드 설계/구현 작업 진입 가능**. 운영 배포 조건은 아니며 Phase 1 PR에서 계측/롤백/배포 절차를 별도 확인.

### Phase 1 — 1초 Broadcast PoC + 측정

**목적**: 1초 broadcast로 갈 때 서버 한계를 데이터로 확인.

**작업**:
1. `manager.broadcast` 직렬 → `asyncio.gather` 병렬화
2. broadcast cron 10초 → 1초로 변경 (조건부 diff 유지)
3. 측정 메트릭 로깅 추가 (아래)
4. 1주 모니터링 + 데이터 수집

**측정 항목**:
- broadcast 1회당 DB SELECT latency (p50, p99)
- `build_rates_payload` JSON 직렬화 + diff 비교 시간
- 직렬 vs 병렬 send_json 시간 (연결 N=1, 10, 100, 500 시뮬레이션)
- RDS connection pool 사용률
- broadcast skip 비율 (변경 없음으로 스킵된 비율)
- EC2 CPU/메모리 추이

**측정 범위 / 배포 정책** (v0.2 합의):
- foreground 실시간 화면 기준으로 평가. background WS 유지는 Phase 1 성공 기준에서 제외 (미정 B 결론)
- 1주 측정은 운영 환경에서 진행하되, 배포 범위와 시간대는 Phase 1 PR에서 결정
- 배포 우선순위: **짧은 시간대 cron 차등 → 필요 시 관리자 한정 → (Phase 2 토픽 라우팅 이후) 소수 클라이언트**
- background 상태는 기존 FCM 푸시 + 앱 재진입 시 snapshot 동기화 그대로 유지
- 모바일 트래픽/처리 시간 기본 모니터링은 Phase 1, 본격 배터리 측정은 Phase 2부터

**채택/롤백 기준** (정량 임계값은 사후 합의):
- 1주 측정 데이터 기반으로 유지/롤백/event-driven 전환/임계값 운영 SLO 등록 여부 결정 (미정 C 결론)
- 후보 임계값(추정): p99 DB latency < 100ms, EC2 CPU < 60%, RDS pool < 70% — 측정 후 실측 데이터로 확정

### Phase 2 — Redis-first broadcast (PR3) → 업비트 WS PoC + Topic 프로토콜 v1 (PR4+)

> v0.7 (2026-05-01): Phase 1 fast PoC 결과 — broadcast hot path가 DB latest SELECT 실행 비용에 직접 묶여 정각/15/30/45분 :00초 spike가 발생 (19:00 KST 측정 기준 누적 3.35h: payload_build_ms p99 약 387ms / max 1305ms, job_duration ≥1s 약 3.59건/h). Performance Insights "상위 대기" 16:00 spike 확대 구간(8분 PI window)에서 wait event가 CPU 단일로 관측되고 LWLock/Lock/IO:WalSync wait 0건 확인. 같은 구간 Top SQL에서 bank+source latest SELECT가 부하 대부분 차지(bank 0.21 + source 0.15 AAS = 0.36). Redis-first broadcast 전환은 성능 개선이 아니라 **사용자 경로의 DB tail 의존성을 구조적으로 해제**하는 작업이라 Phase 2의 첫 PR로 격상.

**Phase 2 PR 의존성 그래프**:

| PR | 내용 | 의존성 | 위험도 |
| --- | --- | --- | --- |
| **PR3** (✅ 완료, 2026-05-04, ADR-026) | Redis-first broadcast (mirror + warmup + fallback) + PR3.5 분해 계측 + PR4 MGET + PR5 DXY mirror | 없음 (가장 먼저) | 낮음 (env OFF default + 분기 추가) |
| PR4 (재정의 영역) | crawler 저장 성공 후 Redis write (mirror 보조 + latency 0초) | PR3 | 중간 (16개+ 크롤러). 정당화 보류 — mirror_age p99 정상이라 즉시 정당화 X (ADR-026 한계 분석 참조) |
| PR5 (재정의 영역) | USDT WS collector가 같은 latest path에 write | PR3, PR4 | 중간 (신규 모듈) |
| PR6+ | 토픽 프로토콜 v1, 테더 탭 topic 이름/schema, dual-emit, dxy_futures rollup, snapshot 토픽 분리, alert/graph 분리 | PR5 | 큼 (다수 모듈) |

> 📝 **PR 라벨 차이 안내**: 위 표는 v0.7 합의 시점 계획. 실제 진행은 PR3 → PR3.5 (분해 계측) → PR4 (MGET) → PR5 (DXY mirror) 시퀀스로 broadcast hot path DB-free 달성에 집중. "crawler write-through"는 위 표의 PR4 의미였으나, ADR-026 한계 분석에서 즉시 정당성 약함 — 후속 PR 영역으로 보류.

#### PR3: Redis-first broadcast (✅ 완료 — ADR-026)

> ✅ **2026-05-04 완료** — PR3 → PR3.5 (분해 계측) → PR4 (MGET 1회 통합) → PR5 (DXY mirror) 시퀀스로 broadcast hot path DB-free 달성. 측정 결과는 [ADR-026](DECISIONS.md#adr-026-redis-first-broadcast-hot-path--latest-mirror--dxy-mirror로-db-free-달성) 본문 참조.
>
> **핵심 결과** (PR5 24h 누적, n=31495):
> - payload_build_ms p99 **30.25ms** (PR3.5 baseline 195ms → 6.4× 가속)
> - rates Redis-first hit 100%, DXY Redis-first hit 100%
> - dxy_query_ms count 1827 → **0** (broadcast hot path DB 조회 제거)
> - mirror_age_ms p99 2301ms (3초 ceiling 안정)
>
> **운영 한계 (IN mode)**: 영업시간 outlier 비율 ~4× 증가 (Redis read wall-clock jitter, DB 경합 아님). 후속 진단 영역.

**목적**: broadcast가 매초 DB latest SELECT를 실행하지 않도록 Redis mirror layer를 도입. 사용자 경로(broadcast send)를 DB CPU tail에서 분리.

**환경 변수** (단일 토글 + interval):

```bash
REDIS_LATEST_ENABLED=false       # PR3 활성화 토글 (default OFF, canary로 점진 활성화)
LATEST_MIRROR_INTERVAL_SECONDS=3 # mirror 주기 (PoC 후 1/3/5초 비교로 sweet spot 확정)
```

**Redis schema** (key는 helper 함수로 중앙화):

```text
helper:
- latest_key_bank(bank, currency)    → latest:bank:{bank}:{currency}
- latest_key_source(source, asset)   → latest:source:{source}:{asset}
- latest_key_investing(currency)     → latest:investing:{currency}

value (JSON):
{
  "rate": 1340.5,
  "timestamp": "2026-05-01T19:00:00+09:00",  // 환율 발생 시각 (UI 표시)
  "mirrored_at": "2026-05-01T19:23:45+09:00" // mirror가 갱신한 시각 (stale 판정용)
}
```

**Stale 판정**: `now - mirrored_at > LATEST_MIRROR_INTERVAL_SECONDS * 2` (interval=3 기준 6초 초과 시 stale). ratio=2는 코드 상수로 시작, 운영에서 조정 필요해지면 env로 분리.

**Fallback reason** (4가지 분류 — analyzer 집계 기준):

- `redis_miss`: key 없음 (warmup 직후 또는 신규 데이터)
- `redis_stale`: mirrored_at 6초 초과 (mirror job 정체 또는 정지)
- `redis_error`: Redis 명령 실패 (네트워크/timeout)
- `circuit_open`: Circuit Breaker open 상태 (5 failures → 30s timeout)

> circuit_open은 cache.py 기존 wrapper가 None만 반환하므로 redis_miss와 자동 구분 안 됨. PR3 helper에서 호출 **전** `redis_cache.circuit.can_attempt()` 명시 체크 또는 `(value, reason)` tuple 패턴으로 처리. cache.py 공용 API는 건드리지 않음.

**모듈 구조** (단일 책임 분리):

```text
app/latest_rates_cache.py (NEW)
  - latest key helper × 3
  - value serialize/deserialize
  - warmup_latest_rates()             # FastAPI lifespan startup 1회 호출
  - mirror_latest_rates_once()        # scheduler IntervalTrigger 주기 호출
  - build_rates_payload_from_redis()  # Redis-first read + DB fallback
  - private (value, reason) helper    # circuit_open/miss/stale/error 구분
                                       # redis_cache.client + circuit 직접 사용

app/main.py (변경)
  - lifespan startup에 warmup_latest_rates() 호출
  - broadcast_rates_once의 build_rates_payload 호출 직전 REDIS_LATEST_ENABLED 분기
  - timings에 latest_source / mirror_age_ms / fallback_reason 추가

app/scheduler.py (변경)
  - REDIS_LATEST_ENABLED=true일 때 mirror job 등록 (IntervalTrigger)
  - max_instances=1, coalesce=True

app/config.py (변경)
  - REDIS_LATEST_ENABLED, LATEST_MIRROR_INTERVAL_SECONDS env 추가

scripts/analyze_broadcast_metrics.py (변경)
  - METRICS에 mirror_age_ms 추가
  - 신규 분류기: classify_latest_source(redis|db_fallback)
  - 신규 출력: latest_source 분포, fallback_reason 카운트, mirror_age 통계
```

**변경하지 않는 파일** (SRP / 회귀 위험 0):

- `app/cache.py` — Redis primitive + Circuit Breaker 단일 책임 유지
- `app/crud.py` — 기존 latest 조회 함수(`select_latest_bank_rates_from_db`, `select_a_latest_investing_rate_from_db`, `get_source_rates_as_legacy_format`) 그대로 재사용
- `app/admin/stats.py` — PR3 1차 범위 외 (logger extra + analyzer로 충분, 화면 반영은 후속 PR)
- `templates/admin.html` — 후속 PR

**Redis namespace 의미 분리** (운영 문서):

- `broadcast:latest`: broadcast 전체 payload (변경 감지 diff용, Phase 1.7부터 운영). PR3 도입 후에도 유지.
- `latest:*`: 개별 rate mirror (PR3 신규). broadcast 입력 데이터.

**측정 지표** (PR3 전후 비교):

| 지표 | 현재 (fast baseline) | PR3 후 목표 |
| --- | --- | --- |
| payload_build_ms p99 | 387ms | <50ms |
| payload_build_ms max | 1305ms | <100ms |
| broadcast max_instances/h | 1.79 | 0 |
| job_duration ≥1s/h | 3.59 | <0.1 |
| PI Top SQL bank+source AAS | 0.36 (16:00 spike) | ~0 |
| `fallback_rate` (%) | N/A | <1% |
| `mirror_age_ms` p99 | N/A | < interval * 2 (≤6초) |
| send timeout / pool overflow | 0 | **0 유지** |

**Rollback 경로**: env `REDIS_LATEST_ENABLED=false`로 즉시 복귀 (코드 변경 없음). 가드레일 위반 시 (fallback_rate >5%, send timeout >0, broadcast max_instances 시간당 >3 지속) 자동 복귀 검토.

**미래 승격 옵션**: latest_rates_cache.py 내부 private `(value, reason)` helper는 PR3 단일 사용처. alert path가 Redis-first 전환되는 등 두 번째 사용처 발생 시 cache.py 공용 메서드(`get_with_reason()`)로 승격 (Rule of Three).

#### Phase 2 기존 작업 (PR4~)

**목적**: 토픽 명세와 dual-emit 인프라를 PoC 단계에서 함께 검증.

**작업**:
1. v1 minimal 토픽 프로토콜 구현 (hello/subscribe/snapshot/delta)
2. 업비트 1개 거래소 WebSocket collector 구현 — PR3의 `latest:source:upbit:usdt-krw` path에 직접 write
3. **테더 탭 topic 이름 + snapshot/delta schema 확정** (미정 F 해결): multi-source payload (5거래소 USDT + Investing/KB/Hana reference + KRX 달러선물). 후보 방향: `source + asset + category` 기반 배열. category 예: `reference` / `derivative` / `exchange`.
4. **DXY 토픽 분리 vs 단일 토픽 instrument 분기 결정** (미정 F의 일부): DXY 현물은 달러 탭 보조지표, 선물은 테더 탭 그래프 보조지표 — 두 화면이 같은 토픽 공유할지 분리할지.
5. **백엔드 `get_all_rates_flat`에서 USDT legacy 병합 제거** ([crud.py:340](app/crud.py#L340), [crud.py:365](app/crud.py#L365)) — 테더 탭 데이터는 새 topic으로만 발사. legacy `rates`에는 USD/JPY/EUR + Investing/은행 9개만 유지.
6. **dxy_futures hourly/daily rollup 추가** ([dxy_rollup.py](app/admin/dxy_rollup.py) — 현재 `instrument='dxy'`만 집계. 30일 보관 정책상 dxy_futures 장기 그래프 데이터 손실 방지 위해 rollup 필수). instrument 파라미터화 또는 dxy_futures 전용 함수.
7. **market_index 범용 그래프 API** (또는 dxy_futures 전용 그래프 API) — 테더 탭 그래프 payload에 DXY 선물지수 보조지표 포함 가능하도록.
8. dual-emit 발사: 거래소 tick은 새 topic으로만 (legacy 미발사). FX/은행 tick은 legacy + 새 topic 양쪽 발사.
9. iOS/Android 신 프로토콜 클라이언트 PoC — 테더 탭 데이터를 새 topic 구독으로 수신 (현재 `rates` 배열에서 USDT 받는 테스트 코드 수정)
10. snapshot 캐시 토픽별 분리

**검증 항목**:
- 업비트 WS endpoint 검증 체크리스트 (7번)
- 재연결 안정성 (24시간 모니터링)
- 끊김 시 REST fallback 동작
- 클라이언트 dual mode 호환성 (구버전 + 신버전 동시 운영)

### Phase 3 — 거래소 4종 WS 확장 + 알림 경로 재설계

**작업**:
1. 빗썸/코인원/코빗/고팍스 WebSocket collector
2. 알림 평가 흐름 분리 (DB 동기 → Redis tick 기반 비동기, 10번 섹션)
3. DB 저장 정책 확정 (1초 last 또는 OHLC)
4. graph 토픽 분리 + 저빈도 push

### Phase 4 — Investing 실시간 채널 조사 + 적용

**작업**:
1. DevTools 분석으로 WebSocket/SSE 채널 발견 시도 (8번 섹션)
2. 가능 시 별도 collector, curl_cffi cron은 fallback으로 유지
3. 불가능 시 persistent browser 검토 또는 cron 주기 단축

### Phase 5 — 클라이언트 마이그레이션 + 레거시 제거

**작업**:
1. iOS/Android 신 프로토콜 정식 릴리스
2. 활성 구버전 비율 모니터링 (플랫폼별)
3. 1% 미만 + 6개월 도달 시 레거시 broadcast 제거 후보 결정

### Phase 6 — (선택) 수집기 dirty-event → broadcaster debounce

Phase 1 측정 결과로 결정. 1초 cron으로 충분하면 스킵.

---

## 13. 레거시 호환 전략

### Dual-emit 운영 패턴

서버는 매 변경 발생 시 두 채널에 동시 발사:

1. **Legacy 채널**: 기존 `build_rates_payload` 흐름 유지, 모든 active connection에 전체 broadcast
2. **Topic 채널**: subscribe한 클라이언트에게만 해당 topic delta 전송

구버전 클라이언트는 subscribe 메시지를 보내지 않음 → 서버가 legacy mode로 인식 → 기존 동작 그대로.

### 클라이언트 식별 방법

- `hello` 직후 일정 시간 (예: 5초) 안에 subscribe 안 오면 legacy 클라이언트로 판단
- 또는 클라이언트가 명시적으로 `legacy: true` 플래그를 보낼 수도 있음 (v1.5에서 검토)

### Dual-emit 비용

- 메모리: 메시지 객체를 두 형태로 직렬화 — 무시할 수준
- CPU: snapshot/delta 변환 로직 추가 — Phase 1 측정에 포함
- 일관성: Redis state가 단일 source of truth → 두 채널 모두 같은 값 발사

---

## 14. 모니터링 지표와 Rollback 기준

### 핵심 지표 (Phase 1 이후 운영)

| 지표 | 임계값 | 액션 |
|------|--------|------|
| broadcast latency p99 | > 500ms | 경고 |
| WS 연결 끊김 빈도 | > 5%/분 | 거래소 WS rollback |
| RDS connection pool | > 80% | broadcast 빈도 감속 |
| EC2 CPU 5분 평균 | > 80% | 단계 롤백 검토 |
| 알림 발송 실패율 | > 1% | FCM 경로 점검 |
| 거래소 WS 재연결 빈도 | > 10회/시간 | REST fallback 강제 |

### Rollback 절차

각 Phase별 rollback 절차는 구현 시점에 별도 ROLLBACK.md 또는 PR description에 기록. 핵심:

- Phase 1 → 0: cron 1초 → 10초로 되돌림 (single config flag)
- Phase 2 → 1: 업비트 WS collector 비활성화, REST polling 재활성화
- Phase 3 → 2: 거래소별 개별 toggle (crawler_config 테이블 활용)

---

## 15. 비용/리소스 추정 (개략)

### EC2 t3.small (현재)

- 5개 거래소 WS 상시 연결: 메모리 ~5MB, CPU 무시
- broadcast 1초 cron + 병렬 전송 (100 connection): CPU 사용량 ~5-10% 증가 예상
- Investing persistent browser (Phase 4 후보): 메모리 200-400MB

### RDS db.t4g.micro (현재 1GB)

- broadcast 매초 SELECT 30 row: pool 부담 ~10% 증가 예상 (Phase 1 측정 필요)
- tick 모두 INSERT 시 위험 (수백만~수천만 row/10일) → DB 저장 정책으로 회피

### 네트워크 비용

- legacy + topic dual-emit 기간: 메시지 트래픽 약 2배
- 클라이언트 마이그레이션 완료 후 정상화

---

## 16. 미정 항목

> ⚠️ A/B/C는 v0.2에서 minimum bar 결론형으로 닫힘. 정량 임계값/background 정책 전반은 후속. F는 Phase 2 직전 합의. D/E는 Phase 3+ 합의.
> 라벨 순서는 **합의 진행 우선순위**(메타 → 범위 → 측정 → 후속)로 정렬.

### A. Phase 0 종료 조건 (Phase 1 진입 시점) — ✅ minimum bar 합의 (v0.2)

- **v0.2 commit 시점부터 Phase 1 코드 설계/구현 작업에 진입할 수 있다.** 단, 운영 배포 조건은 아니며 Phase 1 PR에서 계측/롤백/배포 절차를 별도 확인한다.
- Phase 1 진입 전 최소 합의는 B의 PoC 평가 범위 + C의 측정 항목/의사결정 절차로 한정한다.
- C의 정량 임계값과 B의 background 정책 전반은 후속 합의로 남긴다.

### B. 모바일 클라이언트 영향 — ✅ minimum bar 합의 (v0.2)

**v0.2 합의 (Phase 1 PoC 평가 범위)**:

- Phase 1 PoC는 **foreground 실시간 화면 기준**으로 평가한다.
- iOS/Android **background WS 유지는 Phase 1 성공 기준에 포함하지 않는다.**
- background 상태는 기존 **FCM 푸시 + 앱 재진입 시 snapshot 동기화**를 그대로 유지한다.
- Phase 1은 모바일 트래픽/처리 시간 기본 모니터링까지만 한다.
- 본격 배터리 영향 측정은 Phase 2 신 프로토콜 PoC부터 시작한다.

**🟡 후속 합의 대상** (Phase 2 이후):

- iOS background WebSocket 유지 정책 (foreground only / background ping-pong / push-only fallback)
- 1초 broadcast 시 모바일 배터리 영향 측정 방법론
- 앱 cold start 시 snapshot 수신 흐름 (단일 캐시 → 토픽별 분리)
- 최소 지원 버전 정책 도입 여부

### C. Phase 1 PoC 측정 메트릭 — ✅ minimum bar 합의 (v0.2)

**v0.2 합의 (측정 항목 + 의사결정 절차)**:

- **정량 임계값은 v0.2에서 확정하지 않는다** — 측정 데이터 없이 추측으로 박으면 의미 없음.
- 측정 항목 (12번 Phase 1 섹션과 동일): broadcast 1회당 DB SELECT latency (p50, p99) / `build_rates_payload` JSON 직렬화 + diff 비교 시간 / 직렬 vs 병렬 send_json 시간 / RDS connection pool 사용률 / broadcast skip 비율 / EC2 CPU/메모리 추이 / 모바일 트래픽·처리 시간 기본 모니터링.
- **PoC 운영 기간**: 1주.
- **의사결정 절차**: 1주 측정 데이터로 유지 / 롤백 / event-driven 전환 / 정량 임계값 운영 SLO 등록 여부를 결정한다.
- **배포 정책**: 1주 측정은 운영 환경에서 진행하되, 배포 범위/시간대는 Phase 1 PR에서 결정. 우선순위는 **짧은 시간대 cron 차등 → 필요 시 관리자 한정 → (Phase 2 토픽 라우팅 이후) 소수 클라이언트**.

**🟡 후속 합의 대상** (1주 측정 후):

- p99 DB latency 임계값 (후보: 100ms? 200ms?)
- EC2 CPU 5분 평균 임계값 (후보: 60%? 70%?)
- RDS connection pool 임계값 (후보: 60%? 70%?)

### D. Graph 토픽 push 빈도 — 🟡 Phase 3 직전 합의

- 1d / 1w / 3m / 1y 각 range별 push 빈도
- 후보: 분 단위? range 별 다른 빈도? (예: 1d는 1분, 1y는 1시간)

### E. 알림 평가 정량 정책 — 🟡 Phase 3 직전 합의

- `last_notified_at` 윈도우 (재발송 차단 시간)
  - 후보: 5분 / 30분 / 1시간
- DB 저장 정책 — tick 모두 vs 1초 last vs 1초 OHLC

### F. 테더 탭 topic 이름 + snapshot/delta schema + DXY 토픽 분리 — 🟡 Phase 2 PR 합의

> ⚠️ 테더 탭은 단순 `usdt:krw`가 아니라 multi-source 탭이다. v0.1의 `usdt:krw` 명칭은 잠정.
> v0.3에서 KRX 달러선물(가격 리스트 + 그래프 라인 후보)과 DXY 선물지수(그래프 보조지표 후보)가 결정되면서, DXY 토픽 분리 결정도 이 항목에 흡수.

**미정 항목**:

- 토픽 이름 — 후보: `tab:tether` (직관적이지만 UI 결합), `market:tether-premium` (도메인 중심이지만 길다). Phase 2 PR에서 클라이언트 구현과 함께 결정.
- snapshot/delta payload schema — 후보 방향: `source + asset + category` 기반 배열 구조.
  - 예시:
    - investing / usd-krw / reference
    - kb / usd-krw / reference
    - hana / usd-krw / reference
    - krx / usd-krw-futures / derivative
    - upbit / usdt-krw / exchange
    - bithumb / usdt-krw / exchange
    - ... (5거래소)
- category 분류 정합성 검증 — [USDT_TAB_PROPOSAL.md](USDT_TAB_PROPOSAL.md), [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md)와 일관성 확인 필요.
- **DXY 선물지수 보조지표 처리 방식** (v0.3 추가):
  - DXY 현물(`instrument='dxy'`) = 달러 탭 그래프 보조지표 (기존)
  - DXY 선물(`instrument='dxy_futures'`) = 테더 탭 그래프 보조지표 (v0.3 결정)
  - 옵션 X: 토픽을 `dxy:spot` / `dxy:futures`로 분리 — 명확하지만 토픽 수 증가
  - 옵션 Y: `dxy` 단일 토픽 안에 instrument 필드로 분기 — 토픽 수 적지만 클라이언트 필터링 부담
  - 옵션 Z: DXY 선물지수를 테더 탭 토픽 payload에 보조지표로 흡수 (별도 DXY 토픽 분리 안 함)
  - Phase 2 PR에서 토픽 schema와 함께 결정.

---

## 17. ADR 후보 목록

이 계획에서 파생되는 결정들 — 구현 시점에 [DECISIONS.md](DECISIONS.md)에 등재.

- ADR-XXX: WebSocket 구독 기반 토픽 라우팅 도입
- ADR-XXX: Broadcasting 1초 cron 전환 (Phase 1 측정 결과 반영)
- ADR-XXX: 거래소 USDT — REST polling → WebSocket 전환
- ADR-XXX: 알림 평가 흐름 분리 (DB 동기 → Redis 비동기)
- ADR-028: Legacy + Topic dual-emit 마이그레이션 패턴 — 테더 탭 데이터는 topic-only, legacy `rates`는 USD/JPY/EUR + Investing/은행 9개로 한정
- ADR-XXX: 테더 탭 multi-source topic 이름 + payload schema 확정 (source + asset + category 모델)
- ADR-XXX: DXY 화면 분리 — 달러 탭은 현물(`dxy`), 테더 탭은 선물(`dxy_futures`) 그래프 보조지표
- ADR-XXX: dxy_futures hourly/daily rollup + market_index 그래프 API 확장 (Phase 2 작업)
- ADR-XXX: 그래프 토픽 분리 (실시간 tick과 별도)
- ADR-XXX: (선택) Investing 실시간 채널 전환 — 발견 시 별도 ADR

---

## 18. 변경 이력

- **v0.7** (2026-05-01): 24h fast PoC 진행 중 (15:53 KST~) + spike 원인 식별 완료 + Phase 2 PR3 설계 합의.
  - **24h fast window PoC 진입** (`BROADCAST_MODE=window`, `BROADCAST_FAST_HOURS=0-24`, 15:53:54 KST 시작). 노동절(2026-05-01 금) + 주말 트래픽 적은 구간 활용한 본 운영 PoC. **19:00 KST 체크포인트 기준 누적 3.35h** 가드레일 통과: broadcast misfire 약 1.79/h (임계값 3/h의 60%, 전반/후반 →유지), send timeout 0건, pool overflow 0건, 전반/후반 payload_build_ms p99 392→367ms 약간 개선.
  - **spike 원인 — 16:00 spike 확대 구간 기준 CPU 단일 관측** (Performance Insights "상위 대기" 16:00 spike 구간 8분 window에서 CPU 0.39 AAS 단일, LWLock:BufferContent / Lock:transactionid / IO:WalSync 모두 wait 0건). 같은 구간 PI Top SQL에서 broadcast bank/source latest SELECT가 부하 대부분 차지 (bank 0.21 + source 0.15 AAS = 0.36). 좁은 구간 관측이라 다른 spike 시점에서도 동일 패턴인지는 PR3 적용 후 비교로 검증.
  - **CloudWatch 가설 추가 기각** (3시간 평균 + 1분 해상도 zoom 기준): WriteIOPS 3.23/s + DiskQueueDepth ≈0 + EBSIOBalance 100% + BurstBalance 99.6% → I/O 포화/burst credit 고갈 모두 기각. DBLoadCPU 단독 spike(DBLoadNonCPU ≈0) → I/O 아닌 CPU 부하 우세. DatabaseConnections 3 안정 → pool 경합 기각.
  - **EXPLAIN ANALYZE로 실제 SQL baseline 확정**: bank window function 15ms (Index Only Scan + Run Condition `rn ≤ 1` 최적화), investing latest 0.078ms, source_rates window function 30ms. 합계 ~78ms baseline = 정상 broadcast p50과 일치. spike 800ms+는 MVCC visibility check + buffer access CPU 비용 등 시스템 경합 영역으로 추정 (다른 spike 시점 동일 패턴 여부는 PR3 적용 후 비교로 검증). 코드 자체는 효율적이라 parameter tuning(work_mem 등)으로 해결 어려움.
  - **PR3(Redis-first broadcast)를 Phase 2 첫 PR로 격상**: 단일 PR + env OFF default + canary 활성화 패턴 (PR2 BROADCAST_MODE 패턴 일관). 단일 env `REDIS_LATEST_ENABLED=false` 토글 + `LATEST_MIRROR_INTERVAL_SECONDS=3` interval. Redis schema는 helper 함수 3개로 중앙화 (`latest_key_bank/source/investing`), value 최소(`{rate, timestamp, mirrored_at}`), stale 판정 `now - mirrored_at > interval * 2`, fallback reason 4분류(redis_miss/redis_stale/redis_error/circuit_open).
  - **PR3 모듈 구조 합의**: 신규 `app/latest_rates_cache.py` 모듈 (latest 도메인 격리). cache.py·crud.py·admin/stats.py·admin.html 변경 없음. main.py(분기 + lifespan warmup), scheduler.py(mirror job 등록), config.py(env), analyzer(metric 집계)만 변경. circuit_open은 PR3 helper에서 호출 전 `redis_cache.circuit.can_attempt()` 명시 체크 또는 `(value, reason)` tuple 패턴으로 처리 (cache.py 공용 API 안 건드림). 미래 다른 모듈 동일 패턴 필요 시 cache.py로 승격(Rule of Three).
  - **PR4/5/6+ 후속 순서 합의**: PR4 crawler 저장 후 Redis write → PR5 USDT WS collector가 같은 latest path → PR6+ 토픽 분리/dual-emit/dxy_futures rollup/alert·graph 분리.
  - **Investing 403 플래핑 PR2/PR3 무관 재확인**: 외부 Cloudflare 변동성 별도 트랙 유지. CNBC fallback (ADR-025) 정상 작동.
  - **다음 단계**: 24h PoC 종료(2026-05-02 토 16:00 KST 전후) → 주말 모니터링 → 2026-05-04 (월) PR3 구현 시작 (5/5 어린이날 공휴일 후 5/6 수부터 본격). PR3 구현 시 fast 유지하여 PR3 전후 baseline 비교.
- **v0.6** (2026-05-01): Phase 1 운영 진입 + PR2 window 임시 PoC 성공.
  - **Phase 1.5 분해 계측 추가** (`get_all_rates_flat_with_timings`): payload_build_ms 단계별 timing(investing/bank/source_rates_legacy) 식별. 24h baseline에서 spike 주범이 bank/investing/source 쿼리임을 확인.
  - **latest 조회 인덱스 3종 적용**: `ix_investing_currency_ts_id`, `ix_bank_currency_bank_ts_id`, `ix_source_rates_source_asset_ts_id`. 각각 (currency/source, asset, timestamp DESC, id DESC). EXPLAIN ANALYZE 기준: investing 59ms→0.076ms, source_rates 162ms→27ms (Seq Scan + disk spill 회피).
  - **bank VACUUM ANALYZE**: 새 인덱스 직후 visibility map 갱신 → Heap Fetches 16041→0, EXPLAIN 15ms→8ms.
  - **PR2 BROADCAST_MODE 인프라 (`9009aac`)**: `BROADCAST_MODE=normal|fast|window`, `BROADCAST_FAST_HOURS`, `BROADCAST_SEND_TIMEOUT_SECONDS`, `BROADCAST_FAST_SEND_TIMEOUT_SECONDS`. broadcast cron `'0,10,20,30,40,50'` → `'*'` + 함수 첫 줄 mode/second 분기 + DB 조회 전 early return. apscheduler.executors.default logger를 WARNING으로 낮춰 매초 INFO 폭증 차단(`dfbab83`).
  - **PR2 임시 window PoC** (2026-04-30 20:00-20:17 KST, fast 17분): payload_build_ms p99 302ms / max 792ms, bank_total_ms p99 170ms, source_rates_legacy_ms p99 73ms, broadcast_send_ms max 2.3ms, **send timeout/failure 0건, misfire 0건**. 1초 cron + 0.5초 send_timeout 조합이 정각 spike 시간대(20:00, 20:15)에서도 안정 동작 확인.
  - **Investing 403 플래핑은 PR2와 무관**: window 전/중/후 3구간 비교(분당 정규화) 결과 차단 시작 빈도가 0.43/0.65/0.65/min — normal 복귀 후에도 동일. 외부 Cloudflare 변동성으로 분리. CNBC fallback (ADR-025) 0.17→0.45/min로 정상 작동.
  - **다음 단계**: 새벽 02:00-04:00 KST 본 운영 PoC (`BROADCAST_MODE=window`, `BROADCAST_FAST_HOURS=2-4`). 1주 측정 후 정량 임계값 운영 SLO 등록 또는 fast 전환/event-driven 결정 (미정 C 사후 합의).
- **v0.1** (2026-04-26): 초안 작성. 합의된 원칙, 미정 항목, 검증 체크리스트, 단계별 마이그레이션 정리.
- **v0.2** (2026-04-27): Phase 0 종료 조건 합의본.
  - 미정 항목 A/B/C minimum bar 결론형 반영 (정량 임계값/background 정책 전반은 후속).
  - v0.2 commit = Phase 1 코드 설계/구현 진입 가능 (운영 배포 조건 아님).
  - 테더 탭 데이터 topic-only 정책 명시: legacy `rates`는 USD/JPY/EUR + Investing/은행 9개로 한정, 테더 탭 multi-source payload는 새 topic 전용.
  - 새 미정 항목 F 추가 (테더 탭 topic 이름 + schema 확정 — Phase 2 PR).
  - 5번 섹션 `usdt:krw`는 잠정 명칭 표시.
  - 11번 섹션에 "원천 데이터 재사용" 문단 추가 (KB usd-krw 예시).
- **v0.5** (2026-04-28): 빗썸/코인원 wscat smoke test 완료 + 구현 가이드 분리.
  - 빗썸 `ws-api.bithumb.com/websocket/v1` `KRW-USDT` snapshot/realtime `trade_price` 실수신 확인.
  - 코인원 endpoint 정정 — 공식 문서 기준 `wss://stream.coinone.co.kr` (이전 `public-ws-api.coinone.co.kr`은 낡음). subscribe 포맷도 `request_type/quote_currency/target_currency`로 갱신.
  - 코인원 WS payload `data.last` 필드 확정.
  - 고팍스 ticker 전체 구독 후 `USDT-KRW` 필터링 필요 명시 (다른 거래소는 단독 구독 가능).
  - 상세 구현 가이드는 [USDT_EXCHANGE_WEBSOCKET_GUIDE.md](USDT_EXCHANGE_WEBSOCKET_GUIDE.md)로 분리 — 백엔드 collector 정규화 모델, Redis latest state, 응답 파싱 예시, smoke test 명령 포함.
  - 잔여 검증: 업비트/빗썸/코빗 heartbeat 정책, 24h SLA, 5종 무료 tier 약관.
- **v0.4** (2026-04-28): 거래소 5종 WebSocket endpoint 1차 문서 검증.
  - 7번 섹션 표를 endpoint / symbol / subscribe / heartbeat / ticker price 필드 / REST fallback 7개 컬럼으로 확장.
  - 업비트/코인원/코빗/고팍스 ✅ 1차 검증, 빗썸 🟡 부분 검증 (v0.4 시점 상태 — v0.5에서 빗썸/코인원 wscat smoke test 완료로 갱신).
  - 인증 무필요(5종 공통), USDT/KRW 직접 채널 존재(5종 공통) 확인.
  - 코드 변경 없음. PR1/PR2와 독립적으로 Phase 2 직전까지 점진 보강.
- **v0.3** (2026-04-27): 테더 탭 그래프 보조지표 정책 합의.
  - 달러 탭은 기존 DXY 현물(`instrument='dxy'`) 그래프 유지.
  - 테더 탭은 KRX 미국달러선물을 가격 리스트 + 그래프 라인 후보로 둔다.
  - 테더 탭은 DXY 선물지수(`instrument='dxy_futures'`)를 그래프 보조지표 후보로 둔다.
  - dxy_futures rollup/API 작업을 Phase 2 작업 항목에 추가 ([dxy_rollup.py](app/admin/dxy_rollup.py)는 현재 instrument='dxy'만 집계 — 30일 보관 정책상 장기 그래프 데이터 손실 방지 위해 rollup 필수).
  - 5번 섹션 `dxy` 토픽 주석 보강 (현물/선물 분리 미정).
  - 9번 DB 저장 정책 표에 DXY 현물/선물 분리 표기.
  - F 미정 항목에 DXY 토픽 분리/통합 결정 흡수 (옵션 X/Y/Z, Phase 2 PR에서 결정).

---

## 부록: 합의 요약 (한눈에 보기)

✅ **운영 진입 상태** (v0.7, 2026-05-01):

- **24h fast PoC 진행 중** (15:53 KST 시작, 노동절+주말 트래픽 적은 구간). `BROADCAST_MODE=window`, `BROADCAST_FAST_HOURS=0-24`. **19:00 KST 체크포인트 기준 누적 3.35h** 가드레일 통과 (broadcast misfire 약 1.79/h, send timeout 0, pool overflow 0)
- **spike 원인 — 16:00 spike 확대 구간 기준 CPU 단일 관측** (PI 상위 대기 16:00 8분 window에서 CPU 0.39 AAS 단일, LWLock/Lock/IO:WalSync wait 0). 같은 구간 Top SQL에서 bank+source latest SELECT가 부하 대부분 차지(bank 0.21 + source 0.15 AAS = 0.36). EXPLAIN baseline ~78ms vs spike 800ms+는 시스템 경합 영역 (MVCC visibility + buffer access CPU 비용 추정, 다른 spike 시점 동일 패턴 여부는 PR3 후 검증)
- **PR3(Redis-first broadcast) Phase 2 첫 PR로 격상 + 설계 완전 합의**: 단일 PR + env OFF default + canary 활성화. 신규 `app/latest_rates_cache.py` 모듈 분리, cache.py·crud.py 변경 0. 자세한 명세는 12.Phase 2 § PR3 참고
- **다음 단계**: 24h PoC 종료(5/2 토 16:00 KST) → 주말 모니터링 → 5/4 (월) PR3 구현 시작 (5/5 어린이날 공휴일 후 5/6 수~)

🟡 **PR2/PR3와 분리된 후속 이슈**:

- **Investing 403 플래핑**: 외부 Cloudflare 변동성 별도 트랙. CNBC fallback (ADR-025) 정상 작동 중이라 운영 영향 작음. ADR-018/025 강화는 별도 트랙

✅ **합의된 것** (v0.1 + v0.2 + v0.3):
- v1 목표: 서버 tick 수신 후 1초 이내 화면 반영
- 거래소 UI: 200~500ms debounce
- 알림: meaningful observation을 누락하지 않음 (생략 금지 조건에 해당하지 않는 동일 가격 반복 frame은 same-rate evaluation skip 가능 — 가격/비교 알림 공통, 생략 금지 조건은 [USDT_WS_DESIGN_PLAN.md §13.10](USDT_WS_DESIGN_PLAN.md) 참조) + last_notified_at 중복 방지
- 프로토콜: v1 minimal hello/subscribe/snapshot/delta + protocol_version
- 마이그레이션: legacy + topic dual-emit. **단 dual-emit 범위는 USD/JPY/EUR + Investing/은행 9개에 한정. 테더 탭 데이터(USDT 거래소 + KRX 달러선물)는 topic-only**
- 같은 원천 데이터는 여러 topic payload에 재사용 가능 — 데이터 저장 경로 공유와 채널 분리는 별개
- 레거시 제거: iOS/Android 양쪽 활성 구버전 < 1% **그리고** 최소 6개월 경과
- 그래프: 실시간 tick과 분리된 저빈도 토픽
- **DXY 화면 분리 (v0.3)**: 달러 탭 = DXY 현물(`dxy`) 그래프 유지. 테더 탭 = DXY 선물(`dxy_futures`) 그래프 보조지표 후보. 토픽 분리/통합 결정은 F에 흡수 (Phase 2 PR)
- **dxy_futures rollup/API 작업 (v0.3)**: Phase 2 작업 항목 추가. 30일 보관 정책상 장기 그래프 데이터 손실 방지 위해 rollup 필수
- Phase 0 (v0.2 합의): A/B/C minimum bar 결론형. v0.2 commit = Phase 1 **코드 설계/구현 진입** (운영 배포 조건 아님)
- Phase 1: 1초 broadcast PoC + 병렬 전송 + 1주 계측. foreground 한정. 배포 범위/시간대는 Phase 1 PR에서 결정
- Phase 2: 업비트 WS PoC + 토픽 프로토콜 + 테더 탭 topic 이름/schema 확정 + 백엔드 USDT legacy 분리 + dxy_futures rollup/API 추가
- DB는 실시간 전달 경로에서 분리, Redis가 latest state hot path
- iOS/Android 양쪽 운영 중이므로 양 플랫폼 모두 dual-emit 호환 필수 (USD/JPY/EUR + 은행 9개 한정)

🟡 **후속 합의 항목** (16번 섹션, 합의 진행 우선순위 순):
- A. ✅ Phase 0 종료 조건 — minimum bar 합의 완료 (v0.2)
- B. ✅ 모바일 영향 minimum bar 합의 완료 (v0.2). 🟡 background 정책 전반은 Phase 2 이후
- C. ✅ 측정 항목 + 절차 합의 완료 (v0.2). 🟡 정량 임계값은 1주 측정 후
- D. 🟡 그래프 토픽 push 빈도 — Phase 3 직전
- E. 🟡 알림 평가 정량 (last_notified_at 윈도우, DB 저장 정책) — Phase 3 직전
- F. 🟡 테더 탭 topic 이름 + snapshot/delta schema + DXY 토픽 분리/통합 — Phase 2 PR에서 확정

🔧 **검증 체크리스트** (7번 섹션):
- 거래소 5종 WebSocket endpoint **1차 검증 완료** (v0.4) + **빗썸/코인원 wscat smoke test 완료** (v0.5, 2026-04-28). 상세 구현 가이드: [USDT_EXCHANGE_WEBSOCKET_GUIDE.md](USDT_EXCHANGE_WEBSOCKET_GUIDE.md)
- 약관/정책 1차 검증 완료 (2026-04-28) — 상세: [USDT_EXCHANGE_WEBSOCKET_GUIDE.md §14](USDT_EXCHANGE_WEBSOCKET_GUIDE.md#14-약관정책-1차-검증). 시장 관행상 5종 모두 비교 서비스 운영 가능
- 잔여: 업비트/빗썸/코빗 heartbeat 정책, 24h SLA, 5종 약관 미확인 항목 보강 — Phase 2 PR 시점
- Investing 실시간 채널 DevTools 조사 (Phase 4 직전)
