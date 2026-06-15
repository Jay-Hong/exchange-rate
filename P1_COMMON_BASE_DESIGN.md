# P1 공통 base 구현 설계 — control plane + rollout (design / pre-implementation)

> ⚠️ **상태**: 설계 분석 (구현 전, 코드 0). 경계·control plane은 확정, **outcome 계약·revision ID 확보·Lua/WATCH는 open(§13)**.
> **소유권**: PR D 복구 = D 채택([PR_D_RECOVERY_SPEC.md §12](PR_D_RECOVERY_SPEC.md)). 본 문서 = D의 **공통 base(P1)** 구현 경계·migration·control plane·rollout (비교 spec과 분리).
> **산출**: Claude + Codex/검증-Claude 다회 검토 (2026-06-16).

---

## 1. 범위 / 목적

P1 = D 옵션의 공통 base = **source-key revision `(timestamp, id)` + monotonic 5-state atomic write** (direct SET + mirror writer 공유). 기존 transient 퇴행 race도 동시 해소. 본 문서는 그 **배포 경계·control plane·migration·rollout 절차**.

## 2. 배포 경계 — P1b 직행 (관찰 전용 P1a 생략)

- **P1a(shadow)** = race 측정 불가(would-be 분포만) + 배포 1회 추가 → **생략**. (분포는 atomic enable canary의 outcome telemetry가 제공.)
- **P1+D 동시** = blast radius로 기각.
- **P1b 직행** = atomic write 적용 + code-enforced 3-state rollout + canary 통제. (근거: PR_D_RECOVERY_SPEC §7.)

## 3. 3-state rollout (legacy / atomic / halt)

- **legacy**: atomic 미활성 초기 — 기존 unconditional writer. **characterization test로 동작 불변 잠금**(새 deserializer/outcome/telemetry가 legacy 경로 동작·반환 계약을 안 바꾸는지).
- **atomic**: monotonic 5-state write.
- **halt**: write fail-closed — **unconditional fallback 없음**.

**허용 전이** (code-enforced):

- **최초 활성화 = `legacy → halt → atomic` composite** (§8/§9 one-shot — 직접 legacy→atomic 아님, halt-quiesce 경유)
- `legacy → halt`: **통제 명령만** (activation pre-quiesce / incident — 일반 admin 전이 아님)
- `atomic → halt`: admin (incident)
- `halt → atomic`: admin, **protocol·schema 재검증 성공 시만**
- `atomic·halt → legacy`: **금지**
- 모든 전이 = **`mode_generation` 증가 + durable(DB) 반영 후 ACK** (단순 cache 갱신 아님 — §7·§9 동일 프로토콜)

**실효 모드** = `requested_mode` + durable activation/schema 검사로 계산. durable 못 읽으면 **halt**. activation 이력 있는데 legacy 요청 → **halt**.

## 4. control table (singleton, DB 제약)

필드: `control_row_format_version` / `target_write_schema_version` / `required_writer_protocol` / **`activation_epoch`**(atomic 최초 활성화 이력·schema floor) / **`mode_generation`**(legacy/halt/atomic 전환마다 증가하는 **fencing token** — `activation_epoch`와 별개; atomic→halt→atomic에서도 증가해 이전 write 거부) / `requested_mode` / `activated_at` / `updated_at`.

**DB 제약 (구조적)**:

- 고정 singleton PK + **`CHECK(id = 1)`** (PK만으론 다른 PK 값의 2번째 row를 못 막음 → `CHECK(id=1)`로 단일 row 강제)
- `requested_mode` enum/CHECK (`legacy`/`atomic`/`halt`)
- `activation_epoch` non-negative CHECK
- 필수 필드 nullability

**⚠️ `activation_epoch`·`mode_generation` monotonic은 DB CHECK로 보장 불가** (CHECK=intra-row, 이전 값 비교 불가) → **조건부 update** (`UPDATE ... SET <col>=:new WHERE id=1 AND <col> < :new` + affected==1 확인) 또는 `SELECT ... FOR UPDATE` 비교 + 테스트로 보장. (DB trigger는 현 규모 과함.)

## 5. version 의미 (3 분리)

단일 schema_version 금지 (배포 호환 판단 모호):

- `control_row_format_version`: control table row 형식
- `target_write_schema_version`: 새 write가 만들 Redis value schema (**단일 "Redis schema" 아님** — lazy migration 중 legacy/new 혼재)
- `required_writer_protocol`: **preflight 비교 대상**
- **이미지 선언**: `image_min_protocol` / `image_max_protocol` + readable schema 범위 (혼재 read)

## 6. preflight (구 binary 차단)

- **위치**: 배포 스크립트 또는 **새 이미지 one-shot** (구 binary 내부 아님 — 구 이미지 rollback도 차단)
- **조건**: `image_min_protocol ≤ required_writer_protocol ≤ image_max_protocol`
- 실패 시 recreate 차단

## 7. runtime mode (hot path 비용 회피)

- 시작 시 DB floor/mode **필수 검증**
- 프로세스 메모리에 **effective mode 캐시** (per-write DB read 회피 — 단일 프로세스 `--workers 1`)
- admin 변경(atomic↔halt) = **§9와 동일 전환 프로토콜** (local gate 차단 → drain → DB tx[mode + `mode_generation` 증가] → cache 확인 → durable ACK) — 단순 "DB write + cache 갱신" 아님
- **`mode_generation` fencing (진행 중 write 동기화, must)**: write 시작 시 (in-process) `mode_generation` 읽고 허용 → 전환 시 bump + **신규 write 차단** + 진행 중 write **drain (단일 프로세스 공통 in-process gate)** → 전환 완료 ACK. ⚠️ **Lua script로 `mode_generation` fencing은 불가** (Lua는 Redis만 보고 DB generation 못 읽음; Redis 복제는 eviction/sync 재발) → **단일 프로세스 전제에선 공통 gate+drain로 확정**, 다중 프로세스 script fencing은 별도 fence-passing 계약 = 범위 밖. (§11 revision compare-write의 Lua/WATCH는 revision이 Redis value에 있어 별개.)
- 주기 poll로 외부 변경 동기화
- **DB 확인 실패·schema 불일치·control 못 읽음 → halt** (fail-closed 기본)

## 8. activation = one-shot (admin 아님)

- 최초 활성화 전체 = **`legacy → halt → atomic` composite** (§3·§9). **§8 one-shot transaction = quiesce 완료 후 `halt → atomic` 부분**: revision-aware 새 이미지 **one-shot command**가 **단일 transaction**으로 — `activation_epoch ↑`(조건부) + `required_writer_protocol ↑` + `target_write_schema` + `requested_mode=atomic` + `mode_generation ↑`. **일반 admin toggle로 최초 activation 금지.**
- admin = runtime `atomic↔halt` (halt→atomic은 재검증).

## 9. bootstrap (control table 없는 기존 production)

1. idempotent table migration (생성)
2. singleton row seed (`legacy` / `activation_epoch=0` / `required_writer_protocol` 초기값)
3. target image **preflight**
4. app recreate
5. legacy **characterization 확인**
6. **live-app quiesce handshake** (명시 control endpoint/command, DB→poll 아님) — **durable 순서**: (a) **local gate 차단**(신규 write) → (b) **공통 in-flight counter/lock으로 direct(sync)·mirror(async) drain** → (c) **DB tx: `requested_mode=halt` + `mode_generation` 증가** (durable) → (d) **local cache 확인** → (e) **durable `mode_generation`+`in-flight=0` ACK**
   - ⚠️ **durability (must)**: cache만 halt로 바꾸고 DB 미반영 상태에서 ACK 후 one-shot 전 crash → 재시작이 **DB legacy 읽고 write 재개**. **DB `requested_mode=halt` 영속이 ACK보다 먼저.**
7. **one-shot 단일 tx**: protocol/schema/`activation_epoch`/`requested_mode=atomic` + `mode_generation` 재증가 (§8)
8. **app recreate → startup 검증 후 atomic 시작**

⚠️ **activation race (must, blocker)**: 실행 앱을 legacy-cached로 둔 채 DB mode만 atomic으로 바꾸면 poll 전까지 unconditional write 계속 → **activation 순간 invariant 깨짐**. → 위 6–8 (durable halt-quiesce + 공통 gate drain → one-shot → recreate-atomic). 최초 활성화 = **`legacy → halt → atomic` composite** (§3).

singleton 부재·중복·parse 실패 → **halt**.

## 10. legacy value migration (lazy, mixed-tolerant)

revision 없는 기존 `latest:*` value 비교 — **timestamp 기반**(정규화 epoch), blanket-advance 금지:

- legacy ts `<` incoming → upgrade (advance)
- 동일 ts·rate → revision/schema seed + freshness refresh
- legacy ts `>` incoming → **overwrite 금지**
- 동일 ts·다른 rate / parse 실패 → **conflict / fail-closed**

혼재 허용 + **migration 완료 판정은 별도** (open).

## 11. monotonic 5-state write (공통 primitive)

direct SET + mirror 공유, **atomic**:

| state | 조건 | 동작 |
|---|---|---|
| `advance` | incoming revision > Redis | rate/ts/mirrored_at write |
| `refreshed_equal` | 같은 revision + 같은 rate | mirrored_at만 refresh |
| `conflict` | 같은 revision + 다른 rate | fail-closed + surface (invariant 위반) |
| `skipped_newer` | Redis revision > incoming | skip (역행 방지) |
| `failed` | read/parse/write 오류 | — |

- 비교 = **정규화 epoch + integer id** (ISO 문자열 금지 — 동일 ts tie-break = DB `(timestamp DESC, id DESC)`).
- **atomic op 실패 시 unconditional 복귀 금지.** 단 **per-key write 실패**(일시 Redis 오류 등) = 그 write만 **skip + telemetry + mirror/next-cycle 재시도, atomic 모드 유지** / **전역 halt = 의도된 mode 상태**(admin/incident). 단일 Redis blip ≠ 전역 halt.

## 12. 7 선결 조건 (구현 전 고정)

1. revision timing — 부여=flush(provisional) / 외부 사용=post-commit / rollback 폐기
2. legacy migration — §10
3. revision = 정규화 UTC epoch
4. rollout — forward + rollback (§3·6·8)
5. fail-closed — §7·11
6. **5-state ↔ 기존 bool trigger 계약** (outcome 계약, §13) + P1 단독 배포 invariant
7. **mirror revision 공급** — 전용 revision-aware selector/internal type (공개 selector·payload에 id 유출 금지; crud.py:512/564 공유 selector 직접 변경 금지)

## 13. open (다음 결정/설계)

- **outcome 계약**: `write_applied` / `write_healthy` / `should_trigger` + 5-state ↔ caller trigger gating (현 `bool True=trigger` 매핑, crud.py:128/321).
- **revision ID 확보**: flush-row-ref (ChangedRate에 ORM row 보관, provisional id를 commit 전 외부 노출 금지) vs commit-후 재조회 (failure boundary 변경 — crud.py:339 "sink 변환 실패 시 commit 전 차단").
- **atomic primitive**: Lua vs WATCH (선례 0; **direct=sync thread / mirror=async loop 분리** → 공유 coordinator 까다로움 → CAS 무게).
- migration 완료 판정 / subscribe-time initial snapshot (별도 client 계약) / **D layer** (builder effective revision vector + success watermark + retry — P1 아님).

## 14. 참조

- [PR_D_RECOVERY_SPEC.md](PR_D_RECOVERY_SPEC.md) — D 결정 (§12), 옵션 비교 (§7)
- 코드: [crud.py:324](app/crud.py#L324)(orchestrator stage→commit→write→emit) / [:103](app/crud.py#L103)(_write_changed) / [:512](app/crud.py#L512)·[:564](app/crud.py#L564)(selector, id 없음) / [latest_rates_cache.py](app/latest_rates_cache.py)(set_latest·_mirror_all_latest·serialize/deserialize/is_stale, ChangedRate id 없음) / [docker-compose.yml:22](docker-compose.yml#L22)(redis allkeys-lru → marker DB 필수) / [Dockerfile:118](Dockerfile#L118)(--workers 1 단일 프로세스)
