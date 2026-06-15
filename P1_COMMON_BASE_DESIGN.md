# P1 공통 base 구현 설계 — control plane + rollout (design / pre-implementation)

> ⚠️ **상태**: 설계 분석 (구현 전, 코드 0). 경계·control plane·**outcome 계약(§14)·cutover state machine(§15) 확정**. **잔존 open(§13)**: steady-state revision-ID·lease/timeout·Lua/WATCH·migration 완료 판정.
> **소유권**: PR D 복구 = D 채택([PR_D_RECOVERY_SPEC.md §12](PR_D_RECOVERY_SPEC.md)). 본 문서 = D의 **공통 base(P1)** 구현 경계·migration·control plane·rollout (비교 spec과 분리).
> **산출**: Claude + Codex/검증-Claude 다회 검토 (2026-06-16).

---

## 1. 범위 / 목적

P1 = D 옵션의 공통 base = **source-key revision `(timestamp, id)` + monotonic 5-state atomic write** (direct SET + mirror writer 공유). 기존 transient 퇴행 race도 동시 해소. 본 문서는 그 **배포 경계·control plane·migration·rollout 절차**.

## 2. 배포 경계 — P1b 직행 (관찰 전용 P1a 생략)

- **P1a(shadow)** = race 측정 불가(would-be 분포만) + 배포 1회 추가 → **생략**. (분포는 atomic enable canary의 outcome telemetry가 제공.)
- **P1+D 동시** = blast radius로 기각.
- **P1b 직행** = atomic write 적용 + code-enforced 3-state rollout + canary 통제. (근거: PR_D_RECOVERY_SPEC §7.)
- **활성화 timing** = P1b는 atomic write **코드**를 legacy 모드로 land(별도 PR). atomic **활성화는 D layer 준비 후 함께**(atomic+D canary, §15) — watermark 없는 P1-atomic 단독 운영 구간을 만들지 않음. ("P1+D 동시" 기각은 단일 대형 PR 기각이지 *활성화* 동시성 부정 아님.)

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
6. **outcome 계약** (§14) — 5-state writer 결과 ↔ caller/D 발행 결정 분리. legacy-mode bool 경로 불변은 characterization으로 잠금 (production standalone-atomic 전제 제거 — atomic은 D와 함께만 활성, §2/§15)
7. **mirror revision 공급** — 전용 revision-aware selector/internal type (공개 selector·payload에 id 유출 금지; crud.py:512/564 공유 selector 직접 변경 금지)

## 13. open (다음 결정/설계)

- ✅ **outcome 계약** → §14 (resolved). ✅ **readiness/cutover gate** → §15 (resolved — (a)+(c) + seed-in-atomic/publish_blocked + bounded-eventual).
- **steady-state revision ID 확보** (잔존): flush-row-ref (ChangedRate에 ORM row 보관, provisional id를 commit 전 외부 노출 금지) vs commit-후 재조회 (failure boundary 변경 — crud.py:339 "sink 변환 실패 시 commit 전 차단"). bootstrap revision-ID는 §15에서 DB row id 직독으로 해소(별개).
- **atomic primitive**: Lua vs WATCH (선례 0; **direct=sync thread / mirror=async loop 분리** → 공유 coordinator 까다로움 → CAS 무게).
- **lease/timeout 수치** (운영 튜닝, §15 골격과 분리): publisher drain timeout / catch-up bounded 횟수·시간 / lease expiry·renew / status=failed 재시도 정책.
- migration 완료 판정 / subscribe-time initial snapshot (별도 client 계약) / **D layer 내부** (builder effective revision vector + success watermark 저장 + retry/backoff — §14/§15는 P1↔D 인터페이스·cutover까지, D 내부 구현은 범위 밖).

## 14. outcome 계약 (①② — P1↔D 인터페이스)

> 범위: P1 writer 결과 + P1→D 핸드오프 + correctness backstop **역할**. D의 watermark 저장·reconciliation·retry/backoff **내부 구현은 범위 밖**(D layer, §13).

**WriteOutcome** (writer 소유, publish-agnostic):

| 필드 | 값 |
|---|---|
| `state` | advance / refreshed_equal / skipped_newer / conflict / failed |
| `incoming_revision` | 이번 write가 시도한 revision `(epoch, id)` |
| `effective_revision?` | write 후 그 key의 현재 Redis revision (확인 불가 시 None) |
| `redis_write_performed` | **APPLIED \| NOT_APPLIED \| UNKNOWN** |
| `revision_advanced` | **YES \| NO \| UNKNOWN** |
| `error/reason` | 실패 사유 |

`write_healthy`는 state 파생 (저장 안 함): conflict/failed = unhealthy, 그 외 healthy.

**5-state ↔ 두 축** (state 단독 파생 불가 — failed는 실패 지점에 따라 갈림):

| state | redis_write_performed | revision_advanced |
|---|---|---|
| advance | APPLIED | YES |
| refreshed_equal | APPLIED (mirrored_at) | NO |
| skipped_newer | NOT_APPLIED | NO |
| failed (pre-SET) | NOT_APPLIED | NO |
| failed (uncertain SET) | UNKNOWN | UNKNOWN |
| conflict | NOT_APPLIED | NO |

item 4 telemetry reason 매핑 (보수적, SET-relative 지점): `client_unavailable` + SET-전 `writer_exception` → NOT_APPLIED / `set_exception` → 기본 UNKNOWN.

**PendingCandidate** (모든 committed 변경에서 생성 — event-driven latency 최적화, correctness 유일 근거 아님):

- `source`, `asset`
- `desired_revision` = committed DB revision (복구 anchor)
- `observed_effective_revision?` (불가 시 None)
- `write_state`

처리: advance/refreshed_equal/skipped_newer → candidate, D가 asset vector 재구성·발행 판정 / **failed·UNKNOWN → candidate 유지 + 재조회 후 상태 확정 (retry 대상)** / **conflict → candidate 유지 + asset publish 차단 + alert**.

**Correctness backstop** = DB latest revision vs durable success-watermark reconciliation (= D 결정, [PR_D_RECOVERY_SPEC.md §12](PR_D_RECOVERY_SPEC.md)). candidate 소실(commit 후 핸드오프 전 crash)에도 crash/restart 포함 미완료 변경 재구성. candidate = fast path / watermark reconciliation = correctness. **R1 ≤15s는 recovery 시스템 전체에 적용** — candidate fast path뿐 아니라 periodic watermark reconciliation 주기도 R1을 충족하도록 스케줄 (candidate 유실 시에도 backstop이 R1 내 복구).

## 15. cutover state machine (③ — P1/D 활성화)

> 범위: P1↔D 활성화 cutover 계약 (state/gate/atomicity). D publisher 내부·watermark durable 형식은 **범위 밖**(D layer, §13).

**guarantee**: bounded eventual correction (R1). 중간 stale 허용·R1 교정. "DB==Redis 보장" 아님.

**seed 위치**: atomic/publish_blocked에서 reconciliation으로 seed/catch-up (별도 privileged halt-writer 없음 — 동일 atomic primitive + §10 4-case를 실경로 검증). halt = quiesce·protocol 전환 한정.

**durable control (DB, §4 확장)**:

- global: `bootstrap_session_id` / `bootstrap_generation` / `bootstrap_status = idle | running | failed | verified | completed` / `lease(owner, expiry)`
- per-asset: `asset` / `publish_state = blocked | ready` / `ready_revision_vector`(full) / `membership_version`(source_registry 파생)

**states** (write_mode × publish_state): `legacy/ready → halt/blocked → atomic/blocked → atomic/ready`

**전이** (전부 session+generation CAS):

1. legacy/ready → halt/blocked: writer-gate + publisher-gate 둘 다 stop-issue + drain + durable ACK
2. halt/blocked → atomic/blocked: one-shot CAS + 새 session/generation + status=running (writer-gate 재개방, atomic live)
3. [atomic/blocked] reconciliation seed/catch-up (atomic primitive + §10 4-case); 수렴목표 = known-backlog drain
4. readiness = schema 완결 + membership 완결 + backlog drained → CAS(session+gen+status=running) → status=verified (crash-resume marker, publish 허용 근거 아님)
5. **최종 단일 tx**: CAS(expected session/generation/status=verified) → global status=completed + 3 asset publish_state=ready + 각 full vector·membership 저장 → commit → cache refresh → publisher gate open. (부분 갱신 후 crash로 일부 asset만 ready 금지. re-verify는 backlog 축소용이지 원자적 보장 아님)
6. fail/timeout → atomic/blocked + status=failed (no auto-ready)

**publisher gate** (모든 FX publisher 공유 — legacy hook([main.py:754](app/main.py#L754) retrofit) / C1 direct flush / D publisher / queued callback; §9 writer-gate와 동일 primitive 별 token-pool):

- token 획득 → token의 generation/session + durable-cached readiness 검증 → build/send → token 반환 (TOCTOU 차단 — entry-check만으론 부족)
- **방향성 전환 순서**: 차단 = gate-close → in-flight drain → durable blocked → cache/ACK / 개방 = durable commit → cache refresh → gate-open

**불변**: atomic/blocked reconciliation의 skipped_newer = concurrent atomic writer로 **정상**(anomaly 아님) — effective_revision으로 완결성 판단 / any → halt = incident.

**sub-decision (확정)**: full revision vector(작고 진단 가능) / readiness per-asset 저장 / 최초 cutover flip all-at-once(3 asset, 단순성 + 원자성).

## 16. 참조

- [PR_D_RECOVERY_SPEC.md](PR_D_RECOVERY_SPEC.md) — D 결정 (§12), 옵션 비교 (§7)
- 코드: [crud.py:324](app/crud.py#L324)(orchestrator stage→commit→write→emit) / [:103](app/crud.py#L103)(_write_changed) / [:512](app/crud.py#L512)·[:564](app/crud.py#L564)(selector, id 없음) / [latest_rates_cache.py](app/latest_rates_cache.py)(set_latest·_mirror_all_latest·serialize/deserialize/is_stale, ChangedRate id 없음) / [docker-compose.yml:22](docker-compose.yml#L22)(redis allkeys-lru → marker DB 필수) / [Dockerfile:118](Dockerfile#L118)(--workers 1 단일 프로세스)
