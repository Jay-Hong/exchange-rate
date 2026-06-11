# 환율 정보 서비스 (Exchange Rate Comparison Service)

## 프로젝트 개요

실시간 은행 간 환율 차이 비교 서비스 (인베스팅 기준 환율 포함)

### 📚 주요 문서 가이드

**핵심 가이드:**
> 💡 **아키텍처 의사결정:** [DECISIONS.md](DECISIONS.md) - 주요 기술 선택과 그 근거 (ADR). **최신: ADR-031 (KRX Redis 1차 통합), ADR-032 (KRX 가격알림 evaluator — F-1/F-2/F-3 trilogy), ADR-033 (Graph API v2 catalog policy + Amendment 2026-05-27 + Amendment 후속 — source_daily_rates + close_basis/source_method/ohlc_quality / §7 Hana 정책 재작성 — observed_eod + official_historical_backfill 2-source 분리 / Investing 거부), ADR-034 (source_daily_rates canonical daily table — Phase 2d 구현 설계, schema + retention + rate==close invariant + calendar-aware monitoring + rollout sequence)**
> 📊 **Graph API v2 design:** [GRAPH_API_V2_CONTRACT.md](GRAPH_API_V2_CONTRACT.md) - catalog matrix (tab × period × series), provenance schema (close_basis / source_method / ohlc_quality), **Hana daily canonical policy (observed_eod + official_historical_backfill 2-source 분리) + KRX contract chain rollover boundary policy** (Amendment 2026-05-27 + Amendment 후속), external_historical + insufficient_history fallback, endpoint contract. **구현 상세는 [ADR-034](DECISIONS.md#adr-034-source_daily_rates-canonical-daily-table)** (source_daily_rates canonical daily table).
> 🕷️ **크롤러 구현:** [CRAWLERS.md](CRAWLERS.md) - 각 은행별 크롤링 방식과 특수 로직
> 📝 **변경 이력:** [CHANGELOG.md](CHANGELOG.md) - 버전별 변경사항 및 마이그레이션 가이드
> 🔔 **알림 & 구독:** [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md) - 푸시 알림, 인증, 구독 관리 가이드
> 💳 **RevenueCat 서버 사이드:** [REVENUECAT_SERVER_SIDE.md](REVENUECAT_SERVER_SIDE.md) - 구독 검증, Webhook, 캐시 정책

**배포 및 운영:**
> 🐳 **Docker 아키텍처:** [DOCKER.md](DOCKER.md) - Docker Compose 구조 및 확장 전략
> 🚀 **AWS 배포:** [DEPLOYMENT.md](DEPLOYMENT.md) - EC2 인스턴스 설정 및 실전 배포 절차
> 🔄 **재부팅 절차:** [REBOOT_CHECKLIST.md](REBOOT_CHECKLIST.md) - 재부팅 후 검증 체크리스트

**유지보수 기록:**
> 🔧 **2026-04-27:** [MAINTENANCE_2026-04-27.md](MAINTENANCE_2026-04-27.md) - MIBANK URL/DOM 변경 대응 (bank_cd URL, 기준환율 헤더 기반 파싱)
> 🔧 **2026-01-29:** [MAINTENANCE_2026-01-29.md](MAINTENANCE_2026-01-29.md) - Investing Cloudflare 403 차단 대응 (curl_cffi TLS 지문 위장)
> 🔧 **2025-11-06:** [MAINTENANCE_2025-11-06.md](MAINTENANCE_2025-11-06.md) - AsyncIO Queue 도입 (Semaphore 경합 제거)
> 🔧 **2025-11-05:** [MAINTENANCE_2025-11-05.md](MAINTENANCE_2025-11-05.md) - 성능 개선 및 모니터링 시스템 구축

**계획/설계 문서:**
> ⚡ **실시간 아키텍처:** [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md) - **서비스 계약 source of truth**. 1초 broadcast / 거래소 WebSocket / 구독 기반 라우팅 / dual-emit 전략. topic-only Tether/KRX + legacy FX dual-emit 계약 명시.
> 🔌 **USDT 거래소 WebSocket:** [USDT_EXCHANGE_WEBSOCKET_GUIDE.md](USDT_EXCHANGE_WEBSOCKET_GUIDE.md) - 업비트/빗썸/코인원/코빗/고팍스 ticker 구독 및 정규화 가이드 (collector 측, emit 모델과 독립)

**USDT / KRX 도메인 문서** (Phase Z-1 status 정리, 2026-05-06):
> 📋 **KRX Stage 1 운영:** [KRX_CANARY.md](KRX_CANARY.md) - **현재 운영 중**. Stage 0/1/2 runbook, SQL/Redis 검증 명령, 24h baseline 지표, 5/18 만기 관찰 시나리오, rollback 절차. **F-3 KRX 가격알림 운영 활성 (2026-05-26, 섹션 추가)**: env 활성 절차 + iOS canary (Google Sign-In 우회 custom token 패턴) + 토글 매트릭스 + Rollback.
> 📜 **USDT 탭 초기 제안서:** [USDT_TAB_PROPOSAL.md](USDT_TAB_PROPOSAL.md) - **Historical proposal (rollout superseded)**. 제품 방향성/Decision A~F는 참고 유효, "기존 WebSocket에 usdt-krw 포함" rollout은 폐기
> 🧱 **USDT Phase 1 백엔드 설계:** [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md) - **source/asset 도메인 모델 유효 (source_rates / SourceRegistry / 알림 정책)**. legacy `/api/rates` + WebSocket `rates` 통합 rollout은 superseded — 데이터 수신 계약은 [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md) + [ADR-028](DECISIONS.md) 따름
> 📱 **USDT iOS/Android 가이드:** [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md) - **RateSource / 어댑터 / SourceRegistry 모델 참고용**. 데이터 수신 방식(`/api/rates`, `/ws` `rates` 배열)은 V2 protocol(`REALTIME_V2_CLIENT_GUIDE.md`, Phase Z-2 신설 예정)로 대체 예정

**글로벌 가이드:**
> 📚 **공통 개발 규칙:** [~/.claude/CLAUDE.md](file:///Users/jay/.claude/CLAUDE.md) - MCP 설정, 코딩 스타일, Git Convention

### 코드/문서 변경 검증 게이트 (2026-05-06)

최근 KRX/USDT 문서 정합 및 PR6d 작업에서 회상 기반 단정, commit message와 diff 불일치, env/config 정의 후 사용처 연결 누락이 반복됐다. 신규 사례를 메모리에 계속 추가하는 대신, 이 리포에서는 큰 코드/문서 변경 후 아래 절차를 표준 게이트로 적용한다.

1. **Diff와 self-claim 1:1 대조**: commit/push 전 `git diff --stat`와 `git diff`를 직접 보고, commit body 또는 최종 보고에 적는 변경 항목이 실제 diff에 모두 들어 있는지 확인한다.
2. **Env/config 사용처 확인**: env/config 변수를 추가하거나 의미를 바꾸면 `rg <ENV_NAME>`로 정의와 사용처를 함께 확인한다. 의도적으로 아직 사용하지 않는 값은 commit body에 "현재 사용처 없음, PR<X>에서 사용 예정"이라고 명시한다.
3. **Caveat 후속 적용**: 문서/코드에 "검증 필요", "unresolved", "Stage 2 전 확인" 같은 caveat을 추가했다면, 다음 단계 진입 전에 처리하거나 명시적으로 보류 결정한다.
4. **외부 검토 표준화 (의미적 변경 default, 2026-05-07 강화)**:
   의미 있는 코드/문서 변경은 commit/push 전에 외부 검토를 기본으로 한다.
   - 검토용 요약: 변경 의도 + 핵심 diff 발췌 + 검토 포인트
   - 흐름: 요약 작성 → 외부 에이전트(Codex/Claude 등) 검토 → 반영 → commit
   - push는 commit과 분리해 별도 GO로 결정
   - 예외: typo, lint, formatting, 변수명/임포트 정렬 같은 비의미 cosmetic 변경
   - 경계가 모호하면 검토 받는 쪽이 default (false negative > false positive)
5. **자동화는 중기 과제**: pre-commit/commit-msg hook은 이 절차가 안정된 뒤 별도 작업으로 도입한다. 지금은 의식적 검증 게이트로 먼저 적용한다.

## MCP (Model Context Protocol) 설정

> 💡 **MCP 전체 가이드**: `~/.claude/CLAUDE.md` 참고 (설치/삭제 명령어, Context 최적화 전략)
> ⚠️ **Context Window 최적화**: 필요한 MCP만 설치, 작업 완료 후 삭제 권장 (세션 재시작 필수)

### 이 프로젝트에서 사용 가능한 MCP

**기본 유지 권장**:
- **context7**: 라이브러리 문서 조회 (2개 도구, 경량)
- **filesystem**: 프로젝트 파일 탐색 (13개 도구)

**필요시 설치**:
- **sqlite**: DB 쿼리 실행 (`data/exchange_rates.db`)
- **github**: PR/Issue 작업 (62개 도구, 토큰 많음 - 사용 후 삭제 권장)

## 목표 사용자

- 초기: 200-500명
- 주 사용층: iOS/Android 네이티브 앱 사용자
- 웹: 관리자/디버깅 전용

## 핵심 아키텍처

```text
[개발/테스트]
크롤러(11개) → Scheduler → SQLite → WebSocket(10초) → 클라이언트

[서비스 환경]
크롤러(11개) → Scheduler → RDS PostgreSQL → WebSocket(1초) → 클라이언트
                              ↓                ↑
                    Redis latest mirror (3초)  ── broadcast Redis-first read (PR3-PR5, ADR-026)
                                    ↓
                          Firebase Auth + FCM → 푸시 알림
```

> **broadcast 데이터 흐름** (PR5 완료, 2026-05-04):
> 크롤러가 DB에 저장 → mirror cycle 3초마다 DB latest를 Redis로 복사 (`latest:bank:*`, `latest:source:*`, `latest:investing:*`, `latest:dxy:current`) → broadcast 매초 Redis 직접 read → Redis 실패 시 DB fallback. broadcast hot path는 DB-free (rates + DXY 모두). 자세한 내용은 [ADR-026](DECISIONS.md#adr-026-redis-first-broadcast-hot-path--latest-mirror--dxy-mirror로-db-free-달성).

### 데이터 소스

- Investing.com (기준 환율)
- DXY 현물/운영 피드 (`instrument='dxy'`, USD/KRW 그래프 보조지표) — `dxy_spot.py` 독립 크롤러 (`/indices/usdollar` `__NEXT_DATA__` → 같은 페이지 CSS → CNBC `.DXY` → Yahoo Finance)
- DXY 선물 (`instrument='dxy_futures'`) — `investing.py`에서 환율과 동시 추출 (`#sb_last_8827`, exchange-rates-table) → `/currencies/us-dollar-index` 폴백 (Yahoo 미사용)
- 9개 은행: KB, 하나, 신한, 우리, IBK, NH, SC제일, 부산, 씨티
- **가상자산 거래소 5종** (USDT/KRW, Phase 1): 업비트, 빗썸, 코인원, 고팍스, 코빗

### 지원 통화

- USD-KRW, JPY-KRW, EUR-KRW
- **USDT-KRW** (Phase 1, 가상자산 거래소 기반)

## 기술 스택

### 백엔드

- **언어**: Python 3.x
- **웹 프레임워크**: FastAPI
- **DB**: SQLite (개발/테스트), AWS RDS PostgreSQL (운영)
- **ORM**: SQLAlchemy
- **스케줄러**: APScheduler (AsyncIOScheduler)
- **크롤링**: requests + BeautifulSoup4, Selenium, curl_cffi (Investing 전용, TLS 지문 위장), yfinance (DXY 현물 Yahoo 최후 폴백)
- **SSL/TLS**: Let's Encrypt (Certbot 자동 갱신, 90일 주기)
- **리버스 프록시**: Nginx (HTTPS, HTTP/2, wss://)

### 프론트엔드

- **iOS**: Swift (네이티브)
- **Android**: Kotlin + Jetpack Compose
- **웹**: Vanilla JS + WebSocket (관리자용)

### 실시간 통신

- **프로토콜**: WebSocket Secure (wss://)
- **도메인**: https://fxi.kr
- **브로드캐스트**: 매분 00, 10, 20, 30, 40, 50초 (정확한 시간)
- **하트비트**: Ping/Pong (30초)
- **암호화**: TLS 1.2 & 1.3

## 데이터베이스 스키마

### investing_exchange_rates

```sql
id         INTEGER PRIMARY KEY
currency   TEXT (INDEX)
rate       REAL
timestamp  DATETIME (UTC)
```

### bank_exchange_rates

```sql
id         INTEGER PRIMARY KEY
bank       TEXT (INDEX)
currency   TEXT (INDEX)
rate       REAL
timestamp  DATETIME (UTC)

-- 복합 인덱스: (bank, currency, timestamp)
```

### crawler_config (Phase 1.8)

```sql
id            INTEGER PRIMARY KEY
crawler_name  TEXT (UNIQUE, INDEX)
enabled       BOOLEAN (DEFAULT: TRUE)
updated_at    DATETIME (UTC)

-- 크롤러 활성화/비활성화 설정 (관리자 페이지에서 제어)
```

### market_index_rates (Phase 1A)

```sql
id            INTEGER PRIMARY KEY
instrument    TEXT NOT NULL          -- 'dxy' (향후 다른 지수 확장 가능)
source        TEXT NOT NULL          -- 'investing' | 'yahoo'
rate          REAL NOT NULL
timestamp     DATETIME NOT NULL (UTC)
granularity   TEXT NOT NULL          -- 'realtime' | 'hourly' | 'daily'

-- 인덱스: (instrument, timestamp)
-- 인덱스: (instrument, granularity, timestamp)
-- UNIQUE: (instrument, source, timestamp, granularity)
```

**granularity 구분:**
- `realtime`: 실시간 크롤링 데이터 (10초~1분 간격)
- `hourly`: 시간봉 백필 데이터 (yfinance, 최근 7일)
- `daily`: 일봉 백필 데이터 (yfinance, 최대 1년)

### 알림 관련 테이블 (Phase 2, 구현 완료)

> 상세 스키마: [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md#필요한-db-테이블-rds-postgresql)

- `user_devices` - FCM Device Token 저장 (user_id, device_token, platform)
- `notification_settings` - 알림 조건 설정 (bank, currency, condition, threshold, enabled, triggered)
- `notification_logs` - 알림 발송 히스토리 (중복 방지)

### USDT Phase 1 — Source 기반 테이블 (2026-04-23)

> 💡 **설계 원칙**: 기존 bank/investing 세계와 분리. DB는 `source + asset`, 레거시 API 응답 시에만 `bank + currency`로 변환 (Decision E)
> 📖 **상세 설계**: [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md)

#### source_rates (Phase 1)

```sql
id          INTEGER PRIMARY KEY
source      TEXT NOT NULL         -- 'upbit' | 'bithumb' | 'coinone' | 'korbit' | 'gopax'
asset       TEXT NOT NULL         -- 'usdt-krw' (Phase 2: 'usd-krw-futures' 예정)
rate        REAL NOT NULL
timestamp   DATETIME NOT NULL (UTC)

-- 인덱스: (source, asset, timestamp)
```

#### source_notification_settings (Phase 1)

```sql
id                  INTEGER PRIMARY KEY
user_id             TEXT NOT NULL (INDEX)
source              TEXT NOT NULL
asset               TEXT NOT NULL
condition           TEXT NOT NULL         -- 'above' | 'below'
threshold           REAL NOT NULL
enabled             BOOLEAN DEFAULT TRUE
triggered           BOOLEAN DEFAULT FALSE
last_notified_at    DATETIME
last_notified_rate  REAL
created_at          DATETIME
updated_at          DATETIME

-- 인덱스: (source, asset)
```

#### source_notification_logs (Phase 1)

```sql
id              INTEGER PRIMARY KEY
user_id         TEXT NOT NULL (INDEX)
setting_id      INTEGER NULL          -- 설정 삭제 후에도 로그 유지
source          TEXT NOT NULL
asset           TEXT NOT NULL
condition       TEXT NOT NULL
threshold       REAL NOT NULL
triggered_rate  REAL NOT NULL
success         BOOLEAN NOT NULL
error_message   TEXT
sent_at         DATETIME
```

**source_registry**: 9개 소스 메타데이터는 `app/source_registry.py`의 `SourceDefinition` dataclass에서 관리 (canonical key는 DB 저장 안 함, 필요 시 `f"{source}:{asset}"`로 조합)

## 데이터 관리 정책

- **은행 데이터**: 30일분만 유지 (단기 비교용)
- **인베스팅 데이터**: 장기 보관 (그래프/분석용)
- **DXY 데이터**: realtime 원본은 30일분만 유지 (`dxy`, `dxy_futures` 모두 `market_index_rates`에 저장), hourly/daily rollup은 장기 그래프 보존
- **source_rates (USDT)**: 30일분만 유지 (은행 데이터와 동일 정책)
- **저장 조건**: 변경사항 있을 때만 INSERT (중복 방지)

## 스케줄링 시스템

> 💡 **최신 아키텍처:** 4단계 모드 + 환율 고시 스케줄 기반 최적화 (2025-11-16)
> 📖 **상세 기록:**
> - [ADR-011](DECISIONS.md#adr-011-4단계-모드-환율-고시-스케줄-기반-최적화) - 4단계 모드 도입 (IN, BREAK1, BREAK2, OUT)
> - [ADR-010](DECISIONS.md#adr-010-websocket-broadcasting-스케줄링-방식) - Broadcasting APScheduler cron job 전환
> - [ADR-009](DECISIONS.md#adr-009-3-tier-스케줄링-아키텍처-t3smallmedium-최적화) - 3-Tier 크롤러 스케줄링
> - [ADR-008](DECISIONS.md#adr-008-queue-압력-완화-전략-실시간성-vs-완전성) - Queue 압력 완화 + 실시간성 강화

> 🔖 **이전 개선:**
> - Priority Queue + Timeout (2025-11-08) - [ADR-007](DECISIONS.md#adr-007-selenium-크롤러-우선순위-기반-실행-priority-queue--timeout)
> - AsyncIO Queue 순차 실행 (2025-11-06) - [ADR-006](DECISIONS.md#adr-006-selenium-크롤러-동시-실행-제어---semaphore-vs-asyncio-queue)

### 4단계 모드 자동 전환

- **IN 모드**: 월~금 08:00~20:59 (영업시간, 전체 크롤러 활성)
  - Broadcasting: 매분 00, 10, 20, 30, 40, 50초 (정확한 시간)
  - 크롤러: cron 절대 시간 동기화 (Broadcasting 기준)
  - 10개 은행 전체 크롤링 (DXY는 investing 크롤러에서 동시 추출)

- **BREAK1 모드**: 월~금 21:00 ~ 익일 02:59 (심야, 9개 크롤러)
  - Broadcasting: 매분 00, 10, 20, 30, 40, 50초 (동일)
  - 제외: sc
  - 유지: investing, kb, hana, woori, shinhan, bs, citi, ibk, nh
  - ibk는 00:00~00:05 스킵 (자정 전환기), 00:05부터 Selenium만 사용

- **BREAK2 모드**: 월 06:00~07:59, 화~금 03:00~07:59, 토 03:00~06:59 (고시 마무리, 6개 크롤러)
  - Broadcasting: 매분 00, 10, 20, 30, 40, 50초 (동일)
  - 제외: woori (02:45 종료), ibk (02:05 종료), shinhan (02:30 종료), sc (20:30 종료)
  - 유지: investing, kb, hana, bs, citi, nh

- **OUT 모드**: 토 07:00 ~ 월 06:00 전 (주말, 6개 크롤러)
  - Broadcasting: 매분 00, 10, 20, 30, 40, 50초 (동일)
  - 제외: woori, ibk, sc, citi (주말 고시 없음)
  - 유지: investing, kb, hana, bs, shinhan, nh (주말 중 가끔 변동)
  - 크롤러: cron 시간 단위 (완전 분산, 동시 실행 0개)

### 은행별 환율 고시 스케줄

| 은행 | 고시 시작 | 고시 종료 | 특이사항 |
|------|----------|----------|---------|
| **investing** | 월 06:00 | 토 06:00 | 외환 시장 글로벌 운영 |
| **kb** | 평일 08:30 | 익일 05:00 | - |
| **hana** | 평일 08:30 | 익일 06:00 | 주말 중 가끔 변동 |
| **shinhan** | 평일 08:19 | 익일 02:30 | 주말 중 가끔 변동 |
| **woori** | 평일 08:30 | 익일 02:45 | - |
| **ibk** | 평일 08:30 | 익일 02:05 | 00:00~00:05 스킵 (자정 전환기), 00:05부터 Selenium만 사용 |
| **nh** | 평일 08:40 | 당일 24:00 | 자정 이후/주말 가끔 고시 |
| **sc** | 평일 09:00 | 당일 20:30 | - |
| **bs** | 평일 08:10 | 당일 24:00 | 일요일 넘어갈 때 가끔 고시 |
| **citi** | 평일 09:00 | 익일 06:00 | - |

### 스케줄링 아키텍처 (2025-11-16 최종)

**스케줄러:** APScheduler (AsyncIOScheduler)

#### 1. WebSocket Broadcasting

**실행 시점:** 매분 00, 10, 20, 30, 40, 50초 (정확한 시간, 모든 모드 동일)

```python
# scheduler.py
scheduler.add_job(
    broadcast_rates_once,  # async 함수 직접 등록
    CronTrigger(second='0,10,20,30,40,50', timezone=KST),
    id="websocket_broadcast"
)
```

**설계 원칙:**
- APScheduler cron job으로 정확한 시간 보장
- 크롤러 동기화의 기준 시간
- 변경사항 있을 때만 실제 전송 — trigger 조건: `rates` 또는 `indices.dxy` (DXY live tick) 변화 어느 쪽이든 발화 (`build_rates_payload()`의 JSON 전체 비교)

#### 2. 3-Tier 크롤러 아키텍처 (4단계 모드)

**Tier A:** 기준 환율 + DXY, 최우선 (independent jobs)
- **특징**: 가장 중요한 데이터, 빠른 응답
- **실행 방식**: Request 기반 (curl_cffi, TLS 지문 위장으로 Cloudflare 우회)

**Tier A1 — investing (`task_investing`)**: 은행/통화 기준 환율 + DXY 선물 동반 추출
- **DXY 선물 (`instrument='dxy_futures'`)**: 환율과 함께 `#sb_last_8827`(exchange-rates-table)을 추출. 셀렉터 실패 시 `dxy.py`의 `/currencies/us-dollar-index` 폴백 (60초 쿨다운, Yahoo 미사용)
- **IN/BREAK1/BREAK2**: 10초마다 (Broadcasting 3초 전)
  - `cron(second='7,17,27,37,47,57')`
- **OUT**: 10분마다
  - `cron(minute='7,17,27,37,47,57', second='45')`

**Tier A2 — dxy_spot (`task_dxy`)**: DXY 현물/운영 피드 독립 크롤러
- **DXY 현물 (`instrument='dxy'`)**: `/indices/usdollar` `__NEXT_DATA__` → 같은 페이지 CSS → 외부 chain (CNBC `.DXY` → Yahoo `DX-Y.NYB`). 외부 chain은 주간 세션 / market mode 보존 / fresh-age diff guard 적용
- **IN/BREAK1/BREAK2**: 10초마다 (Broadcasting 9초 전, investing과 6초 엇갈림)
  - `cron(second='1,11,21,31,41,51')`
- **OUT**: 매분 수집 유지 (investing보다 10× 빈도)
  - `cron(minute='*', second='15')`

**Tier B (kb, hana, woori, bs, citi):** 은행 환율, 중요
- **특징**: 중요도 높음, 빈도 높음
- **실행 방식**: Request 기반 (하이브리드 폴백: Request → Selenium)
- **IN/BREAK1/BREAK2**: 20-60초마다 (Broadcasting 3-7초 전, 엇갈림)
  - kb: `cron(second='15,35,55')`
  - hana: `cron(second='5,25,45')`
  - woori: `cron(minute='*', second='53')` (우선순위 높음, 늦게 크롤링)
  - bs: `cron(minute='*', second='33')`
  - citi: `cron(minute='*', second='13')`
- **BREAK1**: woori, bs, citi 유지 (kb, hana도 유지)
- **BREAK2**: bs, citi만 유지 (woori 02:45 종료)
- **OUT**: bs만 유지 (60분마다, 33분 45초)

**Tier C (shinhan, ibk, nh, sc):** Selenium, 순차 처리
- **특징**: 메모리 집약적, Request→Selenium 폴백으로 부하 감소
- **실행 방식**: AsyncIO PriorityQueue 순차 실행
- **부하 감소 전략**: Request(mibank) 먼저 시도 → 실패 시 Selenium 폴백
  - shinhan, nh, sc: 항상 Request 우선
  - ibk: IN 모드(08:30~20:59) Request 우선, 00:00~00:05 스킵 (자정 전환기), 00:05~02:59 Selenium만 사용
- **IN**: 매분 cron (shinhan: 18초, ibk: 34초, nh: 54초, sc: 58초)
- **BREAK1**: ibk(34초), nh(54초), shinhan(18초) 유지 (sc는 20:30에 크롤링 중단)
- **BREAK2**: nh(54초)만 유지 (ibk는 02:05 종료, shinhan은 02:30 종료, sc는 20:30 종료)
- **OUT**: nh(3분 45초), shinhan(13분 45초) 유지 (주말 중 가끔 변동)

**Selenium Queue 관리:**
- **Request 우선 전략**: Queue 압력 대폭 감소 (대부분 Request 성공)
- **우선순위 기반 실행**: 빠른 크롤러 우선 (hana → shinhan → nh → ibk → sc)
- **개별 타임아웃**: 크롤러별 45초 통일 (실시간성 우선)
- **자동 재시도**: 실패 시 우선순위 +1000으로 재실행
- **중복 작업 방지**: Worker 처리 중 + Queue 대기 중 체크
- **Non-blocking Queue**: put_nowait()으로 APScheduler 멈춤 방지
- **Queue 크기**: 25 (메모리 최적화)
- **압력 완화**: 80% (20/25) 초과 시 새 작업 거부
- **헬스체크**: 매분 03/23/43초 (20초 간격), 60초 stuck 시 자동 재시작

### IN 모드 타임라인 (1분 기준)

```
00초: Broadcasting
01초: dxy_spot (DXY 현물)
05초: hana
07초: investing (+ DXY 선물 동반 추출)
10초: Broadcasting
11초: dxy_spot (DXY 현물)
13초: citi
15초: kb
17초: investing (+ DXY 선물 동반 추출)
18초: shinhan (Selenium Queue)
20초: Broadcasting
21초: dxy_spot (DXY 현물)
25초: hana
27초: investing (+ DXY 선물 동반 추출)
30초: Broadcasting
31초: dxy_spot (DXY 현물)
33초: bs
34초: ibk (Selenium Queue)
35초: kb
37초: investing (+ DXY 선물 동반 추출)
40초: Broadcasting
41초: dxy_spot (DXY 현물)
45초: hana
47초: investing (+ DXY 선물 동반 추출)
50초: Broadcasting
51초: dxy_spot (DXY 현물)
53초: woori (우선순위 높음)
54초: nh (Selenium Queue)
55초: kb
57초: investing (+ DXY 선물 동반 추출)
58초: sc (Selenium Queue)
```

**특징:**
- Broadcasting 직전(3-7초)에 Request 크롤러 실행 → 최신 데이터 반영
- woori는 citi보다 우선순위 높음: 53초 실행 → 사용자 표시 시간이 실제 변경 시간과 유사
- Selenium 크롤러는 Queue 순차 처리 (Request 먼저 시도)
- 최대 동시 실행: 1-2개 (Request 기반 크롤러만)

### 설계 원칙

- **환율 고시 스케줄 기반**: 은행별 실제 운영 시간에 맞춰 크롤러 활성화/비활성화
- **mibank 딜레이 고려**: 은행 고시 종료 후에도 mibank 반영 지연 대비 (마지막 고시 누락 방지)
- **Broadcasting 동기화**: 크롤러가 Broadcasting X초 전에 실행 → 실시간 반영
- **Request 우선 전략**: Selenium 크롤러도 Request(mibank) 먼저 시도 → Queue 압력 감소
- **실시간성 > 완전성**: 타임아웃 엄격화로 빠른 실패 → Queue 정체 방지
- **리소스 분산**: OUT 모드 동시 실행 0개 → CPU 스파이크 제거
- **예측 가능성**: cron 절대 시간 → 매시간 같은 패턴
- **자동 복구**: Worker stuck 시 자동 재시작, 실패 작업 자동 재시도

## 크롤러 구조

**공통 패턴:**
- 2-3개 폴백 URL, CSS Selector 기반, 변경 시에만 DB 저장

**상세 가이드:** [CRAWLERS.md](CRAWLERS.md) (각 은행별 특수 로직, 트러블슈팅)

## API 엔드포인트

### WebSocket

- `WS /ws` - 실시간 환율 스트리밍 (매분 00, 10, 20, 30, 40, 50초)

**Payload 구조** (`build_rates_payload()` 기반):

```json
{
  "type": "rates",
  "data": {
    "rates": [ /* 30개 (10 은행 × 3 통화) */ ],
    "indices": {
      "dxy": {"rate": 99.234, "timestamp": "2026-04-18T09:07:45.189537+09:00", "source": "investing"}
    },
    "metadata": {"updated_at": "...", "currencies": [...], "banks": [...], "total_count": 30}
  },
  "graph_buckets": { /* 후속 broadcast에만 append, 초기 메시지엔 없음 */ }
}
```

- `data.indices.dxy` — **10초 해상도** DXY live tick. `crud.get_latest_dxy_rate()` 기반 (investing > yahoo). 초기 연결 메시지에도 포함됨 (Redis `BROADCAST_CACHE_KEY`가 `build_rates_payload` 결과 저장).
- `graph_buckets.usd-krw.dxy` — **분당 :03초** 집계된 10분 bucket 히스토리. 후속 broadcast에서만 append (Redis 캐시 기록 이후 단계).
- 두 경로는 해상도/갱신 주기/캐시 경로가 달라 **분리 유지**. 상세는 `DESIGN_DXY_GRAPH.md` §6.3 참조.
- 구 iOS 앱은 `indices` 필드 미인지 시 Codable이 자동 무시 (하위 호환).

### REST API (폴백용)

- `GET /api/rates` - 전체 환율 (모바일 앱용)
- `GET /api/rates/{currency}` - 특정 통화쌍
- `GET /api/investing/{pair}` - Investing.com 특정 통화
- `GET /api/banks/{pair}` - 모든 은행 특정 통화
- `GET /api/graph/{currency}` - 그래프 데이터 (파라미터: `range`=1d/1w/3m/1y, USD/KRW에 DXY 포함)
- `GET /api/news` - 환율 관련 뉴스 (파라미터: `limit`, `hours`) — Phase 1B
- `GET /health` - 헬스체크

### Admin API (HTTP Basic Auth 필요)

> 관리자 페이지 전용 API. `admin` / `ADMIN_PASSWORD` 인증 필요.

- `GET /admin/api/dashboard` - 통합 대시보드 데이터 (시스템, 크롤러, 브로드캐스트 상태)
- `GET /admin/api/logs` - 로그 조회 (파라미터: `log_type`, `hours`, `limit`, `bank`)
- `GET /admin/api/crawler-config` - 크롤러 활성화 설정 조회
- `POST /admin/api/crawler-config` - 크롤러 활성화/비활성화 토글
- `GET /admin/api/crawler/stats` - 크롤러별 통계 (성공률, 평균 실행시간)
- `GET /admin/api/queue-status` - Selenium Queue 상태 조회
- `GET /admin/api/redis-status` - Redis 메모리 및 Circuit 상태
- `GET /admin/api/monitor/current` - 현재 시스템 리소스 (메모리, CPU, Chrome)
- `GET /admin/api/monitor/history` - 시간별 리소스 히스토리 (파라미터: `hours`)

### 계정 API (Apple App Store 5.1.1(v) 준수)

- `DELETE /api/user/me` - 사용자 계정 데이터 삭제 (Firebase ID Token 필요)
  - 삭제 대상: `notification_logs`, `notification_settings`, `user_devices`
  - 응답: `204 No Content`
  - 오류: `401`(토큰 만료/무효/철회), `500`(서버 처리 오류), `503`(Firebase 연결 오류)
  - 클라이언트 권장: 401 시 재인증 후 재시도, 서버 삭제 완료 후 Firebase Auth 삭제

### 알림 API

> 상세 스키마: [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md#-개인화-알림-동작-흐름)

- `POST /api/register-device` - FCM Device Token 등록 (Firebase ID Token 검증 필수)
- `POST /api/notification-settings` - 알림 설정 생성
- `GET /api/notification-settings` - 사용자 알림 설정 조회
- `PUT /api/notification-settings/{id}` - 알림 설정 수정 (토글 ON/OFF)
- `DELETE /api/notification-settings/{id}` - 알림 설정 삭제

**알림 생성 요청 (POST):**

```json
{
  "bank": "hana",
  "currency": "usd-krw",
  "condition": "above",
  "threshold": 1475.0,
  "is_enabled": true
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `is_enabled` | `bool` | 선택 | 활성화 여부 (기본값 `true`, `null` 전송 시 422 에러) |

**중복 처리 정책:**

- 동일 조건 (bank + currency + condition + threshold) 알림 존재 시:
  - 새로 생성하지 않고 기존 설정의 `enabled` 상태만 업데이트
  - `is_enabled=true` → `triggered=false` 초기화 (재알림 가능)
  - `is_enabled=false` → `triggered` 유지 ("발송됨" 상태 보존)

**1회성 알림 동작:**

- 알림 발송 시 자동 비활성화 (`enabled=false, triggered=true`)
- 사용자가 토글 ON (`PUT {is_enabled: true}`) → 자동 초기화 (`enabled=true, triggered=false`)

### 응답 형식

```json
{
  "rates": [
    {
      "currency": "usd-krw",
      "bank": "kb",
      "rate": 1340.50,
      "timestamp": "2025-10-04T14:30:00+09:00"
    }
  ],
  "metadata": {
    "updated_at": "2025-10-04T14:30:00+09:00",
    "currencies": ["usd-krw", "jpy-krw", "eur-krw"],
    "banks": ["investing", "kb", "hana", ...],
    "total_count": 30
  }
}
```

## 인프라 환경

### 현재 서비스 환경

**인프라 구성:**

```text
AWS Cloud (서울 리전)
├── VPC (같은 네트워크)
│   ├── EC2 t3.small (2 vCPU, 2GB RAM)
│   │   └── FastAPI + 크롤러 + Redis (Docker)
│   └── RDS PostgreSQL db.t4g.micro (프리티어)
│       └── 환율 데이터 + 사용자 데이터
│
└── 외부 서비스
    ├── Firebase Auth (로그인, 무료)
    └── Firebase FCM (푸시 알림, 무료)
```

**EC2 인스턴스:**

- **인스턴스**: t3.small (2 vCPU, 2GB RAM)
- **OS**: Ubuntu 24.04 LTS (x86_64)
- **동시 접속**: 최대 ~500명 (WebSocket)

**AWS RDS PostgreSQL:**

- **인스턴스**: db.t4g.micro (2 vCPU, 1GB RAM, ARM64)
- **스토리지**: 20GB SSD (gp2)
- **기간**: 12개월 무료 (AWS 첫 가입 후)
- **네트워크**: 같은 VPC 내 통신 (지연 1-3ms, 비용 $0)
- **12개월 후 비용**: ~$15/월

### CloudWatch 알람

| 알람 이름 | 조건 | 설명 |
|----------|------|------|
| FXi-EC2-CPU-High | CPUUtilization > 80% | EC2 CPU 과부하 |
| FXi-EC2-CPU-Credit-Low | CPUCreditBalance < 50 | EC2 버스트 크레딧 부족 |
| FXi-RDS-CPU-High | CPUUtilization > 80% | RDS CPU 과부하 |
| FXi-RDS-Memory-Low | FreeableMemory < 100MB | RDS 메모리 부족 |
| FXi-RDS-Storage-Low | FreeStorageSpace < 2GB | RDS 스토리지 부족 |

- **SNS 주제**: `fxi-alerts` (이메일 알림)
- **평가 기간**: 5분 내 1개 데이터 포인트

### 비용 예상

| 항목 | Phase 1 (12개월) | Phase 2 (12개월 후) |
|------|-----------------|-------------------|
| EC2 t3.small | $15/월 | $15/월 |
| RDS db.t4g.micro | $0 (프리티어) | ~$15/월 |
| Firebase Auth/FCM | $0 | $0 |
| **합계** | **$15/월** | **$30/월** |

### 확장 계획 (사용자 1,000명 이상 시)

1. ✅ **PostgreSQL 전환** → RDS 프리티어로 해결 (2026-01)
2. ✅ **Redis 캐시 도입** → Phase 1.7 완료 (EC2 Docker)
3. ✅ **변경 감지 시스템** → Phase 1.7 완료
4. ✅ **CloudWatch 알람** → EC2/RDS 모니터링 설정 완료 (2026-01)
5. **EC2 업그레이드** (t3.small → t3.medium, 필요 시)
6. **RDS 업그레이드** (db.t4g.micro → db.t4g.small, 필요 시)

## Docker 배포 및 관리

### 변경 종류별 절차 — force-recreate vs build 경계 (2026-05-19 lesson)

> ⚠️ **Coinone canary 활성화 시 ModuleNotFoundError 사고 (2026-05-19)**: `git pull` 후
> `docker compose up -d --force-recreate fastapi`만 실행 → image rebuild 안 되어 새 모듈
> (`coinone.py`) 부재 → `ModuleNotFoundError: No module named 'app.crawlers.usdt_ws.coinone'`.
> `docker compose build fastapi` 선행 후 정상 동작. **build와 force-recreate는 다른 역할**.

| 변경 종류 | 절차 | 이유 |
| --- | --- | --- |
| **코드 변경 포함** (`.py` 등 application code) | `git pull` → `docker compose build fastapi` → `docker compose up -d --force-recreate fastapi` (또는 한 번에 `docker compose up -d --build fastapi`) | image에 새 코드를 포함시켜야 컨테이너 안에 반영. `--force-recreate`만으로는 image rebuild 안 됨 |
| **env만 변경** (`.env` 토글 — `USDT_WS_*_ENABLED` 등) | `docker compose up -d --force-recreate fastapi` (rebuild 불필요) | env_file은 컨테이너 재생성 시 다시 읽힘. 코드 변경 0이므로 image 그대로 사용 |
| **신규 모듈 추가 후 검증** | `docker exec exchange-rate-app python -c "from app.crawlers.usdt_ws.coinone import CoinoneWsClient; print('OK')"` import check | image rebuild 누락 시 즉시 발견 가능. 운영 lifecycle 시작 전 sanity 확인 |

**`docker compose restart`는 env_file 변경을 반영하지 않음** (기존 컨테이너 stop/start만). env 변경 시 `--force-recreate` 필요. `KRX_FUTURES_ENABLED` 토글 등 운영 영향 큰 env에는 반드시 `--force-recreate`.

### 코드 변경 후 Docker 재배포 절차

#### 일상적인 코드 변경 (권장)
```bash
# 변경된 부분만 재빌드하고 컨테이너 재시작
docker compose up -d --build

# 로그 확인
docker compose logs -f
```

#### 캐시 문제 발생 시 클린 빌드
```bash
# 1. 컨테이너 중지 및 제거
docker compose down

# 2. 캐시 없이 완전 재빌드
docker compose build --no-cache

# 3. 컨테이너 시작
docker compose up -d

# 4. 로그 확인
docker compose logs -f
```

#### 완전 초기화 (개발 환경 리셋)
```bash
# 1. 컨테이너 및 볼륨 제거 (주의: 데이터 삭제됨)
docker compose down -v

# 2. 모든 Docker 리소스 정리
docker system prune -a -f

# 3. 재빌드 및 시작
docker compose up -d --build

# 4. 로그 확인
docker compose logs -f
```

### 유용한 Docker 명령어

#### 정리 작업
```bash
# 중지된 컨테이너만 제거
docker container prune -f

# 사용하지 않는 이미지 제거
docker image prune -f

# 빌드 캐시 제거
docker builder prune -f
```

### 배포 시 주의사항

1. **볼륨 유지**: `docker compose down -v`는 데이터베이스 데이터까지 삭제하므로 신중하게 사용
2. **캐시 활용**: `--no-cache`는 빌드 시간이 오래 걸리므로 문제가 있을 때만 사용
3. **로그 확인**: 배포 후 반드시 로그를 확인하여 정상 작동 여부 체크
4. **단계적 접근**: 간단한 명령어부터 시도하고, 문제가 있을 때만 전체 클린 빌드 수행

## 크롤링 리스크 대응

### 문제점

- 은행 웹사이트 구조 변경 시 크롤러 중단
- CSS Selector 무효화
- User-Agent 차단

### 대응 방안

1. **다중 폴백 URL** (현재 구현됨)
2. **정기적인 Selector 검증** (주 1회 권장)
3. **크롤링 실패 알림 시스템** (TODO)
4. ✅ **User-Agent 로테이션** (Investing 크롤러 적용, Safari/Chrome UA 풀)
5. ✅ **TLS 지문 위장** (curl_cffi + safari17_0, Cloudflare 우회) - [ADR-018](DECISIONS.md#adr-018-investing-cloudflare-차단-대응---curl_cffi-tls-지문-위장)

## 파일 구조

```
exchange-rate/
├── app/
│   ├── __init__.py          # 로거 export
│   ├── config.py            # 환경 설정 (ENV, LOG_LEVEL, 텔레그램 등)
│   ├── logging.py           # 구조화된 로깅 시스템 (중앙 설정)
│   ├── database.py          # DB 연결 설정
│   ├── models.py            # SQLAlchemy ORM 모델
│   ├── schemas.py           # Pydantic 스키마
│   ├── crud.py              # DB CRUD 로직
│   ├── cache.py             # Redis 클라이언트 (Circuit Breaker) - Phase 1.7
│   ├── latest_rates_cache.py # Redis latest mirror layer (PR3-PR5, ADR-026)
│   ├── main.py              # FastAPI 서버, WebSocket
│   ├── scheduler.py         # APScheduler, 4단계 모드 전환
│   ├── source_registry.py   # USDT Phase 1 source 메타데이터 (SourceDefinition)
│   │
│   ├── crawlers/            # 크롤러 도메인 (2025-10-25 리팩토링)
│   │   ├── __init__.py
│   │   ├── constants.py     # 크롤러 공통 상수 (HEADERS, SELENIUM_OPTIONS, TIMEOUT)
│   │   ├── utils.py         # 크롤러 공통 함수 (parse_rate_text, create_selenium_driver)
│   │   ├── runner.py        # Selenium 크롤러 subprocess runner
│   │   ├── investing.py     # Investing.com 크롤러
│   │   ├── kb.py            # KB은행 크롤러
│   │   ├── hana.py          # 하나은행 크롤러
│   │   ├── shinhan.py       # 신한은행 크롤러
│   │   ├── woori.py         # 우리은행 크롤러
│   │   ├── ibk.py           # IBK기업은행 크롤러
│   │   ├── nh.py            # NH농협은행 크롤러
│   │   ├── sc.py            # SC제일은행 크롤러
│   │   ├── bs.py            # 부산은행 크롤러
│   │   ├── citi.py          # 씨티은행 크롤러
│   │   ├── dxy_spot.py      # DXY 현물/운영 피드 (instrument='dxy') 독립 크롤러 — /indices/usdollar __NEXT_DATA__ → CSS → CNBC → Yahoo
│   │   ├── dxy.py           # DXY 외부 폴백 유틸: 현물용 CNBC/Yahoo + 선물용 /currencies/us-dollar-index
│   │   └── usdt_sources.py  # USDT/KRW 5개 거래소 통합 크롤러 (Phase 1, fan-out)
│   │
│   ├── admin/               # 관리자 도메인 (2025-10-25 리팩토링)
│   │   ├── __init__.py
│   │   ├── log_reader.py    # 로그 조회 (관리자 페이지용, 백엔드 bank 필터링)
│   │   ├── log_cleaner.py   # 오래된 로그 파일 자동 삭제
│   │   ├── stats.py         # WebSocket 브로드캐스트 통계 수집
│   │   ├── crawler_stats.py # 크롤러 통계 수집 (성공률, 실행시간)
│   │   ├── monitor.py       # 시스템 모니터링 (메모리, CPU, Chrome) - Phase 1.5
│   │   ├── graph_cache.py   # 그래프 데이터 Redis 캐시 - Phase 1A
│   │   └── dxy_rollup.py   # DXY realtime → hourly/daily 집계 (rollup)
│   │
│   ├── news/                # 뉴스 피드 도메인 (Phase 1B)
│   │   ├── __init__.py
│   │   ├── sources.py       # RSS 소스 정의 (NewsSource + NEWS_SOURCES)
│   │   ├── filters.py       # 필터 시스템 (잡음/관련도/macro/severity/industry)
│   │   ├── fetcher.py       # RSS 수집 + 파싱 + upsert + cleanup
│   │   ├── kb_fetcher.py    # KB API 수집 (속보, [전문] PDF 추출)
│   │   └── upsert.py        # 공통 Redis upsert (KB↔RSS 병합 규칙)
│   │
│   └── notifications/       # 알림 도메인 (2025-10-25 리팩토링)
│       ├── __init__.py
│       ├── fcm.py           # Firebase Cloud Messaging 푸시 알림 (Phase 2)
│       └── telegram.py      # 텔레그램 알림 (관리자 알림용)
│
├── scripts/
│   ├── backfill_history.py              # DXY 히스토리 백필 (yfinance, daily/hourly)
│   └── migrate_market_index_granularity.py  # granularity 컬럼 마이그레이션
├── data/
│   └── exchange_rates.db    # SQLite DB
├── logs/                    # 로그 파일 (자동 생성)
│   ├── app.log              # 모든 운영 로그 (INFO+, 크롤러 포함)
│   └── error.log            # 에러/경고만 (WARNING+)
├── static/                  # 은행 아이콘
├── templates/               # 관리자 웹 UI (순수 HTML+JS, Jinja2 미사용)
│   ├── index.html           # 메인 대시보드 (API 확인)
│   └── admin.html           # 관리자 페이지
├── .env                     # 환경 변수 (git 제외)
├── .gitignore               # Git 제외 파일
├── requirements.txt         # Python 패키지
├── CLAUDE.md                # 이 파일 (프로젝트 가이드)
├── CRAWLERS.md              # 크롤러 특수 로직 가이드
├── DECISIONS.md             # 아키텍처 의사결정 기록 (ADR)
├── CHANGELOG.md             # 버전별 변경사항 및 마이그레이션 가이드
├── DOCKER.md                # Docker Compose 구조 및 확장 전략
├── DEPLOYMENT.md            # EC2 인스턴스 설정 및 실전 배포 절차
├── REBOOT_CHECKLIST.md      # 재부팅 후 검증 절차 가이드
├── MAINTENANCE_2025-11-05.md # 성능 개선 및 모니터링 시스템 구축
└── MAINTENANCE_2025-11-06.md # AsyncIO Queue 도입 (Semaphore 경합 제거)
```

> 💡 **구조 설계 원칙**: API-first 서비스 (모바일 앱이 메인, 웹은 관리자 전용)
> **확장성**: React 전환 시 `frontend/` 폴더 추가만 하면 됨 (Backend/Frontend 약한 결합)

## 주요 로직

**중복 방지 INSERT**: 마지막 레코드와 비교, 변경 시에만 저장 (`crud.py`)

**WebSocket 브로드캐스트**: 매분 00, 10, 20, 30, 40, 50초 정확한 시간, APScheduler cron job, 변경사항 있을 때만 전송 (`main.py`, `scheduler.py`)

**브로드캐스트 통계**: 성공률, 평균 주기, 건강 상태 추적 (`admin/stats.py`)

## 개발 가이드라인

**크롤러 추가**: [CRAWLERS.md](CRAWLERS.md) "새 은행 추가 가이드" 참고

**DB 마이그레이션**: SQLAlchemy 자동 생성, 변경 시 Alembic 권장

### 로깅 시스템

**구조화된 로깅**: JSON 형식, KST 타임존, exception() 강화

**파일**:
- `app.log` - 모든 운영 로그 (INFO+, 크롤러 포함)
- `error.log` - 에러/경고만 (WARNING+)

**환경 설정** (`.env`):
- `ENV`: development (컬러) / production (JSON)
- `LOG_LEVEL`: DEBUG / INFO / WARNING / ERROR

**로거 사용**:
```python
logger = logging.getLogger("exchange_rate.crawler.kb")
logger.info("⚡️ 환율 변경", extra={"pair": "usd-krw", "rate": 1340.5})
logger.exception("크롤링 실패", extra={"bank": "kb"})  # except 블록
```

**관리**: 로테이션 10MB, 백업 3개, 10일 자동 삭제

**상세 가이드**: `app/logging.py`, `app/admin/log_reader.py`

## 보안 고려사항

- [x] **HTTPS/SSL 적용** (Let's Encrypt, 2025-11-17)
  - 도메인: `fxi.kr`
  - 자동 갱신: Systemd Timer (매일 KST 04:30, 05:30)
  - TLS 1.2 & 1.3, HSTS 헤더 (1년)
- [x] **민감 정보 환경 변수화** (python-dotenv, .env 파일)
- [ ] CORS 설정 (특정 도메인만 허용)
- [ ] API Rate Limiting (현재: Nginx 3 req/s)

## 관리자 페이지 (/admin)

### Phase 1: 심플 대시보드 ✅ 완료 (2025-10-17)

**핵심 기능**:
- 4개 대시보드 카드 (WebSocket, 메모리, 브로드캐스트, 에러)
- 크롤러 상태 뱃지 (에러 개수, 클릭 시 로그 필터링)
- 로그 뷰어 2개 탭 (실시간 모니터링, 에러 추적)
- 자동 새로고침 30초

**접속**: `http://localhost:8000/admin` (admin / .env의 ADMIN_PASSWORD)

**기술**: Vanilla JS, HTTP Basic Auth, 통합 API (`/admin/api/dashboard`)

### Phase 1.5: 모니터링 시스템 강화 ✅ 완료 (2025-11-05)

**배경**: 시간이 지날수록 시스템이 느려지는 문제 해결

**추가된 기능**:
- **Chrome 프로세스 모니터링 카드**: 프로세스 개수 + 메모리 사용량 실시간 표시
- **자동 정리 시스템**: 좀비 Chrome 프로세스 매분 03/23/43초 자동 정리 (60초 이상 실행된 것)
- **리소스 모니터링**: 메모리/CPU 사용량 5분마다 자동 수집 및 히스토리 저장 (5시간)
- **임계값 알림**: 메모리 70% 이상, Chrome 프로세스 3개 이상 시 경고

**모니터링 API**:
- `GET /admin/api/monitor/current` - 현재 시스템 상태
- `GET /admin/api/monitor/history?hours=1` - 시간별 히스토리 (Chart.js용)

**핵심 개선사항**:
- `app/crawlers/utils.py`: driver.quit() 실패 시 강제 종료
- `app/scheduler.py`: 좀비 프로세스 정리 + 모니터링 통계 수집
- `app/admin/monitor.py`: 시스템 모니터링 모듈 (신규)
- `app/main.py`: WebSocket 연결 관리 안정화

**상세 내역**: [MAINTENANCE_2025-11-05.md](MAINTENANCE_2025-11-05.md)

### Phase 1.6: Selenium Queue 모니터링 ✅ 완료 (2025-11-13)

**배경**: Selenium 크롤러 Queue 상태 가시성 확보 및 중복 작업 방지

**추가된 기능**:
- **Queue 모니터링 카드**: 실시간 Queue 크기, 사용률, 현재 처리 작업, 대기 작업 목록 표시
- **중복 작업 방지**: Worker 처리 중 + Queue 대기 중 체크로 중복 작업 제거
- **사용률 시각화**: 60% 미만(녹색), 60-80%(주황), 80% 이상(빨강)
- **대기 작업 뱃지**: 일반/재시도 구분, 대기 시간 표시

**모니터링 API**:
- `GET /admin/api/queue-status` - Selenium Queue 상태 조회 (10초마다 자동 업데이트)

**핵심 개선사항**:
- `app/scheduler.py`: Queue 중복 방지 로직, 상태 캐시, 10초마다 모니터링
- `app/main.py`: Queue 상태 API 엔드포인트 추가
- `templates/admin.html`: Queue 모니터링 카드 및 실시간 업데이트

### Phase 1.7: Redis 브로드캐스트 캐시 ✅ 완료 (2025-11-27)

**배경**: WebSocket 초기 접속 최적화 및 변경 감지 시스템 구축

**추가된 기능**:
- **Redis 브로드캐스트 캐시**: 초기 접속 시 DB 대신 Redis 캐시로 즉시 데이터 전송
  - Cache key: `broadcast:latest` (3.6KB JSON)
  - Memory: ~1MB stable (1% of 100MB)
  - Circuit Breaker: 5 failures → 30s timeout → auto-recovery
- **변경 감지 시스템**: JSON 비교로 실제 변경 시에만 브로드캐스트
  - "⏸️ 변경사항 없음" vs "📡 브로드캐스트 완료" 구분 로깅
  - 대역폭 최적화: 변경 없으면 전송 스킵
- **Redis 모니터링 카드**: 실시간 메모리, 키 개수, Circuit 상태 표시

**버그 수정**:
- Broadcasting `updated_at` 타임스탬프 문제 (항상 다른 값 생성)
  - `datetime.now()` → DB의 실제 최신 `max(rate["timestamp"])` 사용
  - 변경 감지 정확도 100% 달성

**모니터링 API**:
- `GET /admin/api/redis-status` - Redis 메모리, 키, Circuit 상태 조회

**핵심 개선사항**:
- `app/main.py`: Broadcasting 버그 수정, Redis 상태 API, 변경 감지 로직
- `app/cache.py`: Circuit Breaker 기반 Redis 클라이언트
- `templates/admin.html`: Redis 모니터링 카드 추가

### Phase 1.8: 크롤러 제어 ✅ 완료 (2025-12-02)

**배경**: 특정 크롤러의 일시적 비활성화 필요 (은행 사이트 점검, 차단 대응)

**추가된 기능**:
- **크롤러 활성화/비활성화**: 관리자 페이지에서 개별 크롤러 토글
- **crawler_config 테이블**: 크롤러별 enabled 상태 저장
- **실시간 반영**: 토글 즉시 스케줄러에 반영

**관련 스키마**: `crawler_config` 테이블 (CLAUDE.md 데이터베이스 스키마 참조)

### Phase 1.9: Redis-first broadcast hot path ✅ 완료 (2026-05-04)

**배경**: 매초 broadcast가 DB latest SELECT (rates + DXY)에 직접 묶여 정각 spike 발생 ([ADR-026](DECISIONS.md#adr-026-redis-first-broadcast-hot-path--latest-mirror--dxy-mirror로-db-free-달성))

**추가된 기능**:
- **신규 모듈** `app/latest_rates_cache.py`: Redis latest mirror layer
- **mirror keys**: `latest:bank:{bank}:{currency}`, `latest:source:{source}:{asset}`, `latest:investing:{currency}`, `latest:dxy:current`
- **제어 key**: `latest:index` (atomic snapshot 일관성)
- **mirror cycle**: APScheduler IntervalTrigger 3초
- **broadcast Redis-first read**: `fetch_rates_from_redis()` (MGET 1회 통합)
- **DXY mirror + DXY-only fallback** (PR5): rates 성공 + DXY 실패 시 DXY만 DB
- **신규 env**: `REDIS_LATEST_ENABLED`, `LATEST_MIRROR_INTERVAL_SECONDS`
- **신규 메트릭**: `latest_source` / `mirror_age_ms` / `latest_index_get_ms` / `latest_data_get_ms` / `latest_decode_ms` / `latest_dxy_get_ms` / `dxy_path` / `latest_dxy_fallback_reason` / `payload_assemble_ms` / `payload_assemble_without_dxy_ms` / `payload_build_unmeasured_ms`

**측정 결과** (PR5 24h 누적, n=31495):
- payload_build_ms p99 **30.25ms** (PR3.5 baseline 195ms → 6.4× 가속)
- DB hot path **100% → 0%** (rates + DXY 모두 Redis-first hit 100%)
- dxy_query_ms count **1827 → 0**
- mirror_age_ms p99 2301ms (3초 ceiling 안정)
- 영업시간 IN mode (n=1801): p99 75.97ms (Redis read jitter ~2.5×, DB 경합 아님)

**핵심 파일**:
- `app/latest_rates_cache.py` (신규, mirror + Redis-first read + DXY mirror)
- `app/main.py` (broadcast hot path Redis-first 분기 + 분해 계측)
- `app/scheduler.py` (mirror job 등록)
- `app/config.py` (env)
- `scripts/analyze_broadcast_metrics.py` (메트릭 집계)

### Phase 1A: DXY 그래프 보조지표 ✅ 완료 (2026-03-10)

**배경**: USD/KRW 환율 그래프에 달러지수(DXY)를 보조지표로 표시 ([ADR-019](DECISIONS.md#adr-019-dxy-보조지표---granularity-기반-2-part-merge-전략))

**추가된 기능**:
- **DXY 현물/운영 피드 (`instrument='dxy'`)**: `dxy_spot.py` 독립 크롤러
  - Primary: `/indices/usdollar` `__NEXT_DATA__` (curl_cffi, TLS 지문 위장)
  - Fallback1: 같은 페이지 CSS selector (`[data-test="instrument-price-last"]` 등) — source는 동일하게 `investing` 저장
  - Fallback2 (외부 chain): CNBC `.DXY` (1순위) → Yahoo Finance `DX-Y.NYB` (최후 보루)
  - 외부 chain 가드: ICE DX 주간 세션 / market mode 보존 정책 / fresh-age diff guard
  - 독립 cron job (`task_dxy`)으로 등록 ([scheduler.py](app/scheduler.py))
  - `crawler_config`에 `dxy` 행 사용 (관리자 페이지에서 활성/비활성 토글)
- **DXY 선물 (`instrument='dxy_futures'`)**: `investing.py`가 환율과 동시 추출
  - Primary: exchange-rates-table `#sb_last_8827`
  - Fallback: `dxy.py`의 `fetch_dxy_from_investing_fallback()` → `/currencies/us-dollar-index` (60초 쿨다운)
  - Yahoo는 미사용 (현물/운영 DXY 계열이라 선물 저장에 부적합)
- **market_index_rates 테이블**: 범용 시장 지수 테이블 (granularity 컬럼)
- **그래프 API 확장**: `/api/graph/{currency}?range=1d|1w|3m|1y`
  - USD/KRW에만 DXY 보조지표 포함 (API key=`dxy`, UI 표시명=달러지수)
  - 1d: 10분 버킷 (realtime only)
  - 1w: 1시간 버킷 — **full-window 전략** (hourly + realtime 전체 7일 조회, hourly > realtime dedup)
  - 3m/1y: 1일 버킷 — **daily + recent realtime tail 전략** (daily 전체 + realtime 최근 7일 overlap)
- **기간별 DXY 쿼리 전략** (graph_cache.py `build_period_dxy_series()`):
  - 1w: 단일 쿼리, `PARTITION BY timestamp`에서 hourly > realtime 우선 → gap 있어도 realtime이 자연 보충
  - 3m/1y: daily 전체 윈도우 조회 + realtime은 `last_bf_ts - 7일`부터만 tail 조회 (`_DXY_DAILY_REALTIME_TAIL_DAYS = 7`)
  - 상수: `_DXY_DAILY_REALTIME_TAIL_DAYS = 7` (graph_cache.py line 31)
- **히스토리 백필 스크립트**: yfinance로 daily(1년)/hourly(7일) 데이터 사전 적재
- **DXY rollup 스케줄**: realtime → hourly(매시 :05) / daily(매일 00:05 KST) 자동 집계
  - 백필 종료 이후 구간을 rollup이 연속 커버 (Yahoo 재실행 불필요)
  - source 보존: 원본 realtime의 실제 source를 그대로 사용
  - idempotent: INSERT ON CONFLICT UPDATE
- **수동 gap 복구 함수** (`app/admin/dxy_rollup.py`):
  - `backfill_hourly_range(start_utc, end_utc)`: UTC 구간의 realtime → hourly 일괄 생성 (배타 상한)
  - `backfill_daily_range(start_kst, end_kst)`: KST 날짜 구간의 hourly/realtime → daily 일괄 생성 (배타 상한)

**배포 순서** (운영 환경, 상세: [DEPLOYMENT.md](DEPLOYMENT.md#db-마이그레이션-v1120-dxy-granularity)):
1. `git pull` → `docker compose build` (새 이미지 빌드)
2. `docker compose run --rm fastapi python scripts/migrate_market_index_granularity.py`
3. `docker compose up -d` (서비스 시작)
4. `docker compose exec fastapi python scripts/backfill_history.py`

**핵심 파일**:
- `app/crawlers/dxy_spot.py`: DXY 현물/운영 피드 독립 크롤러 (`instrument='dxy'`, `/indices/usdollar` `__NEXT_DATA__` → CSS → CNBC → Yahoo)
- `app/crawlers/investing.py`: DXY 선물 동시 추출 (`instrument='dxy_futures'`, exchange-rates-table `#sb_last_8827`)
- `app/crawlers/dxy.py`: 외부 폴백 유틸 (현물: `fetch_dxy_from_cnbc` / `fetch_dxy_from_yahoo`, 선물: `fetch_dxy_from_investing_fallback` → `/currencies/us-dollar-index`)
- `app/models.py`: MarketIndexRate 모델 (granularity 컬럼)
- `app/admin/graph_cache.py`: 그래프 시계열 구축 (2-part merge, 버킷 집계)
- `app/admin/dxy_rollup.py`: DXY realtime → hourly/daily rollup
- `app/crud.py`: DXY CRUD (insert, get_latest, get_for_period)

### Phase 2: FCM 푸시 알림 ✅ 완료 (2025-12-21)

**배경**: 앱 종료 상태에서도 환율 알림 필요 ([ADR-003](DECISIONS.md#adr-003-알림-시스템---websocket-vs-push-notification))

**추가된 기능**:
- **Firebase Auth 연동**: ID Token 검증으로 사용자 인증
- **FCM Device Token 관리**: 디바이스별 푸시 토큰 저장
- **알림 설정 API**: 목표 환율 도달 시 푸시 알림 발송
- **1회성 알림**: 발송 후 자동 비활성화, 토글 ON으로 재활성화

**알림 API**:
- `POST /api/register-device` - FCM Device Token 등록
- `POST /api/notification-settings` - 알림 설정 생성
- `GET /api/notification-settings` - 알림 설정 조회
- `PUT /api/notification-settings/{id}` - 알림 설정 수정/토글
- `DELETE /api/notification-settings/{id}` - 알림 설정 삭제

**핵심 파일**:
- `app/notifications/fcm.py`: Firebase Admin SDK 초기화, 푸시 발송
- `app/crud.py`: 알림 설정 CRUD (mark_setting_triggered 등)
- `app/main.py`: 알림 API 엔드포인트

**상세 가이드**: [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md)

### Phase 1B: 환율 뉴스 피드 ✅ 완료 (2026-03-28)

**배경**: 환율 변동의 원인을 이해할 수 있는 뉴스 제공

**데이터 소스**:
- **RSS** (연합인포맥스 4개 피드): 채권/외환, 국제뉴스, 해외주식, 증권 — ~2시간 딜레이
- **KB API** (fx.kbstar.com): 동일 인포맥스 뉴스를 딜레이 없이 제공 — 속보 소스

**핵심 기능**:
- **잡음 제외 필터**: 인사/부고/정치 키워드 제외 (noise_only 일원화)
- **KB↔RSS 병합**: nsid 기반 upsert, RSS 유효값 우선, published_at min()
- **content_type 분류**: `external_link`(일반 기사), `report_pdf`(은행 보고서 PDF 직링크)
- **`*` 기사 처리**: 제목에 "(본문없음)" 추가, link 유지
- **`[전문]` 기사 처리**: KB 상세에서 PDF URL 추출, report_pdf로 분류
- **시간순 정렬**: 순수 published_at 내림차순
- **초보/상보 near-duplicate collapse**: 시간 클러스터 방식
- **Redis-only 저장**: 24시간 윈도우, DB 불필요

**스케줄링**:
- KB API: 5분마다 :15초 (외환+경제 탭, 2페이지씩)
- RSS: 5분마다 :45초 (ETag/Last-Modified 조건부 GET)
- 모드 무관 (24시간 동일)

**뉴스 API**: `GET /api/news` (파라미터: `limit`, `hours`)

**핵심 파일**:
- `app/news/sources.py`: RSS 소스 정의
- `app/news/filters.py`: 잡음 제외 필터 + 제목 정규화
- `app/news/fetcher.py`: RSS 수집
- `app/news/kb_fetcher.py`: KB API 수집 ([전문] PDF 추출)
- `app/news/upsert.py`: 공통 Redis upsert (KB↔RSS 병합 규칙)

**구현 상세**: `app/news/` 코드 (source of truth, Phase 1B 안정화 완료 — 임시 NEWS_IMPL_SPEC.md는 삭제, git history 보존)

### USDT Phase 1: 테더 탭 백엔드 foundation ✅ 백엔드 완료 / 서비스 미출시 (2026-04-23 구현, 2026-05-06 status 갱신)

> ⚠️ **운영 현실 (2026-05-06)**: 백엔드 수집/저장/알림 API는 완료. 운영 앱(iOS 2026-01-21 / Android 2026-03-13)에는 **테더 탭 없음**. 아래 "기존 API 확장 (legacy 통합)" 항목은 iOS dev/test 단계의 임시 모델이고 **서비스 출시 계약이 아니다**. 서비스 계약은 **topic-only**다 ([ADR-028](DECISIONS.md) / [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md)).

**배경**: 김치프리미엄 전략(KRX 선물 매도 + USDT 매수 헷지) 사용자를 위한 거래소 간 USDT/KRW 비교

**설계 원칙** (Decision E — 백엔드 도메인 모델만 유효):

- 기존 bank/investing 세계는 유지 (API/앱 호환성)
- 새 source 기반 세계를 별도 도입 (`source + asset`)
- ⚠️ "API 표면은 `bank + currency`로 어댑터 변환" 부분은 iOS test-era 임시 결정. 서비스 출시 계약은 topic-only

**추가된 기능 (백엔드)**:

- **거래소 5종 수집**: 업비트, 빗썸, 코인원, 고팍스, 코빗 USDT/KRW (REST polling 10초, 24/7)
- **새 데이터 모델**: `source_rates`, `source_notification_settings`, `source_notification_logs`
- **Source 알림 API**: `/api/source-notification-settings` (4종, 거래소 전용, reference는 400 + 기존 API 안내)
- **30일 보관 cleanup**: 매일 03:31

**legacy compatibility 경로 (test-era, 서비스 계약 아님)**:

- `/api/rates`, `/api/rates/usdt-krw`, WebSocket `rates` 배열에 usdt-krw 엔트리 자동 포함 — iOS dev/test 단계에서 추가됨
- 운영 앱이 테더 탭을 갖지 않으므로 사용자 영향 0
- 토픽 프로토콜 v1 도입 시 legacy `rates`에서 USDT 분리 예정 (별도 PR — Phase Z-2)

**알림 API**:

- `POST /api/source-notification-settings` - source 기반 알림 생성 (category=exchange만 허용)
- `GET /api/source-notification-settings` - 알림 설정 조회 (asset 필터 지원)
- `PUT /api/source-notification-settings/{id}` - 알림 설정 수정 (source/asset 변경 시 재검증)
- `DELETE /api/source-notification-settings/{id}` - 알림 설정 삭제 (멱등성)

**핵심 파일**:

- `app/source_registry.py`: SourceDefinition 데이터클래스 (9개 소스 메타데이터, canonical key는 DB 저장 안 함)
- `app/crawlers/usdt_sources.py`: 5개 거래소 통합 크롤러 (ThreadPoolExecutor fan-out)
- `app/models.py`: SourceRate, SourceNotificationSetting, SourceNotificationLog
- `app/crud.py`: source CRUD + `get_source_rates_as_legacy_format` 어댑터 + `process_source_rate_alerts`

**FCM payload type**: `source_rate_alert` (기존 `rate_alert`와 구분, 기존 앱은 unknown type 무시)

**알려진 기술 부채**:

- `metadata.banks` / `metadata.currencies` dead fields (iOS/Android에서 파싱만 되고 사용 안 됨, non-optional이라 즉시 제거 불가)
- 상세: [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md#known-tech-debt)

**설계/구현 문서**:

- [USDT_TAB_PROPOSAL.md](USDT_TAB_PROPOSAL.md): 제품 방향 + Decision A~F
- [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md): 백엔드 설계 + 구현 순서 + 기술 부채
- [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md): iOS/Android 공통 클라이언트 설계 가이드 (RateSource 모델, SourceRegistry, FCM type 분기)

**향후 Phase**:

- Phase 2: 테더 탭 그래프 (DXY와 동일한 rollup 전략), KRX 달러선물 (재배포 권리 확인 후)
- Phase 3: 비교 알림 (`comparison_alerts` 스키마는 Phase 1 설계 문서에서 잠김)

### KRX 미국달러선물 (KIS Open API) — Stage 1 운영 + Close finalizer 2차 작업 배포 완료 (2026-05-17)

**배경**: 김치프리미엄 전략 사용자가 거래소 USDT 외에 KRX 미국달러선물(USDF) 호가/체결도 함께 보고자 함. 기준 만기 종목 단축코드 예: A75605 (2026-05-18 만기, KIS master로 동적 resolve).

**최신 운영 상태 (2026-05-17 17:07 KST 기준)**:

- Stage 1 (DB 저장 관찰) — 운영 중. `KRX_FUTURES_ENABLED=true`
- **Close finalizer 2차 작업 (Stage 1-5)** — 배포 완료. `c2fb796` ([KRX_CLOSE_SNAPSHOT_PLAN.md](KRX_CLOSE_SNAPSHOT_PLAN.md) §5)
  - WS-first close finalizer + REST fallback 1회 + Redis TTL captured flag race 방지
  - F1 (DB row 중복) / F2 (flag race) / F3 (test hang) 모두 구조적 해결
  - `KRX_CLOSE_FINALIZER_ENABLED=true` default, false 시 1차 PR (c0855ff) 동작 rollback
  - 첫 실측: 2026-05-18 (월) 15:45 KST CF close + 2026-05-19 (화) 06:00 KST CM close

**운영 진입 절차**: [KRX_CANARY.md](KRX_CANARY.md) — Stage 0/1/2 단계별 env, 검증 명령(SQL/Redis), 24h baseline 지표, 5/18 만기 관찰 시나리오, **close finalizer 첫 실측 체크리스트**, rollback 절차 (Rollback A/B), Stage 2 진입 조건 체크리스트.

**핵심 원칙 — KRX optional source**: 서버/앱의 baseline 서비스(은행 + investing + USDT)는 KRX 없이 항상 정상 동작. KRX는 선택적 데이터 소스로 격리되며, 어느 단계의 실패도 다른 startup/shutdown/broadcast 경로에 영향 없음.

**격리 토글 (env)**:

| Env | Default | Scope |
| --- | --- | --- |
| `KIS_APP_KEY` / `KIS_APP_SECRET` | 미설정 | 미설정 시 bootstrap warning 후 격리 (다른 서비스 영향 X) |
| `KRX_FUTURES_ENABLED` | `false` | WebSocket 연결 / 신규 DB 저장 (수집 lifecycle) |
| `KRX_CLOSE_FINALIZER_ENABLED` | `true` | Close finalizer 2차 작업 정책 (Stage 1-5, c2fb796 deploy 2026-05-17). `false` 시 1차 PR (c0855ff) 동작 rollback — KrxDbWriter close-grace skip 미진입 + KrxCloseWindowWriter early return + REST 3 retry 그대로 |

**노출 정책 (Z-2d, 2026-05-12)**: `KRX_BROADCAST_INCLUDE` env는 Z-2d cleanup에서
제거됨. legacy `latest:index`/`/api/rates*`/WebSocket `rates` 배열 노출 여부는
`app/legacy_policy.should_include_source_in_legacy_rates` allowlist가 단일 진실
소스 — KRX는 allowlist 미포함이라 항상 차단. KRX는 topic-only source로
`krx:usd-krw-futures` 같은 별도 topic 채널에서만 노출 예정 (Phase Z-2 후속 PR).

**Canary 단계** (env 토글로 단계적 활성화):

1. **Stage 0 — 비활성 baseline**: `KRX_FUTURES_ENABLED=false`. 코드는 배포되어 있으나 lifecycle 자체가 비활성. 운영 영향 0.
2. **Stage 1 — DB 저장만 (현재)**: `KRX_FUTURES_ENABLED=true`. KIS WebSocket → DB(`source_rates`)까지만. legacy 경로 노출은 Z-2d allowlist로 자동 차단 (KRX 미포함). KRX tick 정상성 + DB write throughput을 24h 관찰.
3. **Stage 2 — topic 노출** ([ADR-028](DECISIONS.md) 합의 후 재정의됨, 2026-05-06): KRX 데이터는 새 topic protocol(예: `krx:usd-krw-futures`)로만 발사하고 legacy `rates` 배열에는 미포함. topic protocol v1 도입(별도 PR — Phase Z-2 영역) 완료 후 활성화. 단순 토글로 legacy `rates`에 KRX 노출은 ADR-028 위반이라 진행하지 않음.
4. **롤백**: 어느 단계에서든 KRX 장애 / KIS API 점검 / 데이터 이상 발견 시 `KRX_FUTURES_ENABLED=false`로 격리. 환경변수 변경 후 process 재생성 필요 (config는 import 시 1회 읽기, `docker compose restart`는 env_file 변경을 반영하지 않음). `docker compose up -d fastapi` 후 visible effect: `KRX_FUTURES_ENABLED=false`는 즉시 lifecycle 미시작 (수집 중단). legacy 노출은 Z-2d allowlist로 항상 0.

**현재 구현/운영 상태 (PR6 ~ PR6e)**:

- ✅ KIS WebSocket adapter (H0CFCNT0/H0CFASP0 주간, H0MFCNT0/H0MFASP0 야간) + 호가 tick fanout 차단 + PINGPONG 처리
- ✅ KisApprovalManager (approval_key cache + 만료 5분 마진 + asyncio.Lock)
- ✅ 만기 자동 resolve: `select_active_usd_futures_contract` (KIS 상품 마스터 cp949 fixed-width 파싱). **PR6c-2d-1 (2026-05-07)**: swap point를 만기일 11:30:00 → **만기일 07:00 KST**로 변경 (사용자 대표 월물 선제 전환, 거래 관행 + 06:00 boundary race 회피)
- ✅ KrxDbWriter (1초 window debounce + insert-if-changed + asyncio.to_thread DB write + race-prevention finally)
- ✅ PR6e 운영 보강: 새 WebSocket subscribe 직후 `_last_tick_at` reset으로 세션 경계 stale carry-over 차단, KRX 저장 rate는 `Decimal(...).quantize(Decimal("0.1"))`로 0.1 KRW tick 정규화
- ✅ Latest mirror skip 분기 — Z-2d allowlist 통일 후 KRX는 항상 mirror skip (legacy_policy 단일 진실 소스)
- ✅ Scheduler lifecycle: `start_krx_futures_client` / `_bootstrap_krx_futures_client` / `shutdown_krx_futures_client` (background bootstrap → main.py lifespan blocking 방지, current-task 매칭 finally cleanup)
- ✅ **PR6c-2d-1 자동 contract reconcile (2026-05-07, 5/18 임시 안전모드)**: APScheduler 5분 cron `_reconcile_krx_futures_contract`. 동작: resolve → current contract 비교 → 다르면 shutdown + start. 안전장치: 만기 차이 > 45일 점프 의심 보류 / resolve 실패 격리 / None 처리. 5/18 통과 후 hybrid (06:01 + boundary)로 축소 검토 예정 (PR6c-2d-5 후보).
- ✅ 운영 Stage 1: `KRX_FUTURES_ENABLED=true`로 KIS WebSocket → DB 저장만 활성화. legacy 노출은 Z-2d allowlist로 항상 0 (`latest:index`에 KRX key 미포함).
- 🟡 REST snapshot/fallback ([ADR-027](DECISIONS.md#adr-027-krx-미국달러선물-stage-2-진입-전-rest-snapshotfallback--stale-정책-초안)) — WebSocket 끊김 시 최신성 보전. **telemetry canary 활성화 (2026-06-07 주말)**: guard(판정+telemetry) + robust calendar 배포(`23df2d2`) + `KRX_REST_FALLBACK_ENABLED=true` + recreate (verified: health·calendar live·KRX bootstrap A75606 ERROR 0). **write/broadcast 0 (telemetry-only)** → 주말 idle. **6/8 1차 관찰(CF 08:30~15:45 + CM 18:00~, read-only 2회 + 독립 교차): stale 0 / guard 0 / fallback 0 / write 0(`fallback_last_at` null) = WS 안정 정상 결과. 4-gate는 stale에서만 evaluate → production 미실증(첫 실측은 향후 WS stale 시).** 4-gate(calendar + session + active contract + freshness → fill/exclude). **Stage C 실 반영(topic/Redis write)은 미구현** — ADR-028 topic + KRX Stage 2 선행. 5/25 교훈: close snapshot 차단(`KRX_CLOSE_REST_WRITE_ENABLED=false`) / general fallback 장중 + guard. PR6c-2d-1 자동 rollover로 만기 layer 분리.

**핵심 파일**:

- `app/sources/kis_master.py`: 상품 마스터 fetcher + ContractInfo + active contract resolver
- `app/sources/kis_futures.py`: H0CFxxx0/H0MFxxx0 column spec + payload parser + session/holiday helpers
- `app/crawlers/krx_kis.py`: KisApprovalManager + KisFuturesClient + KrxDbWriter
- `app/scheduler.py`: KRX lifecycle helpers (`resolve_active_krx_futures_contract` side-effect free + bootstrap/shutdown)
- `app/latest_rates_cache.py`: `should_include_source_in_latest()` 분기 (KRX broadcast 토글)
- `tests/test_krx_kis.py`, `tests/test_krx_scheduler.py`: 격리/회귀 검증

### Phase 3: 고급 기능 (사용자 500명+, 예정)
- 통계 & 분석 (Chart.js, 성공률 그래프)
- 환율 이상치 감지 (ML)
- 다중 알림 조건 지원


## 문서 관리 가이드라인

### 자동 Reminder (CRITICAL) 🚨

**IMPORTANT:** 다음 조건에 해당하는 코드 변경 후, **반드시** 사용자에게 질문하세요:

> "문서 업데이트 확인을 위해 `/docs-check`를 실행하시겠습니까?"

**트리거 조건:**
- `app/crawlers/*.py` 파일 수정 (크롤러 로직)
- `app/models.py`, `app/schemas.py` 수정 (DB 스키마)
- 새 기능 추가 (feat: 커밋)
- 주요 리팩토링 (refactor: 커밋)
- 새 은행/통화 추가

**사용자 응답:**
- "Yes" / "Okay" → `/docs-check` 실행
- "No" / "나중에" → 건너뜀 (명시적 선택, 기록)

**중요**: 이 Reminder는 **선택 사항이 아닙니다**. 위 조건에 해당하면 항상 물어보세요.

---

### 문서 역할 정의

이 프로젝트는 **3개의 주요 문서**로 구성되어 있으며, 각각 명확한 역할이 있습니다:

| 문서 | 역할 | 업데이트 트리거 | 예시 |
|------|------|----------------|------|
| **CLAUDE.md** | 프로젝트 전체 가이드 | 아키텍처 변경, 기술 스택 추가, DB 스키마 변경 | PostgreSQL 전환, 새 은행 추가 (개수 변경) |
| **CRAWLERS.md** | 크롤러 구현 세부사항 | 크롤링 로직 변경, 특수 로직 추가, 주의사항 발견 | SC 방식 적용, Alert 처리 변경 |
| **DECISIONS.md** | 아키텍처 의사결정 (ADR) | 기술 선택, 트레이드오프 결정 (4가지 기준 충족) | WebSocket vs Node.js, Docker 채택 |

### 문서 업데이트 기준 (간략)

**CRAWLERS.md**: 크롤러 로직 변경, 특수 로직 추가, 트러블슈팅 패턴
**DECISIONS.md**: 아키텍처 결정 (ADR 4가지 기준: 비가역성, 영향 범위, 대안 존재, 장기 유지)
**CLAUDE.md**: 은행/통화 개수 변경, DB 스키마 변경, 기술 스택 변경

**도구**: `/docs-check` - 변경된 파일 분석 후 자동으로 문서 업데이트 체크리스트 제시

---

## 향후 개선 사항

- ✅ 구조화된 로깅, 관리자 페이지, 도메인 기반 구조
- ✅ Redis 브로드캐스트 캐시, 변경 감지 시스템 (Phase 1.7)
- ✅ FCM 푸시 알림, Firebase Auth 연동 (Phase 2)
- ✅ RDS PostgreSQL 전환 (2026-01)
- ✅ iOS 앱스토어 출시 완료 (2026-01-21)
- ✅ DXY 보조지표 그래프 (Phase 1A, 2026-03-10)
- ✅ Android 출시 완료 (2026-03-13)
- ✅ 환율 뉴스 피드 — RSS + KB API 병행 수집, noise_only 24h (Phase 1B, 2026-03-28)
- ✅ Phase Z-2 series — topic protocol foundation, legacy 임시 경로 정리, USDT Redis-first 전환 ([USDT_TOPIC_MIGRATION_PLAN.md](USDT_TOPIC_MIGRATION_PLAN.md) Z-2a~Z-2e, 2026-04~05)
- ✅ ADR-029 USDT mirror skip + direct write + read-path DB fallback (2026-05-12)
- ✅ ADR-030 latest:index 책임 분리 — per-key freshness 전환 (Proposed, deployed 2026-05-13 `489359c` observing) ([DECISIONS.md ADR-030](DECISIONS.md))
- ✅ ADR-031 KRX 미국달러선물 Redis 통합 — 1차 부채 해소 (Proposed, deployed 2026-05-14 `0756329` observing) ([DECISIONS.md ADR-031](DECISIONS.md), 5/18 만기 rollover는 [KRX_CANARY.md](KRX_CANARY.md))
- ✅ KRX fanout refactor — A/B/C/A-pre 완료 (behavior-change-0), D는 Stage C와 함께 검토 ([KRX_FANOUT_REFACTOR_PLAN.md](KRX_FANOUT_REFACTOR_PLAN.md))
- ✅ Phase B.2 — Tether topic publish trigger 분리 (PR1~PR3 + PR4 Step A, [USDT_WS_DESIGN_PLAN.md §14](USDT_WS_DESIGN_PLAN.md)). `direct_coalesced` 운영 활성화 완료(2026-05-16): 비구독 guard path + iOS dev 구독 success path 검증, Upbit 실시간 체감 확인. 다음은 main.py legacy hook fallback 격하/완전 제거 조건 정리.
- ✅ KRX close snapshot 1차 PR — CF 15:45 / CM 06:00 단일가 종가 REST 보강 (`c0855ff`, [KRX_CLOSE_SNAPSHOT_PLAN.md](KRX_CLOSE_SNAPSHOT_PLAN.md)). primary target = Redis latest (DB는 best-effort history). 7일 운영 측정 후 2차 PR WS grace drain 검토 (2026-05-15)
- ✅ **KRX close finalizer 2차 작업 (Stage 1-5)** — WS-first close finalizer + REST fallback 1회 + Redis TTL captured flag race 방지 (`68b8702..c2fb796`, deploy 2026-05-17 17:07 KST). F1/F2/F3 구조적 해결 + Codex counter invariant. `KRX_CLOSE_FINALIZER_ENABLED` env default true. 첫 실측 2026-05-18 CF / 2026-05-19 CM. 7일 telemetry 후 3차 PR scope 결정 (DB unique / 종가 read API / open auction / REST close fallback **코드 완전 제거** vs 검증 가능성 재검토 [2026-05-25 정책 PR `6a43785` 이후 default off] / Stage E)
- ✅ **§12.8.3 USDT 5 source — alert coalescer + Redis freshness grain + topic trigger SET-only** (2026-05-24~25):
  - (5b-1) `PriceAlertCoalescer` 신규 모듈 (5초 wall-clock grain, A-3 패턴) + (5b-2) `alert_evaluator` window-aware functions + (5b-3a) out-of-order tick drop + (5b-3b∪4) `UsdtAlertEvaluator` composition + `triggered_rate` 통합 (`ba2fce1..0f5c3a8`)
  - (5b-bis) USDT Redis value JSON 5 fields (legacy `timestamp=seen_at` alias + 신규 `rate_changed_at` / `seen_at` / `mirrored_at`) + 옵션 A in-memory state (`_last_written_usdt_state`)로 warm same-rate/same-bucket SET 자체 SKIPPED + Redis GET 회피 (saturation 완화 핵심 효과). `93ffae0`
  - (5d-a) `UsdtLatestWriteOutcome.{FAILED, SKIPPED, SET}` enum 도입 + 5 source writer trigger 분기 SET-only — Topic = change notification 역할 (heartbeat 아님), SKIPPED는 silent. `59276af`
- ✅ **USDT legacy REST polling 비활성** — `USDT_LEGACY_REST_POLLING_ENABLED=false` default (2026-05-25, `ed0885c`). 5b/5d-a series로 WS fanout이 Redis(tick path + 5s grain coalescing) / DB(1초 window writer) / Alert(coalescer) 책임 처리 + source-specific REST fallback probe가 stale 시 동일 fanout 재사용 — 상시 polling 중복. `app/crawlers/usdt_sources.py`의 `fetch_*_usdt_tick` helper는 WS fallback이 재사용하므로 모듈 보존. supervisor 도입은 별도 후속 PR (자연 측정 데이터 기반 결정).
- ✅ **KRX 2026-05-25 휴장일 사고 + REST write source 격리 정책** (2026-05-25, `170380f` hotfix + `6a43785` 정책 PR, [DECISIONS.md ADR-027 follow-up](DECISIONS.md) / [KRX_CLOSE_SNAPSHOT_PLAN.md §5.7](KRX_CLOSE_SNAPSHOT_PLAN.md) / [KRX_CANARY.md](KRX_CANARY.md)):
  - **사고**: 부처님오신날(5/24 일) 대체공휴일 캘린더 누락 → KIS REST가 5/22 stale 종가(1516.8)를 `rt_cd=0` 정상 응답으로 반환 → DB row id=429785 + Redis latest를 5/25 15:45 KST timestamp로 잘못 기록 → 단말 노출
  - **4단계 대응**: (1) Production read-only 식별 → (2) DB row id=429785 DELETE + Redis 직전 정상값(1519.9, 5/22 야간 CM close, ts=2026-05-23T06:00+09:00) 복구 → (3) hotfix `170380f` (`KRX_2026_KNOWN_HOLIDAYS`에 5/25 추가 + 5/26 06:00 CM skip 회귀 잠금) → (4) 정책 PR `6a43785` (`KRX_CLOSE_REST_WRITE_ENABLED=false` default, `KrxCloseSnapshotController._sync_write` 단일 분기로 finalizer enabled 여부와 무관 차단, retry short-circuit, `KrxCloseWindowWriter` WS frame 신뢰 경로 유지)
  - **5 anchor 결정**: (1) KIS REST close snapshot은 더 이상 authoritative write source 아님, (2) REST 호출은 diagnostic으로 유지 + DB/Redis write는 default off, (3) Case B는 REST 성공 write가 아니라 `rest_write_blocked` diagnostic signal, (4) `KrxCloseWindowWriter` WS close frame write는 신뢰 경로 유지, (5) Stage E는 close finalizer 정책과 직교 **[(1)(2)(3)은 2026-06-10 gate-checked write 재설계로 부분 supersede — [KRX_CLOSE_SNAPSHOT_PLAN §5.7.8](KRX_CLOSE_SNAPSHOT_PLAN.md): 6/9 WS silent-stall 종가 누락(차단이 정확한 종가를 버린 반대 방향 실패) 후 flag=true 의미를 gate-checked write(calendar/contract/session evidence[tick recency 3h]/sanity 전부 통과 시에만 write + CF daily append tail)로 변경, flag=false=shadow 평가+차단. default false 유지 — env 활성화는 6/15 rollover 후 별도 GO]**
  - 신규 counter `rest_write_blocked`. 신규 7 tests (hotfix 4 + 정책 PR 3). 32 + 173 tests passed.
- ✅ **KRX 가격알림 evaluator F-1/F-2/F-3 trilogy 운영 land** (2026-05-26, [ADR-032](DECISIONS.md) / [KRX_FANOUT_REFACTOR_PLAN §5.2 F](KRX_FANOUT_REFACTOR_PLAN.md) / [KRX_CANARY F-3 섹션](KRX_CANARY.md) / [ALERT_SUBSCRIPTION_GUIDE source-based anchor](ALERT_SUBSCRIPTION_GUIDE.md)):
  - **F-1** (`e5ef42e`, default false land, 24 tests): `KrxAlertEvaluator(UsdtAlertEvaluator)` thin subclass + `KrxAlertTickHandler` payload→AlertObservation adapter + `_drain_alert_tick_handlers` boundary helper (외부 검토 #1 보강). 3 invariants 잠금: SET-only ❌ / close grace skip ❌ / session boundary drain.
  - **F-2** (`9cbd7ae`, API 허용 + helper 분리, 11 tests): `source_registry.validate_alert_source_asset` FastAPI 비의존 helper 분리 ([memory: project_main_py_helper_placement] firebase_admin import chain 우회), `category in {exchange, derivative}` 허용, KRX `phase1_enabled=True`. main.py `_validate_phase1_source_asset`는 HTTPException thin wrapper.
  - **F-3 운영 활성** (`KRX_ALERT_EVALUATOR_ENABLED=true` env, 2026-05-26 13:40 KST): iOS canary end-to-end 성공 — setting id=6 POST 13:40:06 → FCM 발사 13:40:08 (2초) → iOS 도착 (LG U+ 잠금화면 "📈 미국달러F USD-KRW-FUTURES [1504.7↑이상 도달] 1505.30") → DB triggered=true + log success + alert_evaluator 예외 0건.
  - **dead alert gap 정책**: F-2 land ~ F-3 활성 사이는 의도된 canary staging (API 등록 가능 + 발송 안 됨). 운영 단말 영향 0 (테더 탭 자체가 운영 앱에 없음, 테스트 iOS canary 전용).
  - **후속 cleanup (2026-05-26 별도 commit으로 land)**: main.py `_validate_phase1_source_asset` → `_validate_alert_source_asset_or_400` rename (의미 변화 반영) + `USDT_PHASE1_CLIENT_GUIDE.md` line 571 "category=='exchange'" stale fix.
  - **invariant 2 closed by 2026-05-27 close grace canary**: 5/27 15:45 CF close 직전 활성 알림 등록 → close finalizer 15:46:00 rate=1499.6 → alert evaluator 15:46:01 FCM sent → iOS APNs 도착 ~15:46:10 (사용자 확인 기준 ~9s 이내, 실제 도착은 그보다 빠름). 5축 통과 (close finalizer / Stage E / alert evaluator / structured event case=A / iOS). [structured event persist](DECISIONS.md) commit `18b06f5` 첫 운영 검증 동시 통과. invariant 1/3은 별도 (1번 코드 구조 보장 유지, 3번 CF→CM boundary 별도 등록 필요). 상세: [KRX_CANARY 5/27 섹션](KRX_CANARY.md).
- ✅ **Amendment 후속 — source_daily_rates canonical table 도입** (2026-05-27): v2 장기 그래프 데이터 모델 결정. **`source_daily_rates` canonical daily table 도입** (Phase 2d 우선순위 두 목적 분리 — 장기 coverage ↓ / hot path 안정화 ↑) + **`close_basis` provenance enum 5 values** (`krx_cf_close_1545` / `bithumb_24h_kst_close` / `hana_observed_eod` / `hana_official_historical_backfill` / `investing_observed_eod` [ADR-035 D1]) + **`source_method` enum 6 values** (observed_rollup / external_backfill / close_finalizer / **bithumb_candlestick_api** [Amendment 2026-06-01, 구 bithumb_candlestick_backfill] / kis_daily_backfill / krx_openapi_daily [Step 4B]) + **§7 Hana 정책 재작성** (single source 폐기 → observed_eod canonical + official_historical_backfill 분리, Investing 거부) + **외부 API hot path 제거 정책** (그래프 요청은 source_daily_rates 단일 조회). 상세: [DECISIONS.md ADR-033 Amendment 후속](DECISIONS.md) + [GRAPH_API_V2_CONTRACT.md §7 재작성](GRAPH_API_V2_CONTRACT.md).
- ✅ **ADR-034 — source_daily_rates canonical daily table 구현 설계 land** (2026-05-28, Proposed): ADR-033 Amendment 후속의 storage/operational level 분리 ADR. **schema (15 주요 필드 + invariant)** — `(source, asset, date_kst)` unique key, `rate`/`high`/`low`/`close`/`ohlc_quality` OHLC + `close_basis`/`source_method`/`contract_code`/`basis_date`/`published_at`/`captured_at`/`metadata_json` provenance + Numeric(14,6) precision. **3 column 직교 분리** — `close_basis` (의미) × `source_method` (수집) × **`ohlc_quality` top-level column** (`source_ohlc` / `observed_rollup` / `close_only`). **rate == close app-level invariant** + `rate != close` drift monitoring. **calendar-aware missing row alert** (KRX 영업일 / Bithumb 365일 / Hana 영업일 / official_backfill basis_date 기준). **source별 backfill + daily append** (Bithumb candle / KIS daily chain / Hana official + observed_eod) + idempotent upsert + metadata_json replace policy + nullable fields COALESCE. **rollout sequence** (schema → dry-run → partial backfill → 전체 backfill → daily append enable → Phase 2e endpoint switch). Open: Bithumb candle date ownership / retention 기간 / rebuild interface / ohlc_quality enum 명칭. 상세: [DECISIONS.md ADR-034](DECISIONS.md).
- ✅ **Phase 2d Step 1 land** (2026-05-28): ADR-034 §14 Rollout sequence step 1 (Schema 추가) 완료. **변경 파일**: `app/models.py` SourceDailyRate ORM class + `scripts/migrate_source_daily_rates.py` (idempotent `__table__.create(checkfirst=True)` + `--dry-run` 모드) + `app/source_daily_rates.py` helper module (get/upsert/batch_upsert/delete_range/find_rate_close_drift + Numeric→float `row_to_dict` 변환). ADR-034 Open #14 (indexes initially UNIQUE만) / #16 (Numeric(14,6) 확정) / #17 (DB CHECK 보류, app-level invariant only) Accepted 전환. 운영 영향: 코드 배포 시 `create_all()` 빈 table 자동 생성 가능 (lock 거의 없음). Production migration 적용은 별도 GO. 후속: Step 2 backfill dry-run.
- ✅ **Phase 2d Step 2 (Bithumb) land** (2026-05-28): ADR-034 §14 Rollout sequence step 2 — Bithumb 24h candlestick dry-run validator. **변경 파일**: `scripts/backfill_bithumb_source_daily_rates.py` (신규, 9 validation suite + `sys.exit(1)` on issue + Step 2/3 경계 보존 = `upsert()` 호출 없음 + 외부 의존성 0 — requests만 사용). **검증**: 전체 904 candles 0 issue, exit 0. **핵심 발견**: Bithumb 24h candle `ts_ms` KST 00:00 anchor 일관 (`date_kst` 변환 1:1) + 904일 span 누락 0 (2023-12-07 ~ 2026-05-28) + ascending sort + OCHL `[ts_ms_int, open_str, close_str, high_str, low_str, volume_str]` 실측 확정 + 단일 호출로 전체 fetch. 후속: KIS / Hana dry-run + Step 3 partial backfill (ADR-034 §14 잠정 KRX 먼저).
- ✅ **Phase 2d Step 2-2 (Hana) land** (2026-05-28): ADR-034 §14 Rollout step 2 — Hana official_historical dry-run validator. **변경 파일**: `scripts/backfill_hana_source_daily_rates.py` (신규, 10 validation suite + `[FETCH 실패]` / `[PARSE 실패]` structured output + Step 2/3 경계 보존 = `upsert()` 호출 없음 + 외부 의존성 0 — requests + bs4 이미 사용). **검증**: USD·JPY·EUR 4 case smoke 모두 0 issue, exit 0. **핵심 발견**: 휴일 fallback 정책 정확 동작 (2026-05-24 일 요청 → 2026-05-22 금 자동 fallback, `date_kst = basis_date`, `fallback=true` signal) + USD/JPY/EUR 3통화 동일 회차/timestamp 공유 (`pbldSqn=1081` + `published_at=2026-05-27 07:46:40 KST`) + `close_only` fallback path 처음 활성 (Bithumb은 `source_ohlc`만 cover) + `published_at` top-level KST timezone-aware datetime 첫 활성 검증. GRAPH_API_V2_CONTRACT.md §7 line 284 검증 사례 1차 fetch부터 100% 재현. 후속: KIS daily chain dry-run (Step 2-3) + Step 3 partial backfill.
- ✅ **Phase 2d Step 2-3 (KIS) land** (2026-05-28): ADR-034 §14 Rollout step 2 — KIS daily chain dry-run validator. **Step 2 영역 closed** — Bithumb·Hana·KIS 3-source dry-run 모두 완료. **변경 파일**: `scripts/backfill_kis_source_daily_rates.py` (신규, 13 validation suite + 2 mode `dynamic`/`--known-boundary-smoke` + structured failure `[CONFIG/TOKEN/MASTER/FETCH/PARSE 실패]` + Step 2/3 경계 보존 = `upsert()` 호출 없음 + 외부 의존성 0 — requests + dotenv + 운영 `KisAccessTokenManager` 재사용으로 drift 회피). **검증**: 두 모드 모두 mapped 25 / probe 1, exit 0. **핵심 발견**: dynamic mode가 GRAPH §7-new anchor 자동 재현 (운영 영속성) + user-facing chain mapping `[previous.expiry, this.expiry)` 반열린 구간 + 2026-05-18 row → A75606 contract close=1496.500 (GRAPH §7-new B-B probe 사례 100% 재현) + today 제외 default (`--include-today` opt, intraday close 오염 회피) + mapped/probe 분리 (probe out-of-range는 WARNING surface only, KIS stale 동작 known) + KIS 1일 1회 발급 원칙 → shared cache (`KisAccessTokenManager` 재사용) + `_build_before_previous_dynamic` (previous 사용 시작 anchor, SAFE_FETCH_DAYS 임의 range 폐기). 보정: Codex 6 review rounds (외부 의존성 / token cache / chain B / 사용 구간 정정 / 4 Blocker + 2 Non-blocker / before_previous anchor). 후속: **Step 3 partial backfill 실측 적재** (ADR-034 §14 §3 잠정 KRX 먼저, service value 기준).
- ✅ **Phase 2d Step 3 first land (KRX writer)** (2026-05-28, **Stage 1+2+3 모두 land 완료** — production RDS 7 rows + boundary close=1496.500 production-level 재현): ADR-034 §14 Rollout step 3 first PR — KRX current contract (A75606) partial backfill writer. **변경 파일**: `scripts/backfill_kis_source_daily_rates.py` 확장 (`--write` default off + `--start-date` + `--end-date` + `--allow-production-write` default off + `check_production_write_guard` + `validate_write_range` + `ensure_source_daily_rates_table_created` + `write_with_transaction`). **First PR scope (옵션 A)**: KRX current A75606만 / `[2026-05-18, 2026-05-27]` / 7 영업일. **Production guard (Round 8 early call)**: main 진입 직후 + KIS API call (token/master/daily fetch) 모두 전 — production .env에서 `--write` 잘못 실행 시 **output 1 line + exit 1, KIS API call 0** (이전 사고 시 104 lines + token rotation 위험). dialect/host redacted (CLAUDE.md 보안 원칙). **Transaction pattern (Round 7)**: `upsert(commit=False)` loop + post-write SELECT with **date range filter** + 7 validations (exact count `==`, `expected_dates set == written_dates set`, invariant, contract_code, basis_date/published_at IS NULL, duplicates, enum/literal, boundary sample 1496.500). 검증 통과 → commit / 실패 → rollback (atomic). **Local SQLite smoke**: 7 rows committed + idempotent rerun 통과 + DB query 직접 검증 — boundary close=1496.500 (GRAPH §7-new B-B probe DB level 재현) + rate==close + basis_date/published_at IS NULL + enum/literal 모두 정확. **Stage 분리**: Stage 1+2+3 모두 land 완료 (Stage 3 production RDS write 2026-05-28 KST 18:30 — RDS manual snapshot `fxi-pre-step3-2026-05-28` 직후, `--allow-production-write` 적용, **production verify: drift 0 / range 7 rows / contract A75606 단일 / boundary close=1496.500 (GRAPH §7-new B-B probe 사례 production-level 재현) / rate==close + basis_date/published_at IS NULL + enum/literal 모두 정확**). 운영 fastapi runtime 갱신은 Phase 2e endpoint switch 시점 별 배포 영역 (현재 source_daily_rates read 호출자 0 — user-facing 영향 0). **Rollback anchor**: `delete_range(db, "krx", "usd-krw-futures", date(2026,5,18), date(2026,5,27))`. **DB read 호출자 0** (v2 endpoint Phase 2e 미진입 — user-facing 영향 0). 후속: Step 3 옵션 B (chain previous + current 확장) → Hana/Bithumb Step 3 → Step 4 full backfill → Step 5 daily append → Step 6 v2 endpoint switch.
- ✅ **Phase 2d Step 3 옵션 B Round 9 land** (2026-05-28, **Stage 1+2+3 모두 land 완료** — production RDS A75605 18 rows + 전체 chain 25 rows + boundary close=1496.500 유지): ADR-034 §14 Rollout step 3 옵션 B — KRX chain previous + current 확장 (first PR의 current 한정 → previous A75605 + current A75606 chain single transaction 적재 지원). **변경 파일**: `scripts/backfill_kis_source_daily_rates.py` 확장 — `--contract current|previous|both` (default current, 후방 호환) + `validate_write_range(chain_start_expiry, chain_end_expiry, chain_label)` multi-contract 시그니처 + `write_with_transaction(contract_codes: set[str])` 시그니처 확장 + post-write SELECT `contract_code.in_()` + range filter + `expected (date_kst, contract_code) pairs set` 강화 검증 (multi-contract 혼입/누락 차단) + help 문구 polish. **Chain 사용 구간 (mode별)**: current=`[previous.expiry, current.expiry)` / previous=`[before_previous.expiry, previous.expiry)` / both=`[before_previous.expiry, current.expiry)`. **Local SQLite smoke 5단계 모두 PASS**: dry-run regression / previous 18 rows / both 25 rows (18 prev + 7 cur single transaction) / idempotent rerun / cross-mode regression — drift 0 + boundary 2026-05-18 → A75606 close=1496.500 both mode 재현. **Codex Round 9 Blocker 0** + Non-blocker (help 문구) commit 전 정정. **Stage 분리**: Stage 1+2+3 모두 land 완료 (Stage 3 production write `--contract previous` only — current 7 rows 재-upsert 회피, manual snapshot 생략 — 옵션 B는 incremental write라 과보호, 자동 backup 1 Day + `delete_range` rollback anchor 신뢰. **production verify: 18 rows committed (A75605 [2026-04-20 ~ 2026-05-15]) + 7 post-write validations passed + drift 0 + current A75606 7 rows 영향 0 (cross-mode regression) + 전체 chain 25 rows + boundary 2026-05-18 → A75606 close=1496.500 유지**). 운영 fastapi runtime 갱신은 Phase 2e endpoint switch 시점 별 배포 영역 (현재 source_daily_rates read 호출자 0 — user-facing 영향 0). **Rollback anchor**: `delete_range(db, "krx", "usd-krw-futures", date(2026,4,20), date(2026,5,17))` (previous only). 후속: Hana/Bithumb Step 3 → Step 4 full backfill → Step 5 daily append → Step 6 v2 endpoint switch.
- ✅ **Phase 2d Step 3 Hana first PR Round 1 land** (2026-05-28, **Stage 1+2+3 모두 land 완료** — production RDS Hana USD 4 rows + close_only path production-level 첫 활성 + drift 0): ADR-034 §14 Rollout step 3 — Hana first PR. KRX Step 3 옵션 B land 후 다음 source 다각화. **schema의 미검증 path 처음 production-level 활성** (close_only fallback + basis_date NOT NULL + published_at NOT NULL + metadata_json.pbldSqn provenance). **변경 파일**: `scripts/backfill_hana_source_daily_rates.py` 확장 — `--write` / `--start-date` / `--end-date` / `--include-today` / `--allow-production-write` + `check_production_write_guard` (KIS writer 패턴 재사용, dialect/host redacted) + `validate_write_range` (Hana는 contract chain 없음 → 단순 range + today 가드) + `ensure_source_daily_rates_table_created` (idempotent) + `fetch_and_dedup_calendar_range` (**Hana-specific**: calendar-day loop + basis_date dedup + fallback events) + `write_with_transaction_hana` (Hana 방향 metadata policy — contract_code IS NULL / basis_date+published_at+pbldSqn NOT NULL / close_only fallback). **First PR scope (옵션 A)**: USD only / `[2026-05-21, 2026-05-27]` / calendar 7일 → unique 4 rows (휴일 dedup). **Codex Round 1 Blocker 정정**: post-write SELECT `date_kst >= min AND date_kst <= max` (range query) → **`date_kst.in_(expected_dates)`** (Hana calendar-day dedup 비연속 expected_dates 휴일 gap 안 false failure 차단 — KRX Round 7 Blocker 2와 동등 패턴). **권장 보완**: Rollback anchor `delete_range` → **expected_dates 개별 `date_kst.in_()` delete snippet** (range delete의 휴일 gap 안 기존 row 영향 risk 회피). **Local SQLite smoke 8단계 모두 PASS**: py_compile / dry-run regression / production guard 차단 / first run 4 rows / **Codex Blocker 검증 (gap fake row 3개 삽입 후 재실행 — exit 0)** / cleanup + clean state final / idempotent rerun / DB query 직접 검증 (drift 0 / dates [2026-05-21, 2026-05-22, 2026-05-26, 2026-05-27]). **핵심 발견**: 휴일 fallback + dedup 정확 (5/23 토 + 5/24 일 + **5/25 월 대체공휴일** 모두 5/22 fallback, KRX_CANARY 2026-05-25 사고 캘린더와 일관) + close_only path 첫 production-level 활성 + pbldSqn 각 row 별 다른 회차 (1356/2513/1081/1271) + published_at KST timezone-aware (다음날 발표). **Hana writer vs KRX writer 패턴 차이**: KRX는 contract chain fetch (1 call → 다수 row) + contract_code.in_() filter, Hana는 calendar-day loop (1 call → 1 row) + basis_date dedup + date_kst.in_(expected_dates) filter. **Stage 분리**: Stage 1+2+3 모두 land 완료 (Stage 3 production write 2026-05-28 KST — manual snapshot 생략, Hana incremental write 자동 backup + 개별 dates delete rollback anchor 신뢰. `--currency USD --start-date 2026-05-21 --end-date 2026-05-27 --allow-production-write`. **production verify: 4 rows committed (Hana usd-krw [2026-05-21, 2026-05-22, 2026-05-26, 2026-05-27]) + post-write validations passed + drift 0 + 휴일 dedup 정확 (5/23/24/25 → 5/22 fallback) + close_only path production-level 첫 활성 + pbldSqn 각 row별 다른 회차 1356/2513/1081/1271**). 전체 production source_daily_rates state: KRX 25 rows + Hana 4 rows + Bithumb 0 rows. 운영 fastapi runtime 갱신은 Phase 2e endpoint switch 시점 별 배포 영역. **Rollback anchor** (production-safe, 비연속 expected_dates 개별 삭제): `db.query(SourceDailyRate).filter(source=='hana', asset=='usd-krw', date_kst.in_([date(2026,5,21), date(2026,5,22), date(2026,5,26), date(2026,5,27)])).delete()`. 후속: Hana JPY/EUR 진입 → Bithumb Step 3 first PR → Step 4 full backfill → Step 5 daily append → Step 6 v2 endpoint switch.
- ✅ **Phase 2d Step 3 Bithumb first PR Round 1 land** (2026-05-29, **Stage 1+2+3 모두 land 완료** — production RDS Bithumb USDT/KRW 7 rows + 모든 top-level metadata None path production-level 첫 활성 + all candle_ts_kst present + drift 0 / **ADR-034 §3 schema 3 직교 path 모두 production 활성 완료** — KRX/Hana/Bithumb): ADR-034 §14 Rollout step 3 — Bithumb first PR. KRX Step 3 옵션 B + Hana first PR Round 1 land 후 source diversity 마지막 핵심 path 진입. **schema의 모든 top-level metadata None path 첫 production-level 활성 준비** (contract_code=basis_date=published_at=None — KRX는 contract_code 필수 + Hana는 basis_date+published_at+pbldSqn 필수와 모두 다른 가장 단순 row mapping) + **candle_ts_ms/candle_ts_kst metadata_json 격리** (KST 00:00 anchor 정책). **변경 파일**: `scripts/backfill_bithumb_source_daily_rates.py` 확장 — `--write` / `--start-date` / `--end-date` / `--include-today` / `--allow-production-write` + `check_production_write_guard` (KRX/Hana writer 패턴 재사용) + `validate_write_range` (Bithumb 24/7 거래 → 단순 range + today 가드, 휴일 처리 없음) + `ensure_source_daily_rates_table_created` (idempotent) + `write_with_transaction_bithumb` (Bithumb 방향 metadata policy — 모든 top-level metadata None + candle_ts_ms+candle_ts_kst metadata_json 격리). **First PR scope (옵션 A)**: USDT/KRW only / `[2026-05-21, 2026-05-27]` / calendar 7일 → expected 7 rows (24/7 거래 — 휴일 dedup 없음). **Codex Round 1 Blocker 정정**: `--write + --limit` 동시 사용 시 hard reject (early check, fetch 전, partial write 위험 차단). **Non-blocker 2 정정**: post-write `metadata_json.candle_ts_kst` validation 추가 (KST 00:00 anchor 정책 핵심) + 연속성 회귀 가드 `expected_count == (end-start)+1` 명시 검증 + cleanup `InvalidOperation` unused import 제거. **Local SQLite smoke 6단계 모두 PASS**: py_compile / dry-run regression / production guard 차단 / **--write+--limit hard reject 검증** / cleanup + first run 7 rows + post-write validations passed / DB query (all candle_ts_kst present + drift 0 + 모든 top-level metadata None) + idempotent rerun. **Bithumb writer vs KRX/Hana writer 패턴 차이**: Fetch 단위 — KRX contract chain (3 calls 다수 row) / Hana calendar-day (N calls 1 row each) / **Bithumb 단일 fetch (1 call 전체 candles) + date range filter**. Dedup — KRX contract_code IN / Hana basis_date / **Bithumb 없음 (24/7 연속)**. Metadata policy — KRX contract_code NOT NULL / Hana basis_date+published_at+pbldSqn NOT NULL / **Bithumb 모든 top-level metadata None + candle ts metadata_json 격리**. **Stage 분리**: Stage 1+2+3 모두 land 완료 (Stage 3 production write 2026-05-29 KST — manual snapshot 생략, Bithumb incremental write 자동 backup + `delete_range` rollback anchor 신뢰, 24/7 연속이라 range delete 안전 — KRX/Hana 비연속과 다름. `--start-date 2026-05-21 --end-date 2026-05-27 --allow-production-write`. **production verify: 7 rows committed (Bithumb usdt-krw [2026-05-21, 2026-05-27] 연속) + post-write validations passed + drift 0 + 모든 top-level metadata None (contract_code/basis_date/published_at) + all candle_ts_kst present (KST 00:00 anchor 격리) + close_basis=bithumb_24h_kst_close + source_method=bithumb_candlestick_backfill + ohlc_quality=source_ohlc**). **전체 production source_daily_rates state: KRX 25 + Hana 4 + Bithumb 7 = total 36 rows**. **ADR-034 §3 schema 3 직교 path 모두 production-level 활성 완료**: KRX (source_ohlc + contract_code + chain rollover) / Hana (close_only + basis_date+published_at+pbldSqn) / Bithumb (source_ohlc + 모든 top-level None + candle metadata_json). 운영 fastapi runtime 갱신은 Phase 2e endpoint switch 시점 별 배포 영역. **Rollback anchor**: `delete_range(db, "bithumb", "usdt-krw", date(2026,5,21), date(2026,5,27))` (24/7 연속이라 range delete 안전). 후속: Hana JPY/EUR 다각화 → Step 4 full backfill → Step 5 daily append → Step 6 v2 endpoint switch.
- ✅ **Phase 2d Step 5 daily append minimum first PR land (Bithumb only)** (2026-05-29, **Stage 1+2+3 모두 land 완료** — manual smoke production write (Bithumb 2026-05-28 1 row) + OS crontab 등록 (UTC 15:01 = KST 00:01 매일) + 7일 안정 운영 모니터링 진입): ADR-034 §14 Rollout step 5 daily append job **minimum viable first PR**. KRX/Hana/Bithumb 3 source partial backfill production land 후 ongoing freshness 확보 단계 진입. **Scope 분리 정책 (Codex 합의)**: 첫 PR = Bithumb only / Hana observed_eod 별 PR (사전 read 5 항목 — bank_exchange_rates schema / Hana source naming / 전일 last 추출 기준 / source_method enum / missing day) — 기존 official endpoint writer 재사용 금지 (schema 위반 risk) / KRX 별 PR (close finalizer 통합, CF 15:45 KST 종가 직후 trigger). **변경 파일**: `scripts/daily_append_source_daily_rates.py` 신규 orchestrator — 기존 backfill writer subprocess 호출 (신규 write logic 0) + argparse `--source bithumb` (choices 잠금) + `--date YYYY-MM-DD` (default yesterday KST) + `--write` (default dry-run = 3 command preview only, subprocess 실행 X) + `--allow-production-write` (writer에 forward) + subprocess timeout 180s + cwd=REPO_ROOT (production cron robustness) + script_path .resolve() (cron 환경 절대경로 보장). **Codex Round 1 Blocker 정정**: parse_row_count `Optional[int]` (regex 매칭 실패 None과 0 명확 구분) + `EXPECTED_ROWS_PER_SOURCE = {"bithumb": 1}` + summary `returncode == 0 AND rows == expected_rows` **양방향 검증** → daily append freshness silent failure 차단 (writer stdout 포맷 변경/capture 깨짐 시 exit 0 + parse 실패 → FAIL 표기). **Local SQLite smoke 5단계 모두 PASS**: py_compile / dry-run preview (3 command — validation/local write/production write) / write mode 1 row committed PASS / silent failure 시뮬 (--date 2020-01-01 → exit 1 FAIL 정확 표기) / argparse choices=["bithumb"] 차단 (--source hana invalid). **Stage 3 production verify**: manual smoke 2026-05-28 Bithumb 1 row committed (`bithumb: PASS (1 rows committed, expected=1, exit=0)`) + post-write validations passed + drift 0 + candle_ts_kst=2026-05-28T00:00:00+09:00 KST anchor 격리 + 전체 Bithumb rows 8 (7 backfill + 1 daily append). **OS crontab 등록 완료**: `1 15 * * * cd ~/exchange-rate && /usr/bin/docker compose run --rm fastapi python scripts/daily_append_source_daily_rates.py --write --allow-production-write >> ~/logs/daily_append.log 2>&1` (UTC 15:01 = KST 00:01 매일) — 기존 `monday_reopen_exec.sh` cron 보존 + daily append line 추가, `/usr/bin/docker` 절대경로 (cron PATH 회피) + `~/logs/daily_append.log` 로그 destination. cron 첫 발화 예정: 2026-05-29 UTC 15:01. **운영 fastapi runtime 미교체** — APScheduler in-process schedule과 별 영역 (OS cron이 `docker compose run --rm` 새 one-shot container 띄움, 운영 fastapi process 영향 0). **전체 production source_daily_rates state**: KRX 25 + Hana 4 + Bithumb 8 = total 37 rows. 후속: 7일 안정 운영 모니터링 → Hana observed_eod 별 PR (사전 read 5 항목) → KRX close finalizer 통합 별 PR → retry/backoff/structured error/alert → Step 4 full backfill → Step 6 v2 endpoint switch (Phase 2e + 장기 그래프 DB 표준화 ADR-035).
- ✅ **Phase 2d Step 3 Hana observed_eod first PR Round 1 land** (2026-05-31, **Stage 1+2+3 모두 land 완료** — production RDS write Hana usd-krw 2026-05-28 1 row + 이중 독립 verify drift 0 + overlap 0): ADR-034 §14 Rollout step 3 — **Hana observed_eod 별 PR** (Step 5 daily append에서 분리된 canonical append path). 기존 `backfill_hana_source_daily_rates.py` (official_historical_backfill)와 **별 path** — 외부 endpoint 미사용, 우리 DB `bank_exchange_rates` 내부 관측값 read. **변경 파일 (둘 다 신규)**: `scripts/backfill_hana_observed_eod_source_daily_rates.py` (observed_eod writer — dry-run/write/production guard 4 anchor 재사용 + KST→UTC naive boundary + carry-in baseline rollup) + `tests/test_hana_observed_eod_writer.py` (영구 회귀 16 test, in-memory SQLite + patch 주입). **핵심 설계 (Codex 8 round 수렴)**: bank_exchange_rates = change-only table (rate 변경 시에만 INSERT, 하루 불변이면 row 0개 가능) → **liveness gate ⊥ baseline quality 직교 분리** (changes 존재 = write gate / prev 신선도(age<=7d) = high·low rollup 포함 여부만, write gate 아님). rollup = ([prev] if baseline_ok else []) + changes, close=rate=changes[-1] (invariant), high·low = rollup max·min. **case 매트릭스**: 평일/주말 changes>=1 → write / 평일 changes==0 → skip_error (exit 1, `weekday_no_changes`) / 주말 changes==0 → skip_ok (exit 0, `weekend_no_changes`) [PR A1에서 calendar 통합으로 superseded — 영업일 무변동=`business_day_no_changes` skip_error / 공휴일 무변동=`holiday_no_changes` skip_ok 추가]. nullable contract_code/basis_date/published_at 모두 None (observed_eod 방향). close_basis=hana_observed_eod / source_method+ohlc_quality=observed_rollup. **write-path Blocker 2 (Codex temp SQLite 재현 + 자기 코드 trace 확인)**: B1 range atomicity (실패 range 부분 commit → skip_error 시 transaction 전 abort, all-or-nothing) + B2 write-path validation 우회 (음수/precision raw commit → DRY_RUN_CHECKS를 transaction 전 적용) + N1 metadata fail-closed + N2 dry-run 이중 query 정리. **영구 test 16 PASS** (process_date 6 / write_transaction 4 incl **official overlap rollback 안전망** + metadata fail-closed / `_run_write` 3 / production guard 3) + /tmp smoke 11 case 58 check. **overlap 안전망 실측**: official row와 overlap 시 upsert nullable-preserve로 basis_date/published_at leftover → post-write nullable validation fail → rollback → official row 보존 (corruption 차단, ADR-034 §10 Open 전까지 자동 차단). **First PR scope**: USD only / **2026-05-28 단일 row** (official 4 rows `[5/21,22,26,27]`와 overlap 0). **Phase 1-3 production read-only query** (2026-05-31 EC2 `docker compose run`): change_rows=1096 (정상 평일) + prev age 254.6s fresh + source_daily_rates existing row=0 → 예상 row close=rate=1496.1 / high=1510.8 / low=1494.3 / rollup_point_count=1097. **Stage 3 선행 필수**: push 후 EC2 `git pull` + `docker compose build fastapi` (one-shot 이미지에 신규 writer 반영 — scripts/는 image COPY, live container recreate 불필요). 후속: Hana JPY/EUR 다각화 → orchestrator `--source` 확장 + Hana daily append cron (KST 00:01) → holiday calendar + heartbeat → Step 4 full backfill → Step 6 v2 endpoint switch. **[후속 진행: holiday calendar=PR A1 완료 / orchestrator `--source all` + production cron 교체=PR A2 완료 (deploy 2026-06-01: pre-smoke PASS + cron `--source all` 교체 / 운영 closure 2026-06-02 00:01 KST: 통합 관찰 41→43)]** 상세: [DECISIONS.md ADR-034 §14](DECISIONS.md).
- ✅ **Phase 2d PR A1 — Hana daily append 자동화 foundation (calendar + JSON verdict 계약)** (2026-05-31, **Stage 1+2+3 모두 land 완료** — commit `a7b128d` + push + EC2 배포 + in-image holidays/fixture 검증 + Bithumb JSON manual smoke PASS, **첫 JSON cron 발화 PASS** — 6/1 00:01 KST cron이 Bithumb 5/31 신규 row JSON 경로 적재 40→41, 운영 검증 완전 closure): ADR-034 §14 step 5 후속. cron line 변경 없이 writer verdict 계약 + 한국 공휴일 calendar 도입 (운영 cron `--source all` 전환은 **PR A2**). **배경**: 기존 orchestrator는 `parse_row_count` regex + Bithumb 고정 1-row 가정 → Hana는 주말·공휴일 0 row 정상이라 통합 불가 → source-aware JSON verdict 필요 + 기존 Hana writer 공휴일을 평일 오판 → 공휴일 calendar 필요. **변경 파일**: `app/calendars/{__init__,kr_holidays,hana_business_days}.py` 신규 (`holidays.SouthKorea(categories=(PUBLIC,BANK), observed=True)` + `classify_hana_calendar_day` 단일 진실 소스 weekend>holiday>business_day) + `app/daily_append_verdict.py` 신규 (verdict 계약 공유 소스) + Hana/Bithumb writer `--emit-daily-append-verdict` + orchestrator regex→JSON fail-closed + requirements holidays 추가. **verdict 계약**: `DAILY_APPEND_VERDICT_JSON={version,source,asset,date_kst,status,reason,rows}` (write→written/skip_ok→skipped/skip_error→error). **source-aware policy**: bithumb(24/7)=written only/usdt-krw, hana=written|skipped/usd-krw. **holiday calendar**: **observed=True load-bearing** (observed=False면 5/25 대체공휴일 drop — 실측) + PUBLIC∪BANK(근로자의날 5/1 BANK 전용) + 12/31 한국 공휴일 아님(사용자 정정) + override hook 미도입(YAGNI). **case 갱신**: 공휴일 changes==0 → skip_ok(holiday_no_changes) 신규 / 평일·비공휴일 changes==0 → skip_error(business_day_no_changes). **Codex adversarial Blocker 4 + NB (재현 확인 후 fix)**: B1 non-object JSON crash→FAIL(isinstance dict) / B2 asset 미검증→source별 asset 검증 / B3 Bithumb skipped 허용→source-aware allowed_statuses / B4 bool·str rows(True==1)→type(rows) is int / NB written→reason None. **Local test 59 PASS** (calendar 9 + Hana writer 22 + orchestrator 28 incl 재현 6 + CLI guard subprocess 4) + Bithumb end-to-end PASS. **Stage 3 선행**: EC2 `docker compose build fastapi` + **in-image `holidays.__version__=="0.97"` + fixture 확인** + Bithumb JSON 경로 manual smoke. 상세: [DECISIONS.md ADR-034 §14 PR A1](DECISIONS.md).
- ✅ **Phase 2d Bithumb provenance Amendment — source_method rename (`bithumb_candlestick_backfill` → `bithumb_candlestick_api`)** (2026-06-01, **Stage 1+2+3 모두 land 완료** — production RDS 11 rows surgical rename old 0→new 11 + drift 0 / **다음 cron 적재 검증 완료** — 2026-06-02 00:01 KST 발화): ADR-034 §6/§7/§9 Amendment. PR A1 cron 검증 중 발견 — ADR §7/§9는 Bithumb daily append=`observed_rollup`(DB rollup) 규정이나 구현은 candlestick backfill writer 재사용 → backfill·append 모두 **공식 24h candle API**. 데이터 정확(candle 실 OHLC가 DB rollup보다 우월), **label만 부정확**. **결정 (옵션 a, Codex 7 round)**: candlestick canonical 인정 + `source_method` 단일 값 **`bithumb_candlestick_api`** (방법=획득방식 → backfill/daily는 timing이지 방법 아님) + `ohlc_quality=source_ohlc`·`close_basis=bithumb_24h_kst_close` 유지 + **`ingest_mode` 미도입**(YAGNI — metadata upsert replace 소실 위험). **변경**: Bithumb writer `SOURCE_METHOD` 상수(build+validator 공유) + docstring + `models.py` docstring + DECISIONS §6/§7/§9 + GRAPH §6/§7 + CLAUDE amend (과거 history rewrite ❌, Amendment supersede) + `migrate_bithumb_source_method.py` 신규 + 회귀 test 13 (Codex 7종 + 추가 3 + fail-open count 방어 3). **migration 계약**: FOR UPDATE lock → count 재확인(`--expected-old N --expected-new M`) → snapshot → source_method만 UPDATE → **all-cols surgical 불변 검증** → post-verify(old_after=0/new_after=M+N/total 불변) → idempotent(old=0 AND new=M+N skip / 아니면 stale abort). **배포 순서**: 신코드 배포 후(cron이 new 기록) → cron 15:01 UTC 회피 + one-shot 미실행 확인 → pre-query → migration → post-query → 다음 cron (운영 race 절차 차단 — FOR UPDATE는 신규 insert 못 막음). **가격/OHLC/metadata/captured_at 변경 0 — provenance source_method 문자열만 UPDATE**. **production verify (2026-06-01)**: 11 rows old 0/new 11, source_method 외 전 컬럼 **in-transaction surgical 불변 검증 통과** (commit 자체가 11행 before/after 일치 증거) + SSH read-only 독립 전수 (Codex) **drift 0 / OHLC(low≤close≤high) / nullable / candle_ts 전수 정상** (= committed after-state 확인). **다음 cron 신규 append가 api로 적재되는지 검증 완료 (2026-06-02 00:01 KST 발화 — Bithumb 2026-06-01 row source_method=bithumb_candlestick_api, A2 --source all 통합 관찰)**. non-urgent — A2 전 provenance 정리로 Hana(observed_rollup)/Bithumb(candlestick_api) 수집방식 분리 명확. 상세: [DECISIONS.md Bithumb provenance Amendment](DECISIONS.md).
- ✅ **Phase 2d PR A2 — daily append orchestrator `--source all` (bithumb + hana) + per-source 격리** (deploy 2026-06-01 / 통합 관찰 closure 2026-06-02 00:01 KST, **Stage 1+2+3 모두 land 완료** — pre-smoke PASS + cron `--source all` 교체 + 통합 관찰 41→43): ADR-034 §14 step 5 후속. 기존 orchestrator(Bithumb only)를 bithumb+hana 다중 source로 확장 (KRX는 close finalizer 별 PR). **변경 (scripts/daily_append_source_daily_rates.py)**: `SUPPORTED_SOURCES`=["bithumb","hana","all"] + `SOURCE_RUN_ALL`=("bithumb","hana") tuple + `resolve_sources()` / `build_hana_command` (observed_eod CLI quirk: write→start/end+emit, dry-run→--date) / `execute_source` helper + main loop per-source `except Exception` 격리 (synthetic FAIL + continue, BaseException 금지) + `_REQUIRED_RESULT_KEYS` shape 검증 (malformed result[None/키 누락]→synthetic FAIL) / CLI default "bithumb" 유지 (후방 호환) / source-aware dry-run preview. **per-source 독립**: 각 source 별 subprocess + commit (cross-source transaction 아님, 부분 성공 의도, re-run idempotent upsert). test 14 (→ orchestrator 42 PASS). **Codex 7 round** (설계 4 + 구현 2 + 문서 정합 1) — 격리 경계 / cron history 보존 / `_REQUIRED_RESULT_KEYS` shape 검증 / 통합 관찰 정책. **운영 검증** (2026-06-02 00:01 KST = 6/1 15:01 UTC 통합 발화): 두 source 독립 실행·commit **실증**(성공 경로) — Bithumb 2026-06-01 `bithumb_candlestick_api` + Hana 2026-06-01 `observed_rollup`, total 41→43; **실패 격리(Hana 실패 시 Bithumb 보존)는 영구 test 검증 — 이번 발화 실패 0이라 운영 미발동**. cron `1 15 * * * --source all` 활성. Bithumb amendment 검증도 동시 closure. 범위 밖: KRX close finalizer 통합 / Hana JPY·EUR / retry·backoff. 상세: [DECISIONS.md ADR-034 §14](DECISIONS.md).
- ✅ **Phase 2d Step 4A Stage 1 land — Hana official overlap guard + write-path self fail-close + range dry-run gate** (2026-06-02) [historical candidate anchor — 아래 Step 4A Hana 완료 anchor에서 superseded]: Step 4 full backfill을 **4A**(기존 writer 즉시 — Bithumb 1년 + Hana official 1년)/**4B**(KRX 12-month contract-chain 구현)로 분리. **Stage 4A-0 gap repair 완료**(production, 코드 변경 0 — 기존 observed_eod writer 실행): A2 cron 교체 전까지 Hana daily 미포함이라 누락된 Hana observed_eod 2026-05-29(Fri)·05-30(Sat) 적재(retained raw 1048/416) → **total 43→45 / Hana 6→8 / drift 0 / 5-31(Sun) weekend skip 유지** (official 4 rows[5/21~27] 불변). **Stage 1 코드** (`scripts/backfill_hana_source_daily_rates.py` + `app/source_daily_rates.py`): **overlap guard**(write_with_transaction_hana pre-write — expected_dates IN + FOR UPDATE + `close_basis != official` reject → official이 observed_eod canonical 안 덮음, 관측 안전망 대칭) + **conditional conflict update**(공용 upsert `update_only_if_existing_close_basis` optional, 기본 None 후방 호환 — absent-key concurrent observed insert도 skip→rollback, PG·SQLite compile 확인) + **HanaWriteOutcome**(inserted/updated 구분, rollback inserted-only — official one-shot 단독 전제) + **`--require-empty-target`**(gap-only 강제) + **`--range-dry-run`** DB-free gate(validate_write_range + empty fail-close) + **`_validate_cli_combo`**(모호 조합 한 곳 fail-close) + **미래 date reject**(include-today로도, backfill 과거만) + **write-path pre-write `_ROW_DRY_RUN_CHECKS`**(음수/precision/published_at KST 값 validator를 transaction 전 atomic 차단 — dry-run 생략한 직접 --write도 self fail-close). **Codex 6 round**(race/reverse/future/CLI combo/write-path bypass 엣지 전수). **runbook**: --range-dry-run → 출력 dedup expected_dates(휴일 fallback로 request range ≠ 실제 date_kst) 확정 → 그 dates DB read-only target 0 → official one-shot 단독 `--write --require-empty-target --allow-production-write` → post-verify + inserted-only rollback. **Stage 분리**: Stage 4A-0(production 완료) / Stage 1(코드 land) / **1년 backfill 실행은 아래 Step 4A 완료 anchor에서 closure** [⚠️ monthly batch 표현은 single full-range로 **superseded** — 월 경계 휴일 fallback 충돌로 monthly 폐기, 아래 Step 4A Bithumb anchor + Hana 보강 PR 참조]. test 32 + 관련 회귀 77 = **109 passed**. 경계 전환 정책(ADR-034 §10 Open)은 미결정 — 결정 전까지 overlap fail-close. 상세: [DECISIONS.md ADR-034 §14](DECISIONS.md).
- ✅ **Phase 2d Step 4A Bithumb 1년 backfill 완료 (Stage 3 production)** (2026-06-02): ADR-034 §14 Step 4 full backfill의 **4A Bithumb 부분** — USDT/KRW 1년 gap-only backfill production 적재. **single full-range write** (Bithumb은 단일 API fetch라 monthly 분할 자체가 불필요). **절차**: RDS manual snapshot `fxi-pre-step4a-bithumb-2026-06-02` (`available` 확인) → write 직전 candidate `[2025-06-02, 2026-05-20]` overlap COUNT=0 **재확인**(point-in-time gate) → `--write --start-date 2025-06-02 --end-date 2026-05-20 --allow-production-write` → 즉시 post-verify. **stdin 함정 1건 발생·해소**: 원격 `bash -s` heredoc 안 `docker compose run -T`가 뒤쪽 shell 명령을 stdin 소비 → write 미실행(DB 12 유지로 감지) → 파일 기반 명령에 `</dev/null` 추가 재실행, 중복/부분 적재 0. (단 `python - <<'PY'` inline 전달엔 `</dev/null` 금지 — 코드 본문이 stdin. `python -c`/파일 script에만 적용. write 경로 파이프 회피 `> log 2>&1; rc=$?`로 원본 exit code 보존. 향후 bulk 감사 로그는 `/tmp` 아닌 `~/logs/`에 보존.) **결과**: 신규 **353 rows** → Bithumb 12→**365** (연속 [2025-06-02, 2026-06-01]), 전체 source_daily_rates **398** (Bithumb 365 + Hana 8 + KRX 25). **이중 독립 검증** (Claude post-verify + Codex production read-only): date gaps 0 / drift(rate≠close) 0 / OHLC 위반 0 / nullable metadata 0 / candle_ts_kst 누락 0 / close_basis=bithumb_24h_kst_close ×365 / source_method=**bithumb_candlestick_api** ×365 (amendment 반영). **rollback shelf-life**: write+post-verify 종료로 작업 창 닫힘 → `delete_range`는 **상시 rollback 명령 아님**. 작업 중 즉시 rollback = candidate range `delete_range` / **작업 종료 후 = snapshot 기반 복구 또는 교집합 historical write 부재 재검증 후에만**. snapshot = 삭제 전까지 유지되는 **durable recovery anchor** (restore = 별도 인스턴스 생성, "영구 복구" 아님). **rollback 정책 정확화** (과거 Step 3 anchor의 "24/7 연속이라 range delete 안전" 표현 supersede): 24/7 연속성은 내부 휴일 gap 부재일 뿐, range delete 안전은 별개 — **gap-only write 직전 확인 + 교집합 historical/manual write 부재 + 동일 작업 창 내 즉시 rollback**일 때만 허용. **Hana official 1년 backfill — monthly runbook superseded**: Step 4A Stage 1의 "monthly batch" runbook(line 1305 "1년 monthly backfill" + DECISIONS 5541 "gap-only monthly batch")은 **single full-range로 폐기·대체** (월 경계 휴일 fallback 충돌). **보강 PR — Stage 1 commit + Stage 2 push 완료 (`930e667`)** (throttle 3.0초[분당 30회 보수 + live Hana crawler 같은 endpoint 분당 최대 4회 rolling 공유, monotonic 첫 요청 cooldown + 실패 요청 포함] + HTTP 429 즉시 abort[Retry-After 로그, exit nonzero, 남은 fetch 0] + EXPECTED_DATES_JSON helper + CLI fail-close test 2 + 회귀 sleep mock → Hana 63 passed). **Hana 1년 single full-range write 완료** (2026-06-03 — 237 신규 → Hana 246, 전체 637, 아래 Step 4A Hana write 완료 bullet). 상세: [DECISIONS.md ADR-034 §14](DECISIONS.md).
- ✅ **Phase 2d Step 4A Hana 1년 write 완료 (Stage 3 production) + Step 4A 전체 완료** (2026-06-03): ADR-034 §14 Step 4 full backfill의 **4A Hana 부분** — official_historical_backfill 1년 gap-only single full-range write. **절차**: throttle 보강 PR(commit 930e667) EC2 배포(git pull + build → image `6b6a5f`, marker 3개 확인, 운영 container 불변) → BREAK1에서 full-range `--range-dry-run`(nohup, **4/4 통과**: exit 0 / fetch_errors 0 / EXPECTED_DATES_JSON 237 dates / DB target count 0) → RDS snapshot `fxi-pre-step4a-hana-2026-06-03` `available` → write 직전 DB target count 0 **재확인**(point-in-time preflight, 최종 방어는 transaction 내부 `--require-empty-target`) → nohup `--write --currency USD --require-empty-target --allow-production-write` (`</dev/null` + exitcode/pid/log + launch 확인) → 즉시 post-verify. **결과**: 신규 **237 official rows** (inserted 237 / updated 0) → Hana 9→**246**, 전체 source_daily_rates 400→**637** (KRX 25 + Hana 246 + Bithumb 366). **이중 독립 검증** (Claude post-verify + Codex production read-only): dry-run manifest 237 **1:1**(missing·extra 0) / drift(rate≠close) 0 / official metadata 위반 0(close_only + basis_date + published_at + pbldSqn) / **observed_eod 5 = 개수·날짜·분류 유지**(overlap guard 결과 — "전 컬럼 불변"은 before snapshot 미비교라 단정 회피). **rollback shelf-life**: snapshot = 명시 삭제 전까지 유지되는 **DB 전체 recovery anchor**(restore = 별도 DB 인스턴스 생성, 실제 engine compatibility는 복구 시 확인) / inserted_dates 237 IN delete는 작업 창 종료 → **상시 rollback 명령 아님**(이후 snapshot 복구 또는 재검증 전제). **post-snapshot** `fxi-post-step4a-2026-06-03` `available` (**Step 4A post-verify 후 생성** — snapshot 2026-06-03 03:20 KST, DB 전체 recovery anchor. post-verify ~01:39 KST와 시차. 637은 별도 post-verify 결과이며 snapshot 직접 조회로 재확인한 값은 아님). **배포**: image `6b6a5f`(commit 930e667 포함) + 6/3 daily append cron PASS(bithumb·hana written). **Step 4A 1년 coverage production 완료** (2026-06-03 ~01:39 KST post-verify state: KRX 25 + Bithumb 366(1년 coverage 365 + 2026-06-03 00:01 KST cron이 2026-06-02 row 1건 append) + Hana 246[official 241 + observed_eod 5] = **637**). **engine**: PostgreSQL **17.9** (2026-06-06 DB 쿼리 재확인 `server_version_info=(17,9)`; 17.6→17.9 원인은 AWS auto minor upgrade 추정, 무영향). **cron 정상 확인**: daily append cron이 6/6까지 매일 적재(2026-06-04~ 관찰 해소 — total 2208 / 최신 date hana·investing·bithumb 6/6, krx 6/5[주말 close finalizer 없음]). **manual snapshot cleanup 완료** (2026-06-06 사용자 — `fxi-pre-investing-backfill-2026-06-05` 1개만 유지, 나머지 7개 삭제. backfill 완료·이중검증으로 복구 창 종료). 상세: [DECISIONS.md ADR-034 §14](DECISIONS.md).
- ✅ **Phase 2d Step 4B — KRX OpenAPI source 전환 + 25-row migration + full-year backfill 완료** (2026-06-04): KIS year-series 한계(A755xx/2025 미조회 — `rt_cd=0`이나 `output2=[]`) → **KRX 공식 OPEN API `fut_bydd_trd`(선물 일별매매정보) date-based로 source 전환**. 구현 8 단위 (`c2caa50`..`1103ecf` 구간, feat 8 + enum docs 1 = 9 commits): parser/filter → row→source_daily 변환 → compare transitional(값·contract·metadata 일치 시 source_method kis↔krx 차이만 허용 + `PASS_WITH_TRANSITIONAL` 게이트) → date-based manifest builder(weekday-only loop + **BAS_DD==요청일 stale 가드** [KRX_CANARY 2026-05-25 동형 차단]) → range dry-run wiring(`_emit_compare_and_gate` KIS 공유) → HTTP helper(주입 가능, AUTH_KEY env, throttle+429) → main CLI(`--range-dry-run`) → migration writer. **운영 dry-run GO** (EC2 production RDS, scoped `[2026-04-20, 2026-05-27]`, 실측 TRANSITIONAL=25 / ROWS_TO_WRITE=0 / COMPARE_HARD=0 / MISSING=3[공휴일 05-01·05-05·05-25] / PASS_WITH_TRANSITIONAL — KIS↔KRX 값 전수 일치). **production migration** (snapshot `fxi-pre-krx-source-method-2026-06-04`, `scripts/migrate_krx_source_method.py` DB-only scoped surgical, 25 rows `kis_daily_backfill`→`krx_openapi_daily`, drift 0, boundary 2026-05-18=`1496.500000`/A75606 보존, Claude+Codex 이중 독립 검증). source_method enum 6 values. KIS는 daily backfill에서 verification/fallback 격하(realtime broadcast 유지). **full-year backfill 완료 (동일 2026-06-04)**: gap-only write 경로(`write_krx_gap_rows` insert-only + `--write` CLI, `8d79da4`/`087edc0`) → full-year dry-run(`[2025-06-03, 2026-06-03]`, MANIFEST 243 / ROWS_TO_WRITE 218 / MATCHED 25 / COMPARE_HARD 0 / PASS) → snapshot `fxi-pre-krx-fullyear-backfill-2026-06-04` → production write(218 gap insert, 25→243, 기존 불변) → post-verify(**2025 142행**[KIS 미조회 구간], by_method `{krx_openapi_daily:243}`, post-write ROWS_TO_WRITE=0, rate≠close/OHLC/dup 0, Claude+Codex 이중 독립 검증). **KRX 1y coverage 달성(243 rows, 2025+2026)** — Hana 1년 backfill 수준 정렬 + Bithumb 1y와 장기 그래프 입력 균형. 상세: [DECISIONS.md ADR-034 §8](DECISIONS.md).
- ✅ **Graph API v2 catalog policy land (Phase 2b)** [historical record — Decision 5는 Amendment 후속에서 superseded] — [ADR-033](DECISIONS.md) + [GRAPH_API_V2_CONTRACT.md](GRAPH_API_V2_CONTRACT.md) (2026-05-27, Proposed). 10개 decision anchor: legacy `/api/graph/{currency}` 유지 / new app v2 / catalog = tab × period × series / Citi v2 catalog 제외 (수집 유지) / ~~Hana 30d actual + Investing backfill~~ (Amendment 2026-05-27에서 Hana 자체 historical 단일 source로 정정 → 다시 Amendment 후속에서 hana_observed_eod canonical + hana_official_historical_backfill 2-source 분리로 정정) / Bithumb·KRX Investing backfill 금지 / Bithumb·KRX 외부 historical 또는 daily rollup 없으면 insufficient_history / DXY_futures 1d only / source_rates 30d cap = daily rollup 없이 자연 확장 불가 / daily rollup retention은 별도 결정. v2 endpoint 구현 (Phase 2e) + Bithumb·KRX historical 조사 (Phase 2c) + daily rollup 구현 (Phase 2d) 별도 트랙.
- ✅ **Phase 2c external_historical source 확정 + ADR-033 Amendment 2026-05-27** (2026-05-27) [Decision 5 Hana 부분은 Amendment 후속에서 superseded]: Bithumb 공식 candlestick API (902일, 무인증, ccxt OCHL schema 잠금) / KRX KIS `inquire-daily-fuopchartprice` + A75YMM contract chain (만기 지난 월물 조회 가능, monthly front-month chain이면 1y는 최대 12 contracts, 각 contract 사용 구간만 좁게 fetch하면 보통 100건 cap 회피, **rollover boundary 확정 — 만기일 07:00 KST user-facing swap point**: daily graph는 expiry_date 당일/이후 = next contract) / Hana official historical endpoint (20년+, 휴일 자동 fallback, 매매기준율 인덱스 7, **회차 정책 확정 — `pbldSqn=` 빈값 기본 일일 row + 회차 provenance 저장**, "최종 회차" 공식 단정 X). ADR-033 Decision 5 ~~(Hana = Investing backfill → Hana 자체 historical 단일 source)~~ [Amendment 후속에서 2-source 분리로 재정정] + Decision 7 (Bithumb/KRX = insufficient_history → external_historical 확정, insufficient_history는 신규 자산/source 장애에 여전 유효) + Decision 10 (장기 coverage 목적 daily rollup 우선순위 ↓, internal cache/장애 대비 목적은 future optimization 보존) Amendment. GRAPH_API_V2_CONTRACT.md §7 [Amendment 후속에서 정책 재작성 — observed_eod + official_historical_backfill 분리]. §7-new는 KRX contract chain rollover boundary policy. 상세: [DECISIONS.md ADR-033 Amendment](DECISIONS.md) + [GRAPH_API_V2_CONTRACT.md](GRAPH_API_V2_CONTRACT.md).
- ✅ **KRX Stage E — tick-level Redis writer 운영 활성** (E-1 `c3cb1c1` default false land / E-2 2026-05-26 운영 활성, `KRX_REDIS_TICK_WRITE_ENABLED=true`): KRX Redis write timing을 DB-insert-bound → tick-level(5-field freshness schema)로 전환 = [REALTIME_ARCHITECTURE_PLAN.md §4.1](REALTIME_ARCHITECTURE_PLAN.md) all-source fanout 계약의 KRX 측 정렬 완료. 구현 단계에서 Stage E는 ADR-027 Stage C/REST 반영 정책과 **직교**로 재분류됨(`app/config.py` 주석 / `c3cb1c1`) — close grace tick은 `KrxCloseWindowWriter` 단독 처리(non-interference). 상세 근거는 [KRX_CANARY.md](KRX_CANARY.md) 토글 매트릭스 E-2 anchor. Follow-up: 다음 KRX 영업 세션 live tick-level 5-field 갱신 hard telemetry.
- 🔜 **All-source observation fanout 통합 phase** — [REALTIME_ARCHITECTURE_PLAN.md §4.1](REALTIME_ARCHITECTURE_PLAN.md) anchor. 남은 순서: (1) [Bank/Investing β](USDT_TOPIC_MIGRATION_PLAN.md) (DB-first monolithic → observation fanout 재설계 + main.py legacy hook 격하 가능성) → (2) USDT 5 source 공통화 검토 (3 도메인 검증 패턴 input, 선제 abstraction 금지 원칙).
- 🔜 장기 realtime roadmap — 신규 단말은 topic 구독 모델만 사용, broadcast cycle은 구버전 호환 후 deprecate. 테더 탭 모든 표시 자산이 Redis latest write-through 성공 지점 기반 trigger를 갖춘 뒤 legacy hook 완전 제거.
- 🔜 5/18 KRX 만기 rollover 관찰 + ADR-027 REST fallback 수치 확정 → Stage C 결정
- ✅ **Gopax silent-session reconnect fix land** (2026-06-03, `c4a51e5` / PR #1): 5/28 15:52 silent WS session(tick·heartbeat 정지인데 `ConnectionClosed` 미발생 → `connection_status=stale`이 label-only no-op이라 ~5일 고착) 직접 대응 — mid-session stale / no-first-tick → `_SilentSessionError` → reconnect + 첫 tick backoff 리셋 분리 + backoff bounded probe + USDT Redis `<` 역행 가드(`SKIPPED_REGRESSION`). 다른 4 source는 `_ping_loop` 능동 close 보유, Gopax만 부재했던 게 원인. **교훈: stale은 label만으론 부족, reconnect 액션 필요.** 후속 3종 중 **①②는 2026-06-11 완료**(아래 anchor), **③(DB exchange-ts, migration) + KRX task-death gap**만 잔존 — 구현-ready spec은 [USDT_WS_DESIGN_PLAN.md §12.9.8](USDT_WS_DESIGN_PLAN.md), 비긴급 별 PR.
- ✅ **§12.9.8 ① Gopax heartbeat-alive·ticker-dead detector land + 배포** (2026-06-11, `ef99c66`): Gopax silent-session fix(`c4a51e5`)가 *살아있는 task 안* reconnect라 미커버하는 "heartbeat fresh + ticker만 dead"(시장 거래 중인데 WS 데이터만 정지)를 REST-발산 기반으로 검출. `fetch_gopax_last_traded_ms`(/tickers `lastTraded`=체결시각, 기존 `fetch_gopax_usdt_tick`/ticker write-path 무접촉 → Redis `<` 가드 보존) 신규 + `GopaxRestFallbackController` 별 task detector(degraded probe 시 REST `lastTraded` > WS 저장 `lastTraded` strict 비교 → client reconnect flag → recv loop `_SilentSessionError("ticker_dead")`) + race guard(valid WS tick에서 flag clear) + 2단 인계(flag 1회 → reconnect 후 지속 시 no-first-tick guard 인계, 무한 루프 부재) + metrics `ticker_dead_check_count`. silence 기반 미적용 이유 = Gopax 정상 sparse traffic(tick_age ~180s)에서 false-positive. 14 단위 테스트. 검출 지연 ≈ degraded 600s + cooldown 0~300s ≈ 10~15분(5/28 5일 고착 대비), false-positive 구조적 0. 배포 후 live(normal, 평상시 발화 0이 정상 — 실증은 다음 ticker-dead 사건). 두 Claude + Codex 상호 검증(commit `ef99c66` suffix 일치). 5-source 공통화는 후속. 상세: [USDT_WS_DESIGN_PLAN §12.9.8 ①](USDT_WS_DESIGN_PLAN.md).
- ✅ **§12.9.8 ② 5-source WS collector task supervisor land + 배포 + 활성** (2026-06-11, `db8461b`): ①/silent-session fix가 *살아있는 task 안* reconnect라 미커버하는 "collector task 자체가 죽음"(client.start() 예외 escape/cancel → `_run_usdt_ws_*_client` except가 log-only, 부활 X / legacy REST polling 안전망 5/25 비활성)을 30s watchdog(`_usdt_ws_supervisor_tick`)이 메움 — done() task 감지 → `shutdown_*`(idempotent teardown, 구 client 잔여물 정리) → `start_*`(fresh, `not task.done()` dedup 가드 재사용). **`USDT_WS_SUPERVISOR_ENABLED` env**(default false → job 자체 미등록, 배포 ≠ 동작 변화 + interval ≥1 import guard) + **main.py shutdown race guard flag**(`signal_usdt_ws_supervisor_shutdown`, shutdown 체인 전 set — stop()/task await 구간 globals not-None window를 'task None skip'만으론 못 막아 명시 flag 필수, main.py shutdown 순서 직접 확인) + **per-source backoff**(consec 1=즉시, 2부터 60·120·240… cap 300s, 영구 포기 X — 실패 restart도 backoff 소비[storm 방지], 생존 5분+ reset — 단일-tick-alive 아님[45s-crash-loop 무력화 방어]) + per-source 격리. registry 5 source(late-binding lambda로 module-global task 최신 참조). 16 단위 테스트. 배포(force-recreate)+활성(`USDT_WS_SUPERVISOR_ENABLED=true`) 후 5 source(upbit/bithumb/coinone/korbit/gopax) 전수 감시 + restart 0(healthy — 평상시 0이 정상, 실증은 다음 task-death 사건) + APScheduler job 등록 확인. 두 Claude 상호 검증. ⚠️ **KRX는 ② 1차 제외**(bootstrap+client 2-task + reconcile cron 구조 상이)였으나 **task-death gap은 별 분기로 land**(2026-06-11): `_reconcile_krx_futures_contract`에 `client 비None AND task.done()` 분기(shutdown_krx→bootstrap(resolved) + post-condition) + ②의 shutdown flag를 `_collector_shutdown_initiated`로 **일반화**(supervisor+reconcile 공유, "app shutdown=단일 사실" DRY) + enum `task_dead_restarted`/`task_dead_restart_error` + backoff 없음(5분 cron throttle) + 6 tests(전체 1261 green). **일반 배포**(보류 불필요 — 본 분기는 dead task에만 발화해 6/15 rollover[live task]와 **직교**: rollover 분기 미변경 + `test_live_task_contract_mismatch_rollover` 회귀로 잠금. 발화 시에도 `last_result=task_dead_restarted` telemetry로 귀속 가능 → 보류가 사주는 명료성 거의 0, 비용[배포 freeze]만 실비용 — [[feedback_deploy_timing_cost_benefit]]. 15:35~15:50 CF close finalizer 창만 회피). 6/15 rollover 관찰 시 reconcile `last_result`(rollover vs task_dead_restarted)로 분기 구분. 실제 잔존은 **③ DB exchange-ts**(latest-staleness는 super-lite로 land[아래 bullet], full migration은 보류·폐기). 상세: [USDT_WS_DESIGN_PLAN §12.9.8 ②](USDT_WS_DESIGN_PLAN.md).
- ✅ **§12.9.8 ③ DB exchange-ts super-lite first PR land** (2026-06-11, 세 리뷰어 수렴): out-of-order stale write가 DB latest를 오판시키는 비대칭(Redis는 `<` 가드로 막지만 **DB 미차단** — DB writer가 exchange ts 미전달이라 stale이 now()로 latest 기록)을 **migration/watermark/read-path 없이** 해소. 핵심 통찰 = **DB는 history store**(Redis latest-only는 stale skip이 맞지만 DB는 stale도 제 exchange ts의 과거 관측으로 보존이 맞음) + 기존 `insert_source_rate_if_changed(timestamp=)` param + `timestamp DESC` 쿼리가 이미 받을 수 있음(KRX close가 매일 쓰는 검증된 rail). USDT 5 WS DB writer가 tick exchange event ts(`crud.event_ms_to_utc_naive(tick["timestamp_ms"])` — ms→UTC naive 공용 helper, DRY)를 전달 → stale은 제 과거 ts로 들어가 latest 안 됨(history엔 보존). **DB writer 경유 전체(WS tick + REST probe fanout) 적용**, 직접 crud 호출(KRX/legacy polling)만 미적용. per-source timestamp_ms 전수 확인(Upbit/Bithumb `trade_timestamp`·Korbit `lastTradedAt`·Gopax `lastTraded`=체결시각 4 / Coinone `timestamp`=ticker event time 1 — Redis 가드와 동일 값이라 일관). 4 신규 테스트(stale-not-latest / same-rate skip / ms 변환 / dedup timestamp-latest 잠금) + 9 기존 회귀 수정 + 전체 2097 green. 혼재(구 row=save-time / 신 row=exchange-time): 정상 tick은 ≈동일·stale만 의도적 개선, migration 0. full(영속 `exchange_ts` 컬럼)은 추가 가치(재시작 직후 첫 write 가드) 대비 비용 약해 보류·폐기. KRX는 KIS frame ts 시맨틱 검토 후 별 PR. 상세: [USDT_WS_DESIGN_PLAN §12.9.8 ③](USDT_WS_DESIGN_PLAN.md).
- ✅ **Phase 2d KRX daily-append (in-process close finalizer) — Units 1-4c land + 6/5 canary 성공** (2026-06-05, **production end-to-end 검증 — Claude+Codex 이중 독립**): ADR-034 §9 KRX daily append 구현 — **cron 아닌 in-process close finalizer hook** (Bithumb/Hana cron one-shot과 다른 메커니즘: CF 15:45 KST 종가 직후 trigger, 같은-날 freshness + 종가 캡처에 묶여 "단순 cron 부적절"). **6 units** (`b7bec28`..`f04d9b8`): U1 `get_krx_cf_session_rollup` (CF session 08:30~15:45 KST source_rates rollup → high/low/point_count) / U2 `build_krx_cf_append_row` (point_count==0 → close_only[high=low=close] / else observed_rollup + close clamp[high=max(rollup_high,close)·low=min(rollup_low,close)] + clamp diagnostics) / U3 `decide_krx_cf_append_action`+`write_krx_cf_append_row` (INSERT/SKIP/HARD pure guard — non-KRX·non-finalizer / key mismatch / method allowlist[krx_openapi_daily·close_finalizer] 외 / close mismatch → HARD, close match → SKIP) / U4a `append_krx_cf_daily_row` (rollup→build→guard→INSERT commit / SKIP·HARD rollback) / U4b CF finalizer hook (`KrxCloseWindowWriter._sync_write` success tail session=="CF" → append, **exception 격리 — finalizer 영향 0**) / U4c `KRX_DAILY_APPEND_ENABLED` gate (default false → **배포 ≠ 동작 변화**, 활성화 = env 토글 + 재생성). **변경**: `app/source_daily_rates.py` + `app/crawlers/krx_kis.py` + `app/config.py`. **테스트**: U1 7 / U2 10 / U3 14 / U4a 8 / U4b·4c close-window +5 회귀 통과. **6/4 manual fill** (gate 활성 전 — 6/4 CF 종가가 이미 source_rates에 있어 1회성 `append_krx_cf_daily_row(db, date(2026,6,4), 1530.6, "A75606")` → observed_rollup H=1531.0/L=1522.2/C=1530.6, KRX 243→244, snapshot 생략[1-row delete-anchor], Codex 이중 검증). **6/5 canary 성공** (`.env KRX_DAILY_APPEND_ENABLED=true` + `--force-recreate`[**build 없음, image f86195e 불변**] 15:35 KST 활성화 — config 3 flags[DAILY_APPEND·CLOSE_FINALIZER·FUTURES] True + WS fresh[tick age 15.7s]): **15:46:00.76 KST close finalizer 발화** → `[krx_cf_append] INSERT date=2026-06-05 contract=A75606 close=1538.9 — 신규 date`. **이중 독립 검증**(Claude+Codex read-only): close_finalizer / observed_rollup / **cf_session_point_count 7005 == source_rates CF session[08:30~15:45] tick count 7005** / rate=close=1538.9 / H=1549.0 / L=1531.8 (intraday 범위 보존) / close_basis=krx_cf_close_1545 / contract A75606 / basis_date·published_at None / drift·OHLC·dup 0 / **KRX 244→245**. provenance 자연 전환 (6/5·6/4 close_finalizer·observed_rollup / 6/2·6/1 krx_openapi_daily·source_ohlc). **첫 라이브 in-process 종가 샘플링** — `KRX_DAILY_APPEND_ENABLED=true` 유지 (rollback 불필요), 매 CF close going-forward 자동. 상세: [DECISIONS.md ADR-034 §9](DECISIONS.md). 후속: Hana JPY/EUR 다각화 / Phase 2e endpoint switch. **6/11 회귀 canary**: ⑥(ADR-035 D3 구 hourly hook 제거 — 같은 `_sync_write`) 운영 반영 후 첫 CF close → 15:46:00 `[krx_cf_append] INSERT date=2026-06-11 A75606 close=1531.2`(close_finalizer/observed_rollup/krx_cf_close_1545/cf_session_point_count=5935/KRX 248→249). **hook 제거가 daily finalizer-tail 무손상 확증**(unit trip-wire[shared writer] 미커버 live 회귀). WS Case A — REST gate-checked write(6/15 활성) 미실증이라 KRX_CLOSE_SNAPSHOT_PLAN 직교.
- ✅ **Phase 2d Hana JPY/EUR 다각화 완료 — 통화 파라미터화 + official 1년 backfill + observed_eod 보존 + 3통화 정합** (2026-06-05, **production end-to-end 검증 — Claude+Codex 이중 독립**): Hana를 USD 단독에서 **USD/JPY/EUR 3통화**로 확장 (4단계). **(1) 코드** (`cb62208`): observed_eod writer USD 하드코딩(`ASSET`) 제거 → `--currency {usd-krw,jpy-krw,eur-krw}` 파라미터화 + write persister `SUPPORTED_CURRENCIES` allowlist guard (구 상수 방어 복원) / orchestrator run unit=(source,asset), hana 3통화 per-currency 격리 (writer 29 + orchestrator 47 = 76 tests). **(2) official 1년 backfill** (2025-06-02~5/27, snapshot `fxi-pre-hana-jpyeur-official-2026-06-05`): JPY/EUR 각 range-dry-run(241 / fetch errors 0) → write → 독립 verify (각 241 / external_backfill·close_only / drift·OHLC·dup·metadata 누락 0 / USD·JPY·EUR 동일일 pbldSqn 일치 — Hana 통화별 같은 회차). **(3) 배포** (`a2d066b`, **build-only**): `docker compose build fastapi` (image f86195e→c77b5a0, **`--force-recreate` 안 함** — f04d9b8..a2d066b는 scripts/docs만, app/ 불변이라 **live KRX daily-append untouched**) + smoke (새 image의 observed writer `--currency` + orchestrator 4 run units 반영 확인 — CLAUDE 2026-05-19 Coinone build-verify 교훈). **(4) observed_eod 보존** (5/28~6/4, 각 7 rows): cb62208 observed writer가 bank_exchange_rates 내부 read (Hana endpoint 무관) → dry-run(USD와 동일 7일 — 5/31 weekend skip / 5/30·6/3[선거일] 변동有 write) → write → 독립 verify (각 7 / hana_observed_eod·observed_rollup / **intraday range 보존[H≠L]** — bank 30일 retention age-out 전 perishable 캡처 / metadata 모두 None[provenance split: observed는 official metadata 없음] / drift·OHLC·dup 0). **결과: 3통화 완전 동일 구조** — usd/jpy/eur 각 **248 = external_backfill 241(≤5/27) + observed_rollup 7(5/28~6/4)**, Hana **730→744**, 전체 source_daily_rates **1357** (KRX 245 + Hana 744 + Bithumb 368). **provenance boundary 정책 (Y안 채택)**: official ≤5/27 + observed 5/28~6/4 + cron 6/5+ going-forward 활성(6/6 00:01 첫 발화 PASS — 6/5 3통화 적재 확인). X안(official ~6/4)이 아닌 Y안 근거 = observed intraday range가 perishable(retention)이라 지금 안 잡으면 영영 못 잡음 + USD cross-currency 정합. rollback: per-currency 비연속 7-date IN delete (snapshot 생략 — 소량 all-insert). 후속: ✅ 6/6 00:01 cron 6/5 JPY/EUR 3 rows 적재 확인 (Hana 744→747, hana_observed_eod / observed_rollup, drift·OHLC 0 — Investing daily append 통합 cron 동반 검증) / Phase 2e endpoint switch.
- ✅ **ADR-035 D1 Investing daily canonical writer + production backfill 완료** (2026-06-05, **production end-to-end — Claude+Codex 이중 독립**): ADR-035 D1 첫 구현 — `investing_exchange_rates`(장기 raw) → `source_daily_rates` daily rollup. 3m/1y hot path 마지막 gap(Investing) 해소. **(1) writer** (`4f38b05`): `scripts/backfill_investing_source_daily_rates.py` 신규 + tests 신규 — Hana observed_eod 패턴 차용 + **단순화**(carry-in baseline 제거 — Investing 데이터 풍부 / business_day_error 제거 — Investing gap은 시장 주도 → skip-empty calendar). `--range-dry-run`(skip day-of-week 분포 + 일요일 외 skip flagging, EUR sparse surface) / `--require-empty-target`(gap-only) + `InvestingWriteOutcome` inserted/updated 구분(rollback inserted-only) / 0-row fail-close(multi-day) + `--expected-min-rows` / CLI mode-arg fail-close(start·end·include-today mode 없이 / allow-production-write write 없이 / date range mode와 → reject). close_basis enum 4→5(`investing_observed_eod`) **전수 정합**(Decision B 표+매핑 + §6 본문 enum list + observed_rollup 설명 + GRAPH §6 + models docstring). **Codex 6 findings 반영**(F1 inserted/updated rollback, F2 enum docs, F3 0-row fail-close, F4 CLI combo, F5 model docstring, F6 mode-arg fail-close). **(2) quantize fix** (`a11e6f7`): **range-dry-run이 production write 전 JPY Float artifact 발견**(`921.6100000000001` 13자리 — JPY/KRW per-100 환산 계산값, `Decimal(str(float))`이 보존 → Numeric(14,6) 초과 185건). build close/high/low `quantize(0.000001)`(max/min raw 비교 후 결과만 quantize → OHLC ordering 보존). usd/eur 무영향(직접 저장 ≤6자리). 147 passed(Investing 29 + 회귀 118). **(3) production backfill** (range `[2025-06-05, 2026-06-04]` 1년, snapshot `fxi-pre-investing-backfill-2026-06-05` user 생성): investing canonical=0 사전 read-only 확인 → **명시적 write GO 분리**(snapshot available 확인 ≠ 837 rows mass write 허가 — auto-mode classifier가 별도 GO 요구, 분류기 판단 타당) → 3통화 순차 `--write --currency <cur> --require-empty-target --expected-min-rows 270 --allow-production-write`(각 inserted 279 / updated 0). **결과**: usd/jpy/eur 각 **279**, Investing **837**, 전체 source_daily_rates **1357→2194**(KRX 245 + Hana 744 + Bithumb 368 + Investing 837). **이중 독립 verify**(Claude post-verify + Codex production read-only 일치): drift(rate≠close) 0 / OHLC violation 0 / **duplicate date 0**(Codex 추가 확인) / nullable(contract_code·basis_date·published_at) violation 0 / metadata point_count missing 0 / close_basis=investing_observed_eod·source_method·ohlc_quality=observed_rollup / **JPY quantize 6자리**(957.520000 류, 13자리 artifact 0). **calendar**: 365일 중 279 write / 86 skip(Sun 52 FX 휴장 + Sat 32 change-only 주말 저활동 + Thu 2 약한 flag US 휴일/크롤러 gap 추정 — backfill gap 허용). **recovery anchor**: snapshot `fxi-pre-investing-backfill-2026-06-05`(DB 전체) + 통화별 inserted_dates IN delete(write+post-verify 종료로 상시 명령 아님) + `~/logs/inv_backfill_*.log`. v2 endpoint(Phase 2e) 미구현이라 write 후 user-facing 영향 0. 후속(별도): **Investing going-forward daily append**(✅ 완료 — `bef10eb` orchestrator 통합 + cron 첫 발화 검증 2026-06-06, 위 별도 anchor) / Phase 2e v2 endpoint(source_daily_rates read 전환) / source_hourly_rates(1w).
- ✅ **ADR-035 D1 Investing daily append orchestrator 통합 + cron 첫 발화 검증** (2026-06-06, **production end-to-end — Claude+Codex 이중 독립**): backfill(2026-06-04까지)에서 멈춰 있던 Investing을 매일 자동 갱신 — KRX/Hana/Bithumb과 going-forward freshness parity. **(1) 구현** (`bef10eb`): verdict 계약(`app/daily_append_verdict.py`) `no_observation` reason union 추가 + **orchestrator source-aware `valid_skipped_reasons`**(SOURCE_POLICY per-source subset + assert — Hana는 no_observation 비허용, Investing은 weekend_no_changes 비허용 → 전역 추가의 정책 느슨화 회피) + `SOURCE_RUN_ALL`(bithumb→hana 3→investing 3) + resolve_run_units 다통화 일반화 + build_investing_command + Investing writer `--emit-daily-append-verdict`(단일일 written/skipped, **0-obs도 skipped/no_observation sentinel 방출 — exit-0-no-sentinel orchestrator FAIL 방지, Codex catch**). multi-day 0-row fail-close 유지. **163 tests**(writer verdict 4 + combo 2 + source-aware reason 4 + build/resolve + 회귀). **(2) EC2 deploy** (build-only): `git pull` + `docker compose build fastapi`(cron이 쓰는 image 반영, running fastapi recreate 안 함 — script + verdict 계약). dry-run smoke로 orchestrator investing 3통화 인식 확인. **(3) cron 첫 발화 검증** (2026-06-06 00:01 KST `--source all`): investing **2026-06-05** 3통화 written(usd C=1553.83 / jpy C=969.49[**quantize 6자리, artifact 0**] / eur C=1795.45, investing_observed_eod / observed_rollup, drift·OHLC·dup 0) + 동반 bithumb 1 + hana 3 → **cron summary 7 units 전부 PASS**. KRX는 cron unit 아니나 in-process close finalizer(15:45)가 same-date(2026-06-05) row 1건 적재. totals **2194→2201**(bithumb 369 / hana 747 / krx 245 / investing 840). **이중 독립 verify**(Claude + Codex). sanity: 같은 6/5 hana usd 1553.30 vs investing usd 1553.83(~0.5 KRW) 정합. **→ 4 source freshness 완성** (bithumb/hana/investing=cron 00:01 + krx=close finalizer 15:45 — source_daily_rates 매일 자동 최신). 후속(별도): ✅ Phase 2e v2 endpoint(source_daily_rates read 전환) — Phase 2e MVP land + 운영 deploy 완료 2026-06-06 / source_hourly_rates(1w).
- ✅ **ADR-035 Phase 2e MVP land — v2 graph endpoint (catalog + tab, source_daily_rates read, 3m/1y)** (2026-06-06, **로컬 구현+테스트 + 운영 deploy 완료**): canonical daily layer를 실제 v2 그래프 endpoint가 read하는 hot path 완성 — ADR-035 "외부/raw hot path 제거" 달성. write-side(4 source 적재) 완료 후 read-side 전환. **(1) graph_v2.py 로직** (`7bc1d65`): `app/graph_v2.py` 신규 — `build_catalog()`(MVP 3m/1y subset, usd/jpy/eur/tether tab × series, DXY 노출 탭만 index axis_group, §3/§9 근거) + `build_tab(db, tab, period)`(SERIES_REGISTRY id↔DB key 분리 + **kind dispatch**: source_daily_rates는 get_range+row_to_dict / DXY는 market_index_rates.daily reader). **동적 provenance**(distinct close_basis 1개=single / 2개+=mixed[Hana만]) + per-point(mixed→close_basis/source_method, KRX→contract_code) + **insufficient_history partial coverage**(첫 row > start+7일, §6 — 빈 series뿐 아니라 신규 자산 미충족도). **Codex 2 findings**(F1 insufficient partial / F2 DXY source priority investing>cnbc>else, crud.py `_dxy_query_single` 일치 — last-row dedup 회귀 차단) 반영. 12 tests(catalog/unsupported/tab/Hana mixed/single/KRX contract_code/DXY/DXY priority/insufficient empty·partial·full/range). **(2) main.py thin wiring** (`9d43773`): `GET /api/v2/graph/catalog` + `GET /api/v2/graph/tab` — tab 404(unknown_tab) + period 400(unsupported_period + v1 fallback hint) + build_tab(asyncio.to_thread 비차단). 로직 0(graph_v2 호출만) / **v1 `/api/graph/{currency}` 변경 0**(legacy 공존 §12). **(3) endpoint 테스트 harness** (`c7c218c`): `tests/conftest.py`(firebase stub + DATABASE_URL=file-backed sqlite, collection 시작 시 모든 import 전 — main.py firebase_admin+RDS 연결 회피, **재사용 가능**, in-memory override+fd close [Codex]) + `tests/test_graph_v2_endpoints.py` 6종(catalog 200/unsupported 400/unknown tab 404/tab empty insufficient/v1 무변경, TestClient lifespan 미진입). **검증**: graph_v2 12 + endpoint 6 = 18 tests. **MVP 범위**: 1d=v1 realtime / 1w=source_hourly_rates(D3) 후속 → v2는 3m/1y만, 1d/1w는 400 unsupported(insufficient_history와 분리 — 데이터는 v1에 있음). **문서=full target(§4 1d/1w 포함) / 구현=MVP subset(3m/1y)** 비강제. **운영 deploy 완료 2026-06-06** (f04d9b8→0e52b7a recreate, image 9e2e3186; Claude+Codex 이중 독립 검증: catalog 200·tab usd 3m 200 series[77,63,79]·1d 400·v1 200·health healthy·KRX status=normal·ERROR 0). client 미연동이라 실사용 caller는 아직 0(프론트 cutover 별도). 후속(별도): source_hourly_rates(1w, ADR-035 D3) / 프론트 client cutover. 상세: [DECISIONS.md ADR-035](DECISIONS.md) + [GRAPH_API_V2_CONTRACT.md §13](GRAPH_API_V2_CONTRACT.md).
- ✅ **ADR-035 D3 source_hourly_rates(1w) Bithumb 트랙 완료 — backfill + auto-append cron** (2026-06-08, **production end-to-end — Claude+Codex 이중 독립**): 첫 hourly source가 self-maintaining. Step 1 schema/helper/migration (`6e5b9bc`) → Step 2 dry-run validator + close_basis `bithumb_observed_hourly` contract (`854dd08`) → Step 3 write path (`2cb66c4`) → **production backfill 14d 336 bucket** → hourly append PLAN (`c83c503`) + WRITE path + retention prune (`39b6681`) → **OS cron `5 * * * *`(매시간 :05) 연결 + 첫 발화 검증** (2026-06-08 18:05 KST: inserted=1/updated=47/prune=1, count 336 유지, window `2026-05-25 18:00 ~ 2026-06-08 17:00` 1시간 전진, log+DB 이중 검증, drift/OHLC/provenance 0). **설계**: 직전 완료 hour까지 최근 2일 idempotent re-roll(correctness=catch-up이 late tick self-correct, freshness=cron minute) + require_empty 미사용 append + retention prune(14d rolling, production 첫 동작) + single-fetch(plan=commit snapshot). caveat: cron 매시간 :05 발화(매시간이라 tz 무관 — append가 KST 직전 완료 hour 처리, incomplete hour 제외) / lag metric ad-hoc 시 inflated(:05 정상 발화 lag ~5분, 보수적). 후속(각 별 GO, 당시 기준): 다른 source(Investing/Hana/KRX) hourly 다각화 + v2 1w endpoint switch(source_hourly_rates read) — **Investing/Hana + v2 1w endpoint + KRX hourly backfill(Step 2+3) 완료, KRX append/cron + CM 야간만 남음**. 상세: [DECISIONS.md ADR-035 D3](DECISIONS.md).
- ✅ **ADR-035 D3 source_hourly_rates Investing+Hana 트랙 완료 — 3 source self-maintaining** (2026-06-08~09, production end-to-end Claude+Codex 이중 독립): Bithumb에 이어 hourly 다각화 완료. **Investing**(per-currency usd/jpy/eur): validator+contract `investing_observed_hourly`(`14aaa66`) → write path + B.write_with_transaction 파라미터화(`fc3a4c0`) → backfill 14d 707 → append PLAN(`c4df362`)+WRITE+atomic prune(`b37bd65`) → cron `7 * * * *`(:07) 첫 발화 검증. **Hana**(per-currency, **mixed ohlc_quality** observed_rollup/close_only — single-tick hour close_only): validator+contract `hana_observed_hourly`(`9187977`) → write path + B ohlc_quality str→set 파라미터화(`1a04e70`) → backfill 14d 684 → append PLAN(`9f96550`)+WRITE+atomic prune(`e70e55b`) → cron `9 * * * *`(:09) 첫 발화 검증. **carry-forward write 미적용=실관측 bucket만**(canonical source of truth, Hana 고시 step function render는 v2 endpoint read-side defer). **→ Bithumb :05 / Investing :07 / Hana :09 모두 backfill + auto-append + atomic prune self-maintaining**(source_hourly_rates 1735 = 336+715+684). cron stagger(:05/:07/:09)로 컨테이너 동시 회피 + 로그 분리. 후속(각 별 GO): KRX hourly 다각화 — **v2 1w endpoint switch는 아래 bullet에서 land+deploy 완료**. 상세: [DECISIONS.md ADR-035 D3](DECISIONS.md).
- ✅ **ADR-035 Phase 2e v2 1w endpoint land + production deploy — source_hourly_rates read-side 완성** (2026-06-09, 로컬 23 test + production end-to-end 검증): write-side(3 source hourly self-maintaining) 후 read-side 전환 — 1w 그래프가 source_hourly_rates로 실제 채워짐. **graph_v2.py**: period→granularity 분기(3m/1y=daily=source_daily_rates / 1w=hourly=source_hourly_rates) — sdr reader granularity별 get_range(daily=date_kst / hourly=bucket_ts_kst datetime 경계) + bucket_date/bucket_ts 추출자 / market_index(DXY) reader granularity=hourly 시 KST 시 floor bucketing + source priority dedup / build_tab granularity 도출 + bucket_size 1h / catalog 1w. **insufficient_history tolerance granularity별**(daily 7d / hourly 2d — 1w partial coverage[마지막 1~2일만] 오판 방지, Codex finding). **main.py**: 1w 자동 허용(is_supported_period, 로직 0) + docstring/hint. **Hana carry-forward/step render는 read-side에서도 미적용 — 실관측 bucket 그대로 반환**(write 정책 일치, frontend render). 1d는 v1 realtime 유지. commit `88b9505`. **production deploy**(`--force-recreate`, 2026-06-09): `/api/v2/graph/tab?tab=tether&period=1w` 200 + bucket_size 1h + **실 hourly 데이터**(bithumb 170 / investing 117 / hana 115 / dxy 122 insufficient=False, krx 0 insufficient=True) + 1d→400 + v1 200 무변경 + collection 회귀 clean(error 0 / KRX CM status=normal). caveat: period_range는 3m/1y/1w 공유 inclusive [today-N, today](1w ≈ 8일, uniform semantics — docstring 명시). 후속(각 별 GO): KRX hourly 다각화는 **아래 bullet에서 Step 2+3 완료** — append/cron + CM 야간 + 프론트 client cutover(현재 caller 0)만 남음. 상세: [DECISIONS.md ADR-035](DECISIONS.md) + [GRAPH_API_V2_CONTRACT.md §13](GRAPH_API_V2_CONTRACT.md).
- ✅ **ADR-035 D3 source_hourly_rates KRX 트랙 Step 2+3 완료 — 4-source backfill 완성** (2026-06-09, production end-to-end Claude+Codex 이중 독립): 1w에서 유일하게 insufficient였던 KRX를 실 hourly로. **Step 2** dry-run validator(`4ce4828`): `krx_observed_hourly`(신규 enum, session-agnostic — CF/CM 공통, mixed/migration 회피) + CF-only(KST 08:30~15:45) filter(CM 야간 제외) + contract_code는 source_daily_rates daily row 재사용(KIS 미호출) + metadata.session=CF + 10 validations. **Step 3a** write path(`25d63de`): 공유 B.write_with_transaction을 `allow_contract_code`+`post_write_validator` 파라미터로 **default-preserving 확장**(3-source 동작 불변, 118 hourly test 회귀 0) + KRX in-transaction post-write(contract NOT NULL + session=CF, 실패 시 rollback — dry-run validate_contract/session과 symmetric). **production write 72 rows**([2026-05-26, 2026-06-08] 9 CF 영업일 × 8, gap-only, snapshot 생략+rollback anchor) → 이중 독립 verify(contract A75606×72 / drift·OHLC·session=CF 0 / **15:00 hourly close == daily CF close ALL MATCH** — close finalizer 15:45 종가 캡처 실증). **v2 1w endpoint krx.usd-krw-futures insufficient True→False**(data=32, contract_code per-point 노출). **→ source_hourly_rates 4-source(Bithumb/Investing/Hana/KRX) backfill 완료**. 단 KRX는 append/cron 없어 static — self-maintaining은 append PR에서. **[재설계 2026-06-10 — D6-① 구현 land]**: 6/9 사고(hook의 daily 의존 cascade + CF-only 미완성)로 KRX hourly를 **cron + 월물 제거 + CF/CM 통합**으로 전면 재설계 — `scripts/hourly_append_krx_source_hourly_rates.py` 신규(Bithumb append mirror + **empty-window graceful skip** [주말~월 아침 무세션 hour cron 실패 방지] + report-only daily-close 진단) + 15 tests. contract_code 미저장(공유 writer default가 None 강제 — delete 누락 시 fail-close 실증) + CM 야간 자연 커버(bucket 월물 혼합은 무거래 구간 06:00~08:30 ⊃ swap 07:00로 구조적 불가) + 1w per-point contract 노출 자연 소멸(daily 3m/1y는 불변). **운영 전환 + 정리 완료(2026-06-10)**: production 재구축(구 72행 dump→delete→14d write **188 bucket** — overlap 주간 56개 dump 값 완전 일치 무료 회귀 baseline PASS + window 경계 보완[5/26·5/27<22 16개 부재 정상] + 06:00 singleton + 야간 확장 + daily-close 진단 8 MATCH/6/9 DIVERGE) → OS cron `:11` 등록+첫 발화 검증(inserted=1/updated=41/prune=1, net 188 sliding window, range 한 칸 전진) → v2 1w `per_point_metadata []` 라이브 확인(contract 소멸 + 야간 hour API 도달) → **구 hook/모듈/env 제거(별도 commit)**: in-process hourly hook(krx_kis.py) + `KRX_HOURLY_APPEND_ENABLED` env + `app/krx_hourly.py` + 구 CF-only backfill 스크립트 + 2 test 파일(1255 lines) 삭제, trip-wire False-arm(공유 writer live 검증) 영구 회귀 잠금. 남은: 프론트 client cutover. 상세: [DECISIONS.md ADR-035 D3](DECISIONS.md).
- 🔜 CI/CD, 유닛 테스트
