# P1 공통 base 구현 설계 — control plane + rollout (design / pre-implementation)

> ⚠️ **상태**: 설계 분석 (구현 전, 코드 0). 경계·control plane·**outcome 계약(§14)·cutover state machine(§15)·revision-ID 확보(§16)·atomic primitive Lua(§17) 확정**. **P1 설계 open 전부 resolved**(§13 — lease/timeout=§18 / migration 완료=§15·§18). 잔여 = P1 범위 밖(D layer 내부) + 별도 client/server 계약(subscribe-time initial snapshot).
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
- 잔여 — **P1 범위 밖**: D layer 내부 (builder effective revision vector + success watermark 저장 + retry/backoff — §14/§15는 P1↔D 인터페이스·cutover까지).
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
4. readiness = **migration_complete(전 asset)** + backlog drained → CAS(session+gen+status=running) → status=verified (crash-resume marker, publish 허용 근거 아님). **migration_complete(asset)** = membership_version match + 전 required source key 존재 + v2 + `(revision_key, rate_key)`==verification snapshot. **revision-current은 cutover baseline 게이트 전용**(post-ready 불변 아님 — §18).
5. **최종 단일 tx**: CAS(expected session/generation/status=verified) → global status=completed + 3 asset publish_state=ready + 각 full vector·membership 저장 → commit → cache refresh → publisher gate open. (부분 갱신 후 crash로 일부 asset만 ready 금지. re-verify는 backlog 축소용이지 원자적 보장 아님)
6. fail/timeout → atomic/blocked + status=failed (no auto-ready)

**publisher gate** (모든 FX publisher 공유 — legacy hook([main.py:754](app/main.py#L754) retrofit) / C1 direct flush / D publisher / queued callback; §9 writer-gate와 동일 primitive 별 token-pool):

- token 획득 → token의 generation/session + durable-cached readiness 검증 → build/send → token 반환 (TOCTOU 차단 — entry-check만으론 부족)
- **방향성 전환 순서**: 차단 = gate-close → in-flight drain → durable blocked → cache/ACK / 개방 = durable commit → cache refresh → gate-open

**불변**: atomic/blocked reconciliation의 skipped_newer = concurrent atomic writer로 **정상**(anomaly 아님) — effective_revision으로 완결성 판단 / any → halt = incident.

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
- **post-ready periodic reverify** (유지): **구조적 문제만** — v1 재출현 / malformed v2 / membership mismatch / missing required key / schema·version mismatch → readiness drop(durable CAS) + re-migrate. **Redis-only 구조 scan**(DB revision 비교 없음 → revision-drift false-positive 구조적 불가) + publisher gate 즉시 block.
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

## 19. 참조

- [PR_D_RECOVERY_SPEC.md](PR_D_RECOVERY_SPEC.md) — D 결정 (§12), 옵션 비교 (§7)
- 코드: [crud.py:324](app/crud.py#L324)(orchestrator stage→commit→write→emit) / [:103](app/crud.py#L103)(_write_changed) / [:512](app/crud.py#L512)·[:564](app/crud.py#L564)(selector, id 없음) / [latest_rates_cache.py](app/latest_rates_cache.py)(set_latest·_mirror_all_latest·serialize/deserialize/is_stale, ChangedRate id 없음) / [docker-compose.yml:22](docker-compose.yml#L22)(redis allkeys-lru → marker DB 필수) / [Dockerfile:118](Dockerfile#L118)(--workers 1 단일 프로세스)
