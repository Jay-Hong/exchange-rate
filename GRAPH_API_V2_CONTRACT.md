# Graph API v2 Contract

> **상태**: Draft (2026-05-27)
> **관련 ADR**: [ADR-033](DECISIONS.md#adr-033-graph-api-v2-catalog-policy--legacy-공존--hana-backfill--bithumbkrx-actual-only--dxy_futures-1d-only)
> **DB audit 근거** (2026-05-27 production EC2):
> - bank_exchange_rates: 9 banks × 3 currencies, 30.1~30.2일 span (cleanup 30d 실측)
> - investing_exchange_rates: USD/JPY/EUR 모두 443.1일 span (장기 backfill 적격)
> - source_rates (USDT 5 + KRX): 30.3~30.4일 span (30d cap 실측), KRX 22.5일 (5/4 land 이후)
> - market_index_rates: dxy daily yahoo 376일 / dxy hourly investing 77일 / dxy_futures realtime 29.1일 only (hourly/daily rollup 없음)

## 1. Scope / Out of scope

**Scope**:
- Graph API v2 endpoint contract (catalog 발견 + 탭 그래프 fetch)
- catalog matrix 정의 (tab × period × series)
- provenance schema (actual_source + history_policy + per-point metadata)
- Hana official historical source rule (Amendment 2026-05-27)
- Bithumb/KRX external_historical policy + insufficient_history fallback (신규 자산 / source 장애 / coverage 부족 시점)
- axis groups / units 정의
- legacy v1 (`/api/graph/{currency}`) 공존 정책
- cache key strategy + rollout plan

**Out of scope**:
- 알림 evaluator (별도 path, [ADR-032](DECISIONS.md))
- topic protocol (`krx:usd-krw-futures` 등, [ADR-028](DECISIONS.md))
- WebSocket 그래프 push (`graph_buckets` 후속 broadcast)
- 수집 layer 변경 (Citi 수집 중단 등은 별도 결정)
- daily rollup 구현 (Phase 2d 별도 PR)
- 외부 historical source 발굴 (Phase 2c 별도 트랙)

## 2. Current v1 graph behavior (anchor)

`GET /api/graph/{currency}?range={1d|1w|3m|1y}` ([app/main.py:1831](app/main.py#L1831)):

- 통화: USD/JPY/EUR only
- 1d (10분 bucket): investing + KB + 하나 + DXY (USD 탭만)
- 1w (1시간 bucket, full-window): investing + DXY hourly
- 3m/1y (1일 bucket): investing + DXY daily + realtime tail 7d
- 응답 schema: `{"data": [...], "sources": {...}}` (legacy)
- cache 실측 (2026-05-27 코드 확인):
  - 1d: Redis `graph:{currency}` (TTL 120s) — [app/main.py:605](app/main.py#L605), [app/admin/graph_cache.py:651](app/admin/graph_cache.py#L651)
  - 1w/3m/1y: in-memory `_period_cache["graph:{currency}:{period}"]` — [app/main.py:2024-2046](app/main.py#L2024-L2046)

v2와 hot path 분리 + cache key prefix 분리 (v1 prefix `graph:`, v2 prefix `graph_v2:`).

## 3. v2 product requirements

| Tab | 1d (10min bucket) | 1w (1h bucket) | 3m / 1y (1d bucket) |
| --- | --- | --- | --- |
| USD | 8 banks (Citi 제외) + investing + DXY | investing + Hana (자체 historical 단일 source) + DXY hourly | investing + Hana (자체 historical 단일 source) + DXY daily |
| JPY | 8 banks (Citi 제외) + investing | investing + Hana (자체 historical 단일 source) | investing + Hana (자체 historical 단일 source) |
| EUR | 8 banks (Citi 제외) + investing | investing + Hana (자체 historical 단일 source) | investing + Hana (자체 historical 단일 source) |
| Tether | 5 exchanges (Upbit/Bithumb/Coinone/Korbit/Gopax) + KRX + investing USD + KB USD + Hana USD + DXY + DXY_futures | Bithumb (대표, external_historical) + KRX (external_historical, contract chain) + investing USD + Hana (자체 historical) + DXY hourly | Bithumb (대표, external_historical 902d) + KRX (external_historical, contract chain) + investing USD + Hana (자체 historical) + DXY daily |

**제약** (Amendment 2026-05-27 반영):
- Bithumb 3m/1y: Bithumb 공식 candlestick API external_historical (902일 coverage)
- KRX 3m/1y: KIS `inquire-daily-fuopchartprice` + A75YMM contract chain external_historical
- Hana 1w/3m/1y: Hana official historical endpoint 단일 source (20년+, Investing backfill 폐기)
- DXY_futures는 1d only (1w/3m/1y catalog 미노출)
- Citi는 모든 tab/period에서 catalog 미노출 (수집 layer 유지)
- 은행은 1d catalog 8개 (Hana 외 7개는 외부 historical 미보유 — 1d only), 1w+ catalog는 Hana만 (Hana 자체 historical로)
- `insufficient_history=true`는 여전 유효 — 신규 자산 추가 직후 / source 일시 장애 / 미지원 자산 coverage 부족 시점

## 4. tab × period catalog matrix

Catalog 응답 예시 (USD 탭 일부):
```json
{
  "tabs": [
    {
      "id": "usd",
      "label": "달러",
      "axis_groups": {
        "krw": {"unit": "KRW", "decimals": 2, "side": "left"},
        "index": {"unit": "INDEX", "decimals": 3, "side": "right"}
      },
      "periods": {
        "1d": {
          "default_visible_series": ["investing.usd", "kb.usd", "hana.usd", "dxy"],
          "all_series": ["investing.usd", "bs.usd", "hana.usd", "ibk.usd", "kb.usd", "nh.usd", "sc.usd", "shinhan.usd", "woori.usd", "dxy"]
        },
        "1w": {
          "default_visible_series": ["investing.usd", "hana.usd", "dxy"],
          "all_series": ["investing.usd", "hana.usd", "dxy"]
        },
        "3m": {
          "default_visible_series": ["investing.usd", "hana.usd", "dxy"],
          "all_series": ["investing.usd", "hana.usd", "dxy"]
        },
        "1y": {
          "default_visible_series": ["investing.usd", "hana.usd", "dxy"],
          "all_series": ["investing.usd", "hana.usd", "dxy"]
        }
      }
    }
  ]
}
```

period별 `all_series` / `default_visible_series` 분리:
- `all_series`: 해당 period에서 fetch 가능한 모든 series ID
- `default_visible_series`: 첫 진입 시 체크박스 ON 상태로 표시할 series (client UX)

## 5. Series metadata schema

각 series는 catalog 안에 다음 metadata를 가진다 (Amendment 2026-05-27 — Hana 단일 source / KRX contract chain per-point metadata).

**Hana (자체 historical 단일 source)**:
```json
{
  "id": "hana.usd",
  "label": "하나은행",
  "axis_group": "krw",
  "unit": "KRW",
  "decimals": 2,
  "provenance": {
    "actual_source": "hana",
    "fallback_source": null,
    "fallback_after_days": null,
    "history_policy": "external_historical",
    "coverage_days": 7300,
    "insufficient_history": false
  }
}
```

**KRX (KIS contract chain, per-point `contract_code` 추가)**:
```json
{
  "id": "krx.usd-krw-futures",
  "label": "KRX 미국달러선물",
  "axis_group": "krw",
  "unit": "KRW",
  "decimals": 1,
  "provenance": {
    "actual_source": "krx",
    "fallback_source": null,
    "history_policy": "external_historical",
    "coverage_days": "varies_by_chain",
    "insufficient_history": false,
    "per_point_metadata": ["contract_code"]
  }
}
```

response 안 data point (KRX series만 contract_code 포함, date-to-contract mapping은 §7-new 참조):
```json
{"ts": "2026-05-18T00:00:00+09:00", "rate": 1496.5, "source": "krx", "contract_code": "A75606"}
// ← 만기일 — daily date bucket은 next contract (07:00 swap 후 user-facing)
{"ts": "{expiry_date 이전 마지막 거래일}", "rate": "...", "source": "krx", "contract_code": "A75605"}
// ← expiry_date 이전 — 만기 contract
```

## 6. Provenance / fallback schema

`provenance` 객체 필드 (Amendment 2026-05-27 — Hana/Bithumb/KRX external_historical series의 `fallback_*` fields는 null. DXY 등 다른 series는 자체 provenance 정책에 따름):

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `actual_source` | string | 본 series의 primary source (예: `"hana"`, `"bithumb"`, `"krx"`) |
| `fallback_source` | string \| null | backfill source. **Amendment 후 Hana/Bithumb/KRX external_historical series에서는 null** (backfill 정책 폐기). DXY 등 다른 series는 자체 provenance 정책 (granularity merge / 외부 fallback chain 등)에 따라 별도 결정 |
| `fallback_after_days` | int \| null | **Amendment 후 Hana/Bithumb/KRX external_historical series에서는 null**. 다른 series는 자체 정책 |
| `history_policy` | enum | `"actual_only"` / `"backfill_chain"` / `"rollup_based"` / `"external_historical"` |
| `coverage_days` | int \| string | series 전체 최대 coverage (string은 `"varies_by_chain"` 같은 경우) |
| `insufficient_history` | bool | 요청한 period를 채울 수 없으면 true (신규 자산/source 장애/coverage 부족 시점) |
| `per_point_metadata` | string[] \| optional | data point에 추가 metadata field 명시 (예: KRX의 `["contract_code"]`) |

`history_policy` enum 의미:
- `actual_only`: backfill 없이 actual_source만, 외부 historical 미보유 (예: Hana 외 7개 은행 1d only)
- `backfill_chain`: legacy v1 패턴 — **Amendment 후 v2 series는 사용 X** (historical record로 enum 유지)
- `rollup_based`: daily rollup으로 자체 축적 — future optimization 후보 (Amendment 2026-05-27 우선순위 ↓)
- `external_historical`: 외부 source 통합 (Hana official endpoint / Bithumb candlestick / KRX KIS chain)

`supports_periods` dynamic은 catalog 응답 시점 `history_policy + coverage_days` 조합으로 결정:
- `actual_only` + coverage_days=30: 3m/1y는 `insufficient_history=true` (예: Hana 외 7개 은행)
- `external_historical` + coverage_days = 외부 source 제공 범위 → period 자연 확장 (예: Hana 7300d, Bithumb 902d, KRX varies_by_chain)
- `rollup_based`: future optimization 도입 후 적용 (현재 미사용)

## 7. Hana official historical source rule (Amendment 2026-05-27 — Hana backfill merge rule 대체)

Hana 장기 그래프 (1w/3m/1y)는 **Hana official historical endpoint 단일 source**. 기존 cutoff_ts 기반 Hana 30d + Investing backfill merge rule은 폐기. Investing은 Hana fallback에서 제거 (graph 의미 일관성).

### Endpoint

```
GET https://www.hanabank.com/cms/rate/wpfxd651_01i_01.do
    ?ajax=true&curCd={USD|JPY|EUR}
    &tmpInqStrDt={YYYY-MM-DD}
    &pbldDvCd=0
    &pbldSqn=
    &hid_key_data=
    &inqStrDt={YYYYMMDD}
Referer: https://www.kebhana.com/cont/mall/mall15/mall1501/index.jsp
```

응답 = HTML fragment (text/html;charset=UTF-8). 5/27 검증 — 어제 / 1년 전 / 10년 전 / 20년 전 + JPY/EUR 모두 일관 schema.

### Canonical date 정책 (★ 휴일 dedup 차단)

- 응답 안 `<em>기준일</em> : <strong>YYYY년MM월DD일</strong>` 값을 **canonical date**로 사용
- 요청 날짜 (`inqStrDt`)가 휴일/공휴일이면 응답 기준일은 직전 영업일로 자동 fallback
  - 2026-05-24 (일) 요청 → 응답 기준일 2026-05-22 (금)
  - 2026-01-01 (신정) → 응답 기준일 2025-12-31
- 클라이언트는 영업일 calendar 보유 불필요 (응답에서 reverse-derive)
- 같은 canonical date 중복 요청은 idempotent skip → 주말/공휴일 duplicate point 차단

### Parser 기준

`<td class="txtAr">` numeric cell 추출. 매매기준율은 **인덱스 7 (8th cell)**:

| 인덱스 | 의미 |
| --- | --- |
| [0] | 현찰 사실 환율 |
| [1] | 현찰 사실 spread |
| [2] | 현찰 파실 환율 |
| [3] | 현찰 파실 spread |
| [4] | 송금 보낼 때 |
| [5] | 송금 받을 때 |
| [6] | 외화수표 파실 때 |
| **[7]** | **매매기준율** ← 사용 |
| [8] | 환가료율 |
| [9] | 미화환산율 |

### 회차 (`pbldSqn`) 정책 — 채택 (Amendment 2026-05-27)

Hana endpoint는 일별 여러 회차 (publishing sequence) 발표. v2 daily historical은 **`pbldSqn=` 빈값 요청으로 반환되는 Hana 기본 일일 historical row를 representative rate로 사용**한다.

- **"최종 회차"라고 공식 명칭으로 단정하지 않음** — Hana 측 공식 용어 검증 X
- **응답에 포함된 회차 값은 provenance/debug metadata로 저장** (예: `{"pbldSqn": 1081}`)
- **요청일과 응답 기준일이 다르면 응답 기준일을 canonical date**로 사용 (휴일 자동 fallback dedup 차단)
- 운영 검증상 최신/최종 고시값으로 동작 (5/27 검증: 2026-05-26 USD = 1081회차, 다음날 07:46 발표 / 2025-05-27 USD = 1237회차)

대안 거부:
- 대표 회차 fix: 과거 모든 날짜에서 해당 회차 보장 의문 (검증 비용 ↑)
- Hana가 반환하는 `pbldSqn` 빈값 row를 명시 정책 없이 암묵 사용: 본 채택 정책과 헷갈릴 수 있어 명확 분리 필요

### 운영 정책

- **Daily scrape**: 매일 KST 09:00경 1회 (전일 발표 후, 휴일 자동 처리)
- **Long backfill** (첫 deploy 시):
  - Calendar-day fetch (단순 loop): ~7300 호출/20년 + canonical date dedup 필수
  - Business-day calendar 보유 시: ~5200 호출/20년 (dedup 불필요)
  - **권고: calendar-day + canonical date dedup** (영업일 calendar 외부 의존 차단)
- **Rate limit**: 미확정 (probe 4회 정상 + Codex GET 6 케이스 통과). 보수 운영 — 분당 30회 ceiling 권장
- **DOM 변경 monitoring**: 응답 size 또는 컬럼 수 alert (Hana template 변경 시 historical fetch 일괄 broken 차단). §14 Open question 항목

### Coverage

20년+ (2006년대 ~ 현재) — USD/JPY/EUR 동일 endpoint. 향후 신규 통화 추가 시 endpoint 지원 여부 별도 확인.

### Provenance

- `actual_source="hana"`, `fallback_source=null` (단일 source)
- `history_policy="external_historical"`, `coverage_days=7300` (대략, 정확값은 catalog 응답 시점 측정)
- response 각 point: `{"ts": "...", "rate": ..., "source": "hana"}` (`contract_code` 등 per-point metadata 없음)

## 7-new. KRX contract chain rollover boundary policy (Amendment 2026-05-27)

KRX 미국달러선물 historical은 **KIS `inquire-daily-fuopchartprice` + A75YMM contract chain** (Amendment 2 — Decision 7 변경).

### Endpoint

표준 REST GET:

```
GET https://openapi.koreainvestment.com:9443/uapi/domestic-futureoption/v1/quotations/inquire-daily-fuopchartprice
Headers:
  appkey: {KIS_APP_KEY}
  appsecret: {KIS_APP_SECRET}
  authorization: Bearer {access_token}
  tr_id: FHKIF03020100
  content-type: application/json; charset=utf-8
Query params:
  FID_COND_MRKT_DIV_CODE: CF
  FID_INPUT_ISCD: {A75YMM}
  FID_INPUT_DATE_1: {YYYYMMDD}
  FID_INPUT_DATE_2: {YYYYMMDD}
  FID_PERIOD_DIV_CODE: D
```

### 종목 코드 명명

A75YMM — Y=년 1자리 (6=2026, 7=2027, ...), MM=월 2자리. 매월 1개 종목 발급.

예시:
- A75606 = 2026-06 만기 (현재 active, 2026-06-15 expiry)
- A75605 = 2026-05 만기 (지남, 2026-05-18 expiry)
- A75701 = 2027-01 만기
- A75812 = 2028-12 만기

KIS master에서 20개 미래 contracts active (~3년 forward).

### Chain 패턴

- 각 contract listing 시작 ~ 만기일 데이터 fetch
- Listing 기간: 만기 5~6개월 전부터 활성 (월별 만기 → 동시에 여러 월물이 listing 중)
- **Contract 수는 rollover boundary 정책에 따라 변동**:
  - Monthly front-month chain (rollover boundary = 만기일 07:00 KST user-facing swap point): 1y는 **최대 12 contracts** (월별 만기 × 12개월)
  - 분기 또는 6개월 chain 등 다른 정책: 더 적은 contracts (Phase 2e 전 정책 확정)
- **각 contract의 사용 구간만 fetch하면 보통 100건 cap 안**. 단, **단일 contract의 listing 전체를 wide range로 조회하면 100건 cap에 걸릴 수 있음** (5/27 실측: A75605를 listing 전체 범위로 요청 시 정확히 100 rows cap 도달, A75606 1y range 동일 cap 도달).
- **100건 cap 처리**: chain 전략 사용 시 contract별 사용 구간만 좁게 fetch (cap 자연 회피). 단일 contract wide range 조회 필요 시 date range split.
- Continuation key 없음 — date range 분할 호출

### Rollover boundary 정책 — 채택: 만기일 07:00 KST user-facing swap point (Amendment 2026-05-27)

현재 운영의 user-facing rollover 정책 (만기일 07:00 KST swap, PR6c-2d-1)을 v2 historical chain에도 동일 적용. 거래소 자체 만기일 정산가 boundary 표현은 제거 (user-facing 운영 정책과 충돌).

**Intraday 운영 기준** ([app/sources/kis_master.py:209+](app/sources/kis_master.py) `select_active_usd_futures_contract`):

- expiry_date 07:00 KST 이전: expiring contract (이미 06:00 야간장 종료)
- expiry_date 07:00 KST 이후: next contract (user-facing 선제 전환)
- 07:00 KST는 휴장 (06:00~08:30) 한가운데 — KRX 이벤트 없음

**Daily historical graph 기준** (date-to-contract mapping):

- **expiry_date 이전 날짜**: expiring contract 사용
- **expiry_date 당일 및 이후 날짜**: next contract 사용 (07:00 swap 일관)
- expiring contract의 expiry_date row는 거래소 원월물 이력에는 존재하지만 v2 user-facing graph에서는 제외
- 만기일 정규장 (08:30~15:45) 거래 가격은 user-facing 노출 X (이미 swap 후)

**대안 거부**:
- 거래소 만기일 정산가 boundary: 거래소 자체 가격 이력 관점이나 user-facing 운영 (07:00 swap)과 충돌 → 그래프 history vs 단말 실시간 fanout 불일치 risk
- 만기 전일 boundary: 운영 정책과 1일 차이
- Trading volume 기준: 구현 복잡 + 과거 재현성 ↓

근거: B-B 검증 실측 — KIS daily endpoint가 **A75606 20260518 close=1496.5 반환** (5/18 만기 contract A75605 만기일 후 next contract user-facing 전환됨). A75605의 20260518 row는 거래소 원월물 이력에 존재하지만 v2에서는 제외.

### Provenance per-point

response 각 point에 `contract_code` 포함. **Date-to-contract mapping anchor**:

```json
{"ts": "2026-05-18T00:00:00+09:00", "rate": 1496.5, "source": "krx", "contract_code": "A75606"}
// ← 만기일 — 07:00 swap 후 user-facing, daily date bucket은 next contract A75606
//   (A75605의 동일 date row는 거래소 원월물 이력에 존재하지만 v2 제외)
//   검증: 2026-05-27 B-B probe — KIS daily endpoint가 A75606 20260518 close=1496.5 반환
{"ts": "{expiry_date 이전 마지막 거래일}", "rate": "...", "source": "krx", "contract_code": "A75605"}
// ← expiry_date 이전 마지막 거래일 — 만기 contract A75605
```

(추가 예시: A75606 20260527 daily row는 검증 시점 intraday 진행 중이라 close 값 stale 가능. durable 문서는 검증 완료된 만기일 row만 인용.)

클라이언트는 `contract_code` 선택적 표시 (tooltip 등).

## 8. external_historical policy + insufficient_history (Amendment 2026-05-27)

Phase 2c 검증 후 Bithumb/KRX/Hana 모두 external_historical 확보. 기존 "actual-only state + insufficient_history empty" 분기는 **신규 자산/source 장애/coverage 부족 시점에만 적용** (완전 제거 X).

### Bithumb (external_historical 확정)

- Endpoint: `https://api.bithumb.com/public/candlestick/USDT_KRW/{interval}` (무인증)
- Coverage: 902일 (2023-12-07 KST 시작) — 자연 ~1일/일 확장
- Interval: 1m, 3m, 5m, 10m, 15m, 30m, 1h, 4h, 6h, 12h, 24h, 1w, 1mm (13개)
- Rate limit: ccxt 500ms/request (분당 120회)
- Provenance: `history_policy="external_historical"`, `actual_source="bithumb"`, `coverage_days=902`

#### Bithumb response schema warning (★ 구현 주의)

Bithumb candlestick 응답은 **비표준 OCHL 순서**:

```
[timestamp_ms, open, close, high, low, volume]
```

표준 OHLC 순서가 아님 → 구현자가 일반 가정 (open, high, low, close)으로 처리하면 close/high를 swap 실수 위험.

ccxt `parse_ohlcv` 인용 ([ccxt/python/ccxt/bithumb.py:640-658](https://github.com/ccxt/ccxt/blob/master/python/ccxt/bithumb.py)):

```python
# Bithumb 응답:
#     [1576823400000,  # 기준 시간 (timestamp)
#      "8284000",       # 시가 (open)    ← index 1
#      "8286000",       # 종가 (close)   ← index 2
#      "8289000",       # 고가 (high)    ← index 3
#      "8276000",       # 저가 (low)     ← index 4
#      "15.41503692"]   # 거래량 (volume) ← index 5
# 표준 OHLCV로 변환:
return [
    ohlcv[0],  # timestamp
    ohlcv[1],  # open
    ohlcv[3],  # high   ← swap
    ohlcv[4],  # low    ← swap
    ohlcv[2],  # close  ← swap
    ohlcv[5],  # volume
]
```

### KRX (external_historical 확정 — chain 패턴)

§7-new 섹션 참조. KIS daily endpoint + A75YMM contract chain + per-point `contract_code` metadata.

### Hana (external_historical 단일 source)

§7 섹션 참조. Hana official endpoint + canonical date dedup + 인덱스 7 매매기준율 + 회차 정책.

### insufficient_history 적용 시점 (Amendment 후에도 유효)

`insufficient_history=true` + `data=[]`은 다음 시점에 여전히 적용:

- **신규 자산 추가 직후** (external_historical 미보유)
- **Source 일시 장애** (Hana/Bithumb/KIS 외부 source 응답 실패)
- **Coverage 부족** (특정 통화는 Hana endpoint 미지원 가능성 등)

응답 예시 (변경 없음):
```json
{
  "series_id": "newly-added.asset",
  "data": [],
  "provenance": {
    "actual_source": "...",
    "history_policy": "actual_only",
    "coverage_days": 0,
    "insufficient_history": true
  }
}
```

**클라이언트 UI 분기**: `insufficient_history=true` 시 해당 series 위치에 "데이터 부족" empty state 표시. response status는 **현재 권고 200 OK + 빈 data array** — 최종 status code (200 vs 422)는 §14 Open questions에서 client UX 검증 후 확정 (Phase 2e 구현 PR 진입 전).

### 금지 사항 (ADR-033 Decision 6번 — Amendment 후에도 유효)

- Bithumb USDT/KRW에 Investing USD/KRW를 backfill로 채우면 안 됨 (crypto microstructure ≠ interbank 환율)
- KRX 미국달러선물에 Investing USD/KRW를 backfill로 채우면 안 됨 (futures instrument ≠ interbank 환율)
- Hana 매매기준율에 Investing 기준환율을 backfill로 채우면 안 됨 (Amendment 2026-05-27 — Hana 자체 historical 단일 source, source mix 차단)

## 9. Axis groups and units

테더 탭에서 자산 mix는 두 group으로 구성:
- **KRW group**: USDT 5거래소 / KRX 미국달러선물 / Investing USD / KB USD / Hana USD (~1400~1500원대)
- **Index group**: DXY / DXY_futures (~99~100 지수)

서로 다른 unit이라 동일 axis 표시 불가능. catalog tab metadata에 axis_groups 정의:

```json
{
  "axis_groups": {
    "krw": {"unit": "KRW", "decimals": 2, "side": "left"},
    "index": {"unit": "INDEX", "decimals": 3, "side": "right"}
  }
}
```

각 series는 `axis_group` 필드로 group 매핑 → 클라이언트는 axis_group 별 분리 axis 렌더링. DXY/DXY_futures 노출 범위는 다음과 같다:

- **USD 탭**: 1d/1w/3m/1y의 **DXY only** (DXY_futures 미노출)
- **Tether 탭**: 1d의 **DXY + DXY_futures**, 1w/3m/1y의 **DXY only** (DXY_futures는 1d only 정책)
- **JPY/EUR 탭**: DXY 계열 미노출 → 단일 KRW axis만 사용

DXY/DXY_futures가 노출되는 탭(USD + Tether)에서만 KRW/Index axis_group 분리 렌더링 적용.

## 10. Endpoint contract

### Initial v2 endpoints (Phase 2e 첫 구현 대상)

#### `GET /api/v2/graph/catalog`

전체 catalog (tab × period × series) 응답. 클라이언트 startup 시 1회 fetch (or ETag 캐시 revalidate).

응답 schema:
```json
{
  "tabs": [
    /* Section 4 catalog matrix schema */
  ],
  "version": "2026-05-27",
  "cache_ttl_seconds": 3600
}
```

#### `GET /api/v2/graph/tab?tab={tab}&period={period}`

특정 탭 × 기간의 모든 series 데이터.

응답 schema:
```json
{
  "tab": "usd",
  "period": "1d",
  "series": [
    {
      "id": "hana.usd",
      "data": [
        {"ts": "2026-05-27T14:30:00+09:00", "rate": 1402.5, "source": "hana"},
        {"ts": "2026-05-27T14:40:00+09:00", "rate": 1402.8, "source": "hana"}
      ],
      "provenance": { /* Section 6 schema */ }
    }
  ],
  "metadata": {
    "fetched_at": "2026-05-27T14:50:12+09:00",
    "bucket_size": "10min"
  }
}
```

### Later v2 endpoint (Phase 2e 이후 필요 시 land)

#### `GET /api/v2/graph/series/{series_id}?period={period}`

단일 series 데이터 (디버그용 / 자산 별도 fetch optimization). 첫 구현 필수 아님.

```json
{
  "series_id": "hana.usd",
  "period": "1d",
  "data": [...],
  "provenance": {...}
}
```

## 11. Cache key strategy

- **legacy v1** (유지, 변경 0):
  - 1d: Redis `graph:{currency}` (TTL 120s) — `refresh_graph_cache` 매분 :03 갱신
  - 1w/3m/1y: in-memory `_period_cache["graph:{currency}:{period}"]`
- **v2 catalog**: `graph_v2:catalog` (TTL 3600s, ETag based revalidate 검토)
- **v2 tab**: `graph_v2:tab:{tab}:{period}` (TTL 60s)
- **v2 series (Later)**: `graph_v2:series:{series_id}:{period}` (TTL 60s)

**Invalidation** (Phase 2e 구현 시 도입 예정): scheduler가 새 bucket flush 시점에 v2 tab key 무효화 패턴 추가 — legacy v1 (`refresh_graph_cache`, [scheduler.py:1540](app/scheduler.py#L1540))과 동일 방식. catalog는 자산 추가/제거 시점에만 갱신 — 정상 운영 중에는 TTL 만료까지 유지.

## 12. Legacy coexistence

- legacy v1 `/api/graph/{currency}` 응답 schema 변경 없음 — 구단말 회귀 0 보장
- v2 endpoint와 hot path/cache key prefix 분리
- v1 → v2 마이그레이션은 신규 단말 release 시점 client side로 cutover
- v1 deprecation은 별도 단계 (구단말 사용자 1% 미만 시 검토)
- Redis cache key prefix 분리로 v1/v2 invalidation 독립 — 한쪽 hot path 변경이 다른 쪽 영향 없음

## 13. Rollout plan (Amendment 2026-05-27)

1. **Phase 2b** (완료, 본 문서 + ADR-033 land): 정책 anchor — design 문서 + ADR + CLAUDE.md anchor.
2. ✅ **Phase 2c** (완료, 2026-05-27): Bithumb/KRX/Hana historical source 외부 조사 — 3 source 모두 external_historical 확보. ADR-033 Amendment 2026-05-27 + 본 문서 §3/§5/§6/§7/§7-new/§8 update.
3. **Phase 2d** (우선순위 ↓, future optimization): daily rollup 구현. 장기 coverage 확보 목적은 Phase 2c external_historical로 대체. 단 internal cache / materialization / 외부 source 일시 장애 대비 / 응답 latency 개선 목적은 future optimization 후보 보존. retention 결정은 그대로 별도 ADR.
4. **Phase 2e**: v2 endpoint 구현 PR — catalog + tab graph 2개 endpoint + historical scrape job 통합 (Hana daily + Bithumb candlestick + KRX KIS chain). 진입 전 확정 항목: KRX rollover boundary (옵션 a/b/c) + Hana 회차 정책 (옵션 a/b/c).
5. **Phase 2f** (Later, optional): single series endpoint 추가 — 사용 패턴 확보 후.

## 14. Open questions (Amendment 2026-05-27)

### Phase 2e 전 확정 필요 (Amendment 2026-05-27 이후)

- **insufficient_history 응답 status code**: 200 + empty data (현재 권장) vs 422 — 클라이언트 UX 분기 결정 영향.
- **bucket_size 재정의 여부**: 1d=10min / 1w=1h / 3m/1y=1d는 legacy v1 패턴 그대로인지, v2에서 재정의할지 (예: 1y는 1주 bucket?).
- **★ 신규 — KRX 1w bucket 정책** (제품 UX 결정 영역):
  - (a) source_rates 30일 보유분으로 1시간 bucket (운영 일관, 1w<30일 충분)
  - (b) KIS intraday endpoint 별도 조사 (예: inquire-time-itemchartprice)
  - (c) Daily 다운그레이드 (1주 = 7 points)
- **★ 신규 — Hana 1w bucket 정책** (제품 UX 결정 영역):
  - (a) bank_exchange_rates 30일 보유분으로 1시간 bucket (Hana actual)
  - (b) Daily 다운그레이드
  - (c) v2 catalog 제외 (legacy v1만)

### General open (Phase 2e 외, 운영 후 결정)

- **Hana DOM 변경 monitoring**: 응답 size/컬럼 수 alert 어떻게 구현 (template 변경 시 historical fetch 일괄 broken 차단). §7 참조.
- **Hana rate limit 측정**: 분당/시간당 안전 ceiling 실측 (현재 probe 4-6회 정상, 분당 30회 보수 운영 권장).
- **bulk Excel/TXT endpoint reverse engineering**: doExcelDown JavaScript 함수 reverse — future optimization (HTML fragment만으로 production 충분).
- **v2 catalog version 표시 방식**: ETag 기반 revalidate vs version field 기반 client polling — UX/네트워크 비용 trade-off.
- **클라이언트 cache TTL 정책**: catalog 1시간 TTL은 적절한가? 자산 추가 변경 시점 빠른 반영 필요한가?
- **default_visible_series 정책**: client 별 customization 허용할지, 서버 default로 잠글지.

### 해결됨 (closed by Amendment 2026-05-27)

- ~~Bithumb USDT historical source~~ → ✅ 공식 candlestick API 채택
- ~~KRX 미국달러선물 historical source~~ → ✅ KIS daily endpoint + A75YMM contract chain 채택
- ~~daily rollup retention 결정 시점~~ → 우선순위 ↓ (장기 coverage 목적은 external_historical로 대체). future optimization 도입 시점 별도 ADR.
- ~~KRX contract chain rollover boundary~~ → ✅ **채택: 만기일 07:00 KST user-facing swap point** (현재 운영 정책 일관, §7-new 참조). Daily graph는 expiry_date 당일 및 이후 = next contract.
- ~~Hana 회차 (`pbldSqn`) 정책~~ → ✅ **채택: `pbldSqn=` 빈값 기본 일일 historical row + 응답 회차 provenance 저장** (§7 참조). "최종 회차" 공식 단정 X.

---

## 관련 문서

- [ADR-033](DECISIONS.md#adr-033-graph-api-v2-catalog-policy--legacy-공존--hana-backfill--bithumbkrx-actual-only--dxy_futures-1d-only): 본 문서의 정책 anchor ADR
- [ADR-019](DECISIONS.md#adr-019-dxy-보조지표--granularity-기반-2-part-merge-전략): DXY granularity 2-part merge (legacy comparison only — Amendment 2026-05-27 후 Hana는 자체 historical 단일 source라 본 merge 패턴 미사용)
- [ADR-023](DECISIONS.md#adr-023-데이터-보관-정책-30일-통일-banksource_ratesdxy-realtime): 30일 cap 정책 anchor
- [app/admin/graph_cache.py](app/admin/graph_cache.py): legacy v1 graph 구현 (DXY 2-part merge 패턴)
- [app/admin/dxy_rollup.py](app/admin/dxy_rollup.py): DXY rollup 구현 (Phase 2d source_rates 적용 패턴 참조)
- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md): 서비스 계약 source of truth (topic-only / dual-emit)
