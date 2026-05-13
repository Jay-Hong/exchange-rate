# USDT Topic Migration Plan (Phase Z-2)

> 📅 **작성일**: 2026-05-10
> 🏷️ **상태**: Draft (PR Z-2a — spec 확정)
> 📋 **문서 성격**: **실행 계획 (execution plan)** — 새 계약 결정 X. 이미 결정된 [ADR-028](DECISIONS.md#adr-028-topic-only-tetherkrx--legacy-fx-dual-emit) / [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md)의 출시 계약을 구현하기 위한 plan.

## 0. Scope & Source

본 문서는 **USDT/KRX의 legacy 임시 경로 제거 + topic-only 전환** 실행 계획. 새 계약을 결정하지 않음.

**Source of truth**:
- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md) — 서비스 출시 계약 (topic 채널 / payload schema / dual-emit 범위)
- [ADR-028](DECISIONS.md#adr-028-topic-only-tetherkrx--legacy-fx-dual-emit) — Topic-only Tether/KRX + legacy FX dual-emit 결정

**본 문서에서 결정 가능한 것**: PR 분할 / 작업 순서 / 마이그레이션 timing / 운영 배포 제약.

---

## 1. Current Implementation

USDT는 [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md) Phase 1로 백엔드 완료. 운영 앱(iOS 2026-01-21 / Android 2026-03-13)에는 테더 탭 없음 — Phase 1 노출은 iOS dev/test 단계 임시 모델.

### 1.1 데이터 수집

- [app/crawlers/usdt_sources.py](app/crawlers/usdt_sources.py): REST polling 10s, 5거래소 fan-out (upbit, bithumb, coinone, korbit, gopax)
- ThreadPoolExecutor 기반 병렬 fetch + `insert_source_rate_if_changed` 저장
- 알림 트리거: `_process_source_alerts_safe` → `process_source_rate_alerts` (crud.py:2178)

### 1.2 저장 schema

- `source_rates` 테이블: `(source, asset, rate, timestamp)` — 30일 보관
- `source_notification_settings` / `source_notification_logs` — source 기반 알림
- `app/source_registry.py`: `SourceDefinition` dataclass (category="exchange"|"reference"|"derivative")

### 1.3 Legacy 임시 노출 경로 (test-era compat)

**핵심 어댑터**:
- [app/crud.py:1751](app/crud.py) `get_source_rates_as_legacy_format(db, asset)`: source_rates → `{currency, bank, rate, timestamp}` shape 변환

**호출 지점 3곳** (crud.py 내부):
- line 365: `build_rates_payload` 내부 — WebSocket `rates` 배열 + `/api/rates`
- line 415: `build_rates_payload_with_timings` (계측 변형, 같은 동작)
- line 454: `get_rates_by_currency` — `/api/rates/{currency}` (currency=usdt-krw 분기)

→ 운영 앱에는 테더 탭 없으므로 **사용자 영향 0**, iOS dev/test 단계만 사용.

### 1.4 알림 경로

- `/api/source-notification-settings` (POST/GET/PUT/DELETE) — main.py:2226+
- FCM payload type: `source_rate_alert` (기존 `rate_alert`와 구분)

---

## 2. Target Contract (REALTIME_ARCHITECTURE_PLAN / ADR-028 인용만)

### 2.1 Topic 채널 분리

[REALTIME_ARCHITECTURE_PLAN.md §11](REALTIME_ARCHITECTURE_PLAN.md):
- **legacy `rates`** = USD/JPY/EUR + 은행 9개 + Investing 한정. **테더/KRX 미포함**
- **새 topic 채널** = `fx:*` / `usdt:krw` (잠정) / `krx:*` / `dxy` / `graph:*` / `news`
- **dual-emit 범위** = 환율 탭 데이터(FX/은행/Investing)만. **USDT/KRX는 topic만 발사**

### 2.2 Dual-emit 정의 명확화

- Dual-emit = 기존 앱 호환을 위한 **legacy bridge** (과도기 전략)
- 현재 dual-emit 대상: FX/은행/Investing
- USDT/KRX: **topic-only 출시** (기존 앱에 대응 UI 없으므로 dual-emit 의미 X — ADR-028)

### 2.3 Long-term (Open Questions §6)

본 PR 범위 외:
- legacy rates 전체 제거 (FX/은행/Investing도 topic-only로)
- timing은 클라이언트 마이그레이션 진척 후 별도 PR

---

## 3. Gap (Current → Target)

| 영역 | Current | Target | 작업 |
|---|---|---|---|
| `rates` 배열 USDT/KRX 포함 | crud.py 3곳 호출 (USDT) + KRX 잠재 노출 가능 | topic-only source 전체 제외 | legacy adapter exclusion policy 도입 |
| topic dispatcher | 없음 | `usdt:krw` / `krx:*` topic 발사 | 신규 구현 |
| subscription protocol | 없음 | client subscribe `usdt:krw` 등 | 신규 구현 |
| topic payload schema | 없음 | snapshot/delta (REALTIME_ARCHITECTURE_PLAN §5) | 구체화 |
| 알림 schema | 기존 `source_rate_alert` 그대로 | 변경 X | 영향 없음 |
| `source_rates` 저장 | 그대로 | 그대로 | 변경 없음 |
| `SourceRegistry` | category 분리 (exchange/reference/derivative) | topic 발사 + legacy exclusion 결정에 활용 | 활용 확장 |

**핵심 정정 (Codex 합의 2026-05-10)**:
> USDT/KRX는 topic-only 출시. Dual-emit은 기존 FX/은행/Investing 화면 유지용 legacy bridge로 USDT/KRX에는 적용 X. 따라서 USDT migration은 dual-emit이 아니라 **legacy 임시 경로 제거 + topic-only 전환**.

### 3.1 Legacy adapter 정책 부재 ⚠️ (Codex 2회차 검토 발견, 2026-05-10)

**주의**: `get_source_rates_as_legacy_format()`는 현재 `source_rates` 전체 최신값을 legacy shape로 변환하며 `phase1_enabled` / `category` 등을 필터하지 않는다.

| 경로 | KRX 차단 정책 | 비고 |
|---|---|---|
| Redis latest mirror | ✅ `should_include_source_in_latest()` 차단 | `KRX_BROADCAST_INCLUDE=false` 시 `latest:index`에서 KRX 제외 |
| DB legacy adapter (`get_source_rates_as_legacy_format`) | ❌ **별도 차단 정책 없음** | DB에 KRX row 있으면 `/api/rates`류 DB path에도 들어감 |

**현재 운영 영향**: KRX는 Stage 1에서 사용자 노출 0 (`KRX_BROADCAST_INCLUDE=false`라 broadcast 미포함). 그러나 `/api/rates/usd-krw-futures` 같은 **DB legacy path는 별도 차단 없어 잠재 노출 가능**.

**따라서 Z-2d는 "USDT만 제거"가 아니라 "topic-only source 전체를 legacy rates에서 제외하는 명시적 inclusion policy 도입"이 정확**.

후보 구현:

```python
def should_include_source_in_legacy_rates(source: str, asset: str) -> bool:
    """legacy rates 배열 inclusion policy.

    출시 계약: legacy rates = FX/은행/Investing만. USDT/KRX 등 topic-only는 제외.
    별도 allowlist 또는 SourceRegistry에 명시적 `legacy_rates_enabled` 같은 flag로 결정.
    category 단독 판단 금지 — reference/exchange/derivative는 표시/도메인 분류이지
    legacy 노출 계약 아님 (예: 향후 reference 추가 시 legacy 잘못 노출 위험).
    """
```

대상 source (현재 + 향후): `usdt-krw` (5거래소), `usd-krw-futures` (KRX), 향후 신규 topic-only source.

---

## 4. PR Breakdown

### Z-2a (이번 PR — spec 확정)

- 본 문서 작성 (`USDT_TOPIC_MIGRATION_PLAN.md`)
- 코드 변경 0
- 운영 영향 0

### Z-2b (backend topic dispatcher)

진행 상황 (2026-05-10):

- ✅ Stage 1 완료: `3f54a0c` — `app/topic_dispatcher.py` 신설 (TopicRegistry +
  publish_topic 골격). `TOPIC_DISPATCHER_ENABLED=false` default, 운영 영향 0.
- ✅ Stage 2 완료: `7a71a0d` — main.py WebSocket loop에 dispatcher 통합
  (subscribe/unsubscribe 메시지 + ping/pong 보존 + try/except/finally cleanup
  강화). FF=false라 subscribe 메시지 silently ignore.
- ✅ Stage 3-1 완료: `adad7a6` — 순수 builder `build_tether_tab_payload`
  (`app/usdt_topic_payload.py`). DB 의존 0, topic-agnostic, version=1 schema,
  legacy shape 호환 입력 + topic-native 출력.
- ✅ Stage 3-2 완료: `aeb03cc` — DB 통합 helper `load_and_build_tether_tab_payload`.
  저장소별 dispatch (USDT/KRX → source_rates, 은행 → bank_exchange_rates,
  Investing → investing_exchange_rates), `include_krx` 기본 False, env flag
  미해석 (호출자 책임).
- ✅ Stage 3 Level 1 완료: `9215eaf` — publish wrapper
  `publish_tether_tab_snapshot` (`app/tether_topic_publisher.py` 신규 모듈).
  orchestration 계층 (FF + subscriber guard → builder 호출 → publish_topic
  dispatch). hot path 미연결 (dead code). `TETHER_TOPIC = "usdt:krw"` 상수화 —
  활성화 직전까지 자유 변경.
- ✅ Stage 3 Level 2 완료: `39c6592` — broadcast cycle 임시 hook 연결.
  `app/main.py` broadcast_rates_once의 `is_changed` 분기 안 + `manager.active_connections`
  분기 외부 (legacy 0명이라도 topic 발화)에 `safe_publish_tether_tab_snapshot(db)`
  호출. `app/tether_topic_publisher.py`에 격리 wrapper 추가 (예외 → False 반환 +
  logger.exception, broadcast 영향 X). **임시 위치**: USDT WebSocket/Redis-first
  전환 후 mirror/topic pipeline으로 이동 예정. `TOPIC_DISPATCHER_ENABLED=false`라
  wrapper 진입은 발생하나 즉시 guard return — publish/builder 호출 효과 0.
  - sync/async 경계 회피 근거: `collect_usdt_rates`는 sync (ThreadPoolExecutor),
    `publish_topic`은 async. broadcast_rates_once는 async/main loop 안이라 자연 fit
- ✅ Telemetry 완료: `0330fa2` — Redis-backed counter (`topic:tether:stats` hash)
  및 admin endpoints (`GET /admin/api/topic-status`, `POST /admin/api/topic-status/reset`).
  best-effort 격리 (circuit_breaker 오염 X — `record_failure()` 호출 안 함),
  재배포 후에도 counter 누적 유지.
  - 운영 smoke 확인 (FF=false 상태): `hook_called == skipped_disabled`,
    `built = publish_called = 0`, `error = 0`. hook은 진입하지만 guard 차단으로
    builder/publish 비용 0.
  - FF=true dev/test 시험 success criteria: `built > 0` / `publish_called > 0`
    / `publish_sent_total > 0` / `error = 0`.
- ⏸ Stage 3 Level 3 (활성화) 보류: 5/19+ (5/18 만기 통과 후) 권장 —
  `include_krx` 정책 결정 (`KRX_TOPIC_INCLUDE` 신규 / `KRX_BROADCAST_INCLUDE`
  재사용 / 데이터 존재 게이트), `TOPIC_DISPATCHER_ENABLED=true` 활성화, 클라이언트
  release 동기화. 옵션: 5/12~5/17 중 dev/test client subscribe + 짧은 FF=true 시험으로
  실제 publish path 운영 검증.

#### Dev/test FF=true 시험 runbook (5/12~5/17 권장)

도구: `scripts/subscribe_tether_topic.py` (로컬 python websockets 기반,
`legacy/topic/raw` 메시지 분류 + summary 출력).

**서버 터미널** (ssh ubuntu@3.36.30.32, env 토글 + container recreate):

```bash
cd /home/ubuntu/exchange-rate
sed -i 's/TOPIC_DISPATCHER_ENABLED=false/TOPIC_DISPATCHER_ENABLED=true/' .env
docker compose up -d --force-recreate fastapi   # restart 아님 — env 재로드 필수
```

**로컬 터미널 A** (subscriber 실행, 별도 ssh 불필요 — 로컬에서 wss 접속):

```bash
python scripts/subscribe_tether_topic.py --timeout 600
# [CONNECTED] / [SUBSCRIBED] / [LEGACY] / [TOPIC] / [SUMMARY] / [UNSUBSCRIBED]
```

**로컬 터미널 B** (subscriber 붙은 직후 reset → 시험 구간 분리):

```bash
# enabled=true 확인
curl -s -u "admin:$ADMIN_PASSWORD" https://fxi.kr/admin/api/topic-status | jq '.enabled'

# subscriber 실행 중 reset
curl -X POST -s -u "admin:$ADMIN_PASSWORD" https://fxi.kr/admin/api/topic-status/reset

# 5~10분 후 subscriber 살아있는 동안 telemetry 확인
curl -s -u "admin:$ADMIN_PASSWORD" https://fxi.kr/admin/api/topic-status | jq
```

**시험 종료 — 서버 터미널에서 FF=false 복귀**:

```bash
sed -i 's/TOPIC_DISPATCHER_ENABLED=true/TOPIC_DISPATCHER_ENABLED=false/' .env
docker compose up -d --force-recreate fastapi
```

Success criteria (절대값, best-effort telemetry 특성 고려):

- `enabled == true`
- `subscribed_connection_count >= 1` (subscriber 실행 중일 때만)
- `built > 0`
- `publish_called > 0`
- `publish_sent_total > 0`
- `error == 0`
- subscriber 터미널에 `[TOPIC]` payload 수신 로그 확인

상대 비교 (`hook_called >= built >= publish_called`)는 참고값으로만 — telemetry
best-effort라 일부 Redis 호출 누락 가능, 엄밀 부등식 보장 X.

##### 실행 결과 (2026-05-11 15:54~15:57 KST, FF=true 짧은 시험)

운영 환경에서 실제 topic publish path 첫 검증 — 성공.

- subscriber 1명 (`scripts/subscribe_tether_topic.py --timeout 120`)
- telemetry: `built` / `publish_called` / `publish_sent_total` = **20 / 20 / 20** (정확 일치)
- subscriber 수신: **`[TOPIC]` 24** / `[LEGACY]` 25 / `[RAW]` 0
- payload `data` keys: `usdt_krw` + `usd_krw_banks` + `usd_krw_reference`
  - `usd_krw_futures` 부재 — `include_krx=False` 정상
- `error = 0`, `last_result = "sent"`, 운영 error 로그 0건
- FF=false 복귀 완료 (`subscribed_connection_count=0`, `skipped_disabled` 재개)

검증된 흐름:

- broadcast 변경 감지 → safe_publish_tether_tab_snapshot → publish_tether_tab_snapshot
  → load_and_build_tether_tab_payload → topic_dispatcher.publish_topic → ws.send_json
- legacy broadcast 정상 발화 (topic publish와 별개 채널 동시 유지)
- Telemetry Redis counter 정합 (build → publish 분기 누락 0)

builder/helper는 호출 경로 0이라 wire-up PR과 함께 배포해도 충분.

미결정 (Stage 3 wire-up 시 결정 필요):

- `include_krx` 정책: `KRX_TOPIC_INCLUDE` 신규 flag 도입 vs `KRX_BROADCAST_INCLUDE`
  재사용 vs `KRX_FUTURES_ENABLED + 데이터 존재` 게이트
- publish hook 위치: broadcast cycle 안 vs mirror cycle vs source 수집 직후
  (`changed_rates` 후처리)
- topic 활성화 flag: `TOPIC_DISPATCHER_ENABLED=true` 시점과 클라이언트 release
  타이밍

Stage 1/2/3 1차/2차 모두 `TOPIC_DISPATCHER_ENABLED=false` default 유지.

### Z-2c (USDT topic payload + migration 신호)

- `usdt:krw` topic snapshot/delta payload schema 확정
- 5거래소 + reference (Investing) 포함 방식 확정
- 신 클라이언트가 topic 받기 시작
- `source_rates` legacy 임시 경로는 Z-2d 전까지 유지

### Z-2c-FX (FX topic for topic-only clients, 2026-05-12)

새 단말이 legacy WebSocket/API 의존 없이 모든 외환 탭을 topic API로만 처리할 수
있도록 통화별 FX topic 추가. ADR-028 dual-emit 계약 유지 (legacy WebSocket
`rates` 배열은 그대로 운영, FX topic은 신 클라이언트용 병행 채널).

**Topic 이름** (`<domain>:<asset>` 패턴):

- `fx:usd-krw`
- `fx:jpy-krw`
- `fx:eur-krw`

**Payload schema** (USDT_PHASE1_CLIENT_GUIDE.md "FX topic schema" 섹션 잠금, 8d648a3):

- Top-level: `{type, version, topic, data}` — usdt:krw와 동일
- Entry: `{source, asset, rate, timestamp}` — display_name 부재
- `data.banks`: Required (list, transient 빈 list 허용)
- `data.reference`: Optional (Investing 데이터 + (source, asset) 정확 일치 시만 포함)
- single-asset topic이므로 data key에 asset prefix 미사용

**Step 분할**:

- Step 1 (edc62b6, 2026-05-11): `app/fx_topic_payload.py` builder + 29 tests
- Step 2 (a2e5645, 2026-05-12): `app/fx_topic_publisher.py` orchestration +
  `config.FX_TOPIC_ENABLED` flag (default false) + 15 tests
- Step 3 (5521cf0, 2026-05-12): `main.py` broadcast hook + admin endpoints
  - hook: `is_changed` 분기 안, `manager.active_connections` 분기 바깥
    (tether와 동일 격리 원칙)
  - `GET /admin/api/topic-status/fx` (3 topic 일괄 telemetry)
  - `POST /admin/api/topic-status/fx/reset` (3 topic 일괄 reset)
- Step 4 (5521cf0 배포 후 smoke, 2026-05-12 00:26 KST): FX_TOPIC_ENABLED=false
  배포 검증 통과. 4 invariant 충족: hook_called=8 / skipped_disabled=8 /
  built=publish_called=publish_sent_total=0 / error=0. builder/publish/DB
  비용 차단 + per-asset telemetry write 발생 패턴 정상.
- Step 5 준비 (c725b1e, 2026-05-12): `scripts/subscribe_fx_topic.py` smoke 도구
  — 3 topic 동시 구독 + schema invariant 검증 + exit code 5분류 (silent
  false positive 차단).
- Step 5 활성화 (2026-05-12 00:48 KST): FX_TOPIC_ENABLED=true + force-recreate +
  로컬 smoke 180s 실행. 결과:
  - 3 topic 각 35회 수신 (total 105 messages, invalid=0)
  - 각 payload: banks=9, reference=True
  - telemetry per-topic: hook_called=40, built=publish_called=publish_sent_total=35,
    skipped_no_subscribers=5 (smoke 종료 후 subscriber=0 broadcasts),
    error=0, last_result transition `built` → `sent` → `skipped_no_subscribers`
  - 수치 invariant: hook_called = built + skipped_no_subscribers = 35 + 5 = 40 ✓
  - 결정: FX_TOPIC_ENABLED=true 유지 (단말 release 전이라 사용자 영향 0,
    fx topic 구독자 없으면 skipped_no_subscribers로 builder 비용 차단,
    문제 발생 시 .env로 즉시 false 복귀 가능, usdt:krw 운영 패턴과 일관)

**Telemetry**:

- per-topic Redis key: `topic:fx:<asset>:stats` (tether legacy `topic:tether:stats`와
  namespace 분리)
- counter: hook_called / skipped_disabled / skipped_no_subscribers / built /
  publish_called / publish_sent_total / publish_zero / error
- best-effort 기록: circuit.can_attempt() 체크만 (record_failure 호출 X) — core
  Redis 경로 오염 차단

**Guard 정확성 (multi-topic 환경)**:

- `subscriber_count(topic)` per-topic 사용 (subscribed_connection_count 아님)
- 다른 topic 구독자만 있을 때 해당 topic publisher는 builder 호출 0회로 skip

**격리**:

- 1개 asset 예외도 `safe_publish_all_fx_snapshots`의 per-asset try/except로 격리
- broadcast 정상 흐름 보호 — `logger.exception` + telemetry error +1 + False return

### Z-2d (legacy rates에서 topic-only source 제외 — 운영 완료 2026-05-12)

§3.1에서 발견한 정책 부재 차단. **USDT만 제거 X — topic-only source 전체
inclusion policy 통일**. Step 1-5 완료, 운영 배포 + 통합 smoke 검증 통과.

**Step 분할 (모두 완료)**:

- Step 1 (fa978b0, 2026-05-12): `app/legacy_policy.py` + 14 tests
  - `LEGACY_RATE_ASSETS` / `LEGACY_RATE_SOURCES` tuple 상수
  - `should_include_source_in_legacy_rates(source, asset)` — (source, asset)
    두 set AND allowlist. 호출자 없음 (운영 영향 0).
- Step 2 (eee2912, 2026-05-12): `crud.get_source_rates_as_legacy_format` filter
  - `_filter_source_entries_by_legacy_policy` private helper
  - REST `/api/rates*` + WebSocket DB fallback + Redis mirror seed 자동 커버
  - 현재 source_rates에는 USDT/KRX만 → filter 후 빈 list 반환
- Step 3 (3ae837e, 2026-05-12): `latest_rates_cache.should_include_source_in_latest`
  → `legacy_policy.should_include_source_in_legacy_rates` 단순 위임
  - Redis fast path와 DB fallback 양쪽 정책 일관성 완성 (이중 안전망)
  - `KRX_BROADCAST_INCLUDE` 무력화 (env/config는 별도 cleanup PR)
  - invariant 테스트: latest == legacy in both toggle states
- Step 4 (7d19ff5, 2026-05-12): REST `/api/rates/{currency}` 410 Gone
  - `LEGACY_REMOVED_RATE_TOPICS` dict + `get_removed_legacy_rate_topic` +
    `build_legacy_removed_detail` helper (Option A — main.py firebase_admin
    의존성 회피 위해 detail builder를 legacy_policy 모듈에 배치)
  - `/api/rates/{currency}` handler에 DB 호출 전 fail-fast 분기
- Step 5 (배포 + 통합 smoke 2026-05-12 KST): 6항목 모두 통과

**Step 5 통합 smoke 결과 (2026-05-12 KST)**:

| # | 항목 | 결과 |
| --- | --- | --- |
| 1 | `/api/rates/usdt-krw` | HTTP 410 + `{error: legacy_rate_removed, currency: usdt-krw, use_topic: usdt:krw}` |
| 2 | `/api/rates/usd-krw-futures` | HTTP 410 + 동일 shape, `use_topic: usdt:krw` |
| 3 | `/api/rates/usd-krw` (회귀 보호) | HTTP 200 + 10 banks (investing + 9 banks) |
| 4 | `/api/rates` aggregate | total=30, currencies={eur-krw, jpy-krw, usd-krw}, USDT/KRX 0건 |
| 5 | WebSocket `/ws` legacy `rates` | total=30, 동일 shape, USDT/KRX 0건 |
| 6 | usdt:krw + fx:* topic API | error=0, enabled=true 유지, last_result 정상 분기 |

**검증된 운영 invariant**:

- REST allowlist 적용 — `/api/rates` aggregate + `/api/rates/{currency}` 모두
  topic-only source 자동 제외
- WebSocket broadcast 일관성 — Redis fast path + DB fallback 양쪽 동일 정책
- 410 + use_topic 안내 — 새 단말 마이그레이션 명확
- 회귀 보호 — allowed FX 응답 변경 없음
- Topic API 격리 — usdt:krw / fx:* 모두 영향 없음 (별도 builder, legacy policy 미경유)

**잔존 cleanup 작업**:

- ✅ `KRX_BROADCAST_INCLUDE` env/config 변수 제거 완료 (2026-05-12, Z-2d cleanup
  commit). app/config.py / .env.example / test patch 정리. ADR-027 / CHANGELOG /
  KRX_CANARY 등 historical 문서는 "removed in Z-2d cleanup" 표시로 보존.

**클라이언트 영향**:

- iOS/Android 운영 앱: 영향 0 (테더 탭/KRX 탭 없음, legacy USDT/KRX 요청 안 함)
- iOS dev/test (legacy USDT 의존 시): 410 받음 → topic API 마이그레이션 필요
  (`USDT_PHASE1_CLIENT_GUIDE.md` FX/USDT topic schema 참조)
- topic API(usdt:krw, fx:*): 영향 0 (자체 builder, 정상 운영 유지)

### Z-2e (USDT Redis-first read/write — 운영 완료 2026-05-12)

USDT는 Z-2d allowlist 미통과로 mirror cycle skip → broadcast hot path의 ADR-026
Redis-first 모델 미적용. usdt:krw topic builder의 데이터 freshness/DB 부하를
별도로 해결할 필요 발생. [ADR-029](DECISIONS.md#adr-029-usdt-source는-mirror-cycle-미경유--direct-write--read-path-db-fallback)로
의사결정 기록.

**Step 분할 (모두 완료)**:

- B-Step 1 시도 1 (rollback): `887ccd1` → `82ba062` (revert)
  - `asyncio.run(set_latest(...))` + main loop의 `redis.asyncio.Redis` client 재사용
  - **실패 원인**: event loop binding mismatch — async client는 main loop bound,
    asyncio.run의 새 loop와 호환 X
  - **위협**: `circuit.record_failure()` 누적 → broadcast/mirror Redis path 오염
  - 운영 mirror "일부 실패" warning 발생 → 즉시 rollback
  - 교훈: mock 단위 test로는 event loop binding 호환성 검증 불가. 실제
    environment dry-run 필수 (이후 D1 도입).

- B-Step 1 재시도 (`1f3ab36`, 2026-05-12): sync `redis.Redis` client 별도
  - `latest_rates_cache.set_latest_source_rate_from_sync_job` 추가
  - scheduler thread에 자연스러운 sync client (redis-py sync는 thread-safe pool)
  - async circuit_breaker 미사용 — broadcast/mirror Redis path 격리
  - `_mirror_changed_source_to_redis` (crawler helper) — INSERT 성공 직후 호출
  - D1 dry-run: 로컬 redis 컨테이너에서 4 cases 통과 후 운영 배포
  - 운영 smoke: USDT direct write 실패 0건, `mirrored_at - timestamp` ~6-7ms

- B-Step 2 (`6f743f0`, 2026-05-12): usdt:krw topic builder Redis-first read
  - `get_latest_source_rate_from_sync_job` (sync read helper)
  - `crud.get_latest_source_rates_for_topic` (topic 전용 raw fetcher,
    Z-2d legacy_policy 우회 명시)
  - `TETHER_TAB_EXCHANGE_SOURCES = ("upbit", "bithumb", "coinone", "korbit", "gopax")`
    — usdt_topic_payload.py private 상수, builder-local 출력 순서 계약
  - 5거래소 모두 Redis hit → DB 0회 / 1개 miss → 전체 DB fallback
  - **Stale 시간 판정 X** — USDT는 mirror 갱신 없음, 거래 뜸한 source(gopax 등)는
    자연 오래된 mirrored_at. is_stale 적용 시 매번 fallback → Redis-first 무의미
  - B-Step 1 docstring/로그 메시지 정정 ("mirror cycle 복구" → "read path DB
    fallback") — 운영자 오해 방지
  - 운영 smoke 4 항목 통과: topic API error=0, "mirror cycle 복구" 로그 0건,
    Redis 5거래소 hit, DB fallback 호출 0건

**핵심 정책**:

- USDT는 mirror cycle repair 대상 아님 (Z-2d allowlist 미통과)
- direct write 실패 시 mirror가 복구하지 않음 — read path DB fallback이 단일 안전망
- Redis miss/parse fail → 5거래소 전체 DB fallback (source별 mix 회피, payload 일관성)
- async circuit_breaker 격리 — broadcast/mirror Redis path 보호
- 자세한 의사결정 + Alternative 검토: [ADR-029](DECISIONS.md#adr-029-usdt-source는-mirror-cycle-미경유--direct-write--read-path-db-fallback)

**잔존 작업 (future enhancement)**:

- direct write 실패율 / fallback 호출 빈도 telemetry — mirror repair 없으므로
  운영 모니터링이 영구 stale 감지 단일 수단
- banks/reference/futures Redis-first 확장 (B-Step 3, 별도 phase) — 이쪽은
  mirror cycle 기반이라 USDT와 다른 환경

**클라이언트 영향**:

- 운영 단말: 영향 0 (사용자-facing 변화 없음)
- 단말 freshness 기준 = usdt:krw topic payload (legacy `rates` 배열은 Z-2d로 USDT 제외됨)

**iOS 단말 검증 완료 (2026-05-12)**:

검증 단계 phase 종료 — `usdt:krw` topic 계약을 iOS 단말에서 9/9 PASS로 닫음.
대상: DEBUG/dev 빌드. 비범위: release productization, UX polish, 알림 통합, FX 탭 topic 전환.

| # | 항목 | PASS |
| --- | --- | --- |
| 1 | `usdt:krw` subscribe → snapshot 1회 이상 수신 | ✓ |
| 2 | Swift Codable 매핑 — decoding error 0 | ✓ |
| 3 | `data.usdt_krw` 5거래소 모두 list view 표시 | ✓ |
| 4 | `data.usd_krw_banks` + `data.usd_krw_reference` 표시 | ✓ |
| 5 | `data.usd_krw_futures` Optional 처리 정상 | ✓ |
| 6 | legacy `/api/rates/usdt-krw` 호출 코드 grep 결과 0 | ✓ |
| 7 | WebSocket reconnect → subscribe 재전송 → snapshot 재수신 | ✓ |
| 8 | background → foreground 전환 시 subscribe 재시도 | ✓ |
| 9 | unsubscribe → 추가 snapshot 미수신 | ✓ |

결론: iOS DEBUG/dev 빌드 기준 `usdt:krw` topic 계약 검증 9/9 PASS. 서버-단말 계약은 검증 단계 기준 완료.

후속 phase 후보 (별도 결정):

- iOS productization (release 적용, UX polish, 알림 통합)
- Android 검증 (iOS와 같은 마이그레이션 단계)
- FX 탭 topic 전환 (`fx:*` topic — server는 활성 상태)
- Server B-Step 3 (banks/reference Redis-first 확장) / direct write telemetry

---

## 5. Deployment / KRX Baseline Constraints

### 5.1 KRX baseline window 영향

[KRX_CANARY.md](KRX_CANARY.md) 참조 — Stage A telemetry는 in-memory counter:
- FastAPI 재배포 시 `fb_*` counter 모두 리셋
- 5/11 ~ 5/15 평일 baseline 수집 중 운영 배포 자제
- 5/18 만기 임박 (5/15~5/17) minimal 변경

### 5.2 PR별 권장 배포 timing

| PR | 배포 권장 시점 | 이유 |
|---|---|---|
| Z-2a (이 문서) | 언제든 | 문서만, 운영 영향 0 |
| Z-2b (dispatcher) | 5/19+ (만기 통과 후) | 신규 backend 경로, 안정 검증 필요 |
| Z-2c (USDT topic) | 5/19+ + 클라이언트 준비 | client side 마이그레이션 필요 |
| Z-2d (legacy 제거) | 클라이언트 마이그레이션 완료 후 | 호환 깨짐 방지 |

### 5.3 클라이언트 마이그레이션 호환

- iOS/Android 신 버전이 topic 사용 가능한 상태 → Z-2d 안전하게 진행
- iOS dev/test 클라이언트가 legacy USDT 의존 시 Z-2d 진행 시 영향 — `USDT_PHASE1_CLIENT_GUIDE.md` 갱신 + dev/test 빌드 마이그레이션

---

## 6. Open Questions

### 6.1 Topic 명명 규칙

후보:
- (a) `usdt:krw` — 단일 토픽, payload에 5거래소 + reference 모두 포함
- (b) `usdt:upbit:usdt-krw` 등 거래소별 분리 — 구독 세밀도 ↑
- (c) hybrid: 종합 + 개별 모두 발사

[REALTIME_ARCHITECTURE_PLAN.md §5](REALTIME_ARCHITECTURE_PLAN.md)는 `usdt:krw` 단일 토픽 잠정 — Z-2b 구현 시 확정.

### 6.2 Reference (Investing) 포함 방식

- USDT 탭 UI는 거래소 가격 vs Investing 기준가 비교 — 같은 topic에 포함하는 것이 자연
- 또는 `dxy` 같은 별도 topic으로 분리

### 6.3 Snapshot vs Delta 균형

- 신규 client 접속 시 snapshot 1회
- 그 후 delta (변경된 source만)
- snapshot interval (예: 5분) — 데이터 손실 복구

### 6.4 알림 schema 변경 시 client 호환

- 현재 FCM payload type `source_rate_alert` 그대로 유지면 client 영향 0
- topic schema 추가 정보 (예: 다거래소 비교) 시 schema 확장 필요

### 6.5 Dual-emit 기간 (Long-term, 본 PR 범위 외)

- FX/은행/Investing dual-emit은 legacy 클라이언트 비율이 임계값 이하로 떨어질 때까지 유지
- legacy rates 전체 제거 PR은 별도 (Z-3 가설)
- timing 결정은 운영 metric (legacy `/api/rates` 호출 빈도 + 신규 topic 사용률) 기반

---

## 7. 참조

- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md): 서비스 출시 계약 source of truth
- [DECISIONS.md ADR-028](DECISIONS.md#adr-028-topic-only-tetherkrx--legacy-fx-dual-emit): topic-only Tether/KRX + legacy FX dual-emit 결정
- [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md): source/asset 도메인 모델 (Phase 1 백엔드)
- [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md): RateSource / 어댑터 / SourceRegistry 모델 (Phase 1 클라이언트)
- [KRX_CANARY.md](KRX_CANARY.md): KRX Stage 1/2 운영 + Stage A/B baseline (운영 배포 제약 자료)

---

**다음 단계**: Z-2b (backend topic dispatcher) 설계는 KRX 만기 통과 후 (5/19+) 별도 PR로 진행. Z-2a는 본 문서 commit으로 완료.
