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
- ⏸ Stage 3 Level 2 (wire-up) 보류: 5/19+ (5/18 만기 통과 후) 권장 — Level 1
  wrapper를 hot path (collect_usdt_rates `changed_rates` 후처리 / broadcast
  cycle / mirror cycle 중 baseline 분석 후 결정) + KRX 포함 wrapper 또는
  매개변수 + `TOPIC_DISPATCHER_ENABLED=true` 활성화.

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

### Z-2d (legacy rates에서 topic-only source 제외 — Codex 2회차 정정)

§3.1에서 발견한 정책 부재 차단. **USDT만 제거 X — topic-only source 전체 inclusion policy 도입**.

- `should_include_source_in_legacy_rates(source, asset) -> bool` policy 함수 추가
  - 별도 allowlist 또는 `SourceRegistry.legacy_rates_enabled` 같은 명시적 flag 기반
  - `SourceRegistry.category` 단독 판단 금지 (category는 도메인 분류이지 노출 계약 아님)
  - 출시 계약: legacy rates = FX/은행/Investing만. topic-only (USDT/KRX 등) 제외
- `get_source_rates_as_legacy_format()`에 policy 적용 (crud.py:1751)
  - 호출 3곳 (365/415/454) 자동으로 USDT/KRX 제외됨
- `/api/rates/{currency}` USDT/KRX 분기 deprecation 또는 제거
  - `usdt-krw`: legacy 응답에서 빈 배열 또는 410 Gone
  - `usd-krw-futures`: 동일 (현재는 Stage 1 broadcast 미포함이지만 DB legacy path 잠재 노출 차단)
- legacy `/api/rates` / WebSocket `rates` 경로 자체는 보존
  - 기존 FX/은행/Investing legacy 응답은 `investing_exchange_rates` / `bank_exchange_rates` 테이블 경로 유지 (`get_source_rates_as_legacy_format` 의존 X)
  - `get_source_rates_as_legacy_format()`는 `source_rates` 전용 어댑터 — topic-only source 제외 후 사실상 빈 결과 반환만 하게 되거나, 호출 지점에서 제거
- 클라이언트 영향 점검:
  - iOS/Android 운영 앱: 테더 탭 없으므로 영향 0
  - iOS dev/test: legacy USDT 의존 시 마이그레이션 필요 (USDT_PHASE1_CLIENT_GUIDE.md 갱신)

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
