# KRX Step 4B — 12개월 contract-chain full backfill 설계

> **상태**: 설계 초안 (Proposed). **⚠️ Source 전환 (2026-06-04 KRX OpenAPI POC) — §0 참조.** KIS `inquire-daily-fuopchartprice` contract-chain → **KRX OPEN API `fut_bydd_trd` date-based**. §1~§12의 chain mapping(단위 1)·invariant(§3)·overlap(§2·§6)·compare(§7) 정책은 **유효**하되, **fetch source가 KRX로 전환**된다(§0).
> **anchor**: [ADR-034 §14 Rollout sequence](DECISIONS.md) Step 4 full backfill의 **4B KRX 부분** (4A Bithumb/Hana는 완료).
> **선행 문서**: [GRAPH_API_V2_CONTRACT.md §7-new](GRAPH_API_V2_CONTRACT.md) (KRX contract chain rollover boundary policy), [DECISIONS.md ADR-033 Amendment 2](DECISIONS.md) (KIS daily chain 채택 — §0에서 source 전환으로 부분 supersede).
> **대상 writer**: [scripts/backfill_kis_source_daily_rates.py](scripts/backfill_kis_source_daily_rates.py) (단위 1·3 재사용 / 단위 2·5 재작성 / 단위 4 폐기 — §0.4).

---

## 0. Source 전환 — KRX OpenAPI date-based (2026-06-04 POC) ⭐

> 이 섹션이 §1~§12를 **부분 supersede**한다. chain mapping(단위 1)·invariant(§3)·overlap(§2·§6)·compare(§7) 정책은 **유효**하되, **fetch source가 KIS contract-chain → KRX OPEN API `fut_bydd_trd` date-based로 전환**된다.

### 0.1 배경 — KIS year-series 한계 발견

- Step 4B KIS chain dry-run(2026-06-04 production)에서 **KIS `inquire-daily-fuopchartprice`가 A755xx(2025 만기 series) 미조회** 확인 — `rt_cd=0`이나 `output2=[]`(에러 아닌 정상 빈). A756xx(2026)만 제공(~5.5개월). 시간 retention이 아니라 **year-series 한계**(A75512=2025-12도 빈 응답으로 확정).
- 12개월 coverage가 KIS 단독으로 불가 → 대안 탐색(TradingView=공식 API 부재·24h세션 / Investing=선물 부재 / data.krx.co.kr=로그인 게이트 / data.go.kr=통화선물 불명확) → **KRX 공식 OPEN API `fut_bydd_trd`(선물 일별매매정보, 주식선물外)** 확정.

### 0.2 POC 결과 anchor (2026-06-04, 정식 키 — **키 값 문서 미기재**)

- **KIS 대조 1:1 일치** (KRX `TDD_CLSPRC`(정규장 종가) == KIS close, 둘 다 KRX 원천):
  - 2026-05-18 = **1496.50** / 2026-05-27 = **1499.60** / 2026-06-02 = **1518.00** (우리 KIS A75606과 동일)
- **만기일 mapping 정합** (우리 "만기일=next" rule 유효):
  - 2025-12-15(202512 만기일): `202512`=1477.30(만기월물) + `202601`=1468.90(next) **공존** → user-facing = **next(202601)**
  - 2026-05-18(A75605 만기일): `202605`=1505.80 + `202606`=**1496.50**(next) → next가 KIS와 일치
- **2025 구간 조회 가능성 실증** (전수 아님, **샘플 3날**): 2025-06-02(`202506`=1370.80) / 2025-09-15(`202509`=1390.00) / 2025-12-15(`202512`=1477.30) — KIS 공백(A755xx) 메움.
- **range**: 서비스 명세상 2010-01-04~ 제공. 12M 이용신청으로 최근 12개월 조회 확인.

### 0.3 Source 전환 결정

- **Primary source**: KRX OPEN API `fut_bydd_trd` (endpoint `https://data-dbg.krx.co.kr/svc/apis/drv/fut_bydd_trd?basDd=YYYYMMDD`, 헤더 `AUTH_KEY`, http→https redirect).
- **Fetch unit**: contract-range(KIS) → **`basDd` 날짜별**(그날 전 상장 선물 ~385 rows).
- **Contract selection**: 기존 `_resolve_front_month`/`build_contract_sequence` mapping rule **재사용**(그날 front-month `contract_month` 결정).
- **Row filter**: `PROD_NM=="미국달러 선물"` ∧ `MKT_NM=="정규"` ∧ `ISU_NM.startswith("미국달러 F ")`(**SP 스프레드 제외**) ∧ `"(주간)" in ISU_NM` ∧ **selected front-month YYYYMM 일치**(KRX는 `contract_month`로 join, KIS 단축코드 아님) ∧ `TDD_CLSPRC != ""`.
- **Price**: `TDD_CLSPRC`(정규장 종가) primary, `SETL_PRC`(정산가)는 metadata/audit.
- **KIS**: daily backfill에서 **verification/fallback**으로 격하(realtime broadcast는 유지).

### 0.4 재사용 / 재작성 / 폐기

- ✅ **재사용**: `_resolve_front_month`·`build_contract_sequence`(단위 1) / `compare_manifest_with_existing`(단위 3) / existing seed 충돌·오염 검증.
- 🔨 **재작성**: `build_manifest`(단위 2 — KRX `basDd` date-based fetch + row filter) / `run_range_dry_run`(단위 5 — fetch source + coverage logic) / **coverage gate**(KRX 날짜 기반 `MANIFEST_DATE_MIN/MAX` + missing trading days + selected contract/month — KIS dry-run의 **"window 시작부 통째 누락" 사각지대 해소**: `MANIFEST_DATE_MIN`이 window_start와 과도하게 벌어지면 hard).
- ❌ **폐기**: KIS 100-row cap gate(단위 4 — KRX date-based엔 무관) / KIS A755xx year-series 한계 대응.

### 0.5 Open (전환 후 결정)

- `fut_bydd_trd` 기간 파라미터(`beginBasDt`/`endBasDt`?) 유무 — 거래일별 ~245 calls vs 기간 1 call (fetch 효율). POC는 단일 `basDd`만 사용.
- KRX rate limit(10,000/일) vs throttle 필요성.
- **secret 재발급**: POC 키는 대화에 노출됨 → **운영 전 재발급**, 운영 `.env` `KRX_OPENAPI_KEY`(문서·메모리 미기재).
- **front-month 빈 종가 정책**: POC 샘플 3날에선 selected front-month `TDD_CLSPRC` non-empty(원월물만 `''`)였으나 **전수 미검증** → 구현에서 **selected front-month row가 없거나 `TDD_CLSPRC == ""`이면 coverage hard로 처리**(silent 통과 방지, §0.4 coverage gate 정신).

---

## 1. 배경 / 목적

- production `source_daily_rates` 현황: Bithumb 1년 coverage / Hana 1년 coverage / **KRX는 Step 3 chain 일부만** (옵션 B current+previous, write 직전 read-only 재확인 대상 — 회상값 단정 금지).
- KRX는 3 source 중 **유일하게 장기 coverage 없음** → Tether 탭 3m/1y 그래프의 critical path.
- 목표: coverage window(예: today-1 기준 1년, 또는 Bithumb/Hana와 정렬된 고정 과거 window) 안의 **모든 KRX 거래일**을 source_daily_rates에 적재.

## 2. 4A와의 결정적 차이 — overlap

4A(Bithumb/Hana)는 **빈 영역 gap-only**(target overlap 0, `--require-empty-target`)였으나, **KRX는 Step 3에서 이미 chain 일부를 적재**해 12개월 window와 **교집합이 불가피**. 따라서 4A 가드를 그대로 못 쓰고 별도 overlap policy 필요(§6).

## 3. 최상위 invariant

1. coverage window 안 **모든 KRX 거래일은 정확히 1 contract segment에 속함** — `(date_kst → contract_code)`가 함수.
2. **만기일 당일 = next contract** (GRAPH §7-new + Step 3 실측 A75606 2026-05-18 close=1496.5).
3. **manifest date_kst 중복 0**, gap은 KRX 비거래일만 허용 — **manifest 빌드 시점 fail-close** (DB unique key는 최후 방어, 1차 방어 아님).
4. **write 대상 rows의 overlap 0** — 기존 row는 read-only 비교 후 일치하면 write 대상 제외, 충돌하면 fail-close. (full window DB overlap은 기존 seed로 인해 0 아님 — 4A `--require-empty-target`과 다른 점.)

> **핵심 리스크**: upsert가 `ON CONFLICT (source, asset, date_kst) DO UPDATE`라, partition 오류(off-by-one rollover)가 **충돌 에러가 아니라 silent overwrite**로 나타남. 검증은 DB가 아니라 manifest 빌드 단계여야 함(Crash Early).

## 4. load-bearing gate (구현 전 검토 = manifest partition)

1. contract sequence가 coverage window를 **덮는지**.
2. segment partition이 **겹침 없이 이어지는지**.
3. fetched rows가 **자기 segment 밖으로 안 새는지**.
4. manifest **date_kst duplicate 0**.
5. **boundary rule**: 만기일 = next contract.
6. **DB target overlap policy** 확정 (§6).

외부 reference(공휴일 calendar / Bithumb)는 **load-bearing 아님** → surface 보조만 (§8).

## 5. segment 정의 — 반열린 partition

- segment = `[C_{i-1}.expiry_date, C_i.expiry_date)` (반열린).
- `C_i.expiry_date`는 **다음 구간의 시작**으로 정확히 한 번만 포함 → off-by-one(만기일 중복/누락)이 구간 정의 수준에서 차단. invariant 2(boundary=next)와 자기정합.
- 기존 helper: [_build_previous_dynamic](scripts/backfill_kis_source_daily_rates.py#L115)(current→previous), [_build_before_previous_dynamic](scripts/backfill_kis_source_daily_rates.py#L145)(previous→before_previous) **2단계** → **N단계 chain loop로 일반화** 필요(§9).

## 6. overlap policy — (a) gap-only + 충돌 fail-close + partial target

**결정: (a) gap-only.** (b) idempotent full re-upsert는 배제 (refresh가 필요할 때 별도 정책).

근거:
- (a)에서 기존 dates를 write 대상에서 **제외**하므로 **write 안전이 비교 로직에 의존하지 않음**. 비교는 "manifest 생성 로직 검증"용 bonus.
- (b)는 비교 로직이 write 안전의 **단일 실패점**(비교 누락 시 silent overwrite).
- 기존 KRX seed는 Step 3 production verify 통과한 **ground truth** — 4B는 coverage 확장이지 seed refresh 아님.

**절차 (Stage 1)**:
1. full 12개월 KRX manifest 생성.
2. manifest 자체 검증 (§4 gate: partition / dup 0 / boundary=next / segment 밖 row 0).
3. production 기존 KRX rows **read-only 조회**.
4. 기존 row가 manifest와 **충돌하면 fail-close** (기존 = ground truth → 신규 manifest 생성 로직 버그 의심 → write 중단 + surface. gap을 안 덮는 것과 **별개로** 충돌 자체가 abort 신호 — Crash Early).
5. 기존 row와 동일한 manifest row는 write 대상에서 **제외**.
6. 신규 gap rows만 write — **partial target 허용**(기존 존재 OK + 기존 제외), 4A `--require-empty-target`과 다른 모드.
7. post-write = **full manifest coverage 완성** 검증 (기존 ∪ 신규 = full window, "내가 쓴 것"만이 아님).

## 7. 기존 row vs manifest 비교 키

| 비교 **포함** | 비교 **제외** |
|---------------|---------------|
| date_kst, contract_code | metadata_json.**fetched_at_utc** (매 fetch마다 다름 — 포함 시 모든 overlap이 false 충돌 → fail-close 폭발) |
| close, rate (==close), high, low | metadata_json.mod_yn, acml_vol (준안정 → audit surface만) |
| close_basis, source_method, ohlc_quality | |
| basis_date is None, published_at is None | |
| metadata_json **stable subset**: contract_short_code, contract_month, contract_expiry_date, open | |

> 비교 목적은 "같은 contract·OHLC·provenance인가"(mapping 정확성)이지 audit timestamp 일치가 아님. `fetched_at_utc` 제외는 **load-bearing** ([row mapping](scripts/backfill_kis_source_daily_rates.py#L295)).
>
> **Decimal numeric compare (load-bearing)**: close/rate/high/low/open은 **문자열 비교 금지** — DB는 Numeric(14,6)로 `1496.500000`, manifest는 `Decimal("1496.5")`라 문자열 비교 시 false 충돌 폭발. 특히 `open`은 [line 299](scripts/backfill_kis_source_daily_rates.py#L299)에서 `str(open_dec)` 저장이라 KIS 응답 포맷차("1496.5" vs "1496.50")로 더 위험 → Decimal로 parse 후 numeric compare.
>
> **stable subset 키 누락 정책 (확정)**: 기존 seed에 `open` 누락 시 **surface**(핵심 키 date_kst/contract_code/close로 비교 유지). `contract_short_code` / `contract_month` / `contract_expiry_date` 누락 시 **fail-close** (기존 seed 구조가 Step 3 적재와 다름 = 비정상 — Crash Early). `mod_yn`/`acml_vol`/`fetched_at_utc`는 애초 비교 제외라 누락 정책 무관.

## 8. KIS authoritative trading-day calendar

- **별도 공휴일 calendar 만들지 않음** — KRX 증시 거래일은 은행 영업일과 다름(근로자의날·연말 폐장 등). `holidays.SouthKorea`(Hana용) 재사용 시 drift.
- **KIS 응답 날짜 = authoritative *observed* trading-day set** (별도 calendar 안 만들어 구성 drift 회피). 단 응답이 **모든 거래일을 누락 없이 반환했음을 증명하진 않음** → completeness(KIS 결손 vs 진짜 휴장)는 아래 gap anomaly surface로 **보조 검증**.
- **gap 판정은 외부 reference 의존 금지** → KIS 응답 **내적 일관성**으로:
  - contract별 fetched rows가 요청 usage segment 안에만 (segment 밖 누수 0).
  - 전체 manifest date strictly increasing.
  - duplicate date_kst 0.
  - 인접 row gap ≥4일 → **surface/warning** (연휴는 정상 4일+ gap이라 hard-block 금지).
  - 만기 boundary 주변 `expiry-1 / expiry / expiry+1` 샘플 명시 출력·검증.
- **Bithumb cross-check**: 365 연속이라 KRX 휴장일에도 row 존재 → false positive 多 → **load-bearing 배제, coarse 결손(연속 빔) surface 보조만**.

## 9. 구현 scope

| 분류 | 항목 |
|------|------|
| ✅ **N-general 재사용** | [parse_daily_rows](scripts/backfill_kis_source_daily_rates.py#L263), [validate_duplicates_mapped](scripts/backfill_kis_source_daily_rates.py#L472)(Counter 기반 — mapped_rows 전체 순회, contract 개수 무관), [validate_cap_warning](scripts/backfill_kis_source_daily_rates.py#L494), 13 validation suite, dry-run/`--write`/`--allow-production-write` guard, write_with_transaction |
| 🔨 **신규 구현** | N-contract chain loop (before_previous 너머 N단계), 각 contract usage segment mapping, full manifest 조립, gap-only **partial target** overlap, 기존 row 충돌 비교(stable subset), post-write **full coverage** 검증 |
| ⚠️ **severity 격상** | `validate_cap_warning` → **write-intended path**(`--range-dry-run` 게이트 + production write)에서 **fail-close** (usage segment ≈ 20거래일이라 100 도달 = 설계/endpoint 이상 신호). **하나라도 도달 시 전체 abort**(manifest 신뢰 불가). 순수 탐색 dry-run(Step 2-3 probe)은 warning 유지 |
| 📝 **docstring 갱신** | [line 5/9/12/34/41](scripts/backfill_kis_source_daily_rates.py#L5) ("Step 2-3 dry-run only / DB write X / 3 contracts" — Step 3 `--write`/`--contract` 미반영 stale), [line 475](scripts/backfill_kis_source_daily_rates.py#L475) ("previous + current" → "all mapped segments (N-contract)") |

## 10. rollout sequence

1. **설계 검토** (외부 검토 — 본 문서).
2. **구현** (별도 수정 GO): N-contract chain loop + manifest + overlap + cap fail-close + docstring.
3. **dry-run** (`--range-dry-run` 류): manifest partition gate 전수 통과 + boundary 샘플 + expected (date_kst, contract_code) manifest 확정.
4. **production 기존 KRX state read-only 재확인** (회상값 아님 — write 직전 fact 확인).
5. **RDS manual snapshot** (4A 패턴 — `fxi-pre-step4b-...`).
6. **production write** (gap-only, partial target) → post-write full coverage 검증 + dual verification (Claude + Codex).

## 11. rollback / read-only 원칙

- **rollback shelf-life** (4A 패턴): write+post-verify로 작업 창 닫힘 → `delete_range`/inserted_dates IN delete는 **동일 작업 창 내 즉시 rollback**에서만 안전. 종료 후 = snapshot 복구 또는 재검증. snapshot = 명시 삭제 전까지 durable recovery anchor (restore = 별도 DB 인스턴스).
- KRX는 기존 seed 존재 → rollback은 **신규 inserted dates만** 대상 (기존 25 seed 보존).
- **read-only state 재확인**: KRX row 수/날짜는 Step 3 시점 값이지 실행 fact 아님 — write 직전 production read-only로 재확인 후 overlap policy·rollback anchor 확정.

## 12. 결정 / Open items

### Resolved — item 1·2 (manifest 기반, 2026-06-03 Codex 합의)

**item 1 — coverage window**:
- today-1 기준 **rolling 1년으로 산정**, 실행 시 계산된 `start_date`/`end_date`를 **manifest/log에 고정 기록** (재현성 + idempotent — "동적 window"로 구현 금지).

**item 2 — chain loop / boundary**:
- contract sequence = **window_start가 속한 front-month resolve 후 forward 생성** (종료: `segment ∩ [window_start, window_end] ≠ ∅`).
- **window_start == expiry_date → next contract** resolve (invariant 2 일관 — 누락 시 첫 segment 경계 오류).
- **segment = calendar date 기준**(만기일 = 셋째 월요일 **+ 휴장 시 직전 영업일로 보정**, 그 외 거래일 무관) / **실제 rows = KIS 응답 trading-day set 기준** — 2-layer 분리.
  - 2026-08-16: `_compute_expiry_date`에 휴장 보정이 들어가면서 segment 경계도 함께 이동했다.
    영향 날짜는 2026-02-13, 2026-08-14 두 건(주중 기준)이며, 달력 날짜 기준으로는
    2/13~15 · 8/14~16 총 6일의 계약 배정이 바뀐다.
- **window_start 또는 window_end가 휴장일이면** 첫/마지막 row가 경계일과 불일치 가능 — **정상** (gap 아님).
- **manifest expected dates = KIS trading-day set ∩ [window_start, window_end]**.

### Open — 구현 중 / 별도 결정

- **item 3** (구현 중 결정): gap ≥N일 surface 임계값 (잠정 4일) 확정.
- **item 4** (구현 중 결정): transaction batch — 12 contracts ≈ 240 rows 단일 transaction (작아서 가능) vs contract별. 단일이 단순, partial state 없음.
- **item 5** (4B write와 **분리**, **Phase 2e 전 별도 결정 필수**): KRX daily append(Step 5) 통합. 4B production write를 막진 않으나, Phase 2e v2 endpoint가 source_daily_rates를 1y 그래프에 쓰면 **KRX가 4B 실행일 이후 멈춰 그래프 끝 stale** → freshness 운영 정책 영향. close finalizer(CF/CM 종가, 이미 `source_rates` 기록 중)를 source_daily_rates로 잇는 경로 재사용 가능 — 별도 PR.
