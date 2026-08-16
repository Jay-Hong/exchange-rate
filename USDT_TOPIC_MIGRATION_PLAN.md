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

- `/api/source-notification-settings` (POST/GET/PUT/DELETE) — `app/main.py:3793-3796`(POST) · `app/main.py:3875-3878`(GET) · `app/main.py:3906-3909`(PUT) · `app/main.py:3998-4001`(DELETE)
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

> ⚠️ **2026-07-25 갱신 — 아래 `TOPIC_DISPATCHER_ENABLED=true` 안내는 당시(2026-05) 기준 historical record다.**
> 현재 flag ON은 ADR-039 §8.1 **E3**(REST twin 인증 게이트, land) + **1C**(WS 인증) + 클라 bootstrap 인증 이관을
> 선행 조건으로 갖는다 — 이 문서 절차만 보고 켜지 말 것. 정본: [FREE_TIER_ACCESS_MODEL_PLAN.md](FREE_TIER_ACCESS_MODEL_PLAN.md) §8.1.

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
  - `latest_rates_cache.set_latest_usdt_rate_from_sync_job` 추가
  - scheduler thread에 자연스러운 sync client (redis-py sync는 thread-safe pool)
  - async circuit_breaker 미사용 — broadcast/mirror Redis path 격리
  - `_mirror_changed_source_to_redis` (crawler helper) — INSERT 성공 직후 호출
  - D1 dry-run: 로컬 redis 컨테이너에서 4 cases 통과 후 운영 배포
  - 운영 smoke: USDT direct write 실패 0건, `mirrored_at - timestamp` ~6-7ms

- B-Step 2 (`6f743f0`, 2026-05-12): usdt:krw topic builder Redis-first read
  - `get_latest_usdt_rate_from_sync_job` (sync read helper)
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

**완료 항목**:

- direct write 실패율 / Redis read hit/miss / fallback 호출 빈도 telemetry —
  B-Step Telemetry(2026-05-13, `app/usdt_redis_stats.py` + `GET /admin/api/usdt-redis-stats`)로
  1차 계측 도입. process-bound counter, `started_at` 노출.

**잔존 작업 (future enhancement)**:

- banks/reference/futures Redis-first 확장 (B-Step 3, 별도 phase) — 이쪽은
  mirror cycle 기반이라 USDT와 다른 환경
- telemetry 고도화 (Prometheus / Redis hash 등 외부 monitoring 통합) — 추세
  분석 필요 시점에 검토

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
- Server B-Step 3 (banks/reference Redis-first 확장) — direct write telemetry는
  B-Step Telemetry(2026-05-13)로 도입 완료

### Z-2f (latest:index 책임 분리 — 설계 단계 2026-05-13)

PR Z-2e Step 3b(`a499a08`, 2026-05-13) bank/investing direct write 운영 진입 후
`latest:index.mirrored_at`의 freshness gate 의미가 direct write 시대와 충돌함을
확인. broadcast Redis-first hot path가 개별 data key 갱신과 무관하게 index single
signal에 의존하는 구조 → mirror cycle 격하/제거의 전제 조건이 막힘.

[ADR-030](DECISIONS.md#adr-030-latestindex-책임-분리--freshness는-per-key-mirrored_at으로-판단)
(Proposed, 2026-05-13)에서 옵션 C 채택. 상세 설계는 ADR-030 참조.

**1차 범위**:

- `latest:index`는 key membership list로 격하 (mirrored_at field는 schema 유지하되 read path에서 ignore)
- freshness는 각 data key의 mirrored_at으로 판단 (기존 `is_stale()` 재사용)
- 1개라도 per-key 문제 → 전체 DB fallback (보수적)
- mirror cycle 3초 유지 (warmup/repair/keys 관리/unchanged key refresh)

**비범위**:

- mirror cycle interval 격하 (Z-2g 후보)
- 부분 fallback (per-asset) — future phase
- schema 정리 (mirrored_at field 제거)

**검증 시 ADR-030 status를 Accepted로 전환**.

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

### 6.6 Bank/Investing source observation-based fanout (후속 phase 후보, 2026-05-22 추가)

**컨텍스트**: PR4 Step B (main.py legacy hook 격하/제거)는 Bank/Investing이 source-level topic trigger router를 갖추지 못해 보류됨. main.py hook은 단순 fallback이 아니라 은행/Investing 변경을 fx:* + usdt:krw 양쪽 topic으로 발사하는 primary bridge 역할. 따라서 단순 trigger 추가가 아니라 수집/저장/알림/topic publish 전체 구조 재설계가 필요. 자세한 사유는 [USDT_WS_DESIGN_PLAN.md §14.7 2026-05-22 보강](USDT_WS_DESIGN_PLAN.md#147-legacy-piggyback-격하-정책) 참조.

📌 **Scope 해석 잠금 (2026-05-25 추가)**: 본 phase는 [USDT_WS_DESIGN_PLAN.md](USDT_WS_DESIGN_PLAN.md) §12.8.3.3에서 "(3) Bank/Investing β 영역 — coalescer `window=0` pass-through 적용"으로도 cross-ref되지만, **window=0 alert coalescer 적용은 본 phase 본체의 작은 하위 측면일 뿐 ≠ 본 phase scope**. 본체는 DB-first monolithic → observation fanout 재설계 + freshness metadata 정렬 + main.py legacy hook 격하 가능성까지 포함하는 대규모 refactor. 단순 "window=0 framework cleanup"으로 좁게 해석 금지.

📌 **우선순위 메모 (2026-05-25 추가)**: [KRX Stage E](KRX_FANOUT_REFACTOR_PLAN.md) (KRX Redis tick-level 전환) 다음 진입 후보. KRX는 단일 source라 scope가 좁고 USDT 검증 패턴 (freshness metadata + SET-only topic trigger + legacy timestamp 호환)을 KRX에 먼저 이식하여 패턴 안정성을 확보한 뒤 Bank/Investing β로 진입. 본 phase 안정화 후 USDT 5 source 공통화 검토 (3 도메인 검증 패턴 input 기반, 선제 abstraction 금지 원칙 정렬). 우선순위 합의 — KRX Stage E → Bank/Investing β → USDT 공통화.

📌 **상위 anchor**: 본 follow-up은 [REALTIME_ARCHITECTURE_PLAN.md §4.1 "All-source observation fanout contract"](REALTIME_ARCHITECTURE_PLAN.md) 아래 통합 phase의 Bank/Investing 측 진입점이다. terminology(`rate_changed_at` / `seen_at` / `mirrored_at`)는 [REALTIME_ARCHITECTURE_PLAN.md §4.1.3](REALTIME_ARCHITECTURE_PLAN.md)을 따른다.

📌 **공통 설계 축 vs 현재 경로 차이**: KRX의 [KRX_FANOUT_REFACTOR_PLAN.md §5.2 Stage E](KRX_FANOUT_REFACTOR_PLAN.md) (`KrxRedisLatestWriter` DB-insert-bound → tick-level 전환)와 **같은 설계 축**(observation fanout + freshness metadata 분리)을 공유한다. 다만 **현재 경로는 다르다**: Bank/Investing은 DB-first monolithic(crud.py 안에서 DB → Redis helper → `process_rate_alerts` 직렬), KRX는 DB-insert-bound Redis write(`KrxDbWriter` 안에서 DB insert 성공 후 Redis write)다. 두 source는 같은 통합 phase에서 다른 진입점으로 흡수된다.

**목표**:

- 크롤러 fetch 결과를 source observation으로 표현하여 Redis writer / DB writer / Alert evaluator / Topic trigger router 단계 분리 (USDT WS 패턴 또는 유사 구조 적용)
- DB SELECT 최소화 — DB INSERT 전 중복 검사를 DB SELECT 대신 Redis 값 기반으로 ([REALTIME_ARCHITECTURE_PLAN.md §1](REALTIME_ARCHITECTURE_PLAN.md) "DB를 실시간 전송 경로에서 분리" 방향 일관)
- fx:* topic trigger와 usdt:krw trigger를 source/asset 기준으로 라우팅
- legacy WebSocket dual-emit은 구버전 앱 호환을 위해 유지 (§6.5 일관)
- 안정화 후 main.py topic publish hook 제거 (PR4 Step B Option A) 재검토

**topic trigger 정책 (검토 input)**:

- topic trigger는 기본적으로 rate 변경 또는 topic payload에 의미 있는 state 변화(예: freshness 회복, stale 복구)가 있을 때 발화한다.
- successful fetch 자체는 seen_at/mirrored_at 갱신으로 기록하되, 같은 rate 반복 fetch가 항상 topic publish를 유발하지는 않는다.
- 각 옵션의 timestamp 분리 정책 (`rate_changed_at` vs `seen_at` vs `mirrored_at`)은 별도 phase에서 확정.

**미래 구조 옵션** (확정 보류):

- **α USDT 패턴 그대로**: 매 fetch → Redis SET + trigger. 단순. 같은 값 반복 publish 우려 (은행/Investing은 값 변화 드물어 USDT보다 동일 payload 발사 비율 ↑).
- **β 절충 (현재 유력 후보)**: Redis는 매 fetch에서 seen_at/mirrored_at 갱신, rate/timestamp는 값 변경 시만 갱신, trigger는 위 정책에 따라 발화. observation freshness 정확 + publish 효율적.
- **γ 보수**: Redis SET/trigger 모두 값 변경 시만, freshness는 별도 추적.

**진입 조건**:

- §12.8 Post-5-source 정책 표준화 결정 후 (5 source 24-72h 관찰 누적 + Upbit/Bithumb 2-signal 전환 결정 등 선결)
- 또는 별도 우선순위 평가

**설계 input**:

- 현재 mirror cycle 기반 구조 ([app/crud.py:102-153](app/crud.py#L102-L153) `_write_changed_bank_rates_to_redis` / `_write_changed_investing_rates_to_redis`) vs USDT 패턴 (tick-driven 단계 분리)
- Redis 입력 옵션 (α/β/γ)
- DB 쿼리 최소화 (Redis 기반 dedup)
- legacy broadcast + fx:* + usdt:krw 3 채널 dual-emit 정책 일관성 (§6.5)
- 작업 분량/위험 평가 (대규모 refactor)

#### 6.6.1 Characterization-first 스코핑 + 현 계약 catalog + PR A land (2026-06-13, 리뷰 수렴)

KRX Stage E 안정화 후 본 phase 진입. 두 세션 Claude + Codex 리뷰 수렴으로 read-only 계약 catalog 확정 + PR A(characterization, behavior-change-0) land. **본 절이 PR B의 fixed anchor**.

**현 계약 catalog (확정)**:

| 층 | 사실 |
| --- | --- |
| 저장 함수 ([crud.py:156](app/crud.py#L156) bank / [:271](app/crud.py#L271) investing) | insert-if-changed(per-pair DB SELECT, `timestamp DESC,id DESC`, None skip) → count>0 시 단일 `db.commit()` → `_write_changed_*_to_redis` → `process_rate_alerts`. **순서 잠금**(docstring). 출력 `new_records_count`. timestamp=`models.get_utc_now()`(save-time — 은행 고시는 관측=의미라 USDT exchange-ts 문제 없음). 2 shape: FCM `{bank,currency,rate}` / topic-native `{source,asset,rate,timestamp=KST}`. 실패 격리: redis best-effort / alert try/except / commit 실패는 상위 전파 |
| Redis 직접 write ([crud.py:102](app/crud.py#L102)/[134](app/crud.py#L134)) | `latest:bank:*`/`latest:investing:*` **latest-only** (write-through topic trigger 없음 = β 핵심 격차). writer는 **bool 반환**(`_write_changed_*`가 무시) → 실패 = exception ∪ False, 둘 다 best-effort(mirror 3s 복구) |
| Legacy hook = topic primary bridge ([main.py:741](app/main.py#L741)) | broadcast 매초 `is_changed`(whole-payload JSON diff) + `FX_TOPIC_ENABLED` → `fx_topic_publisher.safe_publish_all_fx_snapshots(db)`가 **모든 fx snapshot DB 재발행**(source-routed 아님). **`FX_TOPIC_ENABLED` production=true** (config default false override, 운영 컨테이너 printenv 확인) → fx: 발행 **live** → PR D는 dead-code 제거 아닌 **write-through 인수 후 전환** |
| outer try/except ([main.py:798](app/main.py#L798)) FACT | publisher 호출(730/741)이 함수 전체 `try`(664)/`except`(798)/`finally: db.close()`(806) 안. **publisher raise → outer except가 crash backstop(프로세스 생존)** / 단 그 cycle의 legacy send(743+)는 유실 → **wrapper no-raise = per-cycle send 가용성 계약**(2층) |
| Blast radius | 9 은행 27 call site(Request+Selenium fallback) + investing 1. 다수 호출부가 count를 상위로 return → **signature(반환 타입 포함) 유지 선호**(호출부 호환) |
| Phase B.2 교차 | [USDT_WS_DESIGN_PLAN §14.7](USDT_WS_DESIGN_PLAN.md) "legacy piggyback 격하" 미결 = β PR D와 동일 대상(중복 아닌 **흡수 관계**) |

**PR 분할 (characterization-first, KRX A/B/C behavior-change-0 패턴 재사용)**:

- **PR A — land (`bb653ee`, 2026-06-13, tests-only / production code 0, CI green / 0 warnings)**: broadcast wiring 3(changed+active→publisher 2 + legacy send + record_success / unchanged→미호출 + record_skip(no_changes) / active-0→publisher 호출 + send 미호출 + record_skip(no_connections), **각 record_failure 미호출 positive control로 outer-except vacuous 차단**) + crud isolation 4(process_rate_alerts 예외→insert 미전파 / Redis False-return→alert 계속 + commit 무영향, 각 Bank/Investing). (v) publisher-raise 제외(내부 격리는 test_fx_topic_publisher가 잠금 + outer except backstop). 신규 `tests/test_broadcast_rates_once_wiring.py` + `tests/test_source_direct_write.py` +class. conftest harness(firebase stub + sqlite)로 main.py import.
- **PR B — land (`a0eb22c`, 2026-06-13, behavior-change-0, CI green / 0 warnings)**: 저장 함수 → staging(`_stage_*_rate_changes`) + 공통 adapter(`_changes_to_redis_updates`/`_changes_to_fcm`) + orchestrator 분리. signature/호출 순서/count==0 게이트/sink dict shape/patch-target 심볼 불변. **`ChangedRate` narrow DTO**(changes-only, `changed_at`=save-time — freshness는 C2). bank/investing staging은 **의도적 중복**(통합 defer). **boundary fix(Codex review)**: sink payload 변환(`to_kst_isoformat`)을 `db.commit()` **전**으로 — 구 staging-loop와 동일 failure boundary(변환 실패 시 commit 전 차단 → DB 미커밋 보존) + lock test 2건. 기존 + PR A characterization 무수정 통과 = behavior-change-0 증명.
- **PR C (write-through 도입) — source-routed trigger-only로 재스코핑 (2026-06-13 설계 확정, 상세 §6.6.2).** 원안 번들 분해: **C1=source-routed topic trigger-only (+ SET-only trigger gating — Redis outcome은 C1 correctness 전제, defer 아님)** / **C2=freshness**(seen_at/mirrored_at 5-field, mirror ownership 재설계 선행) / **C3=DB→Redis dedup**(독립 최적화, change-only INSERT 토대 변경이라 최고 위험). **PR C = C1만.** 근거: fx publish는 publisher 자체 DB session + payload builder **Redis-first + DB fallback**([fx_topic_payload.py:208-227](app/fx_topic_payload.py#L208-L227))이고 published payload는 `{rate,timestamp}`만(seen_at 미포함) → trigger는 freshness(C2)/dedup(C3)와 직교. C1만으로 β 핵심 격차(write-through trigger 부재) 해소 + PR D 잠금 해제.
  - **C1 — repo land (2026-06-14)**: `05f759b`(design §6.6.2) + `950a114`(Inc1 scaffolding — bridge/controller/safe_publish_fx_snapshot) + `b3554cc`(Inc2 emission wiring — crud `list[dict]` outcome + emission + lifespan + bridge orphan race fix) + `645b60a`(Inc3 Redis telemetry). pushed origin/master, **CI green** (run 27471463640, `2164 passed / 1 skipped / 131 subtests / 0 warnings`, lock-version/ubuntu). **default `legacy_piggyback`**. axis #2 SET-only / #3 alert 독립 / #4 legacy gate / shutdown orphan race(lock+barrier) 모두 판별 test-lock.
    - **Stage 0 배포 완료 (2026-06-14, 주말 KRX 무세션 창)**: EC2 image `26e96f8a`, running config `BANK_INVESTING_TOPIC_TRIGGER_MODE=legacy_piggyback` resolved → **behavior-change-0 실증** (emission noop / legacy hook 단독 / 에러 0 / USDT 5-source 재연결 / broadcast loop · KRX 주말 idle 정상). `.env` `FX_TOPIC_ENABLED=true` + `TETHER_TOPIC_TRIGGER_MODE=direct_coalesced` 확인(Stage 2 / cross-route precondition 충족).
    - **dual_shadow armed (2026-06-14, 주말 클린 창)**: `BANK_INVESTING_TOPIC_TRIGGER_MODE=dual_shadow` 플립 + arming 검증(resolved dual_shadow / publish 0 = 유저 영향 0 / 에러 0 / 서브시스템 정상 / flip-time baseline `trigger_*={}`, `.env.bak.predualshadow-20260614` rollback). **bridge production positive-control + direct 전환은 2026-06-15 pending**(당시 시점 기준 — **6/15 완료, 아래 bullet이 supersede**) — 6/15 07:00 리마인더(`trig_01R3HdGsRoTFpgT6yZsf16Yr`): rollover 라이브 + 고시 전 clean baseline → 오전 delta>0(∧ bank변경↔trigger correlate) gate → 15:50 direct+REST flag 번들(gate-conditional).
    - **6/15 direct+REST flag 활성 — C1 직접 발행 가동 + cross-route request path 실증 (2026-06-15)**: 6/14 pending 4축 PASS 후 16:12 flip(`.env.bak.predirect-20260615`). **[A]** KRX rollover A75606→A75607(07:04:45 `reason=scheduled`, 에러 0). **[B] dual_shadow positive control** PASS — 07:01·07:25·08:26 단조 일관 + invariant(`req=flush+coalesced+pending`, pending=0 / err=0 / per-asset USD·JPY·EUR / usd `tether_route_shadow>0`) + SET 실패 0(변경=trigger 1:1) + **12:30 뉴스배포 force-recreate 넘어 연속**(bridge restart 내성). **[KRX health]** 08:45:15 첫 A75607 체결→Redis 6/15(호가=health·체결=fanout 분리 [krx_kis.py:148](app/crawlers/krx_kis.py#L148) 라이브 일치). **[C] CF close daily append** 15:46:01 `INSERT date=2026-06-15 A75607 rate=close=1511.0 H=1513.1 L=1502.3 point_count=5942 close_basis=krx_cf_close_1545` → `contract_code=A75607` 영속 + 직전 6/12·6/11=A75606 = 깨끗한 rollover 경계(재부상 0). **[D] flip** `BANK_INVESTING_TOPIC_TRIGGER_MODE=direct_coalesced` + `KRX_CLOSE_REST_WRITE_ENABLED=true`(.env 부재→append, default false 의존이었음): ① resolved direct/True + KRX Redis·DB 무변화(REST inert) + health + ERROR 0. ② fx direct `flush_direct=called` 성장·err=0·`last_result=publish_zero`(fx:\* 구독자 0 = 정상, 신규 앱 구독 시 publish_success). **cross-route REQUEST PATH 실증**: `tether_route_shadow` frozen(3042)=direct가 shadow 미기록·live 분기 사용([crud.py:291](app/crud.py#L291)) + 고속 샘플 13031회서 `topic:tether:stats` `last_reason=bank_investing_fx_change`[cross-route 전용 reason] 포착 + Bank/Investing USD SET-success(동시각 investing usd-krw 변경 16:23:19.114)와 4ms 상관. **개별 source(Investing 특정) 귀속은 비원자 hset telemetry(source 필드 torn-read=bithumb)라 추정**. **단 `last_reason`은 request 시점 기록([tether_topic_trigger.py:247·296](app/tether_topic_trigger.py#L296)), publish 완료 아님** → Bank/Investing USD SET→request_tether_topic_trigger→controller 진입까지만 실증, **실제 topic publish·payload 구독단말 전달 귀속은 pending**(Codex 2차 정정 수용 — shadow frozen은 necessary-not-sufficient). psycopg `error=4`=restart 전 누적 배경(post-flip 0). **Stage 2 = activated, canary 진행 (2026-06-15 실수신 검증)**: **(usdt:krw)** iOS 테더 탭 실수신 + KB/Hana **표시값이 관찰 시점 Redis rate와 수치 일치**(raw payload bytes/ts 미캡처, investing 근접=정황) — **cross-route-caused publish 귀속은 USDT trigger coalescing + 비원자 telemetry로 미확정**. **(fx:\*)** `subscribe_fx_topic.py`로 **C1 direct subscriber delivery 확인**(`trigger_publish_success` None→7/5/3, C1 controller 전용 카운터) + 같은 구독 구간 **direct+legacy 혼합 스트림 57건 schema 오류 0**(공통 `_publish_fx_snapshot()` builder라 schema 동일 근거 강하나 **direct↔legacy live payload 동치는 미검증**). **남은 미확인**: iOS FX client(앱 fx:\* 미구독) / direct↔legacy live payload 동치 / usdt:krw cross-route causal 귀속([line 710](USDT_TOPIC_MIGRATION_PLAN.md#L710) 조건).
    - **admin `trigger_*` surfacing (item 1, `69357ed` — repo land+CI green, prod deploy 2026-06-15) + direct↔legacy payload equality regression test (item 2, `3dfd0fa`)** (2026-06-15): `/admin/api/topic-status/fx`에 Redis-backed trigger counter 10 + `trigger_last_*` 6 노출(no_loop=process-local 제외; SSH HGETALL 대신 admin endpoint 사용 가능) / item 2는 공통 `_publish_fx_snapshot` core의 direct↔legacy wrapper 동치 회귀 lock. CLIENT_GUIDE 응답 계약 + CHANGELOG 정합. ※ SET-fail 빈도는 trigger_*로 측정 불가(SET-success만 카운트) — 별도 SET-outcome telemetry가 PR D precondition.
    - **item 4 — Bank/Investing SET-outcome telemetry (repo land 2026-06-15, `7ab51ec`, prod deploy 2026-06-15)**: 신규 `app/bank_investing_redis_stats.py`(process-local — attempt/success/failure×3원인[client_unavailable/writer_exception/set_exception]+unknown / derived aggregate / consecutive / last_*) + `set_latest_bank/investing_rate_from_sync_job` 3-zone 배선(`_safe_record_bank_investing_set_stat` hot-path 격리 + `_get_sync_client` 보호로 bool 계약 불변) + admin `GET /admin/api/bank-investing-redis-stats`(reset 없음). **trigger_*가 못 재는 SET-fail 빈도·원인을 직접 계측** = PR D precondition의 **계측 축**. ⚠️ **계측만** — PR D 진짜 gate(SET 실패 시 결정적 복구 발행 경로, §6.6.2 ②)는 failure-injection characterization test → recovery-trigger/retry 후속(미완). tests +25(모듈 12 + 배선 11 + endpoint 2).
    - **이후 (6/16+ 관찰/별도 GO)**: REST flag 첫 act 관찰(6/16 06:00 CM~ WS-miss 시 gate-checked write, 6/15 활성 완료) / 6/20 06:00 calendar 경계 / PR D/E / SET-failure 복구 결정(별도 SET-outcome telemetry 선행).
- **PR D (FX legacy hook 격하)**: C1 fx canary(Stage 2) + **SET-failure 복구 publish 경로 결정**(precondition, §6.6.2 ②) 후 main.py broadcast-diff **fx hook([main.py:741](app/main.py#L741))만** 격하/제거(B.2 §14.7) → **fx:* source-trigger-only**. usdt:krw는 불변(tether hook([main.py:730](app/main.py#L730)) 유지 → 계속 이중 발행).
- **PR E (tether legacy hook 격하, B.2 §14.7 흡수)**: usdt:krw 전 source(USDT 5 + KRX + bank/investing cross-route) direct trigger 안정 + SET-failure 복구 경로 후 tether hook([main.py:730](app/main.py#L730)) 제거 → usdt:krw source-trigger-only. **cross-route(C1 Stage 2 canary)가 enabler.** legacy WebSocket dual-emit은 PR D/E 무관 유지.

**Open (스코핑 결정 — 2026-06-13 확정, 상세 §6.6.2)**: **C1 source-routed trigger-only 채택** — α/β/γ write-빈도 축은 C1에서 미발생(Redis schema 미변경, trigger=rate 변경 = PR B `changes`). β의 freshness-write 절반은 C2로 분리, 최종 확정은 C2 dual_shadow telemetry / **진입조건** = 별도 우선순위 진입(§12.8 선결 아님) / **dedup cold-start** = C1은 dedup DB-based 유지라 위험 0, C3 진입 시 Redis miss→DB SELECT fallback(USDT read path) 정책.

**정정 이력 (미래 독자 — 부분-read 단정 반복 방지)**: 본 catalog는 3연속 정정 후 확정 — (1) "characterization 공백" over-claim(실제 `TestInsertBankRatesCallOrder`/`...Investing...`가 순서/commit-fail/unchanged/redis-exception→alert/대칭 이미 잠금) → (2) "(v) call-site 무가드" → (3) **outer try/except(798) 발견**(세 리뷰어 모두 call-site만 보고 함수 경계 미확인). **교훈: 함수 계약 단정 전 try/except/finally 경계까지 읽을 것.**

#### 6.6.2 PR C 설계 확정 — source-routed trigger-only (C1) (2026-06-13, 2-세션 Claude + Codex 5라운드 수렴 — SET-only gating·PR D/E 분리 정정 포함)

§6.6.1 PR C 원안을 **3개의 분리 가능한 behavior change**로 분해하고 **PR C = C1(trigger-only)**로 확정 (characterization-first / behavior-change-0, PR A/B land 방식 일관). C2/C3는 별도 PR + 자체 telemetry.

**설계를 규정하는 핵심 발견 (file:line 검증 완료)**:

1. **fx:*에 trigger 계층 부재** — `usdt:krw`만 [tether_topic_trigger.py:199](app/tether_topic_trigger.py#L199)(mode FF + coalesce + telemetry) 보유. fx는 [fx_topic_publisher.py:125](app/fx_topic_publisher.py#L125)(publish "how")만 있고 "when" 계층 0 → PR C가 `fx_topic_trigger.py` 신설(tether 복제 + 3-topic).
2. **mirror cycle = bank/investing key 공동 writer** — [_mirror_all_latest:1429](app/latest_rates_cache.py#L1429)/[:1417](app/latest_rates_cache.py#L1417)가 `latest:bank:*`/`latest:investing:*`를 **3초마다 `serialize_value` 3-field로 재기록**. USDT/KRX는 [should_include_source_in_latest:1347](app/latest_rates_cache.py#L1347) allowlist 제외라 direct writer 단독 소유(5-field 안전) — 정반대. → C2(freshness 5-field) 이식 시 mirror가 3초마다 덮어씀 + dual-writer state drift = **mirror ownership 재설계 선행 필요**(C1과 직교).
3. **crud insert = no-loop worker thread** — [make_request_crawler_wrapper:247](app/scheduler.py#L247)가 sync `wrapper()` → APScheduler thread executor → [insert_bank_rates_into_db:239](app/crud.py#L239) 전체가 no-loop thread. KRX는 async handler로 복귀해 trigger 발사([krx_kis.py:1870](app/crawlers/krx_kis.py#L1870))하나 bank/investing엔 복귀점 없음 → inline `request_trigger`는 [tether_topic_trigger.py:276](app/tether_topic_trigger.py#L276) no_loop 분기로 항상 skip → **loop bridge 필수**.
4. **fx publish는 Redis-first read → SET 실패 시 stale 위험** — [fx_topic_payload.py:208-227](app/fx_topic_payload.py#L208-L227) builder가 Redis hit + `is_stale` false면 이전 값 사용. 직접 SET([crud.py:103](app/crud.py#L103) writer bool **폐기**, [latest_rates_cache.py:1046](app/latest_rates_cache.py#L1046) False 가능) 실패 후 즉시 trigger하면 mirror(3s) 복구 전 **stale snapshot 발행**. legacy hook은 broadcast diff가 같은 Redis read 기반이라 이 문제 없음(**C1이 새로 도입하는 failure mode**) → **SET-only trigger gating 필수**(USDT/KRX [krx_kis.py:1870](app/crawlers/krx_kis.py#L1870) 패턴: outcome=SET일 때만 trigger).

**라우팅 (소스 완결 — 코드 read로 검증)**:

| 변경 소스 | → topic |
| --- | --- |
| 모든 bank(9)/investing 변경 (3통화) | `fx:<asset>` |
| **kb·hana·investing의 usd-krw 변경만** | `usdt:krw` cross-route 추가 |

- usdt:krw payload가 kb/hana usd-krw + investing usd-krw를 읽음: [usdt_topic_payload.py:74](app/usdt_topic_payload.py#L74)(`TETHER_TAB_BANK_SOURCES=("kb","hana")`) + [:307-325](app/usdt_topic_payload.py#L307-L325). → cross-route 없으면 **tether hook(730) 제거(PR E)** 후 USDT 탭 은행/기준 row가 다음 USDT tick까지 stale(야간 체감) = **cross-route는 freshness 개선 아닌 PR E behavior-change-0 필수 경로** (C1에서 구축·Stage 2 canary, 소비는 PR E). PR D(fx hook만)는 cross-route 불요 — tether hook이 usdt:krw 커버 유지.
- fx payload = `banks`(9) + `reference`(investing)뿐, **DXY 없음**([fx_topic_payload.py:164](app/fx_topic_payload.py#L164)) → 제3 라우팅 소스 없음(DXY-only 변경은 fx 내용 불변 → PR D 후에도 갭 0).

**behavior-change-0 게이팅**:

- 신규 emission 전체를 `BANK_INVESTING_TOPIC_TRIGGER_MODE`(default `legacy_piggyback`) + `_COALESCE_MS`로 게이트. 이름은 fx + usdt:krw cross-route 둘 다 제어하므로 **source(bank/investing) 기준** — `FX_*` 아님.
- **production tether = `direct_coalesced` (live, SSH 실증)**. cross-route를 무조건 wiring하면 land 즉시 usdt:krw가 bank 변경마다 재발행 = behavior-change-0 파괴 → 게이트 필수.
- 3-mode:
  - `legacy_piggyback` (land default): 신규 route 전체 noop. legacy hook이 **bank/investing→topic 유일 경로**(fx:* + usdt:krw의 bank/investing 반영분; usdt:krw 자체는 USDT/KRX direct trigger도 발행 중). **land = behavior-change-0.**
  - `dual_shadow`: fx coalesce 측정(publish X) + 조건부 cross-route는 **`tether_route_shadow` counter만 기록** (실제 `request_tether_topic_trigger` 호출 X — live tether가 발행하므로). counter는 **request-count proxy** — 실제 publish 증가분은 tether가 USDT/KRX와 live coalesce라 더 작음(활성 시간대엔 기존 flush에 흡수, 야간에만 marginal).
  - `direct_coalesced`: fx controller flush(publish, **SET 성공 change만**) + 조건부 cross-route `request_tether_topic_trigger`(live tether 발행). fx·usdt:krw 모두 legacy hook과 **이중 발행**(idempotent — 단 *다른* source SET 실패 시 집계 부분-stale 가능, ≤3s mirror+legacy hook 교정; fx는 PR D / usdt:krw는 PR E에서 해소).

**구조 계약**:

- **`app/topic_trigger_bridge.py` 신규** (crud→main import 금지 — [[project_main_py_helper_placement]] 정합): lifespan이 scheduler 시작 **전** `register_main_loop(loop)`, shutdown 진입 즉시 신규 enqueue 차단. crud worker thread는 이 bridge만 import(함수 내부 import). bridge가 **단일 `call_soon_threadsafe` callback**으로 main loop에서 `fx.request_trigger` + (조건부) `tether.request_trigger`/`tether_route_shadow` 실행 + callback 예외 격리.
- **`FxTopicTriggerController`** ([tether_topic_trigger.py](app/tether_topic_trigger.py) 복제 + multi-topic): `_pending`을 3 fx topic별 key, flush는 **per-topic** `safe_publish_fx_snapshot(asset)`.
- **`safe_publish_fx_snapshot(asset)` 신규** (direct flush 전용): asset validation + 자체 `get_db_context()` + per-asset try/except + 기존 hook_entered/error publisher telemetry 보존. **코어 `_publish_fx_snapshot(db,asset)` 공유, 기존 `safe_publish_all_fx_snapshots(db)`는 불변** (legacy hook db session 1개 유지 = legacy 경로 behavior-change-0).
- crud orchestrator([crud.py:253](app/crud.py#L253)/[:363](app/crud.py#L363)): `changes>0` 직렬 블록(commit→Redis→alert) **끝에** bridge emission 1줄, try/except 격리. signature/순서/count==0 gate 불변.
- **SET-only trigger gating**: `_write_changed_*_to_redis`를 `None`→**`list[dict]` (SET 성공한 update만 반환, 입력 순서·실패 격리·기존 SET 동작 유지)**. bridge엔 **성공분만** 전달 → emission은 SET 성공 change만 trigger, SET 실패는 trigger X + failure telemetry. Redis SET 자체 동작·best-effort·mirror 안전망 불변 — outcome **소비**만 추가. (USDT/KRX `*LatestWriteOutcome` SET-only 패턴 mirror.) **gating은 topic trigger 한정 — alert(`process_rate_alerts`, DB-authoritative)는 전 change 발화·불변** (Redis 장애 시 알림 누락 방지).
- config: `BANK_INVESTING_TOPIC_TRIGGER_MODE`/`_COALESCE_MS` + reason 상수(fx: `FX_TRIGGER_REASON_BANK_CHANGE`/`..._INVESTING_CHANGE`, cross-route: 기존 `TETHER_TRIGGER_REASON_*` 계열 신규).
- **shutdown 순서**: bridge enqueue 차단 → fx controller flush drain → 기존 tether trigger drain → scheduler 종료.

**① 변경 / 불변 경계 (C1 trigger-only)**:

- 변경: 신규 `fx_topic_trigger.py` / `topic_trigger_bridge.py` / `safe_publish_fx_snapshot` / crud emission 1줄(bridge 경유) / **`_write_changed_*_to_redis` outcome 전파 + SET-only trigger gating** / config 2 + reason 상수 / `tether_route_shadow` telemetry.
- 불변: insert_bank/investing signature+`int` 반환(27 call site) / 직렬 순서 commit→Redis→alert + count==0 gate(PR A/B 잠금) / `ChangedRate` DTO + sink shape / **Redis SET 동작·best-effort·mirror 안전망**(outcome은 소비만 추가) / `latest:bank:*`/`latest:investing:*` 3-field schema + mirror cycle(→C2) / insert-if-changed DB SELECT(→C3) / broadcast hot path + is_stale + 알림 경로 / `safe_publish_all_fx_snapshots(db)` + legacy fx hook([:741](app/main.py#L741), PR D까지)·tether hook([:730](app/main.py#L730), PR E까지) 공존 / `_publish_fx_snapshot` 내부.

**② Open 결정**:

- **α/β/γ**: C1에선 freshness-write 빈도 축 미발생(Redis schema 미변경). trigger 정책 = "rate 변경 시 발화"(= PR B `changes>0`, β의 trigger 절반). α(매 fetch publish → 동일 payload 폭발) / γ(freshness 손실) 기각. β의 seen_at-매-fetch write 절반은 C2, **최종 확정은 C2 dual_shadow telemetry**.
- **dedup cold-start**: C1은 dedup DB-based 유지 → 위험 0. C3 진입 시 정책 박제 — Redis miss/재시작 → DB SELECT fallback(USDT read path), Redis=fast-path / DB=correctness backstop, 첫 fetch가 seed + regression guard.
- **SET-failure 복구 + 집계 부분-stale 경로 (PR D/E precondition, 동일 축)**: ⚠️ **preliminary — PR D/E 스코핑 시 telemetry 기반 최종화** (아래 선택지는 directional, locked decision 아님). SET-only는 *trigger source 자신*의 stale-publish만 막음. 두 잔여 — **(a)** SET 실패 변경은 **trigger 자체가 안 남** → 다음 event까지 미발행(bank/investing 변경 드물어 갭 김 — USDT는 tick 빈번해 self-heal); **(b)** `fx:<asset>`은 9은행+investing **집계**라 *다른* source SET 실패 시 성공 source의 trigger가 부분-stale snapshot 발행. **둘 다 legacy 공유·C1 신규 아님**(legacy hook도 Redis-first read, ≤3s mirror 교정) + PR C 중엔 legacy hook 커버. hook 제거(PR D/E) 전 결정 (**trigger 발생 여부 ⊥ publisher 권위 = 별 concern**):
  - **(i)** mirror가 changed value 감지 시 trigger 발생(publish는 mirror-fresh Redis) → (a)+(b) eventually-consistent(≤3s).
  - **(ii)** SET-only 완화 — **SET 실패도 committed-change trigger 발생 + publisher DB-authoritative** → (a)+(b) 즉시 해소. ⚠️ **DB-authoritative 단독(trigger 복원 없이)은 (b) + trigger가 나는 (a)만 해소 — trigger 자체 없는 순수 (a)는 미해소** (publisher가 호출 안 됨). 순수 (a)엔 trigger 복원 필수.
  - **C1 범위는 SET-only gating까지** (복구 경로는 hook 제거 PR의 precondition).
  - **복구 경로 결정 (2026-06-16) — [PR_D_RECOVERY_SPEC.md §12](PR_D_RECOVERY_SPEC.md)**: 위 (i)/(ii) preliminary는 **superseded**. 채택 = **D (success-watermark reconciliation)**. 기준 R1(정상 시 ≤15s server attempt) / R2(best-effort — 예방적 중복 금지, 단 send↔watermark 비원자·watermark 유실 bootstrap의 중복은 허용·telemetry, 전역 bounded 아님) / R3(변경+실패·미완료 revision retry). **A**(무상태)·**B**(주기 재발행)·**C**(attempt-watermark, send 실패 미retry) 탈락. 정책: `sent_count>0` 시만 watermark / `sent_but_uncommitted`(재전송 없이 watermark 재시도) / watermark monotonic(직렬화 or CAS) / restart=Redis watermark 영속+유실 시 1회 bootstrap. 순서: 공통 base(source-key revision + monotonic 5-state write) → D layer → fx hook 제거.

**③ Canary**:

| Stage | 조치 | 관찰 |
| --- | --- | --- |
| 0 land | `BANK_INVESTING_TOPIC_TRIGGER_MODE=legacy_piggyback` default | behavior-change-0, CI green. legacy hook이 bank/investing→topic 유일 경로(usdt:krw는 USDT/KRX direct도 발행). |
| 1 dual_shadow | production env flip + force-recreate | fx 발화/coalesce 빈도 + `tether_route_shadow` counter를 legacy hook 빈도와 대조. publish X → 영향 0. 영업일 1–3일. |
| 2 direct_coalesced | env flip | fx + usdt:krw 이중 발행(부분-stale 가능 — SET 실패 source, mirror+legacy hook 교정). **direct trigger payload == legacy hook payload 동치 검증**(count/success만으론 정합 불충분) + `tether_route_shadow`→실측 전환 + iOS canary. **KRX CF close 창(15:35–15:50) 회피.** |
| 3 = PR D | legacy **fx hook(741)만** 제거 + SET-failure/부분-stale 복구 경로 | **fx:* source-trigger-only**, publish 누락 0 + fx 집계 payload 정합 확인. usdt:krw는 tether hook(730) 유지 → PR E까지 이중발행. |
| 4 = PR E (별도) | tether hook(730) 제거 | usdt:krw source-trigger-only(USDT 5 + KRX + cross-route). **kb/hana/investing 변경이 usdt:krw payload에 실제 반영 확인**(enabler=cross-route). |

Rollback: 각 stage env 1줄 + force-recreate. anchor = `legacy_piggyback`.

---

## 7. 참조

- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md): 서비스 출시 계약 source of truth
- [DECISIONS.md ADR-028](DECISIONS.md#adr-028-topic-only-tetherkrx--legacy-fx-dual-emit): topic-only Tether/KRX + legacy FX dual-emit 결정
- [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md): source/asset 도메인 모델 (Phase 1 백엔드)
- [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md): RateSource / 어댑터 / SourceRegistry 모델 (Phase 1 클라이언트)
- [KRX_CANARY.md](KRX_CANARY.md): KRX Stage 1/2 운영 + Stage A/B baseline (운영 배포 제약 자료)

---

**다음 단계**: Z-2b (backend topic dispatcher) 설계는 KRX 만기 통과 후 (5/19+) 별도 PR로 진행. Z-2a는 본 문서 commit으로 완료.
