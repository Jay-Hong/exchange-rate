# KRX Step 4B — 12개월 contract-chain full backfill 설계

> **상태**: 설계 초안 (Proposed, 2026-06-03). 구현 전 외부 검토 → 구현 → dry-run → snapshot → production write.
> **anchor**: [ADR-034 §14 Rollout sequence](DECISIONS.md) Step 4 full backfill의 **4B KRX 부분** (4A Bithumb/Hana는 완료).
> **선행 문서**: [GRAPH_API_V2_CONTRACT.md §7-new](GRAPH_API_V2_CONTRACT.md) (KRX contract chain rollover boundary policy), [DECISIONS.md ADR-033 Amendment 2](DECISIONS.md) (KIS daily chain 채택).
> **대상 writer**: [scripts/backfill_kis_source_daily_rates.py](scripts/backfill_kis_source_daily_rates.py) (Step 2-3 dry-run validator + Step 3 `--write`/`--contract` 확장 land).

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

## 12. Open items (구현 전 결정)

- coverage window 시작 anchor 확정: today-1 기준 1년 vs Bithumb/Hana 정렬 고정 window.
- N-contract chain loop의 시작점: current에서 backward N회 vs window 시작 date front-month resolve.
- gap ≥N일 surface 임계값(잠정 4일) 확정.
- transaction batch: 12 contracts ≈ 240 rows 단일 transaction (작아서 가능) vs contract별 — 단일이 단순, partial state 없음.
- KRX는 daily append(Step 5) 미포함 → 4B 이후 close finalizer 통합 별도 PR에서 ongoing freshness 결정.
