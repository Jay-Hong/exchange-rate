# P1 공통 base 구현 설계 — control plane + rollout (design + A1~A4 land)

> ⚠️ **상태**: 설계 분석 + **A1~A4 dormant land (2026-06)**: A1 control plane / A2 writer mode-aware gate + revision plumbing / A3 v2 schema + Lua atomic primitive + migration command(runner+CLI) / A4 WriteOutcome+PendingCandidate interface — **전부 dormant, behavior-change-0, live write 미배선**. **A5(cutover/publisher gate skeleton) 진행 중**. 경계·control plane·**outcome 계약(§14)·cutover state machine(§15)·revision-ID 확보(§16)·atomic primitive Lua(§17)·P1b/D 구현 분해(§19) 확정**. **P1 설계 + 구현 분해 전부 resolved**(§13). 잔여 = 별도 client/server 계약(subscribe-time initial snapshot).
> ⚠️ **A5 scope (codex plan-review scope 확정 + codex/Workflow reconcile)**: A5 = publisher gate primitive(**publish_state/CutoverState 기반**, pass-through/dry-run; **A2 snapshot 미사용 — writer gate와 별개**, §15 별 token-pool) + cutover state enum + allowed-transition validator(pure)만. (gate 상태 소스는 plan의 A2 writer-mode가 아니라 reconcile로 publish_state 정정 — writer ATOMIC≠publisher open, atomic/blocked는 writer atomic+publisher CLOSED.) **§15 publisher gate의 legacy hook([main.py](app/main.py) `safe_publish_all_fx_snapshots`) retrofit + durable control DB schema + 6-step CAS 전이 = C6**(activation 한 점). A5는 live `main.py` 무배선 + DB DDL 0 + B-layer(publish/watermark/coordinator) 무접촉.
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

필드: `control_row_format_version` / `target_write_schema_version` / `required_writer_protocol` / **`activation_epoch`**(atomic 최초 활성화 이력·schema floor) / **`mode_generation`**(legacy/halt/atomic 전환마다 증가하는 **fencing token** — `activation_epoch`와 별개; atomic→halt→atomic에서도 증가해 이전 write 거부) / `requested_mode` / **`activated_at`**(atomic activation 시각 — seed/활성화 전 **None**, §8 one-shot에서 set; `activation_epoch=0`과 정합) / `updated_at`.

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

singleton 부재·중복·parse 실패 → **halt** (= activation/atomic context의 진단·fail-closed 라벨; **legacy bootstrap[activation 전] 단계의 부재/corruption은 §19 A2 phase-gate로 legacy passthrough** — A2 배포≠halt).

## 10. legacy value migration (lazy, mixed-tolerant)

revision 없는 기존 `latest:*` value 비교 — **timestamp 기반**(정규화 epoch), blanket-advance 금지:

- legacy ts `<` incoming → upgrade (advance)
- 동일 ts·rate → revision/schema seed + freshness refresh
- legacy ts `>` incoming → **overwrite 금지**
- 동일 ts·다른 rate / parse 실패 → **conflict / fail-closed**

혼재 허용 + **migration 완료 판정 = §15 readiness의 migration_complete(asset)로 흡수** (별도 subsystem 아님 — §15·§18).

## 11. monotonic 5-state write (공통 primitive)

direct SET + mirror 공유, **atomic** (구현 = **Lua**; v2 schema·Lua 책임·v1 migration split = §17):

| state | 조건 | 동작 |
|---|---|---|
| `advance` | incoming revision > Redis | rate/ts/mirrored_at write |
| `refreshed_equal` | 같은 revision + 같은 rate | mirrored_at만 refresh |
| `conflict` | 같은 revision + 다른 rate | fail-closed + surface (invariant 위반) |
| `skipped_newer` | Redis revision > incoming | skip (역행 방지) |
| `failed` | read/parse/write 오류 | — |

- 비교 = **canonical revision `(canonical_epoch_us, id)`** (정수 μs epoch + integer id; ISO/float 금지 — 동일 ts tie-break = DB `(timestamp DESC, id DESC)`). canonical 변환·확보 = §16.
- **atomic op 실패 시 unconditional 복귀 금지.** 단 **per-key write 실패**(일시 Redis 오류 등) = 그 write만 **skip + telemetry + mirror/next-cycle 재시도, atomic 모드 유지** / **전역 halt = 의도된 mode 상태**(admin/incident). 단일 Redis blip ≠ 전역 halt.
- **mirror 제거는 P1 범위 밖** — P1은 mirror 유지(direct+mirror 공유 atomic). 3s mirror polling 축소·제거는 후속 fanout 트랙 ([REALTIME_ARCHITECTURE_PLAN.md §4.1.5](REALTIME_ARCHITECTURE_PLAN.md), replace-before-remove).

## 12. 7 선결 조건 (구현 전 고정)

1. revision timing — flush 후 row.timestamp/row.id에서 capture(provisional, internal) / 외부 노출=post-commit / flush·commit 실패=rollback+폐기 (§16)
2. legacy migration — §10
3. revision = `(canonical_epoch_us, id)` — 정수 μs(naive→UTC, float 금지), direct+selector 공유 canonical 함수 (§16)
4. rollout — forward + rollback (§3·6·8)
5. fail-closed — §7·11
6. **outcome 계약** (§14) — 5-state writer 결과 ↔ caller/D 발행 결정 분리. legacy-mode bool 경로 불변은 characterization으로 잠금 (production standalone-atomic 전제 제거 — atomic은 D와 함께만 활성, §2/§15)
7. **mirror revision 공급** — 전용 revision-aware selector/internal type (공개 selector·payload에 id 유출 금지; crud.py:512/564 공유 selector 직접 변경 금지). direct=flush-row-ref / mirror·bootstrap=revision-aware selector (§16)

## 13. open (다음 결정/설계)

- ✅ **outcome 계약** → §14 (resolved). ✅ **readiness/cutover gate** → §15 (resolved — (a)+(c) + seed-in-atomic/publish_blocked + bounded-eventual).
- ✅ **steady-state revision-ID 확보** → §16 (resolved — flush-row-ref + StagedRateChange + canonical epoch 계약 + rollback 계약). bootstrap revision-ID는 §15 DB row id 직독(별개).
- ✅ **atomic primitive: Lua vs WATCH** → §17 (resolved = **Lua** — v2 compare/write + v1 migration raw-CAS 모두 Lua, Python은 parse/canonical/4-case 전담; sync/async 서버사이드 atomic 통일 + atomicity 구성 보장).
- ✅ **lease/timeout 정책** → §18 (resolved — 정책 중심, 값은 tunable default).
- ✅ **migration 완료 판정** → §15 `migration_complete(asset)` + §18 post-ready reverify (resolved — §15 readiness로 흡수, 별도 subsystem 아님).
- ✅ **구현 분해 (A 5 + B 4 + C 2 = 11)** → §19 (resolved — D layer[B1 watermark / B2a builder / B2b coordinator / B3 retry·R1]를 별도 트랙으로 분해, behavior-change gate 포함).
- 잔여 — **별도 client/server 계약** (PR D와 직교, 별도 open): subscribe-time initial snapshot.

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

**Correctness backstop** = DB latest revision vs **Redis-resident success watermark**(non-durable, 유실→bootstrap) reconciliation (= D 결정, [PR_D_RECOVERY_SPEC.md §12](PR_D_RECOVERY_SPEC.md)). candidate 소실(commit 후 핸드오프 전 crash)에도 crash/restart 포함 미완료 변경 재구성. candidate = fast path / watermark reconciliation = correctness. **R1 ≤15s는 recovery 시스템 전체에 적용** — candidate fast path뿐 아니라 periodic watermark reconciliation 주기도 R1을 충족하도록 스케줄 (candidate 유실 시에도 backstop이 R1 내 복구).

## 15. cutover state machine (③ — P1/D 활성화)

> 범위: P1↔D 활성화 cutover 계약 (state/gate/atomicity). D publisher 내부·Redis-resident watermark 형식은 **범위 밖**(D layer, §13).

**guarantee**: bounded eventual correction (R1). 중간 stale 허용·R1 교정. "DB==Redis 보장" 아님.

**seed 위치**: atomic/publish_blocked에서 reconciliation으로 seed/catch-up (별도 privileged halt-writer 없음 — 동일 atomic primitive + §10 4-case를 실경로 검증). halt = quiesce·protocol 전환 한정.

**durable control (DB, §4 확장)**:

- global: `bootstrap_session_id` / `bootstrap_generation` / `bootstrap_status = idle | running | failed | verified | completed` / `lease(owner, expiry)`
- per-asset: `asset` / `publish_state = blocked | ready` / `ready_revision_vector`(DB-present sources, partial-tolerant) / `membership_version`(source_registry 파생)

**states** (write_mode × publish_state): `legacy/ready → halt/blocked → atomic/blocked → atomic/ready`

**전이** (전부 session+generation CAS):

1. legacy/ready → halt/blocked: writer-gate + publisher-gate 둘 다 stop-issue + drain + durable ACK
2. halt/blocked → atomic/blocked: one-shot CAS + 새 session/generation + status=running (writer-gate 재개방, atomic live)
3. [atomic/blocked] reconciliation seed/catch-up (atomic primitive + §10 4-case); 수렴목표 = known-backlog drain
4. readiness = **migration_complete(전 asset)** + backlog drained → CAS(session+gen+status=running) → status=verified (crash-resume marker, publish 허용 근거 아님). **migration_complete(asset)** = membership_version match + **DB-present source가 v2 + `(revision_key, rate_key)`==verification snapshot** (DB-absent source missing = readiness 실패 아님, partial-tolerant — legacy partial-publish 보존). **revision-current은 cutover baseline 게이트 전용**(post-ready 불변 아님 — §18).
5. **최종 단일 tx**: CAS(expected session/generation/status=verified) → global status=completed + 3 asset publish_state=ready + 각 ready_revision_vector(DB-present)·membership 저장 → commit → cache refresh → publisher gate open. (부분 갱신 후 crash로 일부 asset만 ready 금지. re-verify는 backlog 축소용이지 원자적 보장 아님)
6. fail/timeout → atomic/blocked + status=failed (no auto-ready)

**publisher gate** (모든 FX publisher 공유 — legacy hook([main.py:754](app/main.py#L754) retrofit) / C1 direct flush / D publisher / queued callback; §9 writer-gate와 동일 primitive 별 token-pool):

- token 획득 → token의 generation/session + durable-cached readiness 검증 → build/send → token 반환 (TOCTOU 차단 — entry-check만으론 부족)
- **방향성 전환 순서**: 차단 = gate-close → in-flight drain → durable blocked → cache/ACK / 개방 = durable commit → cache refresh → gate-open

**불변**: atomic/blocked reconciliation의 skipped_newer = concurrent atomic writer로 **정상**(anomaly 아님) — effective_revision으로 완결성 판단 / any → halt = incident.

> ⚠️ **B2b precondition (A4 land 시 확인, codex/Workflow)**: A4 `WriteOutcome.effective_revision`은 skipped_newer 시 **None** — A3-2 `COMPARE_WRITE_LUA`가 `cur_rev`를 알고도 status 문자열만 반환(surface 안 함). 즉 위 "effective_revision으로 완결성 판단"은 현 A4/Lua 계약으로 **fast-path만으론 불충족**. **B2b reconciliation wiring 전** 다음 중 하나로 닫아야 함: (a) A3-2 Lua를 `(status, cur_rev)` 다중반환으로 확장 → A4가 effective 채움, 또는 (b) skipped_newer 완결성을 Redis effective revision **재조회** 또는 watermark reconciliation backstop으로 판단. (a)는 B2b builder/read-reconciliation 방식 확정 후 결정(현 시점 premature). A4는 None이 정보량 기준 정합 — dormant라 user-facing 영향 0.

**sub-decision (확정)**: full revision vector(작고 진단 가능) / readiness per-asset 저장 / 최초 cutover flip all-at-once(3 asset, 단순성 + 원자성).

## 16. revision-ID 확보 (StagedRateChange + flush-ID + canonical 계약)

> §11 compare가 쓰는 revision `(canonical_epoch_us, id)`의 **확보·정규화** 방식. direct write + mirror/bootstrap 두 공급원이 **같은 row → 같은 revision** 산출해야 함 (§11 conflict/skipped_newer false-positive 방지).

**direct write 순서** (insert_bank_rates_into_db 재구성):

```text
stage → StagedRateChange{change: ChangedRate, row: ORM}
  → sink payload 변환 (to_kst_isoformat, pre-flush — 변환 실패 시 flush 전 abort, 현 failure boundary 보존)
  → db.flush()  (INSERT, id 할당)
  → revision = (canonical_epoch_us(row.timestamp), row.id)  plain value capture (commit 전, expire_on_commit 비의존)
  → db.commit()
  → commit 성공 후에만 revision을 Redis/candidate에 노출
```

- **anchor = row.timestamp** (DB 컬럼; changed_at 아님 — ChangedRate docstring C2 `seen_at`/`rate_changed_at` 분리 시 changed_at이 save-time과 divergence → DB 컬럼 추적이 requery-identity future-proof). 명시 입력값이라 flush 후 auto-갱신 가능성 낮으나 **"flush 후 읽는다" 원칙 유지**.
- **ownership**: `ChangedRate`(frozen) sink DTO 청정 유지 / ORM ref는 **StagedRateChange (staging-only transient)** — frozen DTO를 SQLAlchemy lifecycle에 결합 안 함, commit 후 ORM 객체 downstream 미전달(plain value만).
- **direct source** = flush-row-ref (추가 재조회 SELECT 없음 — id via INSERT RETURNING; flush INSERT round trip은 commit에서 어차피 발생). **mirror/bootstrap source** = 내부 revision-aware selector (공개 §512/564 무변경, §12-7).

**canonical 함수 (direct + selector 공유)**:

```text
to_canonical_epoch_us(dt) -> int:
    dt = dt.replace(tzinfo=UTC) if naive else dt.astimezone(UTC)
    delta = dt - 1970-01-01(UTC)
    return delta.days*86_400_000_000 + delta.seconds*1_000_000 + delta.microseconds
```

signed integer μs, **float `.timestamp()` 금지**(μs 스케일 rounding).

**precision 계약**:

- 단위 μs는 **우리 코드(canonical 함수)가 강제**.
- DB(`Column(DateTime)`, precision 인자 없음 — μs는 Postgres/SQLite default 동작이지 schema 강제 아님)의 μs 보존은 **integration test로 검증**.
- flush→requery 정밀도 동일성은 **integration test/preflight로 검증** — 불일치 시 **activation 차단**(해당 env 부적합). direct runtime은 requery 안 하므로 **runtime 감지 아님**(불일치는 §11 spurious conflict/skip로 발현 → preflight가 선차단). silent truncation 금지.
- 하드 강제 필요 시에만 **향후 `timestamp(6)` migration**.

**rollback 계약 (구현 계약 — 현재 orchestrator에 명시 rollback 없음)**:

- **staging(db.add) 이후 변환·flush·commit 어느 단계든 실패 → `db.rollback()`** — db.add가 이미 session에 pending row를 넣으므로 rollback 없이 session 재사용 시 이후 flush/commit에 그 row가 섞여 저장될 수 있음(변환-fail은 INSERT 미실행이나 **pending row 정리 위해 rollback 필요**). + provisional revision 폐기 + Redis·candidate·alert·trigger **0** + 실패 신호(silent 성공 금지).
- rollback **완료 전 해당 session 작업 계속 금지** / rollback **후 session 재사용 여부는 호출자 lifecycle 계약**에 따름.
- 변환은 pre-flush 유지 → 변환-fail 시 INSERT 미실행 = **DB-not-committed boundary 보존**(rollback은 pending row 정리용 추가; 현 orchestrator는 변환-fail에도 명시 rollback 없이 session lifecycle 의존).
- **commit 성공 후 downstream 실패는 rollback 대상 아님** → D reconciliation 복구 (DB는 committed = 정확).

**test**: ① canonical 단위 / ② SQLite flush→requery revision 동일 / ③ Postgres(통합 환경 있으면) 동일 / ④ commit-fail → rollback + provisional discard + downstream 0 / ⑤ 변환-fail → flush 미호출 + rollback + downstream 0 / ⑥ commit 성공 전 Redis·candidate 호출 0 / ⑦ plain-int capture(expire 비의존).

## 17. atomic primitive — Lua + v2 schema + v1 migration split

> §11 monotonic write의 **구현 결정**. revision/rate 비교(§16)는 Lua-safe string 인코딩으로 Redis-side atomic, v1 legacy parsing은 Python으로 분리. **Lua vs WATCH = Lua** (sync/async 서버사이드 atomic 통일 + atomicity 구성 보장 + Python parse 전담으로 Lua는 string/atomic만).

**v2 value schema** (기존에 additive):

- **public (기존 포맷 UNCHANGED — WS/API 소비자 문자열 계약)**: `rate`(float) / `timestamp`(ISO 기존) / `mirrored_at`(ISO 기존). ⚠️ timestamp를 fixed-width로 바꾸면 안 됨 — v2 Lua는 timestamp 비교 안 함(revision_key 사용).
- **internal (atomic 전용, old reader 무시 — §12-7 id 미유출)**:
  - `schema_version` = JSON number `2` (타입 고정; v1 = 부재/≠2 → discriminator)
  - `revision_key` = `"{epoch_us:020d}:{id:020d}"` (fixed-width string; Python 생성, Lua lex compare)
  - `rate_key` = canonical decimal string (`format(Decimal(str(rate)).normalize(), 'f')` — trailing-zero 제거, **exponent 금지**; Python 생성, Lua string equality)
  - `source`/`asset` = debug optional
  - (`timestamp_key` 불필요 — revision_key가 epoch 포함)

**Lua 책임 (semantic parsing 0 — JSON 구조 decode + string compare only)**:

- **v2 atomic compare/write**: cjson.decode로 구조 파싱하되 **비교는 string 필드만**(revision_key lex / rate_key eq):
  - nil → SET v2 (advance, 첫 write)
  - v2 current: revision_key incoming> → advance / == & rate_key== → refreshed_equal(mirrored_at refresh) / == & rate_key!= → **conflict**(data corruption) / incoming< → skipped_newer
  - malformed v2 (decode 실패 / 필드 누락 / schema invalid) → **failed:invalid_schema**(structure corruption — conflict와 분리, telemetry 구분)
  - v1 (schema_version 부재) → return **"migration_required"** (parsing 안 함)
  - client EVAL 예외 → failed / tri-state(pre-send NOT_APPLIED / reply-lost UNKNOWN, §16)
- **v1 migration raw-CAS**: `if GET key == raw then SET v2 else return changed` (raw string equality, parsing 0)

**Python 책임 (timestamp/rate canonical parsing — Lua 아님)**:

- v2 serialization (revision_key/rate_key 생성, canonical helper)
- **controlled migration command** (bootstrap = 첫 사용처, 재실행 가능):
  - GET raw v1 → Python parse(ISO/Decimal) → **§10 full 4-case** (legacy ts< : upgrade / same ts+rate : seed revision / legacy ts> : skip / same ts+diff rate·parse-fail : conflict/fail-closed)
  - 최종 write = **Lua raw-CAS** (raw==본 값일 때만 v2 / changed → Python retry)

**steady-state v1 재출현** (rollback/mixed-deploy): Lua "migration_required" → **block + alert + controlled migration command 재실행 복구** (hot path inline migration 안 함, dead-end 방지).

## 18. lease/timeout 정책 + post-ready maintenance

> §15 cutover 골격 위 운영 정책. **값보다 정책** — 숫자는 tunable default, 정책이 계약. 핵심 불변: **readiness drop은 D가 복구 못 하는 구조적 문제에만** (revision-lag은 D 대상, drop 아님).

**cutover vs post-ready 분리** (revision-current 적용 범위):

- **cutover readiness 검증** (ready 1회 전환): `migration_complete(asset)` (§15-4) — `(revision_key, rate_key)`==verification snapshot 포함 (baseline 확립 게이트).
- **post-ready periodic reverify** (유지): **structural issue만** — v1 재출현 / malformed v2 / membership mismatch / schema·version mismatch → readiness drop(durable CAS) + re-migrate. **ordinary missing source / cache-gap은 drop 아님 — reconciliation/telemetry 대상**(legacy partial-publish 보존). **Redis-only 구조 scan**(DB revision 비교 없음 → revision-drift false-positive 구조적 불가) + publisher gate 즉시 block.
- **post-ready revision lag** (Redis<DB, 정상 pending): **readiness drop 아님** → D reconciliation (bounded-eventual, §15 guarantee). R1 초과 지속 → **alert/retry 강화, readiness 유지**(즉시 block 아님).

**lease/timeout 정책** (값은 tunable default):

| 항목 | 정책 | default |
|---|---|---|
| publisher/writer gate drain timeout | timeout → transition **abort + 이전 durable 상태 유지 + alert** (force·auto-proceed 금지) | ~5s |
| cutover catch-up timeout (max duration/passes) | 초과 → **atomic/blocked + status=failed** (no auto-ready) → manual controlled command 재실행 | ~60s/N passes (저-churn window 권장) |
| bootstrap lease ttl / renew | concurrent bootstrap 방지 + crash takeover (ttl 만료 → 새 command가 fresh session/generation 인수); **ttl > catch-up max** | ttl ~분 / renew ~ttl/3 |
| status=failed retry | **no auto-ready** → 운영자 controlled migration command 재실행 (실패=조사 필요) | manual |
| post-ready structural reverify interval | 구조적 corruption 감지(Redis-only) → drop + block + migration command | ~분 |
| R1 revision-lag alert | revision lag > R1 지속 → D retry/reconciliation 강화 + alert, **readiness 유지** | R1=15s |

**공통 정책**: 모든 timeout/failure = **fail-closed** (abort/stay-blocked/D-recover, alert). force-proceed·auto-ready 금지. readiness drop = **D-unrecoverable 구조적 문제 한정**.

## 19. 구현 분해 (P1b / D layer / Activation)

> P1b/D 구현 PR 분해 + behavior-change gate. 전부 dormant land(3-state mode gate, legacy default) → production write 위험은 Activation(C6) 한 점. 외부 검토 수렴(2026-06-16, Codex + 검증-Claude 다회). 전체 = **A(5) + B(4) + C(2) = 11 units**.

### A. P1 dormant foundation (5 — 전부 behavior-change-0)

- **A1** ✅ **land (2026-06-17, behavior-change-0)** Control table + 3-state infra: control infra read-only, **writer hot path 미연결**(순수 legacy). control-read-fail = status/preflight error surface(writer 미영향). 구현 — `AtomicWriteControl` 싱글톤(CHECK 3) / `app/atomic_write_control.py`(`compute_effective_mode` dormant + trip-wire 테스트로 잠금) / `create_all_app_tables`(control table 제외 → `migrate_atomic_write_control.py`가 운영(non-test) 유일 생성 경로) / never-crash status endpoint. codex CLI 4-round 리뷰(최종 High 게이트 포함) 수렴, 23 tests + 전체 suite green.
- **A2** writer mode-aware enforcement + revision plumbing: writer가 cached effective-mode 읽음 → control 못읽음/schema 불일치 = **halt(fail-closed, §7)**. flush-ID·rollback flow는 **atomic-mode gate 뒤 dormant**. helper·§16 selector pure 추가.
- **A3** v2 schema + Lua infra + migration command: Lua load-but-not-called, v2 helper·migration cmd dormant. **sub-step 분해(codex xHigh plan-review 9 findings 반영)**: **A3-1** v2 serialization(`atomic_value_schema.py` 신규, stdlib) — `make_revision_key(epoch_us,id)`(`{:020d}:{:020d}`, **non-negative guard** — lex==numeric order 필수, canonical 음수는 §16 일반함수에만) / `make_rate_key(rate)`(`format(Decimal(str(rate)).normalize(),'f')`, **finite+non-negative guard, -0.0→"0"**) / `serialize_v2_value`(public rate/timestamp/mirrored_at UNCHANGED + internal schema_version=2/revision_key/rate_key). **A3-2** [land] Lua + load infra(`atomic_lua.py` 신규, Redis) — §17 compare/write Lua(**discriminator 순서: schema_version nil→migration_required / ==2→v2 / else→invalid_schema**) + v1 raw-CAS, `register_script`(Script 객체 등록, **server 미load — 첫 호출 전 SCRIPT LOAD 0**) + thin wrapper(`AtomicLatestWriter`, live 미호출) + Python reference port(`evaluate_compare_write`/`evaluate_migrate_cas`). **A3-3** migration command(Python, full §10 4-case + Lua raw-CAS) — **실행 dormant + lease defer(C6-dep)라 dry-run/single-process 제한**(prod concurrent apply 금지). test: Python reference port(decision-table logic, 항상) + **env-gated 실Redis(syntax/cjson/raw-CAS/atomicity + reference cross-check + SET side-effect; A3에 포함 — C6 미룸 금지; CI redis service로 push마다 실행, 로컬 미가동 시 skip)** + dormancy AST trip-wire(crud/latest_rates_cache/scheduler/main에 eval/evalsha/script_load/register_script/serialize_v2/atomic_lua import·호출 0). additive-only behavior-change-0.
- **A4** [land] WriteOutcome + PendingCandidate interface (§14): `app/atomic_write_outcome.py` 신규 — `WriteOutcome`(state/incoming_revision/effective_revision?/redis_write_performed/revision_advanced/reason/**structural** typed) + `PendingCandidate`(source/asset/desired_revision=incoming/observed_effective_revision/write_state) + `write_outcome_from_lua`(A3-2 전 outcome 매핑: advance/refreshed_equal→APPLIED, skipped_newer→NOT_APPLIED/effective=None, conflict→NOT_APPLIED/effective=incoming, **migration_required/invalid_schema/unsupported→failed+structural**, EVAL None→failed+**failure_kind 필수**[DEFINITE_NOT_APPLIED/UNCERTAIN_AFTER_SEND]) + `candidate_disposition`(typed structural 분기: advance/refreshed/skipped→PUBLISH_CANDIDATE / conflict·structural failed→BLOCK_ALERT / general failed→RETRY). **atomic-mode-only handoff, legacy=bool 불변** — writer 배선·candidate sink/queue·D 소비는 A5/C6/B2b. dormant(app/ 전체 import 0 AST trip-wire). codex plan-review 7 + Workflow 적대 리뷰(4 렌즈) 9 + codex impl 3 findings 반영(typed structural marker / failure_kind 필수 / skipped_newer effective None B2b precondition[위 §15 note]). 19 tests.
- **A5** cutover/writer-publisher gate skeleton: pass-through/dry-run in legacy.

### B. D layer (4 — 별도 트랙, A4 이후, 전부 dormant)

- **B1 Redis success watermark** (non-durable; eviction/비호환 → bootstrap):
  - identity = `{asset, lineage-id(watermark_epoch/bootstrap_session_id), publish_sequence, membership_version, present_revision_vector{source:"epoch_us:id"}, missing_sources[], sent_at(telemetry)}`
  - **monotonic = lineage-scoped publish_sequence**(coordinator-issued per-asset int): same lineage → seq 비교(`>` whole-replace / `==` idempotent / `<` skip) / newer lineage(bootstrap) → wins. **seq 비교는 같은 lineage 안에서만**.
  - lifecycle: watermark 존재 → current.seq 기준 next / missing·비호환 bootstrap → **새 lineage** / restart 후 in-memory retry 유실(비교 X) / eviction-alive → coordinator in-memory seq regression 없이 유지.
  - completion: sent_count>0 only / subscriber-0 → pending. bootstrap: missing/schema/membership mismatch → asset 1회, send 성공 후 기록, R2 telemetry.
- **B2a Builder BuildResult**: `{asset, payload, present_revision_vector, missing_sources, membership_version, completeness[complete|partial|malformed], build_error}`.
  - **partial publish 허용**(current compat — legacy partial-publish 보존), missing_sources = telemetry/reconciliation input, **strict all-source block = future policy**. payload엔 revision/id 미노출(§12.7).
- **B2b Reconciliation coordinator** (= watermark correctness boundary):
  - **D-active 이후 모든 watermark-bearing FX publish는 coordinator/gate 통과**(밖 send = watermark divergence). **publish + watermark write/retry(sent_but_uncommitted 포함) 모두 per-asset coordinator 소유**(publish_sequence 질서 보존).
  - per-asset critical section = build→send→sent_count→watermark write/enqueue(+publish_sequence 발급). 단일 coordinator 직렬화 + defensive guard.
  - 입력 5: candidate fast-path / backstop / startup / FF re-enable / subscriber appearance. pending = DERIVED per-source: DB-not-in-present → pending / DB-rev>present[src] → pending / DB-absent+in-missing → not pending / malformed·schema → structural.
- **B3 retry/backoff + R1 + partial_pending**:
  - **R1 budget**(end-to-end, commit/pending-created → sent_count>0) = **detection interval + build + first send + bounded retry ≤ target(15s, soft)**. candidate 즉시 / startup 즉시 / send-fail ≤15s bounded backoff.
  - lanes: send-retry(re-publish) / watermark-only(sent_but_uncommitted, 재전송 X, coordinator 소유) / backstop.
  - **partial_pending**: DB-valued source가 build에서 빠짐(no-DB-value 아닌 malformed/membership/fallback-fail) → pending 유지(watermark latest seq). **retry = source value availability repair**(현 builder는 Redis miss/stale → DB fallback이라 단순 cache-gap 아님 — availability 회복 수단은 Redis/mirror/targeted reconciliation 등) 후 새 build. **identical partial 재발행 금지**(BuildResult==watermark면 skip). 반복 = telemetry/R1-alert이지 publish-repeat 아님.
  - sent_but_uncommitted = R1-publish 위반 아님(watermark-lag telemetry).

### C. Activation (2 — behavior change, 통제)

- **C6** Atomic+D canary (A1~5 AND B 전부[B1/B2a/B2b/B3] 완료 후): cutover state machine(§15), canary + rollback/halt. legacy hook = coordinator route OR gate-suppress.
- **C7** Legacy FX hook 제거 (readiness/telemetry gate 통과 후).

### behavior-change gate 요약

A1~B3 = **3-state mode(legacy default) gate로 dormant** → production write 위험은 **C6 한 점**(canary+rollback). C7 = hook 제거(C6 stable 후).

### 의존 / 순서

A1→A2→A3→A4 → {A5, B1→B2a→B2b→B3} (A4 이후 병렬) → C6(A+B 전부 후) → C7.

### A2 상세 설계 (reconciliation 수렴, 2026-06-17)

> A2(writer mode-aware enforcement) 설계 closed — workflow 설계 + 내부 adversarial 6 + codex High(6확인·4추가) + reconciliation 3 수렴. **구현 전**. 근거 file:line은 §20 + A1 land 코드.

**대원칙 3**: ① legacy 100% 불변(writer가 mode 읽되 legacy면 기존 commit→Redis→alert→trigger 순서·예외격리·반환계약 비트단위 불변, early-branch만 위에 얹음) ② halt phase-gate(A2 배포만으로 — migration 유무 무관 — live 서비스 절대 halt 안 됨) ③ atomic dormant(atomic 분기/flush-ID/rollback land하되 gate 뒤 dormant, activation=C6).

**crux b — halt 실효 시점**: writer는 **immutable snapshot 1-read** `{diagnostic_effective_mode, activation_latched, enforced_action, mode_generation}` (no-throw accessor). `enforced_action`(legacy/atomic/halt 단일 필드, writer는 이것만 분기)를 `diagnostic_effective_mode`(compute_effective_mode 결과 — None→HALT 진단 라벨 불변)와 **분리** — `effective_mode==HALT`가 곧 "차단" 아님. **derive 우선순위**: diagnostic==atomic→atomic / **valid explicit halt(format 일치 + requested_mode==halt)→halt**(pre-activation에도 §3/§9 legacy→halt quiesce 존중 — 부재/corruption과 구분) / post-activation(activation_latched)→fail-closed halt / 그 외(pre-activation 부재/corruption/atomic-미활성, involuntary)→legacy passthrough. **read 실패**: diagnostic=HALT + enforced는 last-good fail-close(atomic→halt[§7 못읽음→halt], legacy/halt 보존) + latch/gen 보존. `mode_generation`은 관측 단조(fencing token, 회귀 금지). 2-call torn read 금지 → 1-snapshot.

**T1 — post-activation restart (못박음)**: A2 latch = **in-process monotonic latch**(한 번 True면 프로세스 생애 False 금지), restart durable marker 아님. **durable activation marker = DB control row `activation_epoch>0`**(재시작 생존, 읽히는 한). restart 시 control 못 읽는 post-activation ambiguity는 **C6 precondition으로 닫음**(C6 activation/runbook이 readability 보장 or 못 읽으면 fail-closed). A2-1은 새 durable marker 안 만듦. A2는 pre-activation이라 restart/read-fail→legacy가 항상 정확.

**A2 sub-step 재분해**:

- **A2-1 (cache, 완전 dormant)**: `app/atomic_write_runtime.py` 신규 — immutable snapshot + `activation_latched` in-process monotonic + no-throw accessor + `refresh_from_db()`(DB read/compute는 lock 밖, swap만 lock 안) + diagnostic/enforced_action 분리. **live poll scheduling 없음**(import/lifespan 자동 poll 금지 — writer 미연결이어도 live DB-polling은 behavior-change-0 위반). control read-fail 문서정정 동반(아래). writer 미연결 — A1처럼 정의+테스트, live 효과 0.
- **A2-2 (bank/investing writer 분기)**: orchestrator **banner 직후·staging 전** snapshot 1-read gate. legacy fall-through(기존 commit→Redis→alert→trigger 비트단위 불변) / halt·atomic → `_stage_*` 전 `return 0`(db.add 없음→**rollback 불요**; **atomic은 A3 구현 전 fail-closed**, legacy fallback 금지) + skip counter. **Selenium subprocess(runner.py)도 진입 시 refresh**(별 프로세스라 main poll 미공유 — codex plan-review 적발). startup refresh + APScheduler poll(`ATOMIC_MODE_POLL_INTERVAL_SECONDS`, `>=1` validation) — `atomic_write_refresh.refresh_write_mode_cache()` no-throw wrapper(SessionLocal lazy, atomic_write_runtime는 pure 유지). control 없으면 read-fail→legacy(배포≠halt). trip-wire 전환(crud/scheduler 연결, latest_rates_cache는 A2-3까지 금지). characterization green.
- **A2-3 (usdt/krx Redis writer 분기)**: routine tick writer **2개만** gate — `set_latest_usdt_rate_from_sync_job` + `set_latest_krx_rate_from_sync_job_tick_level`. 최상단 snapshot 1-read(Redis I/O 전) → halt/atomic은 **`BLOCKED` outcome**(coalesce `SKIPPED`와 구분 — polling caller success 오인 차단[codex plan-review Medium], trigger 차단; **atomic은 A3 전 fail-closed**, legacy fallback 금지). legacy → 기존 흐름 불변. routine KRX는 prod(`KRX_REDIS_TICK_WRITE_ENABLED=true`)=tick-level(624) + rollback(false)=`KrxDbWriter._sync_db_write` flag=false 분기(`write_after_db_insert`→571)도 gate(두 config 모두 Redis-side gated, benign partial — DB insert는 진행, read-path DB-fallback, codex xHigh Medium). **`set_latest_krx`(DB-bound, 571) 자체는 gate 제외** — `KrxCloseWindowWriter`/REST close(571 직접, 2308/2880)가 공유라 gate 시 close finalizer partial → deferred. **usdt/krx DB-side full halt**(`insert_source_rate_if_changed` gating)는 C6 — A2-3는 Redis-side만. mirror(`_mirror_all_latest`)는 C6까지 미연결 — AST trip-wire로 잠금. approximate block counter + usdt_sources polling caller BLOCKED 처리.
- **A2-4 (§16 revision plumbing pure)** [land]: `app/atomic_revision.py`(신규, stdlib-only) — `to_canonical_epoch_us(dt)->int`(naive=UTC/aware 변환, float `.timestamp()` 금지, 음수 epoch 안전) + `Revision=tuple[int,int]` + `RevisionRow` Protocol(id/timestamp) + `revision_from_row(row)`(direct·selector 공유 구성) + `RevisionedRate` DTO. `crud.py` — `StagedRateChange{change, row}`(staging-only transient, `.revision` property) + `_select_latest_{investing_rate,bank_rates}_with_revision`(internal, 공개 525/554 무변경, ORM 대신 RevisionedRate 반환). **atomic gate 뒤 dormant**(caller 0 — A3 orchestrator flush-ID / C6 mirror·bootstrap wiring), `_stage_*`/`insert_*`/공개 selector live path 무변경(behavior-change-0). test ①②⑦(canonical/flush→requery/plain-tuple) — ④⑤⑥(rollback)은 A3. codex plan-review 5 findings(StagedRateChange 위치·타입/selector scope·DTO/canonical/staging 무변경/test 경계) 반영. 19 tests.

**C6-전 blocker dependency (A2 미구현, atomic 활성 전 필수)**:

- **USDT/KRX atomic revision-ID plumbing + value migration**: (1) DB insert와 Redis write 독립 채널이라 Redis writer가 row.id 못 봄 → atomic 시 §11 compare false-positive. A2-3는 legacy/halt만, atomic revision은 C6 전 재설계. (2) **value migration scope 확장** — A3-3 migration runner는 **bank/investing only**(A2-4가 `_select_latest_{bank,investing}_with_revision`만 제공, USDT/KRX `latest:source:*`엔 revision-aware selector 부재). C6에서 USDT/KRX atomic 켜면 미migrate된 `latest:source:*` v1 값이 Lua `migration_required` 반환 → §17 controlled migration command 재실행 경로 필요한데 현 runner가 USDT/KRX 미cover. → **`_select_latest_source_with_revision` selector 신규 + migration runner `--scope source` 확장**이 C6 전 필수 (holistic 검토 적발).
- **mirror atomicization**: `_mirror_all_latest`가 또 다른 latest:* writer → atomic 시 v1 계속 쓰면 §11 invariant 깸. atomic 활성 전 mirror도 atomic primitive 공유.
- **coalesce bypass/clear**: `_last_written_usdt/krx_state` same-rate/bucket skip이 atomic v2 migration write 건너뜀 → atomic mode는 coalesce bypass, Lua revision compare가 판단.
- **close finalizer write-mode gating + atomic v2 전환**: `set_latest_krx`(571)는 `KrxCloseWindowWriter`/REST close가 공유 + DB write(insert_source_rate_unconditional)와 독립 → A2-3에서 gate 시 partial(DB 후 Redis 차단). atomic/halt 시 close finalizer를 막을지(DB+Redis atomic 단위)는 C6 close-path 정책으로 결정 — A2-3는 571 미gate(close 보호). **⚠️ atomic mode 추가 측면(holistic 검토)**: 571은 `serialize_value`(v1 schema)로 직접 SET + gate 우회(krx_kis 2308/2880 직접 호출). C6에서 KRX atomic 켜고 close finalizer를 **block 안 하면** 매 CF close가 v1 값을 `latest:source:*`에 써서 §11 v2 invariant 깸(다음 atomic write가 v1 만나 `migration_required` block+alert). → C6 옵션: **(a) atomic quiesce 단위로 close finalizer block / (b) close finalizer도 atomic v2 write(serialize_v2_value + Lua compare/write)로 retrofit**. 둘 중 택1 필수.
- **(확인 — dependency 아님) tether/KRX topic publisher는 FX cutover scope 밖**: §15 publisher gate는 FX publisher(fx:usd-krw/jpy-krw/eur-krw) 전용. tether/usdt:krw topic publisher(`request_tether_topic_trigger`/`safe_publish_tether_tab_snapshot`) + KRX tick tether trigger는 topic-only Tether/KRX 별 트랙이라 **FX cutover scope 밖** — C6 FX atomic 활성 후에도 gate 미적용이 의도(누락 아님, holistic 검토 확인).
- **migration command lease (§18)**: `AtomicWriteControl`에 lease owner/expiry 필드 부재(A1 schema) → §18 bootstrap lease ttl/renew(concurrent bootstrap 방지 + crash takeover)는 schema 확장 필요. A3 migration command는 lease 없이 **dry-run/single-process 제한**(prod concurrent apply 금지)으로 land — lease storage/DDL은 migration command가 실제 prod 실행되는 시점(~C6) 별도 unit.
- **usdt/krx DB-side full halt**: A2-3는 Redis-side writer만 gate(usdt 382 / krx tick 624 / krx routine flag=false). DB writer(`insert_source_rate_if_changed`, usdt WS DB writer + KrxDbWriter)는 미gate라 halt 시 DB write는 진행(Redis만 차단 = benign partial, read-path DB-fallback). bank/investing(A2-2)은 orchestrator top gate라 DB+Redis 동시 정지인 것과 비대칭 — usdt/krx의 DB-side 정지(완전 halt/atomic quiesce)는 C6에서 atomic DB+Redis 단위로 결정.

**문서정정 (A2-1 동반 — 구현자 오독 차단)**: `models.py` AtomicWriteControl docstring "A2는 singleton 부재=halt 취급"→"activation 전 phase는 부재/corruption=legacy fall-through, 후엔 halt; compute_effective_mode None→HALT는 진단 라벨, enforcement는 phase-gated" / §9 line 93("부재→halt")에 "= activation/atomic context, legacy bootstrap 단계 부재는 A2 phase-gate로 legacy" 명시.

**공통(전 sub-step)**: snapshot read no-throw, per-write 로그 금지, pre-activation enforcement=legacy passthrough, 기존 commit→Redis→alert→trigger 순서 characterization 유지.

## 20. 참조

- [PR_D_RECOVERY_SPEC.md](PR_D_RECOVERY_SPEC.md) — D 결정 (§12), 옵션 비교 (§7)
- 코드: [crud.py:324](app/crud.py#L324)(orchestrator stage→commit→write→emit) / [:103](app/crud.py#L103)(_write_changed) / [:512](app/crud.py#L512)·[:564](app/crud.py#L564)(selector, id 없음) / [latest_rates_cache.py](app/latest_rates_cache.py)(set_latest·_mirror_all_latest·serialize/deserialize/is_stale, ChangedRate id 없음) / [docker-compose.yml:22](docker-compose.yml#L22)(redis allkeys-lru → marker DB 필수) / [Dockerfile:118](Dockerfile#L118)(--workers 1 단일 프로세스)
