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
- provenance schema (actual + backfill chain + history policy)
- Hana backfill merge rule
- Bithumb/KRX insufficient_history 정책
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
| USD | 8 banks (Citi 제외) + investing + DXY | investing + Hana (30d actual + Investing backfill) + DXY hourly | investing + Hana (30d actual + Investing backfill) + DXY daily |
| JPY | 8 banks (Citi 제외) + investing | investing + Hana (30d actual + Investing backfill) | investing + Hana (30d actual + Investing backfill) |
| EUR | 8 banks (Citi 제외) + investing | investing + Hana (30d actual + Investing backfill) | investing + Hana (30d actual + Investing backfill) |
| Tether | 5 exchanges (Upbit/Bithumb/Coinone/Korbit/Gopax) + KRX + investing USD + KB USD + Hana USD + DXY + DXY_futures | Bithumb (대표) + KRX + investing USD + Hana (30d actual + Investing backfill) + DXY hourly | Bithumb (대표, insufficient_history dynamic) + KRX (insufficient_history dynamic) + investing USD + Hana (30d actual + Investing backfill) + DXY daily |

**제약**:
- Bithumb/KRX 3m/1y는 외부 historical source 또는 daily rollup 도입 전까지 `insufficient_history=true`로 empty
- DXY_futures는 1d only (1w/3m/1y catalog 미노출)
- Citi는 모든 tab/period에서 catalog 미노출 (수집 layer 유지)
- 은행은 1d catalog 8개 (Hana 외 7개는 backfill 없음), 1w+ catalog는 Hana만 (다른 은행 미노출)

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

각 series는 catalog 안에 다음 metadata를 가진다:

```json
{
  "id": "hana.usd",
  "label": "하나은행",
  "axis_group": "krw",
  "unit": "KRW",
  "decimals": 2,
  "color_hint": "#...",
  "provenance": {
    "actual_source": "hana",
    "fallback_source": "investing",
    "fallback_after_days": 30,
    "history_policy": "backfill_chain",
    "coverage_days": 443,
    "insufficient_history": false
  }
}
```

## 6. Provenance / fallback schema

`provenance` 객체 필드:

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `actual_source` | string | 본 series의 primary source (예: `"hana"`) |
| `fallback_source` | string \| null | backfill source (예: `"investing"`). null이면 backfill 없음 |
| `fallback_after_days` | int \| null | actual_source coverage 종료 후 fallback 사용 시점 (예: 30) |
| `history_policy` | enum | `"actual_only"` / `"backfill_chain"` / `"rollup_based"` / `"external_historical"` |
| `coverage_days` | int | 현재 series 전체 최대 coverage (actual + fallback 합산) |
| `insufficient_history` | bool | 요청한 period를 채울 수 없으면 true (response data는 empty array) |

`history_policy` enum 의미:
- `actual_only`: backfill 없이 actual_source만 (예: Bithumb 1d only state)
- `backfill_chain`: actual_source + fallback_source (예: Hana 30d + Investing backfill)
- `rollup_based`: daily rollup으로 자체 축적 (Phase 2d 이후 USDT/KRX 후보)
- `external_historical`: 외부 source 통합 (Phase 2c 이후 USDT/KRX 후보)

`supports_periods` dynamic은 catalog 응답 시점 `history_policy + coverage_days` 조합으로 결정:
- retained source_rates only (max 30d): `actual_only` + coverage_days=30 → 3m/1y는 insufficient_history=true
- daily rollup based: `rollup_based` + coverage_days가 시간 흐름에 따라 증가 → period 자연 확장
- external historical based: `external_historical` + coverage_days = 외부 source 제공 범위 → 즉시 확장

## 7. Hana backfill merge rule

Hana 장기 그래프 (1w/3m/1y) merge 규칙:

1. **cutoff_ts**: 응답 시점 기준 `now - 30 days` (Hana actual coverage 종료 경계)
2. **Hana actual range**: `[cutoff_ts, now]` (closed interval — cutoff 시점 포함)
3. **Investing backfill range**: `[period_start, cutoff_ts)` (right-open interval — cutoff 시점은 Hana actual 우선)
4. **bucket dedup**: 같은 bucket timestamp가 양쪽에 있으면 Hana actual 우선 (실제로는 cutoff에서 정확히 분기되므로 충돌 거의 없음)
5. **gap 처리**: Hana actual range 안에 gap이 있으면 Investing으로 메우지 않음 (Hana actual 구간 안에서는 actual만 표시, 빈 구간 그대로)
6. **point-level provenance 표시**: response data 각 point 별 `source` 필드 포함 (`"hana"` / `"investing"`) — 클라이언트가 선택적으로 색 분기 가능

## 8. Bithumb/KRX actual-only and insufficient-history policy

**actual-only state** (현재):
- Bithumb (`source_rates.bithumb.usdt-krw`): 30일 cap (`SOURCE_RATE_RETENTION_DAYS`)
- KRX (`source_rates.krx.usd-krw-futures`): 30일 cap

**1d**: actual data 정상 제공 (`history_policy="actual_only"`, `coverage_days=30`, `insufficient_history=false`)

**1w**: actual 30d로 7일 채움 (`coverage_days=30` 충분, `insufficient_history=false`)

**3m/1y**: actual 30d로 부족 (3m=90d, 1y=365d). `insufficient_history=true` + `data=[]`:

```json
{
  "series_id": "bithumb.usdt-krw",
  "data": [],
  "provenance": {
    "actual_source": "bithumb",
    "fallback_source": null,
    "fallback_after_days": null,
    "history_policy": "actual_only",
    "coverage_days": 30,
    "insufficient_history": true
  }
}
```

**클라이언트 UI 분기**: `insufficient_history=true` 시 해당 series 위치에 "데이터 부족" empty state 표시. response status는 **현재 권고 200 OK + 빈 data array** — 최종 status code (200 vs 422)는 §14 Open questions 6번에서 client UX 검증 후 확정 (Phase 2e 구현 PR 진입 전).

**금지 사항** ([ADR-033](DECISIONS.md) Decision 6번):
- Bithumb USDT/KRW에 Investing USD/KRW를 backfill로 채우면 안 됨 (crypto microstructure ≠ interbank 환율)
- KRX 미국달러선물에 Investing USD/KRW를 backfill로 채우면 안 됨 (futures instrument ≠ interbank 환율)

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

## 13. Rollout plan

1. **Phase 2b** (본 문서 + ADR-033 land): 정책 anchor — design 문서 + ADR + CLAUDE.md anchor.
2. **Phase 2c** (별도 트랙, 병행 가능): Bithumb/KRX historical source 외부 조사 — 발굴 성공 시 catalog provenance `history_policy=external_historical` 활성화.
3. **Phase 2d**: daily rollup 구현 — DXY hourly/daily rollup 패턴 ([app/admin/dxy_rollup.py](app/admin/dxy_rollup.py))을 source_rates에 적용. retention 결정은 별도 ADR.
4. **Phase 2e**: v2 endpoint 구현 PR — catalog + tab graph 2개 endpoint, 신규 단말 client 통합.
5. **Phase 2f** (Later, optional): single series endpoint 추가 — 사용 패턴 확보 후.

## 14. Open questions

- **Bithumb USDT historical source**: 거래소 공식 API 또는 CSV export 존재 여부? (Phase 2c 조사)
- **KRX 미국달러선물 historical source**: KRX 공식 / KIS API / 공공데이터포털에서 일별 종가 다운로드 경로? (Phase 2c 조사)
- **daily rollup retention**: 영구 / 5년 / 2년 중 storage 비용 + 사용 패턴 기반 결정 (별도 ADR)
- **v2 catalog version 표시 방식**: ETag 기반 revalidate vs version field 기반 client polling — UX/네트워크 비용 trade-off
- **클라이언트 cache TTL 정책**: catalog 1시간 TTL은 적절한가? 자산 추가 변경 시점 빠른 반영 필요한가?
- **insufficient_history 응답 status code**: 200 + empty data (현재 권장) vs 422 — 클라이언트 UX 분기 결정 영향
- **bucket_size 재정의 여부**: 1d=10min / 1w=1h / 3m/1y=1d는 legacy v1 패턴 그대로인지, v2에서 재정의할지 (예: 1y는 1주 bucket?)
- **default_visible_series 정책**: client 별 cutomization 허용할지, 서버 default로 잠글지

---

## 관련 문서

- [ADR-033](DECISIONS.md#adr-033-graph-api-v2-catalog-policy--legacy-공존--hana-backfill--bithumbkrx-actual-only--dxy_futures-1d-only): 본 문서의 정책 anchor ADR
- [ADR-019](DECISIONS.md#adr-019-dxy-보조지표--granularity-기반-2-part-merge-전략): DXY granularity 2-part merge — Hana backfill 패턴 참조
- [ADR-023](DECISIONS.md#adr-023-데이터-보관-정책-30일-통일-banksource_ratesdxy-realtime): 30일 cap 정책 anchor
- [app/admin/graph_cache.py](app/admin/graph_cache.py): legacy v1 graph 구현 (DXY 2-part merge 패턴)
- [app/admin/dxy_rollup.py](app/admin/dxy_rollup.py): DXY rollup 구현 (Phase 2d source_rates 적용 패턴 참조)
- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md): 서비스 계약 source of truth (topic-only / dual-emit)
