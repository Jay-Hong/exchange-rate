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
- provenance schema (actual_source + history_policy + close_basis / source_method / ohlc_quality + per-point metadata)
- Hana daily canonical policy (Amendment 후속 — observed_eod + official_historical_backfill 2-source 분리)
- Bithumb/KRX external_historical policy + insufficient_history fallback (신규 자산 / source 장애 / coverage 부족 시점)
- source_daily_rates canonical daily table (구현 상세는 ADR-034 참조)
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

`GET /api/graph/{currency}?range={1d|1w|3m|1y}` ([app/main.py:2463](app/main.py#L2463)):

- 통화: USD/JPY/EUR only
- 1d (10분 bucket): investing + KB + 하나 + DXY (USD 탭만)
- 1w (1시간 bucket, full-window): investing + DXY hourly
- 3m/1y (1일 bucket): investing + DXY daily + realtime tail 7d
- 응답 schema: `{"data": [...], "sources": {...}}` (legacy)
- cache 실측 (2026-05-27 코드 확인):
  - 1d: Redis `graph:{currency}` (TTL 120s) — [app/main.py:605](app/main.py#L605), [app/admin/graph_cache.py:651](app/admin/graph_cache.py#L651)
  - 1w/3m/1y: in-memory `_period_cache["graph:{currency}:{period}"]` — [app/main.py:2656-2699](app/main.py#L2656-L2699)(선언·조회·eviction) · [app/main.py:2746-2748](app/main.py#L2746-L2748)(저장)

v2와 hot path 분리 + cache key prefix 분리 (v1 prefix `graph:`, v2 prefix `graph_v2:`).

## 3. v2 product requirements

| Tab | 1d (10min bucket) | 1w (1h bucket) | 3m / 1y (1d bucket) |
| --- | --- | --- | --- |
| USD | 8 banks (Citi 제외) + investing + **KRX (ADR-038 D4 ②, default OFF)** + DXY | investing + **KRX** + Hana (observed_eod + official_historical_backfill 2-source 분리) + DXY hourly | investing + **KRX** + Hana (observed_eod + official_historical_backfill 2-source 분리) + DXY daily |
| JPY | 8 banks (Citi 제외) + investing | investing + Hana (observed_eod + official_historical_backfill 2-source 분리) | investing + Hana (observed_eod + official_historical_backfill 2-source 분리) |
| EUR | 8 banks (Citi 제외) + investing | investing + Hana (observed_eod + official_historical_backfill 2-source 분리) | investing + Hana (observed_eod + official_historical_backfill 2-source 분리) |
| Tether | 5 exchanges (Upbit/Bithumb/Coinone/Korbit/Gopax) + KRX + investing USD + KB USD + Hana USD + DXY + DXY_futures | Bithumb (대표, external_historical) + KRX (external_historical, contract chain) + investing USD + Hana (observed_eod + official_historical_backfill 2-source 분리) + DXY hourly | Bithumb (대표, external_historical 902d) + KRX (external_historical, contract chain) + investing USD + Hana (observed_eod + official_historical_backfill 2-source 분리) + DXY daily |

**제약** (Amendment 2026-05-27 반영):
- Bithumb 3m/1y: Bithumb 공식 candlestick API external_historical (902일 coverage)
- KRX 3m/1y: KIS `inquire-daily-fuopchartprice` + A75YMM contract chain external_historical
- Hana 3m/1y: 2-source 분리 정책 (Amendment 후속) — `hana_observed_eod` canonical (앞으로) + `hana_official_historical_backfill` (과거 부족분). Investing은 Hana backfill에 미사용 (Hana series identity 유지). **Invariant (Step 4A, 2026-06-02)**: official backfill은 `hana_observed_eod` canonical row를 덮지 않음 (official→observed = overlap guard + conditional conflict update). 역방향(observed→official)은 기존 observed_eod post-write nullable validation rollback으로 차단. 경계 전환 정책(ADR-034 §10 Open) 결정 전까지 **양방향** overlap reject.
- Hana 1w: Phase 2e 전 결정 (open question, §14 참조 — 후보 a/b/c)
- KRX 1w: Phase 2e 전 결정 (open question, §14 참조 — 후보 a/b/c)
- **USD 탭 KRX (ADR-038 D4 ②, 2026-07-08)**: 전 기간(1d/1w/3m/1y) 편입, 위치는 investing 다음
  (시세 행 순서 일치), **default_visible 미포함**(기본 OFF — 2026-07-03 "최소 2개 시작" 결정 정합,
  KRX는 사용자가 의도적으로 켜는 선물 보조지표). 데이터는 테더 탭 KRX와 동일 reader
  (1d=source_rates / 1w=source_hourly_rates / 3m·1y=source_daily_rates). G2∧G3
  (`KRX_CLIENT_DISTRIBUTION_EFFECTIVE`) off면 `krx.` prefix 필터로 전 기간 catalog/tab 제외.
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
          "default_visible_series": ["investing.usd", "hana.usd"],
          "all_series": ["investing.usd", "krx.usd-krw-futures", "kb.usd", "hana.usd", "shinhan.usd", "woori.usd", "ibk.usd", "nh.usd", "sc.usd", "bs.usd", "dxy"]
        },
        "1w": {
          "default_visible_series": ["investing.usd", "hana.usd", "dxy"],
          "all_series": ["investing.usd", "krx.usd-krw-futures", "hana.usd", "dxy"]
        },
        "3m": {
          "default_visible_series": ["investing.usd", "hana.usd", "dxy"],
          "all_series": ["investing.usd", "krx.usd-krw-futures", "hana.usd", "dxy"]
        },
        "1y": {
          "default_visible_series": ["investing.usd", "hana.usd", "dxy"],
          "all_series": ["investing.usd", "krx.usd-krw-futures", "hana.usd", "dxy"]
        }
      }
    }
  ]
}
```

period별 `all_series` / `default_visible_series` 분리:
- `all_series`: 해당 period에서 fetch 가능한 모든 series ID
- `default_visible_series`: 첫 진입 시 체크박스 ON 상태로 표시할 series (client UX)

**FX 1d series 순서 / default 정책** (사용자 지정 2026-07-03):
- `all_series` 은행 순서 = 앱 은행순서설정(`Bank.displayCases`): investing → kb → hana → shinhan →
  woori → ibk → nh → sc → bs → (usd만) dxy. 토글/차트/은행별환율 섹션 순서를 하나로 통일.
- `default_visible_series` = **investing + hana만** (usd의 dxy·kb도 기본 OFF). 소스가 많아(9~10)
  최소 2개로 시작, 나머지는 사용자 토글. 토글 상태는 client가 per-tab persist(재시작 유지).
- client 토글 라벨은 `Bank.displayName`(국민은행/기업은행/SC제일 등) — server label(KB국민은행 등)과
  다른 짧은 공식명. (server label은 진단/fallback용으로 보존.)

**테더 default_visible / KRX 상호배타 정책** (ADR-038 D4 후속, 사용자 지정 2026-07-09):
- 테더 `default_visible_series` base = `[upbit, bithumb, hana, krx]`(1d) / `[bithumb, hana, krx]`(장기).
  **DXY 기본 OFF**(선물지수도 OFF).
- server는 per-user 무인증이라 base에 `hana`·`krx` 둘 다 포함하고, **client가 `krxVisible`로 상호배타**:
  krxVisible=true → `hana.usd` drop(krx 우선, 실선물 거래자) / krxVisible=false → `krx.*` drop(기존 필터).
  결과: krx 단말 = 업비트+빗썸+달러선물, 비krx = 업비트+빗썸+하나(장기는 업비트 미존재라 자연 제외).
- **테더/달러 그래프 토글 순서**: krx 있으면 참조군 맨 앞(krx → investing → kb → hana). 서버 all_series
  순서와 별개로 client 참조군 정렬(`GraphV2Section.referencesOrder`).
- **DXY ↔ DXY_futures 토글 상호배타**(테더 탭): 둘 다 OFF 허용, 하나 ON 중 다른 하나 ON 시 기존 OFF 전환.

## 5. Series metadata schema

각 series는 catalog 안에 다음 metadata를 가진다 (Amendment 2026-05-27 — Hana 단일 source / KRX contract chain per-point metadata).

**Hana (observed_eod + official_historical_backfill 2-source 분리 — mixed series)**:
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
    "insufficient_history": false,
    "close_basis_mode": "mixed",
    "default_close_basis": "hana_observed_eod",
    "close_basis_values": ["hana_observed_eod", "hana_official_historical_backfill"],
    "default_source_method": "observed_rollup",
    "source_method_values": ["observed_rollup", "external_backfill"],
    "per_point_metadata": ["close_basis", "source_method"]
  }
}
```

per-point data (mixed series, 구간별 close_basis 다름):
```json
{"ts": "2026-04-27", "rate": 1402.5, "close_basis": "hana_observed_eod", "source_method": "observed_rollup"}
{"ts": "2025-12-01", "rate": 1438.2, "close_basis": "hana_official_historical_backfill", "source_method": "external_backfill"}
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
{"ts": "2026-05-18T00:00:00+09:00", "rate": 1496.5, "source": "krx", "high": 1499.0, "low": 1494.0, "contract_code": "A75606"}
// ← 만기일 — daily date bucket은 next contract (07:00 swap 후 user-facing)
{"ts": "{expiry_date 이전 마지막 거래일}", "rate": "...", "source": "krx", "contract_code": "A75605"}
// ← expiry_date 이전 — 만기 contract
```

data point 필드: `ts`(ISO8601 KST) · `rate`(=close) · `source` · **`high`/`low`**(optional, source_daily/hourly의 버킷 고가/저가 — client 단일 소스 음영 밴드용. `close_only` row는 high=low=close. row에 없으면 생략 → client nil → 밴드 미표시. DXY/market_index series는 미노출) · `close_basis`/`source_method`(mixed series per-point) · `contract_code`(KRX).

### 5.1 Serve-time X축 domain (displayDomain 계약 — ADR-039 X축 통일)

**문제**: 클라가 X축 범위를 series 데이터 min/max에서 유도하면, 같은 기간이라도 탭별로 데이터 커버리지가 달라
X축 타임라인이 어긋난다(예: 테더 1주는 빗썸 첫 bucket이 07-12, 달러 1주는 investing 첫 bucket이 07-13 → 두 탭
X축 시작이 다름). **해결**: 서버가 탭 무관 공통 domain을 **serve 시점에** 계산해 응답에 부착하고, 클라는 X축·눈금·
liveAnchor·줌clamp를 이 domain으로 계산한다(실제 데이터 범위/극값은 별개로 historicalBounds 유지).

**envelope (프리미엄 ≠ 무료)**:
- 프리미엄 `/api/v2/graph/tab` → **`metadata`**에 부착: `metadata.domain_start_at`/`domain_end_at`/`live_domain_mode`
- 무료 `/api/v2/free/snapshot` → **`graph`**에 부착(flat FreeGraphBlock, metadata wrapper 없음): `graph.domain_start_at`/…

**필드**:
| 필드 | 값 |
| --- | --- |
| `domain_start_at` | ISO8601 **+09:00** — X축 좌측 경계 |
| `domain_end_at` | ISO8601 **+09:00** — X축 우측 경계 (anchor) |
| `live_domain_mode` | `"rolling"`(1d) \| `"fixed_start"`(1w/3m/1y) |

**정책** (`period_domain(period, anchor)`):
- **1d = rolling**: `[anchor-24h, anchor]`. 클라는 양끝을 render-now로 이동(폭 24h 유지).
- **1w/3m/1y = fixed_start**: `[(anchor KST날짜 - N) 00:00 KST, anchor]`. 클라는 start 고정·우측만 render-now로 확장.
  - N: 1w=7 / 3m=90 / 1y=365. **1w는 정확 168h가 아닌 달력 경계**(today-7 00:00 — 금요일 종가관리 비교용, `period_range`와 동일 의도).

**anchor**:
- 프리미엄 = **serve-time now**(요청당 1회). 전날 빌드된 read-through 캐시도 오늘 domain을 받음(캐시는 date-less → domain을 굽지 않고 serve 시점 attach로 자정 경계 stale 방지).
- 무료 = **as_of**(스냅샷 basis = 매시 HH:30). 매시 고정 불변식과 정합(값도 domain도 as_of 기준).

**불변식**:
- **출력 domain timestamp는 항상 +09:00**(anchor를 KST로 정규화). 입력(무료 as_of)은 tz-aware면 offset 무관 허용·KST 정규화, naive면 거부.
- **canonical/캐시 원본 불변**: attach는 copy에만(Redis 캐시·last-good·free canonical은 domain 없이 깨끗 유지 → 재검증 회귀 0).
- **무료 as_of 파싱 실패/naive**: `attach_free_snapshot_domain` helper는 domain 없이 canonical 그대로 반환(crash 방지 defense-in-depth, 입력 불변). **단 endpoint는 그 전에 `validate_snapshot_payload`가 as_of를 tz-aware(naive 명시 거부) + HH:30 grid + cutoff로 검증**해 bad as_of canonical을 거부(503 또는 last-good) → 이 helper fallback은 endpoint 경로에선 도달 불가. 정상 canonical(aware as_of)만 domain을 받고, fully-naive 오염 canonical도 as_of tz-aware assert에서 걸러진다(cutoff 비교가 naive-vs-naive면 무의미해 새는 것을 차단).
- 404/400 오류 경로는 attach bypass.

**클라(iOS) 적용**: `GraphV2PreparedTab.displayDomain`이 있으면 X축·눈금·liveAnchor·줌clamp를 domain으로 라우팅, 없으면(구 서버/파싱 실패) 기존 data-derived(historicalBounds)로 fallback. 데이터/극값 계산은 항상 historicalBounds.

### 5.1b 무료 스냅샷 `refresh_not_before` (serve-time 재요청 권장 시각 — 클라 폴링 효율화)

> **상태(2026-07-21)**: **서버 slice(7f4e69a) + iOS one-shot consumer(c31dfe9→fb9d15c) 모두 구현**. 서버는 refresh_not_before 부착(additive), iOS는 5분 폴링+20초×9 재시도를 단일 scheduler로 교체(load-owns-reschedule / schedulerFetchingPeriod 소유권 / due-check nextEligibleAt 존중 / cancellation 분리 / 선택탭 게이트 / preload per-period 실패 cooldown, codex 7라운드 하드닝[최종 코드 finding 0], FXiTests 342 passed). **서버 배포 + iOS dev E2E PASS(2026-07-21) — 효율화 slice 서버+iOS end-to-end 완료.** 서버(21398e3/bd111ee): cron :30:19 canonical DB0 write, 응답 조립 top-level rnb=다음 :31:00+09:00(immut/4period/serve·cron 동일 DB). iOS dev E2E 실측: **인증 HTTP 200 + top-level `refresh_not_before`=다음 :31:00+09:00** / **one-shot**(활성 period만 :31:12 발화, 초기 warm 후 5분 폴링 0) / **인접 탭왕복·foreground 무추가 GET 0**. (먼 탭 이동 시 `.page` 페이지 재생성 재요청은 SwiftUI 리텐션 특성, 계약 아님.) 응답 gzip(enc=gzip; 1d body ~137KB 비압축, wire는 압축 — 정확 egress는 nginx body_bytes_sent).

무료는 매시 :30 cron이 canonical을 굽는다(:30:19). 클라가 5분 폴링으로 hour-boundary를 확인하던 것을, 서버가 "이 시각 이후 재요청 권장"을 응답에 실어 **one-shot 스케줄**로 대체할 축을 제공한다(클라 전환은 iOS slice).

**필드** (top-level, 성공 응답 2경로[Redis canonical / local last-good] 모두 부착):

| 필드 | 값 |
| --- | --- |
| `refresh_not_before` | ISO8601 **+09:00** — 다음 재요청 권장 시각 (= 다음 HH:30 publish slot[now 초과] + 60초 = **HH:31:00**) |

**anchor = serve-time now**(as_of 아님). `compute_refresh_not_before(now)` = 다음 :30 slot(now 초과) + `PRECOMPUTE_SECOND(19)+READY_MARGIN(41)`(=60s). **as_of가 stale(cron 지연)이어도 항상 미래 slot 반환** — stale 판단·recovery는 클라가 as_of로 별도 처리.

**불변식**:

- **"재요청 권장 시각"이지 새 데이터 존재 보장이 아니다.**
- **canonical/캐시 원본 불변**: attach는 copy에만(domain과 동일 — Redis/last-good/free canonical엔 미저장).
- **출력 +09:00**: naive now 거부, aware는 KST 정규화.
- **503(canonical 전무)엔 미부착** (스냅샷 자체 없음). **additive** — 구 클라는 미인지 top-level 필드 무시.
- cron 발화 타이밍(`FREE_SNAPSHOT_BASIS_MINUTE`/`PRECOMPUTE_SECOND`)은 scheduler와 compute가 **공유 상수**(ETC).

**클라(iOS) 사용 계약**: `refresh_not_before` + 설치별·탭별 결정적 jitter(10~30s)로 **one-shot 스케줄**(매시 :31:10~:31:30 분산). 구 as_of(cron 지연) 관측 시 20→40→80 bounded backoff, 소진 후에도 저빈도 recovery one-shot으로 fresh까지(장기 안전망). 필드 누락→`as_of+1h+안전지연`, 과거→`now+min` clamp. 선택된 탭만 scheduler 활성.

### 5.2 series `carry_in` — 좌측 gap seed (ADR-039 slice 1)

**문제**: fixed_start(1w/3m/1y) domain은 X축 좌측이 window 시작에 고정되는데, 실관측 데이터의 첫 bucket이 window 시작보다 늦으면(주말·휴일 gap) 좌측이 비어 보인다. **클라가 in-window 첫 관측값으로 backward-fill 하면 틀린다** — 주말 이슈로 gap 상승/하강한 경우 첫 in-window 값은 이미 점프 뒤라 이전 timeline을 왜곡한다.

**해결**: 서버가 **window 시작 직전(strict `<`)의 마지막 실관측값**을 series별 `carry_in`으로 부착한다. 클라는 이를 forwardFilled **입력 배열의 seed**로 넣어 gap 구간을 이전 실제값으로 **평평하게(flat hold)** 유지한다. 근사·backward-fill이 아니라 timeline 직전의 진짜 관측값이다.

**필드** (series 객체 내, nullable):
| 필드 | 값 |
| --- | --- |
| `carry_in` | `{ "rate": float, "observed_at": ISO8601 +09:00 }` 또는 `null` |
| `carry_in.rate` | window 시작 직전 마지막 실관측 close |
| `carry_in.observed_at` | 그 관측의 bucket 시각(daily=00:00 KST / hourly=bucket_ts KST). **원래 관측 시각 보존**(window 시작으로 당기지 않음) |

**정책**:
- **source_daily_rates(3m/1y)** = `date_kst < window_start_date` 마지막 row. `observed_at` = 그 날짜 00:00 KST.
- **source_hourly_rates(1w)** = `bucket_ts_kst < window_start_ts` 마지막 bucket. `observed_at` = 그 bucket_ts KST.
- **DXY(market_index_rates)** = `timestamp < start_utc` 마지막 granularity row(1w=hourly/3m·1y=daily). 좌측 gap seed 회귀 방지 위해 DXY도 부착(4 series 모두 채움).
- **strict `<`**: window 시작 당일/당시 bucket은 `carry_in`이 아니라 `data[]`에 들어간다(중복 금지). prior가 없으면 `null`.
- **provenance 직교**: `carry_in` 유무는 `coverage_days`/`insufficient_history`/`data[]`에 영향 없음(순수 seed).
- **1d(rolling)**: 미부착(dense bucketize라 좌측 빈칸 없음 — slice 2에서 sparse 전환 시 재검토).

**클라 소비**: `carry_in`이 있으면 forwardFilled **입력**에 `(frameStart, rate)`를 맨 앞 seed로 prepend(**그리기 좌표는 frameStart** — `observed_at`은 위장하지 않고 `GraphV2PreparedSeries.carryIn` provenance로만 보존). → 첫 in-window 관측 전까지 flat hold. 클라 leading backward-fill(첫 in-window 값 채움)은 **제거**. `carry_in`이 null이면 seed 없음(데이터 첫 점부터 그림). 가드: `period.isFixedStart` + `!insufficient_history` + `displayDomain.start` 존재 + 첫 obs.ts > frameStart.

> **gap 전환 렌더(slice 3 범위)**: seed는 forwardFilled 입력에 들어가 gap 구간을 flat hold 하지만, gap 끝 마지막 구간은 forwardFilled의 기존 hold 규칙(gap>1.5×bucket) + 클라 `LineMark` 선형보간이라 **1-bucket 램프**(완전 수직 step 아님)로 그려진다 — 이는 그래프 내부 gap과 **동일한** 기존 동작이다. 완전 step/gap-aware 전환은 slice 3에서 좌측·내부 gap을 함께 다룬다(slice 1은 값 교정: 첫값 backward-fill → 진짜 이전값 carry-forward).

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
| `close_basis_mode` | enum | **Amendment 후속**. `"single"` (series 전체 동일 close_basis) 또는 `"mixed"` (구간별 다름 — 예: Hana의 observed_eod ↔ official_historical_backfill 경계) |
| `default_close_basis` | enum | **Amendment 후속**. series-level default value. mixed series에서는 **canonical/future append 기준 기본 close_basis** (예: Hana mixed의 default = `hana_observed_eod`, 앞으로 쌓는 정책 기준 고정값 — backfill 데이터량과 무관 time-invariant). 9 values 중 하나 (아래 참조, Investing daily은 ADR-035 D1, Bithumb/Investing/Hana/KRX hourly는 ADR-035 D3) |
| `close_basis_values` | enum[] | mixed series만 — 본 series가 사용하는 모든 close_basis values (예: `["hana_observed_eod", "hana_official_historical_backfill"]`) |
| `default_source_method` | enum | series-level default. mixed series에서는 **canonical/future append 기준 기본 source_method** (예: Hana mixed의 default = `observed_rollup`). 6 values 중 하나 (아래 참조) |
| `source_method_values` | enum[] | mixed series만 — 본 series가 사용하는 모든 source_method values |

`close_basis` enum 의미 (Amendment 후속 — source identity 명시):

- `krx_cf_close_1545`: KRX CF 정규장 15:45 KST close finalizer
- `bithumb_24h_kst_close`: Bithumb 24h candle KST 00:00 boundary close
- `hana_observed_eod`: 우리 DB에서 KST 해당일 24:00 이전 마지막으로 관측한 Hana 고시값
- `hana_official_historical_backfill`: Hana 사이트 historical row (다음날 새벽 고시, 과거 부족분 보강용)
- `investing_observed_eod`: 우리 DB(`investing_exchange_rates` 장기 보관)에서 KST 해당일 마지막 관측 기준 환율 (ADR-035 D1, Proposed)
- `bithumb_observed_hourly`: `source_rates` raw tick → KST 1h bucket 마지막 관측 close (ADR-035 D3, **1w hourly** — `source_hourly_rates`. daily `bithumb_24h_kst_close`와 다른 granularity: 3m/1y=24h candle, 1w=시간별 관측)
- `investing_observed_hourly`: `investing_exchange_rates` raw 관측 → KST 1h bucket 마지막 관측 close (ADR-035 D3, **1w hourly** — `source_hourly_rates`, per-currency usd/jpy/eur. daily `investing_observed_eod`와 다른 granularity)
- `hana_observed_hourly`: `bank_exchange_rates`(bank=hana) 고시 관측 → KST 1h bucket 마지막 관측 close (ADR-035 D3, **1w hourly** — `source_hourly_rates`, per-currency usd/jpy/eur, ohlc_quality observed_rollup/close_only 분기. daily `hana_observed_eod`와 다른 granularity)
- `krx_observed_hourly`: `source_rates`(krx/usd-krw-futures) raw tick → KST 1h bucket 마지막 관측 close (ADR-035 D3, **1w hourly** — `source_hourly_rates`. **session-agnostic** — CF/CM 공통 enum. daily `krx_cf_close_1545`와 다른 granularity). **[재설계 2026-06-10]**: ~~첫 PR CF 정규장 08:30~15:45 coverage + contract_code는 daily row 재사용~~ → **세션 무관 CF/CM 통합 rollup + contract_code 미저장** (Bithumb/Investing/Hana와 동일 모델 — 월물 제거로 CM 야간 자연 커버 + 일봉 의존 절단). **KRX 1w per-point `contract_code` 노출은 자연 소멸** (graph_v2 has_contract 동적 판정 — daily 3m/1y의 per-point contract는 불변. 필요 시 read-side daily join 옵션, 미구현)

같은 series 안에서 구간별로 다른 close_basis인 경우 per-point metadata로 표시 (특히 Hana의 backfill vs canonical 경계).

`source_method` enum 의미 (close_basis와 직교 — 획득 방식):

- `observed_rollup`: DB 관측 기반 daily rollup (source_rates[KRX/Bithumb] / bank_exchange_rates[Hana] / investing_exchange_rates[Investing, ADR-035 D1])
- `external_backfill`: 외부 API에서 초기 부족분 backfill (Hana official endpoint 등)
- `close_finalizer`: KRX CF close finalizer 결과
- `bithumb_candlestick_api`: Bithumb 공식 24h candle API (backfill + daily refresh append 동일 방법 — Amendment 2026-06-01, 구 `bithumb_candlestick_backfill`)
- `kis_daily_backfill`: KIS daily endpoint + A75YMM chain 초기 backfill

→ **Bithumb은 backfill·append 동일 방법**(`bithumb_candlestick_api` — Amendment 2026-06-01, close_basis·source_method **모두 단일**. candle이 실 OHLC 제공해 DB rollup보다 우월). KRX는 close_basis 동일 + source_method 분기(`kis_daily_backfill` vs `close_finalizer`). Hana는 close_basis + source_method 둘 다 분리. provenance 측면 backfill/운영 구간 식별.

`history_policy` enum 의미:
- `actual_only`: backfill 없이 actual_source만, 외부 historical 미보유 (예: Hana 외 7개 은행 1d only)
- `backfill_chain`: legacy v1 패턴 — **Amendment 후 v2 series는 사용 X** (historical record로 enum 유지)
- `rollup_based`: daily rollup으로 자체 축적 — future optimization 후보 (Amendment 2026-05-27 우선순위 ↓)
- `external_historical`: 외부 source 통합 (Hana official endpoint / Bithumb candlestick / KRX KIS chain)

`supports_periods` dynamic은 catalog 응답 시점 `history_policy + coverage_days` 조합으로 결정:
- `actual_only` + coverage_days=30: 3m/1y는 `insufficient_history=true` (예: Hana 외 7개 은행)
- `external_historical` + coverage_days = 외부 source 제공 범위 → period 자연 확장 (예: Hana 7300d, Bithumb 902d, KRX varies_by_chain)
- `rollup_based`: future optimization 도입 후 적용 (현재 미사용)

## 7. Hana daily canonical policy (Amendment 후속 — 정책 재작성)

Hana 장기 그래프 (3m/1y)는 다음 **2-source 분리 정책**. 기존 "Hana official historical endpoint 단일 source" 표현은 폐기. **1w는 Phase 2e 전 결정 open question** (§14 참조 — KRX 1w + Hana 1w 후보 a/b/c).

### Hana daily canonical 정의 (★)

- **앞으로 쌓이는 구간** (DB observed_eod 산출 가능 구간): `hana_observed_eod`
  - bank_exchange_rates의 KST 24:00 이전 마지막 Hana 관측값
  - = 우리 DB에서 KST 해당일 24:00 이전 마지막으로 관측한 Hana 고시값
- **과거 부족분 보강** (DB raw 관측값 없는 구간): `hana_official_historical_backfill`
  - Hana official endpoint (`wpfxd651_01i_01.do`) historical row
  - 응답 기준일을 canonical date로 사용 (휴일 자동 fallback dedup)
  - 다음날 새벽 고시 row (KST EOD와 timing 의미 다름 — provenance로 명시)
- **두 구간이 섞이는 경계**: per-point `close_basis` provenance로 표시
- **Investing은 Hana backfill source에 미사용** — Hana series identity 유지

### Hana official endpoint (backfill source)

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

### close_basis = krx_cf_close_1545 (Amendment 후속)

- **매일 append (canonical)**: close finalizer가 잡은 CF 15:45 정규장 종가. source_method=`close_finalizer`
- **초기 backfill**: KIS daily endpoint + A75YMM contract chain. source_method=`kis_daily_backfill` (정규장 일봉 close)
- CM 06:00 야간 종가는 별 session close로 보존, 3m/1y daily에는 미사용
- 24:00 통일 거부 — 야간장 진행 중간값은 "close" 의미 부정확
- provenance: `close_basis=krx_cf_close_1545` 명시로 마감 시각 차이 honest

## 8. external_historical policy + insufficient_history (Amendment 2026-05-27)

Phase 2c 검증 후 Bithumb/KRX/Hana 모두 external_historical 확보. 기존 "actual-only state + insufficient_history empty" 분기는 **신규 자산/source 장애/coverage 부족 시점에만 적용** (완전 제거 X).

### Bithumb (external_historical 확정)

- Endpoint: `https://api.bithumb.com/public/candlestick/USDT_KRW/{interval}` (무인증)
- Coverage: 902일 (2023-12-07 KST 시작) — 자연 ~1일/일 확장
- Interval: 1m, 3m, 5m, 10m, 15m, 30m, 1h, 4h, 6h, 12h, 24h, 1w, 1mm (13개)
- Rate limit: ccxt 500ms/request (분당 120회)
- Provenance: `history_policy="external_historical"`, `actual_source="bithumb"`, `coverage_days=902`

#### close_basis = bithumb_24h_kst_close (Amendment 후속)

- **초기 backfill + 매일 append (canonical) 동일 방법**: 공식 24h candle API. source_method=`bithumb_candlestick_api` (Amendment 2026-06-01 — 구 backfill=`bithumb_candlestick_backfill`/append=`observed_rollup` 분기 폐기. candle이 실 OHLC 제공해 DB rollup보다 우월)
- KST 00:00 boundary 확인됨 (B-A 검증, `1701874800000` = 2023-12-07 00:00:00 KST)
- provenance: `close_basis=bithumb_24h_kst_close` 명시

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

**클라이언트 HTTP 캐시 정책 (서버 Redis 캐시와 별개 레이어)**: `GET /api/v2/graph/tab` 성공 응답은 **`Cache-Control: no-store`** ([main.py](app/main.py) `get_v2_graph_tab`). 라이브 데이터(1d 10min 진행봉 + `in_progress` seed, 3m/1y/1w도 매일/매시 갱신)라 클라가 HTTP 캐시하면 cold-launch 시 stale 응답을 씀 — iOS `URLSession.shared`가 cache 헤더 없는 200 GET을 휴리스틱 캐싱해 직전 세션 그래프(닫힌 봉+seed 둘 다)를 ~2-3분 내주던 문제 대응. 속도는 서버 Redis 캐시가 담당하므로 클라 캐시 불필요. iOS 클라도 그래프 fetch를 `.reloadIgnoringLocalCacheData`로 캐시 우회(방어). `/api/v2/topics/snapshot` no-store 정책과 일관. catalog은 정적(version 기반)이라 no-store 미적용.

## 12. Legacy coexistence

- legacy v1 `/api/graph/{currency}` 응답 schema 변경 없음 — 구단말 회귀 0 보장
- v2 endpoint와 hot path/cache key prefix 분리
- v1 → v2 마이그레이션은 신규 단말 release 시점 client side로 cutover
- v1 deprecation은 별도 단계 (구단말 사용자 1% 미만 시 검토)
- Redis cache key prefix 분리로 v1/v2 invalidation 독립 — 한쪽 hot path 변경이 다른 쪽 영향 없음

## 13. Rollout plan (Amendment 2026-05-27)

1. **Phase 2b** (완료, 본 문서 + ADR-033 land): 정책 anchor — design 문서 + ADR + CLAUDE.md anchor.
2. ✅ **Phase 2c** (완료, 2026-05-27): Bithumb/KRX/Hana historical source 외부 조사 — 3 source 모두 external_historical 확보. ADR-033 Amendment 2026-05-27 + 본 문서 §3/§5/§6/§7/§7-new/§8 update.
3. **Phase 2d** (Amendment 후속 — 두 목적 분리): **상세 설계는 [ADR-034](DECISIONS.md#adr-034-source_daily_rates-canonical-daily-table)** (source_daily_rates canonical daily table — schema / retention / unique key / rebuild / backfill·append jobs / timezone/date ownership / calendar-aware monitoring / rate==close invariant / rollout sequence)
   - 장기 coverage 확보 목적의 rollup: 외부 historical source 확보됨 → 우선순위 ↓
   - **Hot path 안정화 / canonical daily table 목적의 `source_daily_rates`**: 우선순위 ↑ (ADR-034 본문 참조)
   - 외부 API hot path 제거 → 그래프 요청 시 `source_daily_rates` 단일 조회
4. ✅ **Phase 2e MVP** (코드+테스트 + **운영 deploy 완료 2026-06-06**): v2 endpoint 2개 — `GET /api/v2/graph/catalog` + `GET /api/v2/graph/tab` (`app/graph_v2.py` 로직 + main.py thin wiring + endpoint harness, commits `7bc1d65`/`9d43773`/`c7c218c`). **MVP 범위 = 3m/1y만** (`source_daily_rates` 단일 조회 hot path) + DXY는 market_index_rates.daily reader. 1d=v1 realtime / 1w=source_hourly_rates(ADR-035 D3) 후속 → v2는 1d/1w **400 unsupported_period**(insufficient_history 아님 — 데이터는 v1에 있음, v1 fallback hint). **v1 `/api/graph/{currency}` 변경 0** (legacy 공존 §12). catalog 코드 상수 = MVP subset(3m/1y) — 본 문서 §4 full target(1d/1w 포함)과 비강제 분리. 18 tests(graph_v2 로직 12 + endpoint 6). 동적 provenance(single/mixed + per-point) + insufficient partial coverage. **운영 deploy 완료 2026-06-06** (HEAD 0e52b7a / image 9e2e3186; Claude+Codex 이중 독립 검증: catalog 200 / tab usd 3m 200 series[77,63,79] / 1d 400 / v1 200 / KRX status=normal / ERROR 0). 후속(별도): source_hourly_rates(1w, Phase 2e+) / 프론트 client cutover. 상세: [DECISIONS.md ADR-035](DECISIONS.md) + CLAUDE.md Phase 2e MVP anchor.
5. ✅ **Phase 2e+ — 테더 1d (10min closed-bucket precompute)** (서버 land, commit `d1ce060`): §4 line 54 테더 1d(11 series — 거래소 5 개별 + KRX + investing/KB/Hana USD + DXY + DXY_futures, 10min bucket)를 v2로 구현. **Phase 2e MVP의 "1d=400"을 테더 한정으로 supersede** (다른 탭 1d는 여전 400 + v1 hint). 1d는 daily/hourly read-through와 데이터 경로가 달라 별 모듈 `app/graph_v2_intraday.py`: raw 테이블(source_rates/bank/investing/market_index realtime) → 10min bucketize + carry-forward(graph_cache `_bucketize` 재사용) + closed-bucket only(진행 중 봉은 iOS live-tail). **cache-first precompute** — cron `*/10 +12s`가 `graph_v2:tab:tether:1d`(TTL 1200s 안전망) 갱신, 요청은 Redis read + miss/**stale-boundary** 시 process-local single-flight rebuild(`--workers 1`; multi-worker 시 Redis NX EX). **stale-boundary 온디맨드 rebuild**: payload에 `_in_progress_start_ts`(build 시점 진행봉 경계) 심어, serve가 "캐시 경계 < 현재 경계"면 경계 통과 후 precompute(:12) 전 창이라 방금 닫힌 봉이 캐시에 아직 없다고 판단 → 온디맨드 rebuild로 즉시 반영(cold-open 후 직전 완료 봉 ~12초 누락 gap 제거, 실측 확정). 클라는 `_` prefix 내부 필드 무시(전방호환). catalog는 테더 1d(11 series) 노출, 전역 supported_periods는 MVP 유지(1d=tab-specific). 11 series가 한 `now_kst`로 일관 버킷 경계(graph_cache builder에 optional now_kst 주입). 전체 3386 tests. 후속: iOS `GraphV2Period.oneDay` + live-tail / 다른 탭 1d / precompute query 배치 최적화(현 22 SELECT/run, 측정 후).
6. ✅ **FX 3탭 1d — intraday per-tab 일반화 (Slice A)** (2026-07-03) [usd 10은 ADR-038 D4 ②(2026-07-08)로 11(krx 편입) — 본 항목은 historical record]: §3:49-54 계약의 FX 1d를 서버 구현 — item 5의 "다른 탭 1d는 여전 400"을 **supersede** (이제 4탭 전부 1d 200). `graph_v2_intraday`의 테더 하드코딩을 `TAB_1D_SERIES` per-tab dict로 일반화: tether 11(불변) / **usd 10**(8 banks[Citi 제외, ADR-033 D4] + investing + dxy — dxy_futures는 테더 전용 유지) / **jpy·eur 9**(8 banks + investing — DXY 계열 미노출 §9:521). reader는 기존 `build_graph_series` 재사용(은행 allowlist 없음 — 신규 reader 0). **은행 순서/default/라벨은 후속(2026-07-03)에서 사용자 지정으로 확정** — all_series 순서 = Bank.displayCases(investing/kb/hana/shinhan/woori/ibk/nh/sc/bs[+dxy]), default_visible = investing+hana만(usd dxy·kb 기본 OFF), client 토글 라벨 = Bank.displayName + 3×3 배치. `build_tab_1d_payload(tab)`/`build_tab_1d_in_progress(tab)`/`precompute_intraday_1d()`(cron `*/10 +12s` 단일 job이 4탭 순회, 탭별 build+SET 즉시 + per-tab 실패 격리) + per-tab 캐시 키 `graph_v2:tab:{tab}:1d[,:in_progress]` + main.py per-tab single-flight lock. stale-boundary rebuild/in_progress seed/no-store 계약 전부 승계. 쿼리량 22→78 SELECT/run(시간당 468). 후속: iOS 달러 탭 v2 교체(Slice B) → jpy/eur(Slice C).

7. **Phase 2f** (Later, optional): single series endpoint 추가 — 사용 패턴 확보 후.

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

### Phase 2d 설계 결정 항목 (Amendment 후속)

- **source_daily_rates retention / unique key / rebuild policy**: Phase 2d에서 schema 확정 시점에 결정. 예상 unique key 후보 `(source, asset, date_kst)`.

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
- [ADR-034](DECISIONS.md#adr-034-source_daily_rates-canonical-daily-table): source_daily_rates canonical daily table — Phase 2d 구현 상세 설계 (schema + retention + provenance + backfill·append jobs + monitoring + rollout)
- [ADR-019](DECISIONS.md#adr-019-dxy-보조지표--granularity-기반-2-part-merge-전략): DXY granularity 2-part merge (legacy comparison only)
- [ADR-023](DECISIONS.md#adr-023-데이터-보관-정책-30일-통일-banksource_ratesdxy-realtime): 30일 cap 정책 anchor
- [app/admin/graph_cache.py](app/admin/graph_cache.py): legacy v1 graph 구현 (DXY 2-part merge 패턴)
- [app/admin/dxy_rollup.py](app/admin/dxy_rollup.py): DXY rollup 구현 (Phase 2d source_rates 적용 패턴 참조)
- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md): 서비스 계약 source of truth (topic-only / dual-emit)
