# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **미국달러지수 선물 분리 저장** (2026-04-27, ADR-024):
  - `instrument='dxy_futures'`로 별도 저장 (`market_index_rates` 테이블)
  - exchange-rates-table `#sb_last_8827`을 환율과 동시 추출 (추가 HTTP 요청 0)
  - 폴백: `/currencies/us-dollar-index` (60초 쿨다운)
  - 향후 테더 탭의 KRX USD 선물 비교 그래프 준비
  - 현재는 raw 수집/저장만 완료 — 조회/그래프/rollup/테더 탭 연결은 후속 작업
  - 활성화/비활성화는 `crawler_config.investing` 토글에 종속 (별도 등록 없음)

- **USDT Phase 1 data feed + source 알림 API** (2026-04-23):
  - **거래소 5종 USDT/KRW 수집** (업비트, 빗썸, 코인원, 고팍스, 코빗)
    - 단일 scheduler job `usdt_sources` (매분 06,16,26,36,46,56초, 24/7 상시)
    - ThreadPoolExecutor fan-out (5개 REST API 병렬, per-source 2s timeout)
    - 변경 시에만 INSERT 정책 (insert-if-changed)
  - **새 테이블 3개**:
    - `source_rates` (source + asset + rate + timestamp, 30일 보관)
    - `source_notification_settings` (source/asset 기반 알림 설정, 기존 notification_settings와 분리)
    - `source_notification_logs` (알림 발송 히스토리, success/error_message 포함)
  - **신규 모듈**:
    - `app/source_registry.py` (SourceDefinition 데이터클래스, 9개 소스 메타데이터)
    - `app/crawlers/usdt_sources.py` (USDT 크롤러)
  - **기존 API 확장 (신규 endpoint 없음)**:
    - `/api/rates`: `rates` 배열에 USDT 엔트리 포함 (`currency=usdt-krw, bank=upbit/bithumb/...`)
    - `/api/rates/{currency}`: `/api/rates/usdt-krw` 지원
    - WebSocket `rates` 배열 자동 확장 (`build_rates_payload()` 경유)
    - `get_source_rates_as_legacy_format()` 어댑터로 source/asset → bank/currency 변환
    - registry sort_order 기준 정렬 (업비트 → 빗썸 → 코인원 → 고팍스 → 코빗)
  - **신규 API** (`/api/source-notification-settings`):
    - POST/GET/PUT/DELETE 4개 엔드포인트
    - `is_phase1_source()` + `category=="exchange"` 서버측 검증 (dead alert 방지)
    - reference 소스(investing/kb/hana)는 기존 `/api/notification-settings` 사용 안내
  - **알림 발송 루프**: `process_source_rate_alerts()` (usdt_sources에서 호출)
    - FCM payload type: `source_rate_alert` (기존 `rate_alert`와 구분)
    - 1회성 발송 (triggered=True + enabled=False)
    - 성공/실패 모두 `source_notification_logs` 기록
    - 표시명은 `source_registry.display_name` 사용
  - **계정 삭제 확장** (App Store 5.1.1(v) 컴플라이언스):
    - `DELETE /api/user/me`에 `source_notification_settings`, `source_notification_logs` 삭제 추가
  - **cleanup job**: `cleanup_old_source_rates` 매일 03:31 (기존 bank cleanup 패턴 재사용)
  - **설계 문서**: `USDT_TAB_PROPOSAL.md`, `USDT_PHASE1_DESIGN.md`
  - **하위 호환성**: 기존 iOS/Android 앱은 usdt-krw 엔트리를 currency 필터링으로 자동 제외, Codable non-optional 필드(banks/currencies)는 유지

- **WebSocket DXY live tick** (`data.indices.dxy`):
  - `crud.get_latest_dxy_rate()` 기반 (realtime granularity, investing > yahoo 우선순위)
  - 초기 연결 메시지 + 후속 broadcast 모두 포함 (`build_rates_payload()` 경유 Redis 캐시 기록)
  - 10초 해상도 live — 기존 `graph_buckets.dxy`(1분 bucket 집계)와 경로 분리
  - broadcast 트리거 조건 확장: rates 또는 DXY 변화 어느 쪽이든 발화 (`build_rates_payload()` 전체 JSON 비교)
  - `insert_dxy_rate_into_db()`의 rate/source dedup 덕에 timestamp-only broadcast 폭증 없음
  - REST `/api/rates`는 별도 응답 로직이라 스키마 무변
  - 하위 호환: 구 iOS 앱은 Codable이 unknown `indices` 필드를 자동 무시

- **환율 뉴스 피드 API** (Phase 1B v2, `GET /api/news`):
  - 연합인포맥스 RSS 4개 소스 + KB API (외환+경제탭) 병행 수집
  - KB API는 RSS 대비 ~2시간 빠른 속보 소스, nsid 기반 자동 병합
  - noise_only 일원화 (잡음 제외만: 인사/부고/정치)
  - content_type: `external_link`(일반), `report_pdf`(은행 보고서 PDF 직링크)
  - `*` 기사: 제목에 "(본문없음)" 추가 + link 유지
  - `[전문]` 기사: KB 상세에서 PDF URL 추출, report_pdf로 분류
  - 초보/상보 near-duplicate collapse (시간 클러스터 방식)
  - Redis-only 저장 (24시간 윈도우, ZSET+HASH)
  - 순수 시간순 정렬, 기본 limit=100, hours=24
  - 스케줄: KB 5분마다 :15초, RSS 5분마다 :45초 (모드 무관)
  - 새 모듈: `app/news/` (sources, filters, fetcher, kb_fetcher, upsert)
  - `app/cache.py` ZSET/HASH 메서드 확장 (bytes→str 디코딩 포함)
  - `app/schemas.py` NewsItem, NewsMetadata, NewsResponse 추가

- **DXY rollup 스케줄** (`app/admin/dxy_rollup.py`): realtime → hourly/daily 자동 집계
  - hourly: 매시 :05분, 직전 완료 시간의 realtime close 집계
  - daily: 매일 00:05 KST, 직전 완료일의 hourly(또는 realtime) close 집계
  - source 보존: 원본 realtime의 실제 source를 그대로 사용
  - idempotent: INSERT ON CONFLICT UPDATE
  - 백필(1회성) 종료 이후 구간을 연속 커버
- **수동 gap 복구 함수** (`app/admin/dxy_rollup.py`):
  - `backfill_hourly_range()`: UTC 구간 realtime → hourly 일괄 생성
  - `backfill_daily_range()`: KST 날짜 구간 hourly/realtime → daily 일괄 생성

### Fixed

- **DXY Yahoo fallback 가드 강화** (2026-04-27, ADR-022):
  - ICE DX 주간 세션 OFF (토 06:00~월 07:00 KST DST) Yahoo 저장 차단 (`_is_dxy_weekly_session_open`)
  - Yahoo 값과 마지막 Investing 값 차이 `0.07` 초과 시 저장 보류
  - 단, latest_investing이 fresh일 때만 가격 가드 적용 (mode별 grace: IN 15분 / BREAK 30분)
  - 4/27 06:00 KST 케이스(시장 개장 전 Yahoo 끼어듦) 및 weekend Friday close 점프 모두 차단됨
  - DST/표준시는 `ZoneInfo("America/New_York")`이 자동 처리

- **MIBANK URL/DOM 변경 대응** (2026-04-27):
  - 구 URL `https://www.mibank.me/exchange/bank/index.php?search_code=...`가 `https://exchange.mibank.me/bank`로 리다이렉트되며 은행 코드가 유실되는 문제 수정
  - 9개 MIBANK 사용 크롤러 URL을 `https://exchange.mibank.me/bank?bank_cd=...` 형식으로 변경
  - 새 DOM(`table.main_table.content`) 지원: `flag_<code>_*.png`에서 통화 코드 추출, `기준환율(원)` 헤더 컬럼 기반 환율 추출
  - 신한/NH/SC의 MIBANK 1차 실패 → Selenium fallback 반복 부하 완화
  - Created [MAINTENANCE_2026-04-27.md](MAINTENANCE_2026-04-27.md): 장애 원인, 검증 절차, 복구 플레이북
- **USDT scheduler job 제거 버그** (2026-04-23, `6a86c01`):
  - 앱 시작 시 `start_scheduler`에서 `task_usdt_sources` job 등록 → 직후 `switch_jobs()`가 `task_` prefix 전체 제거
  - mode-agnostic 상시 실행 의도였으나 첫 모드 전환 시점에 즉시 삭제됨
  - 수정: job id `task_usdt_sources` → `usdt_sources` (prefix 분리)
  - Docker/EC2 런타임 검증에서 발견 (로컬 유닛 테스트로는 잡히지 않음)
- **USDT source 알림 dead alert 버그** (2026-04-23, `bc42bb4`):
  - `_validate_phase1_source_asset()`이 `is_phase1_source()`만 검사하여 reference 소스(investing/kb/hana)도 허용
  - 하지만 `process_source_rate_alerts`는 `usdt_sources` 크롤러에서만 호출되므로 reference 알림은 영원히 발동 안 됨 (dead alert)
  - 수정: `category == "exchange"` 검증 추가, reference는 400 + 기존 API 안내
- **1w DXY carry-forward 버그**: 2-part merge 전략에서 hourly gap 구간의 realtime이 누락되는 문제
  - 원인: hourly 마지막 timestamp 이후부터만 realtime을 조회하여, gap 구간의 realtime이 스킵됨
  - 수정: 1w를 **full-window 전략**으로 전환 (hourly + realtime 전체 7일 단일 쿼리, hourly > realtime dedup)
  - hourly gap backfill 58건 실행 (2026-03-10 16:00 ~ 2026-03-13 04:00 UTC)
- **3m/1y DXY carry-forward 버그**: 동일 취약점이 daily gap에서도 발생
  - 수정: **daily + recent realtime tail 7일 overlap** 전략으로 전환 (`_DXY_DAILY_REALTIME_TAIL_DAYS = 7`)
  - daily gap backfill 실행 (2026-03-11 ~ 2026-03-13 KST)

### Changed

- **데이터 보관 정책 30일 통일** (2026-04-27, ADR-023):
  - 은행 환율(`bank_exchange_rates`): 10일 → 30일
  - USDT 거래소 가격(`source_rates`): 10일 → 30일
  - DXY 현물/선물 realtime(`market_index_rates`): 30일 (신규 cleanup)
  - DXY hourly/daily rollup은 삭제 안 함 — 3m/1y 그래프 보존
  - `crud.delete_old_market_index_rates(days=30, granularities=None)` 신규 (default `["realtime"]`)
  - 매일 03:30~03:32 KST 순차 cleanup

- **DXY_MODE 환경변수 제거** (2026-04-27, ADR-024):
  - 현물(`dxy`)과 선물(`dxy_futures`) 둘 다 별도 저장하는 구조로 toggle 의미 상실
  - `app/config.py`에서 상수 제거, `app/scheduler.py`에서 import + 4곳 분기 제거
  - `task_dxy`는 항상 등록 — `crawler_config.dxy` 토글만 적용
  - 운영 EC2 `.env`의 `DXY_MODE` 라인은 무해하지만 정리 권장

- **DXY 스케줄 조정**: 실시간 비교 품질 개선
  - IN/BREAK1/BREAK2: 10초마다 (`04, 14, 24, 34, 44, 54초`)
  - OUT: 10분마다 (`4, 14, 24, 34, 44, 54분 44초`)
  - 브로드캐스트 정각(`00, 10, 20, 30, 40, 50초`) 유지 기준으로 재배치
  - 주말 OUT 모드에서는 다른 크롤러 분/초 슬롯과 겹치지 않도록 분산

## [1.12.0] - 2026-03-10

### Added - DXY (Dollar Index) Graph Indicator

- **DXY 크롤러** (`app/crawlers/dxy.py`): 달러지수 실시간 수집
  - Primary: Investing.com (`kr.investing.com/indices/usdollar`, curl_cffi TLS 지문 위장)
  - Fallback: Yahoo Finance (`yfinance`, ticker `DX-Y.NYB`)
  - 전환 조건: 연속 5회 실패 OR 5분 stale → Yahoo 자동 전환
  - Circuit Breaker: DXY 전용 (investing.py와 독립, 403 쿨다운 동일 정책)
  - 복구: 쿨다운 해제 후 Investing 재시도, 성공 시 즉시 원소스 복귀
- **market_index_rates 테이블**: 범용 시장 지수 데이터 모델
  - `granularity` 컬럼: `realtime` | `hourly` | `daily` (백필과 실시간 구분)
  - UNIQUE 제약: `(instrument, source, timestamp, granularity)`
  - `source` 우선순위: `investing > yahoo` (ROW_NUMBER CASE WHEN)
- **그래프 API 확장**: `/api/graph/{currency}?range=1d|1w|3m|1y`
  - USD/KRW에만 DXY 보조지표 포함 (API key=`dxy`, UI 표시명=달러지수)
  - 1d: 10분 버킷 (realtime only)
  - 1w: 1시간 버킷 (realtime + hourly, lazy cache 10분 TTL)
  - 3m/1y: 1일 버킷 (realtime + daily, lazy cache 1시간 TTL)
- **2-part merge 전략**: 과거(daily/hourly) + 오늘(realtime) 분리 쿼리
  - 1w 오늘: timestamp 단위 `realtime > hourly` 선택 (공존 허용)
  - 3m/1y 오늘: realtime 있으면 daily 전체 제외 (날짜 단위 배타적)
- **히스토리 백필 스크립트** (`scripts/backfill_history.py`)
  - yfinance로 daily(최대 1년) + hourly(최근 7일) 사전 적재
  - `granularity='daily'|'hourly'`로 구분 저장
- **DB 마이그레이션 스크립트** (`scripts/migrate_market_index_granularity.py`)
  - `granularity` 컬럼 추가, UNIQUE 인덱스 재생성
  - PostgreSQL/SQLite 양쪽 지원, `--dry-run` 옵션
- **스케줄러 통합**: 4단계 모드 모두 DXY 등록
  - IN/BREAK1/BREAK2: 매분 42초
  - OUT: 10분마다 (2분 42초)

### Dependencies

- Added: `yfinance` (Yahoo Finance DXY 폴백)

### Migration

**배포 순서** (운영 환경, 상세: [DEPLOYMENT.md](DEPLOYMENT.md#db-마이그레이션-v1120-dxy-granularity)):
1. `git pull` → `docker compose build` (새 이미지 빌드)
2. `docker compose run --rm fastapi python scripts/migrate_market_index_granularity.py`
3. `docker compose up -d` (서비스 시작)
4. `docker compose exec fastapi python scripts/backfill_history.py`

### Documentation

- Added [ADR-019](DECISIONS.md#adr-019-dxy-보조지표---granularity-기반-2-part-merge-전략): DXY granularity 기반 2-part merge 전략
- Updated [CLAUDE.md](CLAUDE.md): DB 스키마, API, 기술 스택, 파일 구조, Phase 1A
- Updated [CRAWLERS.md](CRAWLERS.md): Group D (시장 지수) DXY 크롤러 섹션 추가
- Updated [DEPLOYMENT.md](DEPLOYMENT.md): granularity 마이그레이션 선행 절차

---

## [1.11.1] - 2026-01-29

### Fixed - Investing Cloudflare 403 Block

- **curl_cffi TLS 지문 위장**: `requests` → `curl_cffi` (Investing 크롤러 전용)
  - Cloudflare JA3/JA4 TLS fingerprint 탐지 우회
  - `impersonate="safari17_0"` (Safari 17.0 TLS 핸드셰이크 모방)
  - `chrome131` 시도 → 403 차단, `safari17_0` → 200 OK 성공
  - Graceful 폴백: `_USE_CFFI` 플래그로 curl_cffi 미설치 시 requests 자동 사용
- **Circuit Breaker**: 연속 403 시 점진적 쿨다운 (5회→1분, 10회→5분, 20회→15분)
- **UA 로테이션**: Safari/Chrome UA 풀에서 impersonate에 맞는 UA 선택
- **Jitter**: 0~2초 랜덤 딜레이 (요청 패턴 분산, Broadcasting 타이밍 고려)
- **로그 억제**: 상태 전이 기반 로깅 (차단시작 ERROR 1회, 지속 WARNING 5분마다, 해제 WARNING 1회)

### Dependencies

- Added: `curl_cffi>=0.7.4,<1.0.0` (Investing 크롤러 전용)

### Documentation

- Added [ADR-018](DECISIONS.md#adr-018-investing-cloudflare-차단-대응---curl_cffi-tls-지문-위장): curl_cffi TLS 지문 위장 선택 근거
- Created [MAINTENANCE_2026-01-29.md](MAINTENANCE_2026-01-29.md): 장애 타임라인 및 복구 플레이북
- Updated [CRAWLERS.md](CRAWLERS.md): Investing 크롤러 Cloudflare 대응 상세
- Updated [CLAUDE.md](CLAUDE.md): 기술 스택 및 리스크 대응 현행화

---

## [1.11.0] - 2026-01-21

### Released - iOS App Store

- **iOS 앱 출시**: App Store에 "환율알림 - 대한민국 주요 은행" 앱 게시
- **심사 전략**: "환율 알림" 중심 포지셔닝으로 Guideline 5.1.1 통과
- **거절→통과**: 초기 거절 사유(로그인 강제) → 알림 기능 중심 설명으로 해결

### Infrastructure - Production Ready

- **RDS PostgreSQL 전환**: SQLite → AWS RDS PostgreSQL (db.t4g.micro)
  - SQLAlchemy dialect 자동 감지 (`psycopg2` 의존성 추가)
  - 환경변수 `DATABASE_URL`로 DB 전환
- **도메인 fxi.kr 전환**: Let's Encrypt SSL 인증서 적용 (HTTPS/WSS)
  - TLS 1.2 & 1.3, HSTS 헤더 적용
  - Certbot 자동 갱신 (Systemd Timer)
- **DB 타임스탬프 UTC 통일**: 저장은 UTC, API 응답은 KST(+09:00) 변환
- **Admin 페이지 보안 강화**: 모니터링 카드 확장 (CPU, 메모리 여유도, 브로드캐스트 건강 상태)
- **Shinhan 주말 크롤링**: OUT 모드에서 shinhan 크롤러 유지 (주말 중 가끔 변동)

---

## [1.10.0] - 2026-01-10

### Changed - MIBANK Parsing Logic Overhaul (Currency-Code Based)

- **Currency-code based parsing**: Extract currency from `href="...?currency=USD"` instead of position-based `tr:nth-child(N)`
  - Immune to table row order changes
  - Uses last cell (매매기준율) for rate extraction
  - See [ADR-017](DECISIONS.md#adr-017-mibank-환율-파싱---position-기반-vs-currency-code-기반)

- **3-layer validation system** for data integrity:
  1. **Completeness check**: USD/JPY/EUR all required (`require_all=True`)
  2. **Absolute range check**: USD 1,000~2,000, JPY 600~1,400, EUR 1,100~2,200
  3. **Deviation check**: Dynamic thresholds based on time gap (soft_fail/hard_fail)

- **New utility functions** (`app/crawlers/utils.py`):
  - `crawl_mibank_rates()`: Currency-code based MIBANK crawling
  - `validate_rate_ranges()`: Absolute range validation
  - `evaluate_rate_deviation()`: Dynamic deviation check with soft/hard fail
  - `get_dynamic_thresholds()`: Time-gap based threshold calculator

- **Bank-specific wrapper pattern** (`_crawl_mibank_{bank}()`):
  - All 9 crawlers now use standardized MIBANK wrapper functions
  - Encapsulates: crawling → range validation → deviation check

- **New DB utility** (`app/crud.py`):
  - `get_last_bank_rates_with_ts()`: Fetch last rate + timestamp for deviation check

- **Common constants** (`app/crawlers/constants.py`):
  - `MIBANK_REQUIRED_CODES`: ("USD", "JPY", "EUR")
  - `MIBANK_REQUIRED_PAIRS`: ("usd-krw", "jpy-krw", "eur-krw")
  - `MIBANK_RATE_RANGES`: Absolute range limits per currency

### Fixed

- **Timezone-naive timestamp bug**: DB timestamps without timezone info caused incorrect deviation calculations
  - Fix: KST localize before comparison (`kst.localize(prev_ts)`)

### Removed

- **MIBANK_SELECTORS**: Deleted from all 9 crawlers (dead code after currency-code parsing)

### Deprecated

- **`crawl_and_save_routine()`** in 4 crawlers marked with `[DEPRECATED]` comment:
  - `sc.py`: Selenium-only crawler, requests not applicable
  - `nh.py`: Selenium-only crawler, requires page click navigation
  - `shinhan.py`: SPA page, requires JavaScript rendering
  - `ibk.py`: Replaced by `try_crawl_with_requests()` with enhanced validation

### Documentation

- Added [ADR-017](DECISIONS.md#adr-017-mibank-환율-파싱---position-기반-vs-currency-code-기반): MIBANK parsing architecture decision
- Updated [CRAWLERS.md](CRAWLERS.md): New utility functions and wrapper pattern documentation

---

## [1.9.0] - 2026-01-08

### Added - Account Deletion API (Apple App Store 5.1.1(v) Compliance)

- **Account deletion endpoint**: `DELETE /api/user/me`
  - Deletes all user data: `notification_logs`, `notification_settings`, `user_devices`
  - Response: `204 No Content` on success
  - Security: `check_revoked=True` for destructive operations
- **Enhanced Firebase token verification**
  - New parameter: `check_revoked` for revoked token detection
  - New error handling: `RevokedIdTokenError` → 401, `CertificateFetchError` → 503
  - Network error handling: `TransportError`, `RequestException` → 503

### Planned
- Dynamic priority adjustment based on crawler success rate (Phase 2)
- Multiple Queue system for crawler groups (Phase 3)

---

## [1.8.0] - 2025-12-02

### Added - 24-Hour Graph Feature with WebSocket Integration

- **24-hour graph API** with 10-minute bucket aggregation
  - Endpoint: `GET /api/graph/{currency}` (usd-krw, jpy-krw, eur-krw)
  - Data format: `[timestamp, max, min, close]` (candlestick structure)
  - Carry-forward mechanism: Empty buckets filled with previous close value for data continuity
  - Redis cache: `graph:{currency}` keys (~3.6KB per currency, 120s TTL)
  - Backend: `app/admin/graph_cache.py` - Scheduler runs every minute at :03 seconds
- **WebSocket graph integration** for real-time updates
  - `graph_buckets` field: Last bucket for all 3 currencies (~600 bytes)
  - Broadcast trigger: Only when rates change (change detection)
  - Mobile optimization: Reuse existing WebSocket connection (no additional radio activations)
  - Server efficiency: 1 broadcast/10s vs 100+ HTTP req/min (99% CPU reduction)
- **Band Chart implementation** (frontend)
  - Single source: Close line (main) + High/Low translucent bands
  - Multi-source: Close lines only for comparison
  - Tooltip filtering: Range display for single-source mode
  - Y-axis padding: 5% margin for better visualization
- **Lazy loading with cache strategy**
  - Initial load: Selected currency only (3.6KB)
  - Currency switch: Cache reuse (0 bytes) or on-demand load
  - Gap detection: 15-minute threshold → full refresh
  - Frontend cache: In-memory storage for all 3 currencies
- **Toggle controls** for graph customization
  - Show/Hide individual sources (INVESTING, KB, HANA)
  - Prevent empty graphs: At least 1 source required
  - Persistent selection across currency switches

### Performance
- **Network efficiency**: WebSocket integration reduces mobile data usage by 95%+
- **Initial load**: 3.6KB per currency (145 buckets × 3 sources × 4 values)
- **WebSocket overhead**: +600 bytes per broadcast (0.6KB / 10s)
- **Cache hit rate**: ~100% for currency switches (no repeated API calls)
- **Mobile battery**: Zero additional impact (reuses existing WebSocket)

### Documentation
- Created [GRAPH_FEATURE.md](GRAPH_FEATURE.md) v3.0 - Complete implementation guide
- Added [ADR-015](DECISIONS.md#adr-015-websocket-graph-integration-vs-incremental-api) - Graph update strategy decision

---

## [1.7.0] - 2025-11-27

### Added - Redis Broadcast Cache & Change Detection

- **Redis broadcast cache** for WebSocket initial connection optimization
  - Cache key: `broadcast:latest` (~3.6KB JSON payload)
  - Initial connection: Redis cache → instant data delivery
  - Memory usage: ~1MB stable (~1% of 100MB maxmemory)
  - Circuit Breaker: 5 failures → 30s timeout → auto-recovery
  - Admin API endpoint: `GET /admin/api/redis-status`
- **Change detection system** for broadcast efficiency
  - JSON comparison: `new_json != cached_json`
  - Smart logging: "⏸️ 변경사항 없음" when no change, "📡 브로드캐스트 완료" when changed
  - Bandwidth optimization: Only broadcast when data actually changes
- **Redis monitoring card** in admin dashboard
  - Real-time memory usage and key count display
  - Circuit breaker status visualization
  - Color-coded health indicators (connected/circuit-open/disconnected)

### Fixed
- **Broadcasting bug**: `updated_at` timestamp issue causing "always different" comparisons
  - Root cause: Used `datetime.now()` instead of DB's actual latest timestamp
  - Impact: Change detection never triggered, wasted bandwidth
  - Fix: Use `max(rate["timestamp"])` from DB data for accurate comparison
  - Result: Change detection accuracy improved to 100%

### Performance
- **WebSocket initial connection**: Instant data delivery via Redis cache
- **Bandwidth optimization**: Significant reduction in unnecessary broadcasts during low-volatility periods

### Documentation
- Updated [REDIS_IMPACT_ANALYSIS.md](REDIS_IMPACT_ANALYSIS.md) with actual measurements
- Updated [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md): Corrected ElastiCache free tier info

---

## [1.6.0] - 2025-11-10

### Added - 3-Tier Scheduling Architecture
- **Crawler statistics system** for monitoring success rate and performance
  - Real-time tracking: success/fail counts, avg duration, last execution time
  - Thread-safe collector with singleton pattern
  - Admin API endpoint: `GET /admin/api/crawler/stats`
  - Integrated with both Request and Selenium crawlers
- **3-Tier scheduling architecture** optimized for t3.small/medium:
  - **Tier A (investing)**: Most critical, highest frequency
  - **Tier B (kb, hana, woori, bs, citi)**: Important, moderate frequency
  - **Tier C (shinhan, ibk, nh, sc)**: Selenium-based, lowest frequency
- **IN mode: cron-based absolute timing** (Broadcasting synchronization)
  - A Group: 10s interval (5s before Broadcasting @ 00, 10, 20s)
  - B Group: 20-60s interval (3-7s before Broadcasting, staggered)
  - C Group: 33.3-150s interval (Broadcasting independent)
- **OUT mode: cron-based hourly distribution** (Zero concurrent execution)
  - A Group: Every 10 minutes (05, 15, 25, 35, 45, 55 min)
  - B Group: Every 10-60 minutes (fully distributed across the hour)
  - C Group: Once per hour (fully distributed: 07, 17, 27, 36, 47, 57 min)

### Changed
- **Worker health check optimization**: 180s → 90s stuck detection
  - Rationale: Timeout 45s × 2 = 90s is sufficient (heartbeat updates at job start)
  - Faster problem detection and auto-restart
- **Selenium timeout adjustment**: shinhan 60s → 45s (consistency with other crawlers)
- **Scheduling strategy**:
  - IN mode: cron with second precision (e.g., `second='5,15,25,35,45,55'`)
  - OUT mode: cron with minute precision (e.g., `minute='5,15,25,35,45,55'`)
  - Request crawlers: Direct execution with stats wrapper
  - Selenium crawlers: Queue-based execution (unchanged)
- **Statistics wrapper**: All crawlers now tracked via `make_request_crawler_wrapper()`

### Performance
- **OUT mode resource distribution**: 10 crawlers spread across 60 minutes
  - Peak concurrent crawlers: 1 (down from potential 6-8)
  - CPU spike elimination: No simultaneous execution
- **Faster failure detection**: Worker restart in 90s vs 180s
- **Better monitoring**: Real-time success rate and duration tracking per crawler

### Documentation
- Added [ADR-009](DECISIONS.md) documenting 3-Tier scheduling architecture
- Updated scheduler.py with comprehensive inline documentation
- Added `app/admin/crawler_stats.py` with usage examples

---

## [1.5.0] - 2025-11-10

### Added
- **Worker health check system** for detecting and auto-restarting stuck workers
  - Heartbeat tracking: Updates every job completion
  - Stuck detection: >180 seconds on same job → automatic restart
  - Health check interval: Every 60 seconds
- **Queue pressure relief policy** (80% threshold)
  - Rejects new jobs when queue >80% full (20/25)
  - Prevents queue overflow and APScheduler blocking
  - Logs rejected jobs for monitoring
- Worker status tracking: `selenium_worker_last_heartbeat`, `selenium_worker_current_job`

### Changed
- **Timeout reduction (50% cut)** for real-time performance:
  - hana: 60s → 30s
  - ibk: 90s → 45s
  - nh: 90s → 45s
  - sc: 90s → 45s
  - shinhan: 120s → 60s
- **Queue size optimization**: 50 → 25
  - Rationale: 80% pressure relief (20/25) + memory savings
  - Prevents excessive job accumulation
- **Crawler schedule adjustment** (IN mode):
  - hana: 20s (unchanged, fastest crawler)
  - shinhan: 60s (adjusted for queue balance)
  - ibk: 60s (reduced frequency for slow crawler)
  - nh: 90s (further reduced for slowest crawler)
  - sc: 120s (minimal frequency for reliability)

### Fixed
- **Queue saturation (100%)** → Reduced to ~80% with pressure relief
- **Worker stuck issue** → Auto-restart when heartbeat >180s
- **Real-time performance degradation** → NH crawler timeout enforced at 45s
- **APScheduler blocking** → Non-blocking queue operations prevent scheduler freeze

### Performance
- Queue utilization: 100% (50/50) → 80% (20/25) stable
- Timeout enforcement: Slow crawlers (66s) now properly timeout at 45s
- Memory savings: Queue size reduction contributes to overall stability
- Worker reliability: Auto-restart ensures continuous operation

### Tradeoffs
- ⚠️ **Real-time vs Completeness**: Timeouts may reject slow crawlers during high Swap usage
- ⚠️ **Queue pressure**: 80% rejection may skip some scheduled jobs (retry on next cycle)
- ✅ **System stability**: Prioritized over 100% data collection rate

### Documentation
- Added [ADR-008](DECISIONS.md) documenting Queue pressure relief strategy
- Updated [CLAUDE.md](CLAUDE.md) scheduling section with new queue size and policies

---

## [1.4.0] - 2025-11-08

### Added
- **Priority Queue system** for Selenium crawlers (AsyncIO PriorityQueue)
- Crawler priority mapping based on execution speed:
  - hana (0): Fastest → 1st priority
  - ibk (1): Medium → 2nd priority
  - nh, sc (2-3): Slow → 3rd-4th priority
  - shinhan (4): Slowest → 5th priority
- **Individual timeout settings** per crawler (60-120 seconds)
- **Automatic retry mechanism** with lower priority (+1000) on failure
- `execute_with_timeout()` wrapper function for timeout enforcement
- Chrome memory optimization:
  - `--window-size=400,300` (reduced from 800x600)
  - `--disable-javascript` for JS engine memory saving
  - `--disable-webgl` for WebGL memory release
- Monitoring logging enabled (DEBUG → INFO level)

### Changed
- `asyncio.Queue` → `asyncio.PriorityQueue` in `app/scheduler.py`
- Selenium timeout values increased to handle Swap I/O delays:
  - hana: 30 → 60 seconds
  - ibk/nh/sc: 60 → 90 seconds
  - shinhan: 90 → 120 seconds
- Queue enqueue format: simple data → priority tuple `(priority, timestamp, job_func, bank_name, is_retry)`
- Worker execution: added timeout wrapper and retry logic
- PriorityQueue maxsize: 10 → 20 (increased capacity)

### Fixed
- **Selenium crawler timeout issues** caused by memory pressure (100% resolution)
- Swap memory usage causing 2-3x slower Chrome process creation
- False timeout failures when Swap I/O delays exceeded fixed timeout limits
- Slow crawlers blocking fast crawlers in simple FIFO Queue

### Performance
- **Timeout occurrence**: 80% failure rate → 0% (60-second monitoring)
- Crawler execution times: All within normal range (0.36-17.50 seconds)
- Priority Queue ordering: Guaranteed execution order (hana → ibk → nh → sc → shinhan)
- Chrome memory per instance: Estimated 50-80MB additional savings
- System stability: RAM 71.9%, Swap 28.2% with no timeouts

### Documentation
- Added [ADR-007](DECISIONS.md) documenting Priority Queue + Timeout strategy
- Created [MAINTENANCE_2025-11-08.md](MAINTENANCE_2025-11-08.md) with detailed implementation
- Updated [CLAUDE.md](CLAUDE.md) scheduling section with Priority Queue info
- Updated Docker deployment section with 3-tier approach (daily/cache/reset)

---

## [1.3.0] - 2025-11-06

### Added
- AsyncIO Queue system for Selenium crawler sequential execution
- Queue Worker for processing Selenium tasks one at a time
- `init_selenium_queue()` and `shutdown_selenium_queue()` lifecycle management
- Crawler grouping: `REQUEST_BASED_TASKS` and `SELENIUM_BASED_TASKS`

### Changed
- **Breaking:** Replaced `BackgroundScheduler` with `AsyncIOScheduler`
- Split crawler tasks into Request-based (concurrent) and Selenium-based (sequential)
- Modified `switch_jobs()` to handle two different execution patterns
- Updated FastAPI `lifespan()` to initialize/shutdown Queue system

### Removed
- Threading Semaphore from `app/crawlers/utils.py`
- Semaphore acquire/release logic from all Selenium crawlers

### Fixed
- Selenium crawler Semaphore contention causing false MIBANK fallbacks (30-40% reduction)
- Memory instability from multiple Chrome instances (50% reduction: 650MB → 320MB)
- Unnecessary fallback to MIBANK when normal collection was possible

### Performance
- Memory usage: 650MB → 320MB (50% improvement)
- Fallback occurrence: 30-40% → 0%
- Sequential execution guarantee for Selenium crawlers

### Documentation
- Added [ADR-006](DECISIONS.md) documenting AsyncIO Queue decision
- Created [MAINTENANCE_2025-11-06.md](MAINTENANCE_2025-11-06.md) with detailed implementation
- Updated [CLAUDE.md](CLAUDE.md) scheduling section

---

## [1.2.0] - 2025-11-05

### Added
- System monitoring module (`app/admin/monitor.py`)
- Chrome process monitoring with count and memory tracking
- Zombie Chrome process cleanup scheduler (runs every 3 minutes)
- Force kill logic for Chrome processes older than 5 minutes
- Monitoring statistics collection (every 5 minutes)
- Admin dashboard API endpoints:
  - `GET /admin/api/monitor/current` - Current system status
  - `GET /admin/api/monitor/history?hours=N` - Historical data
- Chrome process card in admin dashboard UI

### Changed
- Chrome max lifetime: 10 minutes → 5 minutes (`CHROME_MAX_LIFETIME_SECONDS`)
- Chrome cleanup interval: 5 minutes → 3 minutes (`CHROME_CLEANUP_INTERVAL_MINUTES`)
- Log backup count: 5 → 3 (50MB → 30MB total)
- Docker memory limit: 700M → 800M
- Disabled unnecessary system services (snapd, multipathd)

### Fixed
- Selenium zombie process accumulation causing memory leaks
- WebSocket ConnectionManager ValueError on disconnect
- Unsafe list iteration in WebSocket broadcast causing potential crashes
- Chrome processes not properly terminated after `driver.quit()` failure

### Performance
- Memory usage: 647MB → 581MB (66MB improvement)
- Free memory: 309MB → 375MB
- Disk usage: 15GB → 6.3GB (8.7GB reclaimed)
- Chrome zombie process automatic cleanup every 3 minutes

### Documentation
- Created [MAINTENANCE_2025-11-05.md](MAINTENANCE_2025-11-05.md)
- Added [REBOOT_CHECKLIST.md](REBOOT_CHECKLIST.md)

---

## [1.1.0] - 2025-10-26

### Changed
- SC bank crawler: Switched from Selenium to Request-based (cost reduction)
- WOORI bank crawler: Applied SC approach (simplified logic)
- IBK bank crawler: Added Selenium retry logic with MIBANK fallback

### Fixed
- SC bank Alert handling consolidated to single pattern
- Selector fallback improvements for multiple banks

---

## [1.0.1] - 2025-10-25

### Added
- Domain-based project structure:
  - `app/crawlers/` - Crawler domain
  - `app/admin/` - Admin domain
  - `app/notifications/` - Notification domain
- Centralized constants: `app/crawlers/constants.py`
- Common crawler utilities: `app/crawlers/utils.py`
- Log cleaner module: `app/admin/log_cleaner.py`
- WebSocket broadcast statistics: `app/admin/stats.py`

### Changed
- Refactored 10 crawler files to use centralized constants and utilities
- Moved admin-related logic to dedicated domain
- Improved code organization and maintainability

### Documentation
- Added [CRAWLERS.md](CRAWLERS.md) - Detailed crawler implementation guide
- Added [DECISIONS.md](DECISIONS.md) - Architecture Decision Records (ADR)

---

## [1.0.0] - 2025-10-17

### Added
- Initial release of Exchange Rate Comparison Service
- 10 crawler sources:
  - Investing.com (reference rate)
  - 9 Korean banks: KB, Hana, Shinhan, Woori, IBK, NH, SC, Busan, Citi
- Support for 3 currency pairs: USD-KRW, JPY-KRW, EUR-KRW
- FastAPI backend with WebSocket support
- SQLite database with automatic cleanup (10-day retention)
- APScheduler with IN/OUT mode (business hours detection)
- Admin dashboard (`/admin`) with:
  - Real-time crawler status
  - Log viewer (2 tabs: all logs, errors only)
  - WebSocket connection monitoring
  - Memory usage tracking
  - Broadcast statistics
- REST API endpoints:
  - `GET /api/rates` - All exchange rates
  - `GET /api/rates/{currency}` - Specific currency
  - `GET /api/investing/{pair}` - Investing.com rates
  - `GET /api/banks/{pair}` - All bank rates
  - `GET /health` - Health check
- Docker deployment with:
  - Multi-stage build
  - Google Chrome (AMD64 optimized)
  - 700MB memory limit
- Structured logging system:
  - JSON format in production
  - Colored output in development
  - Log rotation (10MB, 5 backups)
  - Auto cleanup (10 days)

### Documentation
- [CLAUDE.md](CLAUDE.md) - Project guide
- [DOCKER.md](DOCKER.md) - Docker deployment guide

---

## Version History

| Version | Date | Highlights |
|---------|------|------------|
| 1.12.0 | 2026-03-10 | DXY 달러지수 보조지표 (Investing + Yahoo 이중 소스, granularity 2-part merge) |
| 1.11.1 | 2026-01-29 | Investing Cloudflare 403 대응 (curl_cffi TLS 지문 위장) |
| 1.11.0 | 2026-01-21 | iOS 출시 + RDS PostgreSQL + 도메인 fxi.kr + Admin 보안 강화 |
| 1.10.0 | 2026-01-10 | MIBANK Currency-Code parsing + 3-layer validation |
| 1.9.0 | 2026-01-08 | Account Deletion API (Apple App Store 5.1.1(v) Compliance) |
| 1.8.0 | 2025-12-02 | 24-hour graph + WebSocket integration + Band Chart |
| 1.7.0 | 2025-11-27 | Redis broadcast cache + Change detection |
| 1.6.0 | 2025-11-10 | 3-Tier scheduling + Crawler statistics |
| 1.5.0 | 2025-11-10 | Queue pressure relief + Health check + Timeout optimization |
| 1.4.0 | 2025-11-08 | Priority Queue + Timeout strategy |
| 1.3.0 | 2025-11-06 | AsyncIO Queue for Selenium crawlers |
| 1.2.0 | 2025-11-05 | System monitoring & zombie process cleanup |
| 1.1.0 | 2025-10-26 | Crawler optimizations (SC, WOORI, IBK) |
| 1.0.1 | 2025-10-25 | Domain-based refactoring |
| 1.0.0 | 2025-10-17 | Initial release |

---

## Migration Guide

### Upgrading from 1.4.x to 1.5.x

**Non-Breaking Changes:**
- No API changes
- No configuration changes required
- Drop-in replacement for v1.4.x

**Steps:**
1. Pull latest code
2. Review changes in `DECISIONS.md` (ADR-008: Queue pressure relief strategy)
3. Rebuild Docker: `docker compose up -d --build`
4. Monitor queue pressure: `docker logs -f exchange-rate-app | grep "Queue 압력"`
5. Verify health check: `docker logs -f exchange-rate-app | grep "헬스체크\|멈춤 감지"`

**Expected Improvements:**
- Queue saturation reduced from 100% to ~80%
- Real-time performance improved (NH crawler now timeout at 45s)
- Automatic worker recovery on stuck (>180s)
- Memory savings from smaller queue (50 → 25)

**Monitoring:**
- Watch for "Queue 압력 초과로 skip" warnings (acceptable, system working as designed)
- Verify timeout enforcement: Slow crawlers should timeout within new limits
- Check health check logs: Workers should auto-restart if stuck >180s

**Rollback:**
- Revert `app/crawlers/constants.py` SELENIUM_TIMEOUT_MAP (×2 values)
- Revert `app/scheduler.py` queue size (25 → 50) and remove pressure relief logic
- Comment out health check function and job

---

### Upgrading from 1.3.x to 1.4.x

**Non-Breaking Changes:**
- No API changes
- No configuration changes required
- Drop-in replacement for v1.3.x

**Steps:**
1. Pull latest code
2. Review `MAINTENANCE_2025-11-08.md` for detailed changes
3. Rebuild Docker: `docker compose up -d --build` (or `--no-cache` if issues)
4. Verify Priority Queue in logs: `docker logs -f exchange-rate-app | grep "우선순위\|Priority"`

**Expected Improvements:**
- 100% resolution of Selenium timeout issues
- Guaranteed execution order (fast crawlers first)
- Additional 50-80MB memory savings per Chrome instance
- Automatic retry for failed crawlers

**Rollback:**
- All changes preserved as comments in code
- Simply uncomment old code and recomment new code in:
  - `app/crawlers/constants.py`
  - `app/admin/monitor.py`

---

### Upgrading from 1.2.x to 1.3.x

**Breaking Changes:**
- `BackgroundScheduler` → `AsyncIOScheduler`
- If you have custom scheduler code, update to use async-compatible patterns

**Steps:**
1. Pull latest code
2. Review `MAINTENANCE_2025-11-06.md` for detailed changes
3. Rebuild Docker: `docker compose build --no-cache`
4. Restart: `docker compose up -d`
5. Verify Queue Worker in logs: `docker logs -f exchange-rate-app | grep "Queue"`

**Expected Improvements:**
- 50% memory reduction (650MB → 320MB)
- 100% elimination of false MIBANK fallbacks
- Smoother Selenium crawler execution

---

## Contributing

Please update this CHANGELOG when making significant changes following these guidelines:

- **Added** for new features
- **Changed** for changes in existing functionality
- **Deprecated** for soon-to-be removed features
- **Removed** for now removed features
- **Fixed** for any bug fixes
- **Security** for vulnerability fixes
- **Performance** for performance improvements
- **Documentation** for documentation updates

---

**Last Updated**: 2026-03-10
