# 크롤러 특수 로직 가이드

> 📅 **마지막 업데이트**: 2026-08-29
> 📚 **관련 문서**: [CLAUDE.md](CLAUDE.md), [DECISIONS.md](DECISIONS.md)
> 📌 **범위**: 이 문서는 환율 크롤러(`app/crawlers/`)만 다룹니다. 뉴스 수집(`app/news/`)은 코드 + [CLAUDE.md](CLAUDE.md) Phase 1B 섹션 참고.
> 🆕 **최근 변경**:
> - IBK 공식 날짜 지정 Request fast path: `inDate` POST + 응답 계약 검증, Selenium은 장애 안전망으로 격하
> - MIBANK URL/DOM 변경 대응: `exchange.mibank.me/bank?bank_cd=` 형식 + `table.main_table.content` 파싱 ([MAINTENANCE_2026-04-27.md](MAINTENANCE_2026-04-27.md))
> - DXY 수집 분리: 현물(`instrument='dxy'`) `dxy_spot.py` 독립 크롤러 (`/indices/usdollar` `__NEXT_DATA__` → CSS → CNBC → Yahoo) + 선물(`instrument='dxy_futures'`) `investing.py` 동반 추출 (`#sb_last_8827` → `/currencies/us-dollar-index`). `dxy.py`는 양쪽 외부 폴백 유틸 모듈
> - Investing 크롤러 Cloudflare 403 차단 대응: curl_cffi TLS 지문 위장 ([ADR-018](DECISIONS.md#adr-018-investing-cloudflare-차단-대응---curl_cffi-tls-지문-위장))
> - MIBANK 파싱 로직 전면 개편: Currency-Code 기반 + 3단계 검증 ([ADR-017](DECISIONS.md#adr-017-mibank-환율-파싱---position-기반-vs-currency-code-기반))
> - 9개 은행 크롤러 `_crawl_mibank_*()` 래퍼 패턴 적용
> - 미사용 함수 `[DEPRECATED]` 주석 표시 (sc, nh, shinhan, ibk)

## 목차
- [개요](#개요)
- [환율 고시 스케줄](#환율-고시-스케줄)
- [크롤러 분류](#크롤러-분류)
- [상세 가이드](#상세-가이드)
- [트러블슈팅](#트러블슈팅)
- [새 은행 추가 가이드](#새-은행-추가-가이드)

---

## 개요

각 은행 웹사이트의 **구조와 특성**, 그리고 **환율 고시 시간**에 따라 크롤링 방식과 스케줄이 다릅니다. 이 문서는 각 크롤러의 특수 로직, 환율 고시 시간, 그리고 주의사항을 정리합니다.

### 왜 통합하지 않았나?

각 크롤러마다 **고유한 특수 로직**이 있어 통합 시 복잡도가 급증합니다:
- NH: 클릭 이동, IBK: 공식 날짜 지정 POST + Selenium 날짜 input fallback
- Woori/SC: AJAX 응답 감지, Hana: iframe 전환
- → **독립성 유지 + 공통 부분만 중앙화** 전략 채택

---

## 환율 고시 스케줄

각 은행의 실제 환율 고시 운영 시간 (실제 데이터 추적 관찰 결과):

| 은행 | 고시 시작 | 고시 종료 | BREAK1<br>(19:00~06:00) | BREAK2<br>(06:00~08:00) | OUT<br>(주말) |
|------|----------|----------|------------------------|------------------------|--------------|
| **investing** | 월 06:00 | 토 06:00 | ✅ | ✅ | ✅ |
| **kb** | 평일 08:30 | 익일 05:00 | ✅ | ⚠️ 등록 유지(고시 window 밖) | ✅ |
| **hana** | 평일 08:30 | 익일 06:00 | ✅ | ⚠️ 등록 유지(고시 window 밖) | ✅ (주말 중 가끔 변동) |
| **shinhan** | 평일 08:19 | 익일 02:45 | ✅ (~02:59:18) | ❌ | ✅ (주말 중 가끔 변동) |
| **woori** | 평일 08:30 | 익일 05:00 | ✅ (~05:04:53) | ❌ | ❌ |
| **ibk** | 평일 약 08:26~08:30 | 익일 06:00 | ✅ (~05:59:34) | ⚠️ terminal capture만 (06:00:34·06:01:34, 화~토) | ❌ |
| **nh** | 평일 08:40 | 당일 24:00 | ✅ | ⚠️ 등록 유지(고시 window 밖) | ✅ (가끔 고시) |
| **sc** | 평일 09:00 | 당일 ~17:50 (관측) | ❌ (19:00 진입과 함께 종료) | ❌ | ❌ |
| **bs** | 평일 08:10 | 당일 24:00 | ✅ | ⚠️ 등록 유지(고시 window 밖) | ✅ (일요일 가끔) |
| **citi** | 평일 09:00 | 익일 06:00 | ✅ | ⚠️ 등록 유지(고시 window 밖) | ❌ |

> **2026-08-28 갱신 근거**
> - **우리 05:00 / IBK 06:00 연장**: 사용자가 각 은행 환율고시 페이지에서 직접 확인. IBK는 **05:59:55**에
>   마지막 고시한 날도 관측됨 → BREAK1(~06:00)의 마지막 정규 실행 05:59:34로는 놓치므로
>   BREAK2에 `task_ibk_terminal`(06:00:34·06:01:34, `day_of_week='tue-sat'`) 추가 —
>   **추가 수집 시도 2회이지 포착 보장은 아니다.**
>   화~토인 이유: IBK 평일 세션이 익일 06:00에 **화~토 아침에만** 종료된다.
> - **IBK 05시 이후 실제 변동 재확인(2026-08-29)**: 공식 일자별 화면의 2026-08-18~27 표본에서
>   야간 꼬리 고시가 있었던 8개 세션 모두 05시 이후 USD 매매기준율이 실제로 1회 이상 변했다
>   (총 39회, 마지막 실제 변경은 세션별 05:06:55~05:58:41). 따라서 수집 주기는 1분을 유지한다.
>   토·일 조회기준일과 8/17 대체공휴일은 공식 무고시였지만, 금요일 조회기준일의 고시는 토요일
>   새벽까지 이어질 수 있으므로 토요일 이른 시각 수집과 terminal capture는 유지한다.
> - **IBK 주간 시작 경계 보강(2026-08-29)**: 같은 공식 상세 화면에서 1회차가 08:26:29에도
>   시작한 세션을 확인했다. 따라서 코드의 조회기준일 전환은 08:30이 아니라 안전한 공백 경계
>   **08:00**으로 둔다.
> - **IBK 개장 전 표 준비 상태 보강(2026-09-03)**: 08:00~첫 고시 전 실응답은 공식 무고시
>   코드가 아니라, 당일 날짜 readback은 맞지만 표와 무고시 코드가 모두 없는 형태였다.
>   08:00~08:34:59에 한해 이 형태를 `preopen_table_pending`으로 분리하고, 이전 서비스일의
>   공식 POST와 DB 회귀 방지로 기존값 유지 또는 놓친 최종 고시 보충을 결정한다.
> - **신한 02:45**: 사용자 확인. BREAK1 cron을 `hour='19-23,0-2'`로 제한 → 02:59:18이 마지막(현행 동작 고정).
> - **SC ~17:50**: 운영 DB 실측(최근 30일 22영업일, usd/jpy/eur 전 통화). 최종 변경 **17:49:00**,
>   18:00 이후 변경행 **0건**(18:00~20:59에 3,960회 폴링). 단 SC는 mibank 우선 경로라 이 수치는
>   "SC 공식 고시 종료 시각"이 아니라 **"그 구간에 우리가 잡는 변경이 없다"**는 뜻이다.
>   여유를 둬 19:00까지 유지(마지막 실행 18:59:58).

**모드 열 기준(고시 가능 시간은 앞의 시작/종료 열과 별도 축):**
- ✅: 해당 모드에 scheduler job 등록
- ⚠️: job은 등록되지만 알려진 고시 window 밖이 포함됨
- ❌: 해당 모드에 job 미등록

---

## 크롤러 분류

### 📦 Group A: 순수 Request (Selenium 없음)

| 은행 | 파일 | 폴백 순서 | mibank 조건 |
|------|------|----------|------------|
| **Investing** | `app/crawlers/investing.py` | 메인 → 서브 | - |
| **KB국민** | `app/crawlers/kb.py` | 메인 → 서브 → mibank | 조건 없음 (항상 시도) |
| **부산** | `app/crawlers/bs.py` | 부산은행 → mibank | 평일 10:00~23:59만 허용 |
| **씨티** | `app/crawlers/citi.py` | 씨티 메인 → 씨티 서브 → mibank | 평일 10:00~23:59만 허용 |

**공통점**:
- `requests.get()` + `BeautifulSoup` 사용 (Investing만 `curl_cffi` 사용, TLS 지문 위장)
- **Selenium 없음** (가장 빠르고 가벼움)
- 정적 HTML 파싱

**mibank 사용 패턴:**
- **조건부 사용 (BS, CITI)**: `is_mibank_rate_reliable()` 함수로 시간대 체크 (평일 10:00~23:59만 허용)
- **조건 없이 사용 (KB)**: 항상 mibank 시도
- **배경**: mibank는 00:00~09:59, 주말에는 영업일 마지막 환율(자정 직전)을 제공하여 부정확할 수 있음

---

### 🔧 Group B: 하이브리드 Request → Selenium 폴백

| 은행 | 파일 | 폴백 순서 | mibank 조건 |
|------|------|----------|------------|
| **하나** | `app/crawlers/hana.py` | Request → Selenium subprocess → mibank | 조건 없음 (항상 시도) |
| **우리** | `app/crawlers/woori.py` | Request → Selenium subprocess → mibank | 평일 10:00~23:59만 허용 |

**공통점**:
- **Request 우선**: 빠른 응답 (80~90% 성공)
- **Selenium 폴백**: Request 실패 시 자동 전환
- **subprocess 격리**: Selenium 실행 시 Chrome 좀비화 방지
- **시스템 부하 감소**: Request가 대부분 성공하므로 Queue 압력 최소화

**mibank 사용 패턴:**
- **조건부 사용 (WOORI)**: `is_mibank_rate_reliable()` 함수로 시간대 체크 (평일 10:00~23:59만 허용)
- **조건 없이 사용 (HANA)**: 항상 mibank 시도 (3차 폴백)

---

### ⚙️ Group C: Selenium Queue (Request 먼저 시도, 2025-11-16 변경)

| 은행 | 파일 | 폴백 순서 | mibank 조건 |
|------|------|----------|------------|
| **신한** | `app/crawlers/shinhan.py` | Request(mibank) → Selenium subprocess | 조건 없음 (항상 시도) |
| **NH** | `app/crawlers/nh.py` | Request(mibank) → Selenium subprocess | 조건 없음 (항상 시도) |
| **SC** | `app/crawlers/sc.py` | Request(mibank) → Selenium subprocess | 조건 없음 (항상 시도) |
| **IBK** | `app/crawlers/ibk.py` | 08:00 이후 공식 당일 GET / 이전은 날짜 POST 직행 → Selenium subprocess(3회) → mibank(조건부) | 평일 10:00~23:59만 허용 |

**공통점**:
- **Request 우선**: 신한/NH/SC는 MIBANK, IBK는 시간대에 맞는 공식 GET/날짜 POST를 우선
- **Selenium 폴백**: Request 실패 시 자동 전환
- **AsyncIO Queue**: 순차 실행 (메모리 제어)
- **subprocess 격리**: 모든 Selenium 실행이 subprocess로 격리
- **영업시간 외 날짜 변경**: IBK 정상 경로는 공식 POST, Selenium 날짜 input은 fallback. SC/Woori는 기존 날짜 변경 로직 사용
- **MAX_DAYS_LOOKBACK**: 최대 10일 과거 조회 (constants.py 중앙 관리)

**mibank 조건부 사용 (IBK):**
- `is_mibank_rate_reliable()`는 **4차 폴백(mibank)에만** 걸린 시간 게이트다
  (`weekday in 0..4 and hour > 9` → 실제 허용 구간은 **평일 10:00~23:59**).
- 공식 Request에는 시간 게이트가 없다. 08:00 이후는 당일 GET을 먼저 시도하고,
  08:00 전에는 빈 당일 GET을 생략해 `try_crawl_with_dated_requests()`가 전 조회기준일부터
  확인한다. 주말 조회기준일은 건너뛰고, 요청 날짜 readback이 일치한 정확한
  `ECBKFEX01589` 무고시 응답이면 이전 평일 후보로 계속 진행한다. 08:00~08:34:59에는
  정확한 당일 readback 뒤 표와 무고시 코드가 모두 없는 응답도 개장 전 준비 상태로 제한해
  같은 lookback을 허용한다. timeout·HTTP 오류·날짜 불일치·명시적으로 판별한 오류 문구·표가 존재하는 응답의
  header/value 계약 위반은 과거값으로 오인하지 않고 즉시 Selenium 안전망으로 넘긴다. 같은 모양의 미지의
  오류 HTML과 개장 전 응답은 완전히 구분할 수 없으므로 이 예외는 당일 35분으로 한정한다.
- 날짜 응답은 표 caption/header, USD·JPY·EUR 완전성/범위, 고시완료시각을 검증한다. 완료시각은
  서버·은행 시계 차이를 고려해 조회시각보다 최대 120초 앞선 값만 허용한다. 무고시 뒤 찾은
  과거 후보는 공식 완료시각이 DB 저장시각보다 뒤일 때 놓친 최종 고시 catch-up으로 저장한다.
  통화별 DB 저장시각이 공식 완료시각보다 120초 넘게 최근이고 값도 다르면 그 통화만 보존해
  회귀를 막고, 누락되거나 안전한 통화는 계속 채운다. 모든 후보가 공식 무고시일 때도 DB의
  3개 통화 값·시각이 모두 있을 때만 보존하고, 빈/부분 DB는 bootstrap 안전망으로 fallback한다.
- **변경 전 운영 기준선**: 2026-08-28 야간 00:05~02:59의 175회가 사실상 전량 ≈10.2초였고
  매 실행 Chrome이 떴다(주간 ≈3.2초). 정상 날짜 POST 성공 시 브라우저 함수는 호출되지 않지만
  기존 subprocess queue와 800M 상한은 불변이며, 실제 메모리·큐 개선은 배포 후 측정한다.
- **BREAK1 (19:00~06:00)**: 매분 `:34` 실행, 마지막 05:59:34.
- **BREAK2 (06:00~08:00)**: 정규 job 없음. `task_ibk_terminal`만 06:00:34·06:01:34(화~토) 실행.
- **OUT(주말)**: 비활성.

---

### 📊 Group D: 시장 지수 (Market Index)

| 지수 | 수집 방식 | 폴백 순서 | 특이사항 |
|------|----------|----------|---------|
| **미국 달러지수(DXY)** | `dxy_spot.py` 독립 운영 피드 | 1차: `/indices/usdollar` `__NEXT_DATA__` → 2차: 같은 페이지 CSS → 3차: CNBC `.DXY` → 4차: Yahoo Finance | 현물/운영 DXY, `instrument='dxy'`, 외부 폴백(CNBC/Yahoo)은 시간/가격/fresh-age 가드 후 저장 |
| **미국달러지수 선물** | `investing.py`에서 환율과 동반 추출 | 1차: `#sb_last_8827` (exchange-rates-table) → 2차: `/currencies/us-dollar-index` | 선물/CFD 계열, `instrument='dxy_futures'`, Yahoo 미사용 |

**특징**:
- **DXY 현물/운영 피드**: `dxy_spot.py`가 독립 스케줄로 수집
- **DXY 선물**: `investing.py`의 exchange-rates-table 크롤링 시 환율과 함께 추출
- `dxy.py`는 CNBC/Yahoo 현물 폴백과 Investing 선물 폴백 함수를 제공하는 유틸리티 모듈
- 은행 환율과 다른 테이블 (`market_index_rates`) 사용
- 현물과 선물은 `instrument='dxy'|'dxy_futures'`로 분리 저장
- DB `source` 컬럼 우선순위: `investing > cnbc > yahoo` (그래프/조회 dedup 적용)

**폴백 정책:**
- **DXY 현물/운영 피드**: 외부 폴백(CNBC → Yahoo) 저장 전 공통 가드 적용
  - ICE DX 주간 세션 OFF 시 chain 전체 차단
  - 마지막 Investing 값이 fresh일 때 0.07 초과 차이면 chain 전체 보류 (외부값 자체 outlier 신호로 간주)
  - chain 내 fetch 실패는 다음 source로 진행 (네트워크 장애 vs outlier 구분)
- **DXY 선물**: Yahoo를 사용하지 않고 Investing 계열 페이지만 사용
- **선물 폴백 쿨다운**: 60초 (retry storm 방지, 셀렉터 장기 파손 대비)

### 🪙 Group E: 가상자산 거래소 (USDT Phase 1, 2026-04-23)

| 거래소 | API 엔드포인트 | price 필드 |
| --- | --- | --- |
| **업비트** | `GET https://api.upbit.com/v1/ticker?markets=KRW-USDT` | `[0].trade_price` |
| **빗썸** | `GET https://api.bithumb.com/v1/ticker?markets=KRW-USDT` | `[0].trade_price` |
| **코인원** | `GET https://api.coinone.co.kr/public/v2/ticker_utc_new/KRW/USDT` | `tickers[0].last` |
| **코빗** | `GET https://api.korbit.co.kr/v2/tickers?symbol=usdt_krw` | `data[0].close` |
| **고팍스** | `GET https://api.gopax.co.kr/trading-pairs/USDT-KRW/ticker` | `price` |

**통합 크롤러**: `app/crawlers/usdt_sources.py`

**특징**:

- **단일 통합 크롤러**: 5개 거래소를 하나의 scheduler job(`usdt_sources`)에서 병렬 처리
- **Fan-out**: `ThreadPoolExecutor(max_workers=5)`, 개별 timeout 2초
- **24/7 상시 실행**: 크립토는 시간 제약 없음, 4단계 모드(IN/BREAK1/BREAK2/OUT) 무관
- **Cron**: `second='6,16,26,36,46,56'` (고정 10초 cadence, 은행 레인과 분산)
- **공개 API**: 모든 거래소 인증 없이 REST로 조회 가능
- **변경 시에만 INSERT**: `insert_source_rate_if_changed()` (기존 bank 크롤러 패턴 재사용)

**데이터 모델**:

- 은행 환율과 다른 테이블 (`source_rates`) 사용 — Decision E에 따라 기존 bank 세계와 분리
- DB 내부: `source + asset` (예: `source="upbit"`, `asset="usdt-krw"`)
- API 응답: `bank + currency`로 어댑터 변환 (`get_source_rates_as_legacy_format()`)
- 정렬: `source_registry.sort_order` 기준 (업비트 → 빗썸 → 코인원 → 고팍스 → 코빗)

**스케줄러 job id 주의**:

- ⚠️ **`task_` prefix 사용 금지**: `switch_jobs()`가 모드 전환 시 `task_` prefix 전체 제거
- USDT는 mode-agnostic이므로 `id="usdt_sources"`로 등록 (과거 `task_usdt_sources`에서 수정됨)
- 해당 버그 수정 이력: `6a86c01 fix: USDT scheduler job이 모드 전환 시 제거되는 버그 수정`

**장애 격리**:

- 개별 거래소 실패가 전체 job을 실패시키지 않음
- `_fetch_one()`에서 `(source, rate, error)` tuple로 결과 반환
- 전체 실패 시 "USDT 수집 전체 실패" 로그 + 저장 스킵
- 부분 실패 시 "USDT 일부 소스 수집 실패" warning + 성공한 소스만 저장

**source_registry.py**:

- 9개 소스 메타데이터 (source, asset, display_name, category, sort_order, phase1_enabled)
- `is_phase1_source()` — 등록되고 활성화된 조합인지 검증
- `get_source_definition()` — (source, asset) lookup
- Phase 1 활성: 5개 거래소 + investing/kb/hana 참조값
- Phase 2 예약: krx (phase1_enabled=False, 자리만 확보)

---

## 상세 가이드

### 📦 Group A: 표준 Requests

#### KB국민은행 (`app/crawlers/kb.py`)

**핵심 로직:**
- 3개 폴백 URL (obank 메인 → obank 서브 → mibank)
- requests + BeautifulSoup만으로 크롤링

**주의사항:**
- Selector 변경 빈번 (월 1회 확인 권장)
- 2026-09-01: IN 모드만 20초(`:15/:35/:55`) → 10초
  (`:09/:19/:29/:39/:49/:59`)로 상향. BREAK1/BREAK2/OUT은 불변
- 2026-09-03: 검증된 같은 10초 레인을 BREAK1/BREAK2로 확대. OUT `:28`은 불변

---

#### 씨티은행 (`app/crawlers/citi.py`)

**핵심 로직:**
- 국가 순서 동적 변경 → 문자열 검색으로 통화 매칭 ("USD", "JPY", "EUR" 텍스트 검색)

**주의사항:**
- Selector 순서 의존 금지

---

#### Investing.com (`app/crawlers/investing.py`)

**핵심 로직:**
- JPY-KRW 스케일링 (100엔당 원화 → 1엔당 원화로 변환)
- **curl_cffi + TLS 지문 위장**: `safari17_0` impersonate로 Cloudflare 우회 ([ADR-018](DECISIONS.md#adr-018-investing-cloudflare-차단-대응---curl_cffi-tls-지문-위장))
- **Circuit Breaker**: 연속 403 시 점진적 쿨다운 (5회→1분, 10회→5분, 20회→15분)
- **UA 로테이션**: impersonate에 맞는 UA 풀에서 랜덤 선택
- **Jitter**: 0~2초 랜덤 딜레이 (요청 패턴 분산)
- **로그 억제**: 차단 상태 전이 로깅 (시작=ERROR 1회, 지속=WARNING 5분마다, 해제=WARNING 1회)

**주의사항:**
- JPY만 ×100 스케일링, 다른 통화 추가 시 확인 필요
- `curl_cffi` 미설치 시 자동으로 `requests`로 폴백 (`_USE_CFFI` 플래그)
- `chrome131` impersonate는 Cloudflare에 의해 차단됨 → `safari17_0`만 사용
- Cloudflare 차단 재발 시: [MAINTENANCE_2026-01-29.md](MAINTENANCE_2026-01-29.md) 플레이북 참고

---

### 📊 Group D: 시장 지수

#### 미국 달러지수(DXY) / 미국달러지수 선물 ⭐⭐

**아키텍처:**
- **DXY 현물/운영 피드**: `dxy_spot.py`가 `/indices/usdollar` 페이지를 독립 수집해 `instrument='dxy'`로 저장
- **DXY 선물**: `investing.py`가 exchange-rates-table의 `#sb_last_8827`를 환율과 함께 추출해 `instrument='dxy_futures'`로 저장
- **dxy.py는 유틸리티 모듈**: `fetch_dxy_from_cnbc()` / `fetch_dxy_from_yahoo()`는 현물 외부 fallback, `fetch_dxy_from_investing_fallback()`은 선물/CFD 계열 fallback에서 사용

**변경 배경:**
- `/indices/usdollar` 운영 피드는 별도 DXY 현물/운영 지표로 유지
- exchange-rates-table의 `#sb_last_8827` 값은 현물 DXY와 섞지 않고 미국달러지수 선물 계열로 별도 보존
- 향후 테더 탭에서 KRX 미국달러 선물과 함께 비교 그래프로 사용할 수 있도록 DB 계층에서 분리

**핵심 로직:**
- **DXY 현물 Primary**: `dxy_spot.py` → `/indices/usdollar` `__NEXT_DATA__`
- **DXY 현물 CSS Fallback**: 같은 페이지의 `[data-test="instrument-price-last"]`
- **DXY 현물 외부 Fallback (chain)**: `_try_external_fallback()`이 CNBC → Yahoo 순서로 시도
  - 1순위 CNBC: `dxy.py` → `https://quote.cnbc.com/.../symbol?symbols=.DXY` (ICE U.S. Dollar Index 공개 quote endpoint)
  - 2순위 Yahoo: `dxy.py` → Yahoo Finance (`yfinance`, ticker `DX-Y.NYB`, `regularMarketPreviousClose` 우선, 10분 stale 한계)
- **DXY 선물 Primary**: `investing.py` → `#sb_last_8827`
- **DXY 선물 Fallback**: `dxy.py` → `/currencies/us-dollar-index`
- **DXY 유효 범위**: 80.0 ~ 130.0 (이상치 필터링)
- **DB 저장**: `market_index_rates` 테이블, `instrument='dxy'|'dxy_futures'`, `source='investing'|'cnbc'|'yahoo'`, `granularity='realtime'`
- **DB 우선순위 (조회/그래프 dedup)**: `investing > cnbc > yahoo` — `crud.get_latest_dxy_rate()` + `app/admin/graph_cache.py`의 `CASE WHEN source = 'investing' THEN 0 WHEN source = 'cnbc' THEN 1 ELSE 2 END`

**폴백 트리거 조건:**
- **DXY 현물**: primary source timestamp 정지, CSS 파싱 실패, hard failure 등에서 `_try_external_fallback()` 실행 (기존 `_try_yahoo_fallback`은 alias로 호환)
- **DXY 선물**: `#sb_last_8827`가 없는 페이지(예: sslfxrates API 폴백) 또는 파싱 실패 시 `_try_dxy_futures_fallback()` 실행

**DXY 현물 외부 fallback 저장 가드 (CNBC/Yahoo 공통 적용):**
- **시간 가드**: ICE DX 주간 세션 OFF 구간에는 chain 전체 차단
  - 금요일 17:00 ET 이후, 토요일 전체, 일요일 18:00 ET 이전
  - DST/표준시는 `ZoneInfo("America/New_York")`로 자동 처리
  - 화~금 일일 휴장(17:00~20:00 ET)은 운영 피드 갱신 관측이 있어 아직 차단하지 않음
- **가격 가드**: 마지막 Investing DXY가 fresh일 때만 외부값과 비교하고, 차이가 `0.07` 초과면 chain 전체 보류
  - `ACTIVE`(평일 08:00~20:59): 마지막 Investing 값이 15분 이내일 때만 적용
  - `QUIET`(그 외 평일): 마지막 Investing 값이 30분 이내일 때만 적용
  - `WEEKEND_PRESERVE`(토 07:00~월 06:00): 기존 OUT 정책 계약을 보존한다. 다만 현재 외부 chain은
    ICE 휴장 가드가 먼저 return하므로 72시간 DB 분기는 실행 경로상 도달하지 않고, silent-stale
    heartbeat도 `DXY_DIRECT_LATEST_ENABLED=true`일 때만 실제 Redis write한다(기본/운영 현재 off)
  - ⚠️ **은행 모드(IN/BREAK1/BREAK2/OUT)가 아니라 `market_mode.get_dxy_policy_state()`의 전용 상태다**
    (2026-08-28 분리 — 은행 BREAK1이 19:00으로 당겨져도 DXY는 21:00 경계를 유지)
  - Investing 값이 오래 멈춘 경우 외부값이 유일한 대체일 수 있으므로 가격 가드를 건너뜀
- **chain 정책**:
  - chain 내 한 source(예: CNBC)가 fetch 성공 + 가격 가드 차단 → 다음 source(Yahoo)도 시도하지 않음 (외부값 자체가 fresh Investing과 충돌하는 outlier 신호)
  - chain 내 한 source가 fetch 실패(네트워크 등) → 다음 source로 진행
  - 한 source 저장 성공 → chain 즉시 종료

**DXY 선물 폴백 쿨다운 (60초):**
- 셀렉터 장기 파손 시 retry storm 방지
- 60초 이내 재호출 무시 (`time.monotonic()` 기준)
- Circuit Breaker 별도 없음 → investing.py의 기존 Circuit Breaker를 공유
- Yahoo Finance는 현물/운영 DXY 계열이므로 선물 저장에는 사용하지 않음

**스케줄:**
- **DXY 현물**: 독립 스케줄 실행 (`crawler_config.dxy`로 활성/비활성 제어)
  - IN/BREAK1/BREAK2: 10초마다
  - OUT: 1분마다
- **DXY 선물**: investing 크롤러와 동일 주기로 실행
  - IN/BREAK1/BREAK2: investing 10초마다 실행 시 선물도 함께 추출
  - OUT: investing **매분**(`:08`) 실행 시 선물도 함께 추출 (배포 2A)
- **보관기간**: `dxy`, `dxy_futures` realtime 원본은 30일 보관, hourly/daily rollup은 3m/1y 그래프 보존을 위해 정리 대상에서 제외

**dxy_futures 현재 상태:**
- ✅ 데이터 수집 + DB 저장 (`instrument='dxy_futures'`)
- ❌ 최신값 조회 API, 그래프 API, rollup, 테더 탭 연결은 후속 작업
- ❌ 알림/표시명/source_registry 등록 없음

**dxy_futures 운영 단위:**
- 독립 크롤러가 아니라 `investing` 크롤러의 부가 수집값
- 활성화/비활성화는 `crawler_config.investing`을 따름 (별도 `crawler_config.dxy_futures` 없음)
- 정상 경로는 같은 HTTP 요청·응답에서 환율 + DXY 선물을 동시에 추출
- 셀렉터 실패 시에만 `/currencies/us-dollar-index` 별도 폴백 요청 (60초 쿨다운)
- 향후 테더 탭에서 1급 데이터화하면 독립 config/스케줄/모니터링을 재검토

**주의사항:**
- `dxy.py`의 함수들은 on-demand 호출이므로 함수 내부 import 사용
- `yfinance`는 무거운 라이브러리 → DXY 현물 Yahoo fallback 시에만 로드
- `yfinance`는 timeout 직접 제어 불가 → 스케줄러 작업 타임아웃(45초)이 상위 보호
- CNBC quote endpoint는 5초 timeout 사용 (빠른 응답이라 짧게 설정)
- 선물 폴백 URL(`/currencies/us-dollar-index`)은 CDN stale cache 위험 있으나, 1차 실패 시 차선으로만 사용

**상세 코드:** `app/crawlers/dxy_spot.py` (현물/운영 DXY + 외부 fallback chain), `app/crawlers/investing.py` (미국달러지수 선물), `app/crawlers/dxy.py` (공용 폴백 유틸리티 — CNBC/Yahoo/Investing)
**관련 ADR:** [ADR-019](DECISIONS.md#adr-019-dxy-보조지표---granularity-기반-2-part-merge-전략), [ADR-022](DECISIONS.md#adr-022-dxy-yahoo-fallback-시간가격fresh-age-가드-정책), [ADR-024](DECISIONS.md#adr-024-미국달러지수-선물-분리-저장--dxy_mode-제거), [ADR-025](DECISIONS.md#adr-025-dxy-현물-외부-fallback-체인--cnbc-추가--yahoo-격하)

---

### 🔧 Group B: 기본 Selenium

#### 신한은행 (`app/crawlers/shinhan.py`)

**핵심 로직:**
- JavaScript 렌더링 필요 → Selenium 필수
- **AsyncIO Queue + subprocess**: 순차 실행 보장, Chrome 좀비화 방지
- 표준 Selenium 패턴 (특수 로직 없음)

**주의사항:**
- ChromeDriver 버전 호환성 확인 (분기 1회)
- subprocess로 격리 실행 (45초 타임아웃)

**상세 코드:** `app/crawlers/shinhan.py`
**실행 방식:** `runner.py` 통해 subprocess 생성 → scheduler.py Queue에서 순차 처리

---

#### 하나은행 (`app/crawlers/hana.py`)

**핵심 로직:**
- **3단계 폴백**: requests → **Selenium subprocess** → mibank
- **Selenium 격리**: subprocess로 실행하여 Chrome 프로세스 좀비화 방지
- **iframe 전환**: `driver.switch_to.frame()` 필수 (Selenium 폴백 시)
- **mibank 최종 폴백**: 조건 없이 항상 시도

**주의사항:**
- iframe 전환 없으면 Selector 찾기 실패
- Selenium 폴백은 subprocess로 격리 실행 (45초 타임아웃)

**상세 코드:** `app/crawlers/hana.py:54-132` (subprocess fallback 포함)
**최근 리팩토링:**
- 2026-09-01: IN 모드만 20초(`:05/:25/:45`) → 10초
  (`:02/:12/:22/:32/:42/:52`)로 상향. BREAK1/BREAK2/OUT은 불변
- 2026-09-03: 검증된 같은 10초 레인을 BREAK1/BREAK2로 확대. OUT `:38`은 불변
- 2025-11-14: Selenium 폴백 subprocess 격리

---

### ⚙️ Group C: 복잡한 특수 로직

#### NH 농협은행 (`app/crawlers/nh.py`) ⭐⭐

**핵심 로직:**
- 메인 페이지 접속 → 링크 클릭 → 환율 페이지 이동
- **AsyncIO Queue + subprocess**: 순차 실행 보장, Chrome 좀비화 방지

**주의사항:**
- 메인 페이지 링크 Selector 변경 감지 (월 1회)
- subprocess로 격리 실행 (45초 타임아웃)

**상세 코드:** `app/crawlers/nh.py`
**실행 방식:** `runner.py` 통해 subprocess 생성 → scheduler.py Queue에서 순차 처리

---

#### IBK 기업은행 (`app/crawlers/ibk.py`) ⭐⭐⭐

**후속 정책 승인 / 아직 미활성 (2026-09-07):** 사용자가 IBK MIBANK writer를
no-write 진단으로 격하하는 데 동의했다. 최종 변경에서는 평일 낮을 포함해 IBK
MIBANK 값은 DB에 저장하지 않고, 기존 비신뢰 시간대 제한을 유지한 선택적·예산 내
진단만 허용한다. 공식 경로를 검증하지 못하면 완전한 기존 DB는 DEGRADED,
빈/부분 DB는 FAILED로 구분하는 계측·경보와 함께 반영한다. 신한 등 다른 은행은
변경하지 않는다. 아래는 **현재 동작**이며 POST-first와 MIBANK 저장 금지는 아직 아니다.

**준비 구현의 범위 (로컬, 미배포) — 세 갈래로 구분한다:**
(1) *해당 범위 검증 완료*: 결과 프로토콜, 유한 subprocess capture, 실행 ID·기준시각 전달,
선택적 부모 배선, 판정 어댑터, 회귀 가드 순수 함수 추출, 단일 서비스일 결과 생성기.
(2) *기본 운영 경로 미전환*: `IBK_RESULT_CRAWLER = None`이고 worker는 인자 없이 생성되므로
IBK를 포함한 모든 은행이 기존 경로로 흐른다. 생성기의 운영 호출자는 없다.
(3) *후속 작업*: 전체 IBK 실행 흐름 연결, Selenium 안전화, POST-first 정책 전환, 실제 경보 연결.
이 검증은 로컬 경계에서 이뤄졌고 실제 HTTP·Selenium·운영 PostgreSQL·부모 경보 검증이 아니다.

**행동보존 준비 단계 (로컬 구현, 미배포):** `crawl_ibk_legacy_result()`가 종료 분기,
Selenium 시도 수, 직접 확보한 저장 함수 반환 개수만 `IbkLegacyResult`로 돌려준다.
공개 진입 함수는 이를 버리고 기존 `None`/예외 계약을 유지한다. 이 타입은
최종 OBSERVED/PRESERVED/DEGRADED/FAILED도, 부모 IPC 프로토콜도 아니다.
HTTP 순서·쓰기 집합·재시도·부모 성공/실패 계측은 불변이다. 특히 저장 반환 0을
unchanged로 확정하지 않으며 기존 MIBANK writer도 후속 원자 배포까지 남는다.
실패 주입은 `tests/test_ibk_legacy_result.py`, 기존 공식 POST 검증은
`tests/test_ibk_historical_request.py`에서 함께 확인한다.

**핵심 로직:**
- **자정 전환기 Selenium 억제**: 00:00~00:05에도 공식 날짜 POST는 실행하고, 실패한 경우에만 UI Selenium을 억제
- **조건부 폴백**: 08:00 이후 당일 GET → 날짜 POST / 08:00 전 날짜 POST 직행 → Selenium (3회) → MIBANK(조건부)
- **공식 POST 계약**: `pageId=SM03020100`, `inDate=YYYY.MM.DD`, `ecrtInqyDscd=01`.
  `#inDate` readback, `일반고시환율 표` caption, `매매기준율` header, USD/JPY/EUR 완전성·범위,
  고시완료시각과 미래시각 여부를 검증한다. 날짜 lookback은 새 후보 요청 시작을
  12초 이내로 제한하는 soft budget을 쓴다(`requests` connect/read inactivity timeout이므로
  이미 시작한 단일 요청의 엄밀한 wall-clock hard deadline은 아니다).
- **Selenium 재시도**: 날짜 변경 실패 시 최대 3회 재시도 (2초 대기)
- **날짜 input 직접 입력**: `send_keys()` + `Keys.ENTER`
- **MIBANK 조건부 실행**: 평일 10:00~23:59만 허용 (00:00~09:59/주말 차단)

**자정 전환기 Selenium 억제 (00:00~00:05):**
> 🚨 **문제**: 자정 직후 Selenium 날짜 변경 시 UI 불안정
> - 캘린더가 랜덤한 날짜까지 이동하여 잘못된 환율 수집
> - 45초 타임아웃 발생

> ✅ **해결**: 공식 날짜 지정 POST는 계속 실행한다. 성공하면 즉시 저장하고,
> 실패한 경우에만 00:00~00:04:59 Selenium UI를 띄우지 않고 마지막 정상값을 유지한다.
> 00:05부터는 Request 장애 시 기존 Selenium fallback을 허용한다.

**주의사항:**
- 08:00 이후: 조회 당일 공식 GET, 실패 시 같은 날짜 POST부터 재검증한다
- **08:00~08:34:59**: 정확한 당일 날짜 readback 뒤 표·공식 무고시 코드가 모두 없으면
  `preopen_table_pending`으로 분리하고, 이전 서비스일의 공식 POST를 확인한다. 과거 후보의
  완료시각과 DB 저장시각을 대조해 기존값을 보존하거나 놓친 최종 고시만 보충한다
- 08:00 이전: 빈 당일 GET 생략 후 전 조회기준일 공식 POST. 주말 조회기준일은 skip하지만 금요일 세션의 토요일
  새벽 고시는 금요일 날짜 화면에서 읽으므로 손실되지 않는다
- 공식 무고시는 최대 10일/12초 soft budget 안에서 여러 평일을 계속 조회해
  월요일·공휴일 연휴 뒤의 마지막 서비스일을 찾도록 한다
- 네트워크·날짜 불일치·코드가 명시적으로 판별하는 오류 문구와 나머지 파서 계약 이상은 즉시 Selenium으로 전환
  ⚠️ 그 판별 문구는 실제 응답 캡처로 확정된 계약이 아니라 기존 테스트 fixture에서 유래한
  방어적 heuristic이다. 실제 문구가 다르면 분기가 발화하지 않고 표 부재로 떨어진다
- 표 없는 미지의 오류 HTML은 정상 개장 전 화면과 완전히 구분할 수 없어, 제한 창 안에서는
  Selenium 진단이 최대 35분 늦어질 수 있다
- 당일 GET도 계속 실패하고 그 응답이 **이미 게시된 당일 고시를 가린 경우**에는, 이 제한 분류
  때문에 진단과 당일 값의 freshness 회복이 `08:35` 이후 첫 유효한 `:34` 실행까지 미뤄질 수 있다.
  실제 복구 완료 시각은 이후 GET·POST·Selenium 결과에 달려 있으며 08:35 가 상한은 아니다.
  회귀 방지는 과거값 덮어쓰기만 막을 뿐 신선도를 보장하지 않는다
- **00:00~00:05**: 날짜 POST는 실행, 실패 시 Selenium만 억제
- Selenium 3회 재시도는 공식 Request 네트워크/응답 계약 장애의 안전망
- MIBANK는 영업일 자정 직전 환율 제공 → 자정/주말에는 부정확
- MAX_DAYS_LOOKBACK 10일 (공휴일 연휴 대응, constants.py 중앙 관리)

**상세 코드:** `crawl_and_save_ibk_bank_exchange_rates`, `try_crawl_with_dated_requests`,
`_fetch_ibk_rates_for_date`, `crawl_and_save_ibk_routine_selenium`
**최근 리팩토링:**
- 2026-09-03: 08:00~08:34:59 당일 표 준비 상태를 제한적으로 분류해 이전 서비스일 검증 후 Selenium 억제
- 2026-08-29: 공식 날짜 지정 POST fast path 추가. 자정 전환기 완전 스킵을 Request 유지 + Selenium 억제로 축소
- 2025-12-10: 자정 전환기 완전 스킵 추가 (2026-08-29에 위 정책으로 대체)
- 2025-10-26: Selenium 3회 재시도 추가, MIBANK 조건부 실행

---

#### Woori 우리은행 (`app/crawlers/woori.py`) ⭐⭐⭐⭐

**핵심 로직:**
- **3단계 폴백**: requests → **Selenium subprocess** (날짜 변경) → MIBANK (조건부)
- **Selenium 격리**: subprocess로 실행하여 Chrome 프로세스 좀비화 방지
- **AJAX 감지**: 테이블 행 개수로 페이지 갱신 확인
- **과거 조회**: 어제부터 MAX_DAYS_LOOKBACK (10일) 순회 (SC 방식 적용)
- **날짜 선택**: 년/월/일 select 박스 각각 선택
- **MIBANK 조건부 실행**: 평일 10:00~23:59만 허용 (00:00~09:59/주말 차단)

**주의사항:**
- 행 개수: 1개(헤더만) = 데이터 없음, 2개+ = 정상 데이터
- **계측 계약**: 공식 Request→Selenium→MIBANK가 모두 실패하거나 마지막
  MIBANK가 `hard_fail`로 저장을 보류하면 호출자에게 예외를 전파한다. DB 값은
  그대로 보존하되 scheduler 성공으로 기록하지 않는다. 유효한 unchanged 응답은 성공이다.
- 테이블 로딩 시간 확보: `time.sleep(0.5)` 필수
- 날짜 변경 실패 시 continue로 다음 날짜 시도 (주말/공휴일 대응)
- select value 형식 변경 주의 ("2025", "01", "01")
- MIBANK는 영업일 자정 직전 환율 제공 → 자정/주말에는 부정확
- Selenium 폴백은 subprocess로 격리 실행 (45초 타임아웃)

**상세 코드:** `app/crawlers/woori.py:59-148` (subprocess fallback 포함), `woori.py:212-282` (crawl_woori_past_date_rates 함수)
**최근 리팩토링:**
- 2026-09-01: IN 모드만 60초(`:53`) → 30초(`:14/:44`)로 상향. BREAK1의
  `:53` 및 05:04:53 종료, BREAK2/OUT 제외는 불변
- 2026-09-03: BREAK1도 05:03까지 `:14/:44`로 확대하되, 마지막 분은 기존
  `05:04:53` 한 번만 남긴 단일 `OrTrigger`로 최종 수집 기회를 보존. BREAK2/OUT 제외는 불변
- 2025-10-26: SC 방식 적용, 날짜 변경 실패 처리 개선, MIBANK 조건부 실행 추가
- 2025-11-14: Selenium 폴백 subprocess 격리 ([ADR-012](DECISIONS.md#adr-012-selenium-폴백-subprocess-격리-chrome-프로세스-좀비화-방지))

---

#### SC 제일은행 (`app/crawlers/sc.py`) ⭐⭐⭐⭐

**핵심 로직:**
- **3단계 폴백**: Request(mibank) → Selenium (SC_BANK_URL) → Selenium 날짜 변경 (SECOND_SC_BANK_URL)
- **MIBANK 우선**: 조건 없이 항상 mibank를 먼저 시도 (2025-11-16 변경)
- **AsyncIO Queue + subprocess**: Selenium 순차 실행 보장, Chrome 좀비화 방지
- **AJAX 감지**: #TMP_RATE 개수 변화로 페이지 갱신 확인
- **Alert 처리**: 조회 버튼 클릭 직후 1회 (자정/주말 "0회차" 메시지)
- **과거 조회**: 어제부터 MAX_DAYS_LOOKBACK (10일) 순회

**주의사항:**
- Alert 미처리 시 크롤링 중단 → `driver.switch_to.alert.accept()` 필수
- #TMP_RATE 개수 1개 = 데이터 없음, 2개 이상 = 정상 데이터
- 날짜 변경 후 AJAX 대기 없으면 이전 데이터 오독
- subprocess로 격리 실행 (45초 타임아웃)

**상세 코드:** `app/crawlers/sc.py:209-292` (crawl_past_date_rates 함수)
**실행 방식:** `runner.py` 통해 subprocess 생성 → scheduler.py Queue에서 순차 처리
**최근 리팩토링:**
- 2025-11-16: Request(mibank) 우선 전략 적용 ([ADR-013](DECISIONS.md#adr-013-4단계-모드-환율-고시-스케줄-기반-최적화))
- 2025-10-26: Alert 처리 단일화

---

### 🛠️ 공통 유틸리티 함수

모든 크롤러에서 공통으로 사용하는 유틸리티 함수들 (`app/crawlers/utils.py`)

#### `parse_rate_text(rate_text: str) -> float`
**기능**: 환율 텍스트 파싱 (쉼표 제거 + float 변환)
```python
>>> parse_rate_text("1,340.50")
1340.5
```

#### `create_selenium_driver() -> webdriver.Chrome`
**기능**: 표준 Selenium Chrome 드라이버 생성
- Headless 모드로 실행
- ChromeDriverManager로 자동 버전 관리

#### `crawl_mibank_rates()` ⭐ NEW (2026-01-10)
**기능**: Currency-Code 기반 MIBANK 환율 크롤링 ([ADR-017](DECISIONS.md#adr-017-mibank-환율-파싱---position-기반-vs-currency-code-기반))

**현재 URL 형식 (2026-04-27):**
```python
MIBANK_SHINHAN_URL = "https://exchange.mibank.me/bank?bank_cd=088"
```

```python
def crawl_mibank_rates(
    url: str,
    bank_name: str,
    required_codes: tuple = ("USD", "JPY", "EUR"),
    require_all: bool = True,
) -> dict:
    """
    통화코드 기반 MIBANK 환율 크롤링

    - 신 DOM: flag 이미지 파일명(flag_usd_*.png)에서 통화 코드 추출
    - 구 DOM 호환: href="...?currency=USD" 파라미터에서 통화 코드 추출
    - 기준환율(원) 헤더 컬럼을 찾아 해당 셀 사용
    - 헤더를 찾지 못하면 마지막 환율 셀로 fallback
    - require_all=True: 필수 통화 누락 시 RuntimeError
    """
```

**사용 예시**:
```python
rates = crawl_mibank_rates(MIBANK_KB_URL, "kb", required_codes=("USD", "JPY", "EUR"))
# returns: {"usd-krw": 1340.5, "jpy-krw": 920.3, "eur-krw": 1450.2}
```

#### `validate_rate_ranges()` ⭐ NEW (2026-01-10)
**기능**: 절대 범위 검증
```python
def validate_rate_ranges(rates: dict, ranges: dict):
    """
    환율이 절대 범위 내인지 검증
    - USD: 1,000~2,000원
    - JPY: 600~1,400원 (100엔당)
    - EUR: 1,100~2,200원
    """
```

#### `evaluate_rate_deviation()` ⭐ NEW (2026-01-10)
**기능**: 동적 변동률 검증 (soft/hard fail)
```python
def evaluate_rate_deviation(rates: dict, last_rates_info: dict, now) -> dict:
    """
    이전 저장값과 비교하여 변동률 검증

    Returns:
        {"soft_fail": bool, "hard_fail": bool, "details": {...}}

    - soft_fail: 경고 후 저장 (마지막 폴백) 또는 Selenium 재검증
    - hard_fail: 저장 보류 (데이터 신뢰 불가)
    """
```

**변동률 threshold (시간 gap 기준)**:
| 시간 gap | soft_fail | hard_fail |
|----------|-----------|-----------|
| ≤10분 | 8% | 20% |
| ≤60분 | 12% | 25% |
| ≤180분 | 20% | 30% |
| >180분 | 30% | 40% |

#### `is_mibank_rate_reliable() -> bool`
**기능**: MIBANK 환율 신뢰성 판단 (시간대별 조건부 실행)
- **반환값**:
  - `True`: 평일 10:00 ~ 23:59 (MIBANK 신뢰 가능)
  - `False`: 평일 00:00 ~ 09:59, 주말 (MIBANK 부정확)
- **사용 이유**: MIBANK는 영업일 자정 직전 환율 제공 → 자정/주말에는 부정확한 데이터
- **한계**: 일반 공휴일은 고려 못함. 단 IBK는 MIBANK 전에 공식 무고시 코드 기반 날짜 lookback으로 보완
- **사용 크롤러**: BS, CITI, IBK, WOORI (4개)
  - 이유: 자정 이후/주말에 MIBANK 환율이 실제 은행 환율과 다른 경우가 많음
- **미사용 크롤러**: KB, HANA, SC, SHINHAN, NH (조건 없이 항상 mibank 시도)
  - KB, HANA: 운영 관측상 MIBANK 환율이 자정/주말에도 비교적 정확함
  - SC, SHINHAN, NH: Primary MIBANK 패턴 (soft_fail 시 Selenium 재검증 가능)

**사용 예시**:
```python
# BS, CITI, IBK, WOORI 크롤러에서 사용
if is_mibank_rate_reliable():
    rates, eval_result = _crawl_mibank_bs(db)
else:
    logger.warning("⏰ MIBANK 차단 (자정/주말)")
```

### 🏦 은행별 MIBANK 래퍼 패턴 ⭐ NEW (2026-01-10)

각 크롤러에서 `_crawl_mibank_{bank}()` 래퍼 함수를 사용하여 MIBANK 크롤링 + 검증을 캡슐화합니다.

```python
# 예: app/crawlers/sc.py
def _crawl_mibank_sc(db: Session) -> tuple[dict, dict]:
    """SC제일은행 MIBANK 크롤링 + 검증"""
    rates = crawl_mibank_rates(MIBANK_SC_URL, BANK_NAME, required_codes=MIBANK_REQUIRED_CODES)
    validate_rate_ranges(rates, MIBANK_RATE_RANGES)
    last_info = crud.get_last_bank_rates_with_ts(db, BANK_NAME, MIBANK_REQUIRED_PAIRS)
    eval_result = evaluate_rate_deviation(rates, last_info, models.get_kst_now())
    return rates, eval_result
```

**적용된 크롤러**: SC, NH, Shinhan, IBK, BS, Citi, Woori, KB, Hana (9개 전체)

---

## 트러블슈팅

### 자주 발생하는 문제 (FAQ)

| 문제 | 증상 | 해결 |
|------|------|------|
| **Selector 오류** | `⚠️ SELECTOR 오류: usd-krw` | 개발자 도구로 새 Selector 확인 → `SELECTORS` 딕셔너리 업데이트 |
| **Selenium Timeout** | `TimeoutException` | WebDriverWait 시간 증가 (5초 → 10초) 또는 Selector 재확인 |
| **날짜 변경 실패** | `⚠️ 날짜 변경 실패` | select value 형식 확인 ("2025", "01", "01"), AJAX 대기 시간 증가 |
| **Alert 미처리** | 크롤링 중단 | `driver.switch_to.alert.accept()` 추가 |
| **AJAX 응답 안 기다림** | 이전 데이터 읽기 | WebDriverWait로 요소 개수/속성 변화 감지 추가 |
| **Cloudflare 403 차단** | Investing 크롤러 `InvestingForbidden` 반복 | curl_cffi impersonate 변경 또는 [MAINTENANCE_2026-01-29.md](MAINTENANCE_2026-01-29.md) 플레이북 참고 |
| **MIBANK 첫 시도 실패** | `mibank 테이블을 찾을 수 없음`, `mibank 필수 통화 누락`, 신한/NH/SC가 Selenium fallback 반복 | URL이 `https://exchange.mibank.me/bank?bank_cd=<은행코드>` 형식인지 확인하고, `table.main_table.content` / `flag_<code>_*.png` DOM 구조 변경 여부 점검. 상세: [MAINTENANCE_2026-04-27.md](MAINTENANCE_2026-04-27.md) |
| **DXY 현물 폴백 지속** | `📦 DXY spot CSS 폴백 저장`, `📦 DXY cnbc 폴백 저장`, `📦 DXY yahoo 폴백 저장` 로그 반복 | `dxy_spot.py` Primary(`/indices/usdollar` `__NEXT_DATA__`) 응답 / 같은 페이지 CSS selector 유효성 확인. 외부 chain은 주간 세션 / market mode / fresh-age diff guard에 의해 게이트됨 |
| **DXY 선물 폴백 지속** | `📦 DXY 선물 폴백 저장` 로그 반복 | 1차 셀렉터(`#sb_last_8827`) 유효성 확인, 60초 쿨다운 후 자동 재시도 (`investing.py` 선물 추출 경로) |

---

### 🚨 은행별 주의사항

| 은행 | 주의사항 | 체크 주기 |
|------|----------|-----------|
| **KB** | Selector 변경 빈번 | 월 1회 |
| **씨티** | 국가 순서 변경 감지 | 주 1회 |
| **신한** | ChromeDriver 버전 호환성 | 분기 1회 |
| **하나** | iframe 구조 변경 | 월 1회 |
| **NH** | 메인 페이지 링크 변경 | 월 1회 |
| **IBK** | 공식 POST 파라미터/pageId, `#inDate` readback, 표 caption/`매매기준율` header, `ECBKFEX01589`, 고시완료시각. Selenium 날짜 input은 fallback으로 별도 확인 | 월 1회 |
| **Woori** | select 박스 value 형식 | 월 1회 |
| **SC** | Alert 메시지 내용 변경 | 월 1회 |
| **DXY 현물** (`dxy_spot.py`) | `/indices/usdollar` `__NEXT_DATA__` 스키마 변경, 같은 페이지 CSS selector 변경, CNBC quote endpoint 응답 형식, Yahoo `DX-Y.NYB` API 변경 | 월 1회 |
| **DXY 선물** (`investing.py`) | 1차 셀렉터(`#sb_last_8827`) 변경, 2차 URL(`/currencies/us-dollar-index`) 구조 변경 | 월 1회 |

---

### 환경 설정 문제

| 문제 | 증상 | 원인 | 해결 |
|------|------|------|------|
| **Brotli 압축 해제 실패** | investing 크롤러만 Docker에서 실패 (로컬은 정상) | `brotli` 패키지 미설치 → requests가 br 압축 미지원 | `requirements.txt`에 `brotli>=1.0.9` 추가 ✅ |

**배경**: investing.com은 Brotli(br) 압축을 우선 사용하며, requests 라이브러리는 `brotli` 패키지가 없으면 br 압축을 해제할 수 없습니다. 로컬 환경(Anaconda)에는 다른 패키지 의존성으로 brotli가 설치되어 있었지만, Docker 환경에는 명시하지 않으면 설치되지 않습니다.

---

## 새 은행 추가 가이드

### 1️⃣ 웹사이트 분석

**체크리스트**:
- [ ] 정적 HTML vs JavaScript 렌더링 확인
- [ ] 환율 데이터 위치 확인 (iframe, 테이블, div 등)
- [ ] 자정/주말 환율 제공 여부 확인
- [ ] 날짜 변경 방식 확인 (input, select, datepicker 등)
- [ ] AJAX 사용 여부 확인 (페이지 갱신 없이 데이터 로드)

### 2️⃣ 구현 난이도 그룹 분류

> 아래 분류는 새 크롤러를 만들 때의 **구현 난이도** 기준이다. 위 운영 아키텍처의
> Request/Selenium 실행 경로 Group A/B/C와 이름만 같고, 런타임 로스터 분류가 아니다.

| 그룹 | 조건 | 템플릿 |
|------|------|--------|
| **Group A** | requests만으로 크롤링 가능 | `app/crawlers/kb.py` |
| **Group B** | Selenium 필요 (특수 로직 없음) | `app/crawlers/shinhan.py` |
| **Group C** | Selenium + 특수 로직 | `app/crawlers/ibk.py` (하이브리드)<br>`app/crawlers/woori.py` (AJAX 감지) |

### 3️⃣ 크롤러 생성

**파일명**: `app/crawlers/<은행코드>.py`

**기본 구조**:
```python
# 표준 라이브러리
import logging

# 서드파티 라이브러리
import requests  # 또는 Selenium
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud
from app.database import SessionLocal
from app.crawlers.constants import HEADERS, DEFAULT_TIMEOUT
from app.crawlers.utils import parse_rate_text, create_selenium_driver

BANK_NAME = '<은행코드>'

# URL 및 Selector 정의
BANK_URL = 'https://...'
BANK_SELECTORS = {
    'usd-krw': '...',
    'jpy-krw': '...',
    'eur-krw': '...',
}

# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")

def crawl_and_save_<은행코드>_bank_exchange_rates():
    """<은행명> 환율 크롤링"""
    db = SessionLocal()
    try:
        # 크롤링 로직
        pass
    finally:
        db.close()
```

### 4️⃣ 스케줄러 등록

**파일**: `app/scheduler.py`

```python
from app.crawlers import investing, kb, <은행코드>

BANK_TASKS = [
    ("investing", investing.crawl_and_save_investing_exchange_rates, 4.9),
    ("kb", kb.crawl_and_save_kb_bank_exchange_rates, 7.9),
    # ... 기존 은행들
    ("<은행코드>", <은행코드>.crawl_and_save_<은행코드>_bank_exchange_rates, <주기>),  # ⬅️ 추가
]
```

### 5️⃣ 테스트

```bash
# 1. Import 테스트
cd /Users/jay/Downloads/Projects/FXi/exchange-rate
python3 -c "from app.crawlers import <은행코드>"

# 2. 크롤링 테스트 (수동 실행)
python3 -c "
from app.crawlers import <은행코드>
<은행코드>.crawl_and_save_<은행코드>_bank_exchange_rates()
"

# 3. 로그 확인
tail -f logs/app.log | grep "<은행코드>"
```

### 6️⃣ 문서 업데이트

- [ ] `CRAWLERS.md`: 새 은행 특수 로직 추가
- [ ] `CLAUDE.md`: 지원 은행 목록 업데이트
- [ ] 관리자 페이지 (`templates/index.html`): 은행 아이콘 추가

---

## 참고 자료

- **프로젝트 가이드**: [CLAUDE.md](CLAUDE.md)
- **아키텍처 의사결정**: [DECISIONS.md](DECISIONS.md)
- **공통 상수**: `app/crawlers/constants.py`
- **공통 함수**: `app/crawlers/utils.py`

---

**마지막 업데이트**: 2026-03-12
**작성자**: Claude Code
**버전**: 1.0
