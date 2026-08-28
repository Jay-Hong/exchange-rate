# PR D 복구 경로 — 비교 spec + 결정 (working doc)

> ⚠️ **상태**: **결정됨 (2026-06-16) — D(success-watermark reconciliation) 채택. §12 참조.** 본문 §1~11은 결정에 이른 중립 비교 분석(G-기준 R1~R3 / 옵션 A~D / best-effort R2). 구현 spec·코드는 후속.
> **목적**: PR D(legacy fx hook 제거) 후 SET/dispatch/publish 실패 복구 설계 옵션을 **중립 비교**.
> **산출 경위**: Claude + Codex 다회 검토 수렴(2026-06-15~16). 도중 폐기된 선결론은 §9 참조.
> **선행물**: Step 1 characterization = [tests/test_pr_d_set_failure_characterization.py](tests/test_pr_d_set_failure_characterization.py) (현 복구 동작 박제, `a1b17a4`).

---

## 1. 배경 / 현 구조 (코드 기준)

- **legacy fx hook**: [main.py:754](app/main.py#L754) `safe_publish_all_fx_snapshots(db)` — broadcast `is_changed` 블록 안에서 **3 fx 토픽 전체** 재발행. 캐시 SET([main.py:733](app/main.py#L733))이 hook **이전**.
- **C1 direct**: SET 성공분만 trigger → `safe_publish_fx_snapshot(asset)` (per-asset). funnel = `topic_trigger_bridge.schedule_on_loop(_run_topic_emission)` ([crud.py:321](app/crud.py#L321)) → `request_fx_topic_trigger`.
- **mirror**: 3s interval, [`_mirror_all_latest`](app/latest_rates_cache.py) DB→Redis **unconditional `_set_latest`**, trigger 없음.
- **broadcast**: normal 모드 실질 10s ([scheduler.py:1471](app/scheduler.py#L1471) `second='*'` + mode/second early-return).
- **fx builder**: [`load_and_build_fx_topic_payload`](app/fx_topic_payload.py) per-source Redis-first, `is_stale`(시간만, 6s) 시에만 DB fallback ([:211-222](app/fx_topic_payload.py#L211)).
- **subscribe**: [`topic_dispatcher`](app/topic_dispatcher.py) `register`는 **registry 등록만**, 즉시 snapshot 미전송. 실패 send ws는 **즉시 registry 제거**([:136-137](app/topic_dispatcher.py#L136)).
- **DB 최신 정렬**: `(timestamp DESC, id DESC)` ([crud.py:201/508/544](app/crud.py#L201), 주석 "동일 초 중복 방지").

---

## 2. 서버측 failure mode (controllable)

| # | mode | 설명 |
|---|---|---|
| **1** | SET-failure | direct SET 실패 → Redis≠DB (Redis stale) |
| **2** | dispatch-miss | SET 성공했으나 publisher **미진입** (schedule_on_loop 실패 등) → Redis==DB지만 미발행 |
| **3** | server publish-failure | publisher 진입 후 예외 or `all_send_failed` (subscriber-0·FF 차단 **제외**) |

## 3. 별도 축 (WS 한계 — 옵션으로 보장 불가/부분)

- **client 전달**: `≥1 send 성공` vs `전 연결 전달` — outbox로도 ≥1 초과 보장 불가.
- **subscriber-0 정책**: 완료로 볼지 재시도로 볼지 (미확정, per-option).
- **재접속 / subscribe-time initial snapshot**: 현재 subscribe=registry 등록만이라 신규 구독자는 다음 발행까지 snapshot 없음. **pre-existing gap** (PR D 이전부터, 별도 계약).

---

## 4. 현재 legacy hook baseline (정확 기술)

- **어떤 broad payload change든 3 fx 전부 재시도** (broad — USD/JPY/DXY 등 무엇이 바뀌든 EUR 포함 재발행).
- **조건부**: ① mirror 복구 **이후**의 ② 다음 broad change + ③ 구독자 존재 시에만 교정 기회. (SET 실패 후 mirror 복구 전 broad publish는 여전히 옛 값 발행.)
- **dedicated retry 없음**: 캐시 SET이 hook보다 먼저([main.py:733](app/main.py#L733)<[754](app/main.py#L754))라 hook이 실패해도 같은 change로 재호출 안 됨. publish 실패는 "**다음 broad change**가 재시도 기회"일 뿐 보장 아님.

→ **parity 기준선 = mode 1·2·3을 "다음 broad change + 구독자 존재 시" 재시도** (절대 보장 아님).

---

## 5. 공통 base (전 옵션 전제 — ⚠️ write-pipeline 계약 변경 포함)

### 5.1 canonical revision `(timestamp, id)`
- DB 최신 정렬과 동일 total order 필요. **timestamp-only 불충분** (same-timestamp 다른 rate 변경을 equal로 오판 → reconcile 누락).
- **id 확보 = write-pipeline 계약 변경**: `ChangedRate`([crud.py](app/crud.py) dataclass: `source/asset/rate/changed_at` — **id 없음**) + staging은 commit **전** DTO 생성이라 신규 row id 미확보.
  - **default 후보**: flush-후 ORM row id 수집 / commit-후 재조회
  - **research(동등성 증명 전)**: 별도 revision 생성 — DB 정렬 순서 동등 + row 연결 증명 전까지 canonical 아님.
- **2 레벨 구분 (must — 옵션 C/D 상태 모델 좌우)**:
  - **per-source-key revision** `(timestamp, id)` — §5.2 monotonic write용 (key 단위 advance/equal/older 판정).
  - **per-asset watermark identity** — 옵션 C/D watermark는 `fx:<asset>` snapshot 전체(9은행+investing)를 식별. 단일 scalar로 부분 변경(kb advance, hana 불변) 표현 불가 → **revision vector** `{source: (timestamp,id)}` 또는 canonical hash.
  - **effective revision (must)** — vector는 DB-read revision이 아니라 **실제 Redis 발행 payload의 effective revision**으로 구성 (monotonic-write 결과가 effective_revision 반환):
    - `advance`/`refreshed_equal` → incoming revision
    - `skipped_newer` → **현재 Redis revision** (DB보다 최신 — DB를 넣으면 발행 payload와 불일치)
    - `failed`/`conflict` → vector 생성 중단 (그 asset 미발행)
  - **keyset 변화 — 2종 분리 (must)**:
    - **membership 변경**(정책상 source 추가/제거) → 명시적 **membership revision** 변경 (정당한 새 composite, 발행).
    - **transient 누락**(DB 조회 실패/일시 부재) → **incomplete asset → 발행 차단 + 다음 cycle 재시도**.
    - 기준 source 집합 = `source_registry` 또는 별도 고정 계약으로 명시 (둘 구분의 근거).
  - **watermark 기록 timing (C/D, must)**:
    - **attempt watermark (C)**: build 완료 후 **send 직전** `built_revision` 기록.
    - **success watermark (D)**: send 후 **`sent_count > 0` 확인 후** `built_revision` 기록 (success는 성공 전 기록 불가).
    - build 예외는 둘 다 미기록 → 재시도. C는 send 시작 후 실패가 attempt 기록 완료라 미복구 / D는 sent_count>0 전까지 미기록이라 재시도.
  - **builder 계약 변경 (must — C/D 선택 시 비용, 공통 base 아님)**: 현재 payload entry는 `{source,asset,rate,timestamp}`로 **id 없음** → built_revision 산출 불가. (A/B는 watermark 없어 불필요.) builder가 payload와 함께 **per-source effective revision vector 반환**(또는 internal build result에 payload+built_revision 포함) 필요. **public WS payload revision 노출 여부는 별도 결정**(기본: 내부 전용).
  - **target ≠ built race (정상)**: `target_revision`(reconciliation 요청) 이후 Redis 변경으로 builder가 더 최신을 읽으면 `built_revision > target_revision` — **정상 race**. 실제 전송한 `built_revision`으로 watermark 갱신, 더 최신 revision은 다음 reconciliation에서 재판단. **불일치 자체를 §5.2 `conflict`로 처리 금지** (conflict는 same-revision-different-rate 한정).

### 5.2 monotonic write — 5 state (direct SET + mirror **공유** primitive, atomic)
| state | 조건 | 동작 | trigger |
|---|---|---|---|
| `advance` | incoming revision > Redis | rate/ts/mirrored_at write | 후보 |
| `refreshed_equal` | 같은 revision + 같은 rate | **mirrored_at만 refresh** (freshness 유지) | 아니오 |
| `conflict` | 같은 revision + **다른 rate** | failed 처리 + **차단** + surface (invariant 위반) | 아니오 |
| `skipped_newer` | Redis revision > incoming | skip (역행 방지) | 아니오 |
| `failed` | read/parse/write 오류 | — | 아니오 |
- **atomic (Lua/WATCH)** — MGET→write 사이 + 늦은 direct writer의 TOCTOU race 방지. (현재 direct SET·mirror 둘 다 unconditional → race 실재.)
- `refreshed_equal`에서 mirrored_at refresh 필수 — skip하면 6s 뒤 `is_stale` → DB fallback (freshness 붕괴).

### 5.3 per-asset 완결 (per-key-latest 합성)
- asset trigger 가능 = 그 cycle DB snapshot의 **전 구성 key**가 {`refreshed_equal` / `advance` write 성공 / `skipped_newer`} 중 하나.
- 하나라도 `failed`/`conflict`/read·parse 실패면 **그 asset 보류**(pending/다음 cycle).
- DB snapshot은 investing+은행 **다중 쿼리(비원자)** → "per-key-latest 합성"으로 정의 (transaction snapshot 아님).

### 5.4 publisher raw outcome 계약 (enum + 부가정보)
> 현재 bool/count 반환은 아래를 뭉침 → **richer outcome 구현 필요**. 완료/재시도 매핑은 **per-option** (여기선 분류만).
>
> **✅ C6-4 land (dormant, behavior-change-0, 2026-06-19)**: richer outcome building block 구현됨 (live wiring은 C6-6 pending) — `topic_dispatcher.publish_topic_detailed` → `TopicSendCounts(attempted, sent, enabled)`로 bare int=0의 `no_subscribers`/`all_send_failed`/`disabled` conflate를 분리(attempted>0 & sent==0 ⟹ all_send_failed) + island pure mapper `atomic_fx_publisher.send_counts_to_send_result`(count→`SendResult` 4-way: SENT/ALL_FAILED/NO_SUBSCRIBERS). live `publish_topic`은 byte-identical 유지(parity test 잠금, delegation은 C7). `disabled`는 `SendDisposition`에 멤버 없어 mapper가 `NO_SUBSCRIBERS`로 collapse(FX는 coordinator step② FF upstream gate라 publisher 미도달) — `enabled` flag는 primitive에 truthful 보존(telemetry). `exception`은 mapper가 생성 안 함 — raise origin 전용(coordinator `SEND_EXCEPTION` funnel).

| outcome | 부가정보 | 비고 |
|---|---|---|
| `sent` | `sent_count`, `subscriber_count_at_start` | partial vs full 구분 (완료 기준 per-option) |
| `no_subscribers` | — | **관측 시점 결과, 영구 완료 아님** |
| `disabled` | **두 flag 상태 모두** (`fx_topic_enabled`, `topic_dispatcher_enabled` — 동시 off 가능) | 단일 enum 불가 |
| `all_send_failed` | — | 아래 전이 참조 |
| `exception` | stage: `build` / `publish` / 기타 | — |

### 5.5 outcome 상태 전이
- `all_send_failed` → 실패 ws **evict**([topic_dispatcher:155](app/topic_dispatcher.py#L155)) → **다음 시도 `no_subscribers`**.
  - **C6-4**: `publish_topic_detailed`는 이 evict 전이를 `publish_topic`과 동일하게 보존 — parity test가 return값 + eviction side-effect 둘 다 잠금(all_failed→evict→다음 호출 no_subscribers).
- subscriber 재등장 → 재발행 방법 정의 필요 (→ subscribe-time snapshot 연결). **(C6-4 범위 밖, C6/B3 pending)**
- `disabled` → 재시도 주기 + 재활성화 후 복구 방식. **(C6-4 범위 밖, C6/B3 pending)**

### 5.6 FX-only scope
- `request_fx_topic_trigger`만. **tether cross-route는 PR E 별 gate** (usd-krw reconciliation에서 호출 금지 — PR D/E 혼선 방지).

---

## 6. guarantee level (axis — 옵션과 별개; 첫 결정)

> guarantee level은 **목표 축**이고, §7 옵션 A~D가 각각 어느 level에 도달하는지는 §7 참조. **옵션 라벨(A~D)은 level을 함의하지 않음** — A·C는 G1(parity)에 미달하는 비교 후보임에 유의.

| level | 의미 | 달성 (옵션) |
|---|---|---|
| **G1 (parity-ish)** | 현재보다 나쁘지 않은 **서버측 재시도** | B·D **달성 가능 후보**(subscriber-0/retry 정책 확정 필요) / A·C 구조적 미달 |
| **G2** | **≥1 send 성공까지** retry | D **후보**(outcome/retry 정책 확정 필요) |
| (3) 전 연결 전달 | 모든 연결 수신 | **절대 보장 불가** (네트워크) |
| (4) 재접속 최신 snapshot | 재접속 client에 최신 제공 | **현재 프로토콜 미지원**; subscribe-time snapshot 또는 resume/ack 계약 추가 시 **server-side 제공 가능**, 단 실제 수신 보장은 불가 |

> ⚠️ **이 결정이 watermark/outbox 채택을 좌우** — **일부 후보(A)는 무상태** 가능, G2(D)는 success-watermark 필요. (3)은 범위 밖, (4)는 server-side 부분 가능(별도 계약).

---

## 7. 옵션 비교 (결론 없음)

failure mode 커버 (✓ = **서버측 재시도** 동작, 전달 보장 아님):

| 안 | m1 SET-fail | m2 dispatch-miss | m3 publish-fail | 상태 | guarantee | 핵심 |
|---|---|---|---|---|---|---|
| **현재 hook** | △ | △ | △ | 무(캐시 diff) | ~G1(조건부) | mirror 복구 후 다음 broad change + 구독자 시 |
| **A** divergence reconciliation | ✓\* | ✗ | ✗ | 무상태 | **G1 미달** | 최단순, broad retry 상실 |
| **B** bounded asset safety republish | ✓ | ✓ | ✓ | (타이머) | **G1 후보** | "주기 내 구독자 존재 시 반복 시도", periodic(순수 source-trigger 아님) |
| **C** = A + attempt-watermark | ✓\* | ✓ | △ | attempt-watermark | **G1 미달** | attempt는 **build 완료 후 send 직전** `built_revision` 기록 → build 예외는 재시도, **send-시도 이후 실패만 미복구**; subscriber-0 정책 필요 |
| **D** success-watermark + retry | ✓ | ✓ | ✓ | success-watermark | **G1·G2 후보** | "≥1 send까지" + subscriber-0·재접속 정책 |

(\* mirror 복구 이후)

> **C의 m3 △ 의미**: build 단계 예외 = watermark 미기록 → **재시도** / send 시작 이후 예외·전송 실패 = attempt 기록 완료 → **미복구**.

**비교 축** (옵션 × 축으로 채울 것):
1. 현재(broad) 대비 재시도 범위
2. 최대 교정 지연 (**mirror 복구 latency 포함**)
3. 상태 저장 (무 / attempt-watermark / success-watermark) + 저장 위치(in-memory/Redis/DB)
4. restart 동작 (상태 소실 시)
5. 발행 중복 (특히 B periodic)
6. subscriber-0 정책 + 재접속 snapshot 연결
7. **공통 base 배선 비용** (canonical revision pipeline + 5-state atomic write — 전 옵션 공유)

**per-option 정책 정의 필요**: 완료 vs 재시도 outcome 매핑 / `all_send_failed→no_subscribers` 전이 / subscriber 재등장 재발행 / `disabled` 재시도·복구 주기.

---

## 8. open decisions (미결 — 이 spec은 결론 안 냄)

1. **guarantee level** (G1 parity-ish vs G2)
2. **옵션** (A / B / C / D)
3. **watermark persistence** (in-memory / Redis / DB) + restart 정책
4. **subscribe-time initial snapshot** 채택 여부 (D / 일부 C와 coupling)
5. **canonical revision id-capture** 방식 (flush-후 id / commit-후 재조회)
6. **subscriber-0** 완료/재시도 정책

> **결정 기준 주의**: item 4 telemetry(`bank_investing_redis_stats`)는 **mode 1(Redis SET write 실패)만** 측정 — mode 2(dispatch-miss)/mode 3(publish 예외·전송 실패)/subscriber-0 재발행/disabled 복구는 **미측정**. 따라서 **B/D 선택 근거가 아니며** SET-복구 운영 우선순위(부분)만 시사. 옵션 결정은 위 1~6(guarantee level + 발행 중복/상태 영속/subscriber 정책)으로 한다.

---

## 9. 제외된 (검토 중 정정된) 가정

> 아래는 수렴 과정에서 **틀린 것으로 판명** — 초안/결정에서 제외.

- ❌ pure-(A) atomic-write-`advanced`만으로 correctness-complete → **published ≠ converged** (SET 성공+publish 실패는 divergence로 미감지).
- ❌ A(divergence) = 현재와 parity → **broad retry 상실** (현재는 any-change 재시도, source-trigger는 per-asset).
- ❌ C(attempt-watermark) = parity+ → **m3 중 send-시도 이후 실패 미복구** (build 후 send 직전 `built_revision` 기록 → build 예외는 재시도되나 send 실패는 watermark 갱신 후라 미재시도).
- ❌ 현재 legacy hook이 publish-failure **보장** → dedicated retry 없음(캐시 diff가 같은 change 재호출 차단).
- ❌ canonical revision = C2 **필수 prerequisite** → 별도 recovery key로 decouple 가능.
- ❌ subscriber-0 = complete → **미확정 정책** (중립 유지).
- ❌ canonical revision = 단순 메타 추가 → **write-pipeline 계약 변경**.

---

## 10. sequencing (방향 확정 후)

방향 결정 시 예상 PR 분할:
1. **공통 base** (전 옵션) — source-key revision 배선(id capture, write-pipeline 계약) + monotonic 5-state atomic write (direct SET + mirror 공유). 기존 transient 퇴행 race도 동시 해소.
2. **옵션 layer** — 선택된 메커니즘. **C/D 선택 시 추가**: builder effective revision vector 반환(build 계약) + per-asset watermark identity (A/B는 watermark 없어 불필요).
3. **legacy fx hook 제거** (PR D 본체).

(tether hook 제거 = PR E 별 트랙.)

---

## 11. 참조

- Step 1 characterization: [tests/test_pr_d_set_failure_characterization.py](tests/test_pr_d_set_failure_characterization.py)
- [USDT_TOPIC_MIGRATION_PLAN.md §6.6.1 / §6.6.2](USDT_TOPIC_MIGRATION_PLAN.md)
- 코드: [main.py:733/754](app/main.py#L733), [crud.py:321](app/crud.py#L321), [latest_rates_cache.py `_mirror_all_latest`](app/latest_rates_cache.py), [fx_topic_payload.py:211](app/fx_topic_payload.py#L211), [topic_dispatcher.py:136](app/topic_dispatcher.py#L136)
- item 4 SET-outcome telemetry (실 SET-failure 빈도 입력): [app/bank_investing_redis_stats.py](app/bank_investing_redis_stats.py), admin `/admin/api/bank-investing-redis-stats`

---

## 12. 결정 (2026-06-16, 사용자 확정)

> Claude + Codex/검증-Claude 다회 검토 + 사용자 결정으로 수렴. §1~11은 이 결정에 이른 비교 분석.

### 12.1 수용 기준 (G — 사용자 확정)

- **R1 (교정 지연)**: 정상 운영(subscriber 존재·기능 활성) 시 복구 대상 확인 후 **≤15초 server-side publish attempt**. 실패 cycle은 hard deadline 아님 — 다음 성공 기회까지 retry + 경고.
- **R2 (best-effort)**: 정상 프로세스 생존 중 **예방적 중복 발행 금지**. 단 (a) send 성공 ↔ watermark 기록 **비원자 window**, (b) watermark 유실 **bootstrap**의 중복은 **허용 + telemetry 관찰** — **전역 bounded 아님**(연속 재시작/Redis 유실 시 반복 가능).
- **R3**: 변경 + **실패·미완료 revision 복구 retry** (상태 추적 필요).

### 12.2 옵션 결정 — D 채택

R1~R3 적용: **A 탈락**(무상태 → mode 2/3 미복구, R3) · **B 탈락**(주기 재발행, R2) · **C 탈락**(attempt-watermark → send 실패 revision 미retry, R3) · **D = 유일 생존**(success-watermark + retry, R1·R2·R3 충족). Client ack/dedup은 범위 밖.

### 12.3 D 정책 (확정)

- **완료**: **모든 경로에서** `sent_count > 0`일 때만 success watermark 갱신. bootstrap에서 subscriber 0이면 **완료 처리 안 함, pending 유지**(send 0 → watermark 미갱신).
- **send ↔ watermark 분리**: send 성공 + watermark 저장 실패 → `sent_but_uncommitted` → 동일 프로세스는 **재전송 없이 watermark 저장만 재시도**. 중복은 그 사이 재시작 시(재시작당 ≤1, 전역 bounded 아님) + telemetry.
- **watermark monotonic**: asset별 publish/watermark 갱신을 **실행 모델에 맞춰 단일 coordinator 직렬화 또는 Redis atomic CAS**(단일 프로세스·단일 coordinator면 직렬화 / 다중 프로세스·우회 경로면 CAS). revision vector는 **실제 전송한 vector 전체를 하나의 identity로** 비교(component merge 금지).
- **subscriber 0**: pending 유지 + polling 중단 + 등장 시 **pending 있을 때만** 즉시 publish + send 후 기록.
- **disabled (FF off)**: pending 유지 + 재활성화 시 즉시 reconciliation.
- **restart**: Redis success watermark 영속(asset별 success revision vector + schema/membership version) + **유실·비호환 시에만 asset별 1회 bootstrap**(R2 명시적 예외, **send 성공 후에만 완료**, telemetry). DB 영속은 과함(per-publish write + 운영데이터 결합)으로 기각.
- **send 실패**: 15초 목표 내 bounded backoff + subscriber 0 전환 시 event-driven 대기.

### 12.4 sequencing (§10)

**공통 base**(source-key revision 배선 + monotonic 5-state atomic write) → **D layer**(builder effective revision vector + success watermark + retry 정책) → **legacy fx hook 제거**. (builder revision vector·watermark는 D layer 비용 — 공통 base 아님.)

### 12.5 구현 spec 이월 (이 문서 범위 밖)

직렬화 vs CAS 택(실행 모델 확정 후) / backoff 간격 / watermark schema·persistence 상세 / subscribe-time initial snapshot 별도 client 계약.
