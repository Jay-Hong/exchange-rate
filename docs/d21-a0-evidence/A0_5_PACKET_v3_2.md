# A0.5 v3.2 — server ordering 결정축 (Codex R6 보존 경로 실행 결속 · 파일 미반영)

> v1·v2는 `_SUPERSEDED`다. v3.1은 raw handoff에서 byte-identical 이력으로
> 보존하되, 운영적 내용은 이 문서가 대체한다. handoff 후 v3.1의 임시 사본은 cleanup 후보다.
> **12축 결정 상태: `OPEN` 8 · `PROPOSED` 4 · `ACCEPTED` 0.**
> **현재 전역 실행 게이트: `BLOCKED_BY_HANDOFF`; handoff lifecycle:
> `HANDOFF_BLOCKED_BY_DESTINATION`; 임시 손실 보호: `HP_REQUIRED +
> TEMP_LOSS_AT_RISK`; lifecycle local protection: `LIFECYCLE_LOCAL_PENDING`.**

R4 수정 전 검토 baseline은 `1187 lines / 72877 bytes / SHA-256
3b1ce346c585ff112d4b456159dd260a0af8b07aead100ce26aabd48708b60dd`였다. 그 검토에서
확인된 H0 atime 자기교란, 동적 sidecar discovery loop, HP generation self-envelope/current
baseline 결속, helper review provenance 결함을 아래 절차에서 수정했다. 현재 파일 hash는 파일
자체에 넣어 self-reference를 만들지 않고 외부 handoff record에서 고정한다.

R5 수정 전 검토 baseline은 `1863 lines / 133671 bytes / SHA-256
86193318e8d212ccc47dfa465bbdafc152b29759d77bd67e1ada12575f5e2bff`였다. R5는 P0 verifier의
무파일 bootstrap, total ownership map, guard와 실제 cleaner 실행 기회의 구분, 비정규 선복사의
비승격 규칙을 명시한다. 이 수정도 현재 파일 hash를 본문에 넣지 않는다.

R6 수정 전 검토 baseline은 `2019 lines / 145931 bytes / SHA-256
f04efc3b2cfa1b1675cdc53f6446a95080ac4ffbaa1f3701ce798c74cc8cbfb4`였다. R6는 Codex unified
exec session을 통한 턴 간 승인 relay, macOS dyld shared-cache native API 결속, digest-only가 아닌
exact verifier source 정적 검토와 file-backed fallback의 실제 발화 조건을 명시한다.

## 0. 판정 규칙 — 결정·증거·게이트를 축별로 분리한다

```text
결정 상태  OPEN · PROPOSED · ACCEPTED · REJECTED · DEFERRED
증거 상태  MISSING · PLANNED · OBSERVED · FAILED · INCONCLUSIVE
축별 기술 게이트
            BLOCKED_BY_DECISION · READY_FOR_SPIKE · PENDING_EVIDENCE ·
            BLOCKED_BY_INPUT · READY_FOR_IMPLEMENTATION · FAIL
전역 실행 게이트
            BLOCKED_BY_HANDOFF · AWAITING_SEPARATE_GO
산출물 lifecycle
            HANDOFF_BLOCKED_BY_DESTINATION · HANDOFF_IN_PROGRESS · HANDOFF_COMPLETE
이관 단계 결과
            ROOT_GENESIS · GENERATION_STARTED · H0_COMPLETE · H0_FAIL · H1_PASS · H1_FAIL ·
            GENERATION_INVALIDATED · CRASH_ABORTED · GENERATION_ORPHAN_ABORTED ·
            STAGING_ORPHAN_RECORDED ·
            H2_APPROVED · HP_REQUIRED · HP_VERIFIED ·
            H3_COMPLETE · H4_COMPLETE · H4_FAIL · H5_COMPLETE · H5_FAIL
HP attempt HP_ATTEMPT_STARTED · HP_ATTEMPT_VERIFIED · HP_ATTEMPT_ABORTED ·
           HP_ATTEMPT_STAGING_RECORDED
임시 손실 위험
            TEMP_LOSS_AT_RISK · TEMP_LOSS_PROTECTED_LOCAL
비정규 사본 UNVERIFIED_RECOVERY_CANDIDATE
lifecycle local protection
            LIFECYCLE_LOCAL_PENDING · LIFECYCLE_LOCAL_VERIFIED
auxiliary local protection
            AUX_LOCAL_PENDING · AUX_LOCAL_VERIFIED · AUX_LOCAL_ARCHIVED · AUX_LOCAL_CLEANED
원본 연속성
            SOURCE_PRESENT · SOURCE_LOST_AFTER_HP · SOURCE_LOST_AFTER_P0_HP ·
            PROMOTED_HANDOFF_SOURCE ·
            UNRECOVERABLE_SOURCE_LOSS
소유 분류  OWNED_REQUIRED · OWNED_REPRODUCIBLE · EXTERNAL_INPUT ·
            FOREIGN · UNRESOLVED
소유 후속  OWNER_ACTION_REQUIRED
외부 input disposition
            ARCHIVE_BYTES_WITH_LICENSE · REFERENCE_ONLY_NETWORK_REQUIRED
기밀성 분류 PUBLIC · ACCESS_CONTROLLED · EXCLUDE_WITH_REASON · UNREVIEWED
배포/라이선스
            REDISTRIBUTABLE · REFERENCE_ONLY · LICENSE_REVIEW_REQUIRED
A0.6 중단 A0_6_DEFERRED · A0_6_ABANDONED
cleanup     CLEANUP_PENDING · CLEANUP_COMPLETE · CLEANUP_PARTIAL
```

`OPEN_NOT_PREFERRED`, `PROPOSED constraint`처럼 정의되지 않은 혼합 상태는 쓰지 않는다.
선호도는 결정 상태와 별도 열에 적는다. 서로 다른 namespace의 상태는 동시에 성립할 수
있다. 예를 들어 어떤 축이 `READY_FOR_SPIKE`여도 전역 게이트가 `BLOCKED_BY_HANDOFF`면
실행하지 않는다.
이 문서의 전역 실행 게이트는 A0.6/D21 spike·구현을 지칭하며, 손실 방지를 위한
H0/H1/HP 보존 절차는 가로막지 않는다.

A0 증거는 `OBSERVED` 하나로 평탄화하지 않는다. 각 주장마다 A0의 원래 등급을 유지한다.

```text
REPRODUCED_POSTGRES · COMPONENT_RUNTIME_CONFIRMED · SOURCE_PROVEN ·
GENERIC_POSITIVE_CONTROL · STRUCTURAL · CURRENT_STACK_E2E_UNPROVEN
```

A0의 18/18은 **단일 advisory transaction lock을 쓴 임시 raw-SQL reducer의 부분 관측**이다.
v3.2 후보의 의미·SQLAlchemy 통합·retention 경계·운영 부하를 검증한 proposal-specific
evidence가 아니다. 따라서 정확한 요약은 다음과 같다.

```text
문제 반례                 A0 원래 등급대로 다수 관측
global-lock primitive     REPRODUCED_POSTGRES 부분 관측(18/18)
완성 후보                 PASS 0
```

Spike 결과명은 위 세 축과 섞지 않는 별도 vocabulary다.

```text
A0.6A  MECHANISM_REPRODUCED · INTEGRATION_BLOCKED · MECHANISM_FAILED
A0.6Q  QUIESCENCE_PROTOCOL_REPRODUCED_LOCAL · QUIESCENCE_BOUNDARY_REPRODUCED_LOCAL ·
       DRAIN_PROOF_INSUFFICIENT · CUTOVER_FAILED
A0.6B  CONDITIONAL_ON_PROVISIONAL_CONTRACT · REDUCER_FAILED
A0.6C  LOCAL_CONTENTION_PROFILE_OBSERVED · PERF_EVIDENCE_PENDING ·
       SHORT_GLOBAL_SUITABLE_FOR_PINNED_PROFILE · NEEDS_FINE_GRAINED_EVALUATION
```

`SQLALCHEMY_DRIVER_ONLY`, `ORM_PROTOTYPE_ONLY`, `LOCAL_PERF_ONLY`는 증거 **범위**이며
증거 상태/등급이 아니다. 예: `OBSERVED (ORM_PROTOTYPE_ONLY)`처럼 상태와 함께 쓴다.

## 1. 현행 계약

```text
POST   token 충돌 시 owner 무조건 덮어씀      crud.py:1791-1808
DELETE UID+token 삭제, revision 없음          main.py:3772 · crud.py:1872
                                               → LEGACY_UNORDERED
```

현 DELETE는 `expected_server_revision`이 없는 legacy 연산이다. 아래 축 1의 exact-CAS
INACTIVE가 아니며, v3 도입 시 migration/fence 대상이다.

## 2. 배포 위상과 quiescence의 정확한 전제

현재 소스가 보여 주는 것은 **main service container 1개 · Uvicorn worker 1개 · Nginx upstream
peer 1개**다. 같은 process lifespan에서 scheduler도 시작되고, 운영 문서에는
`docker compose run --rm fastapi` one-shot 사용례도 있다. 따라서 이를 “시스템 writer process가
항상 하나”로 일반화하지 않는다.

현재 검색에서 `UserDevice` writer는 `app/` 경계 안에 있지만, cutover 때는 one-shot·수동 DML·
별도 DB session을 동결하고 다시 inventory한다.

⚠️ **`--force-recreate`는 writer-drain 증거가 아니다.** 종료 grace 동안 구 handler가 COMMIT에
도달할 수 있고 응답이 유실되면 결과는 미확정이다. 반대로 구 process의 DB connection이 실제로
종료되면 열린 명시적 transaction은 rollback된다. 이미 PostgreSQL이 처리한 COMMIT만 durable하게
남는다. A0 §5.3은 process 사망을 시험한 것이 아니라, handler가 살아 있는 상태에서
`client timeout != no mutation`을 보인 `GENERIC_POSITIVE_CONTROL`이다.

따라서 증명 대상은 “process가 죽어도 transaction이 산다”가 아니라 **claim 활성화 뒤 commit할
수 있는 pre-compat writer/backend가 0**이라는 사실이다.

이 repo에는 이미 재사용 후보가 있다.

```text
AtomicQuiesceSession · AtomicQuiesceAppAck
process-lifetime _BOOT_ID · exact halt-generation
process_started_at > halt_committed_at
confirm_quiesce_drained()
```

이는 durable HALT와 fresh-process ACK의 좋은 선례지만, ACK 하나가 모든 old writer/DB backend의
소멸을 증명하지는 않는다. D21은 이를 **재사용·보강할지 먼저 평가**한다.
`application_name=<release/boot>`은 유용한 선택지이지 유일한 필수 기전은 아니다. pre-stop backend
PID 집합, dedicated DB role과 session census·접속 차단·backend 종료의 결합, writer와 HALT가
공유하는 transaction lock, DB trigger/권한 fence도 후보다. role 이름만으로는 drain이 증명되지
않는다. 선택한 proof에 맞는 deployment-scoped identity 또는 DB serialization evidence가 필요하다.

권위적인 cutover 순서는 축 12 하나에만 둔다.

---

# 12개 결정축

## 축 1 — ACTIVE/INACTIVE와 desired/effective

- **결정**: `PROPOSED`
- **제안**: INACTIVE는 (a) token 없이 current head에서 파생하거나, (b) 인증 UID·lineage에
  결속된 exact current token + expected server revision이 일치할 때만 적용한다.
- **금지**: 임의 token을 RETIRED claim으로 만드는 INACTIVE.
- **미결**: reject의 typed outcome과 client terminal/retry mapping.
- **증거**: A0 적대 #1 `REPRODUCED_POSTGRES`; 수정 후보 `MISSING`.
- **축별 기술 게이트**: `BLOCKED_BY_DECISION` — provisional 의미를 먼저 고정해야 reducer 시험 가능.

## 축 2 — token 소유권·이전·legacy 최초 claim

- **결정**: `PROPOSED`
- **제안**: legacy 전 writer를 같은 reducer/직렬화에 넣은 뒤 compare-and-claim. 첫 claim은 token을
  잠그고 current legacy owner를 확인하며, 다른 owner면 explicit transfer/drain 없이는 reject한다.
- **미결**: absent-row 의미, 합법적인 cross-owner transfer/rekey, 시작 generation.
- **증거**: A0 적대 #3 `REPRODUCED_POSTGRES`; 수정 후보 `MISSING`.
- **축별 기술 게이트**: `BLOCKED_BY_DECISION`; 축 7·12에 종속.

단일 conditional statement만으로는 부족하다. 현 legacy UPSERT가 reducer 밖에서 실행되면 claim 뒤
owner를 다시 덮을 수 있다.

## 축 3 — operation receipt·replay·GC

- **결정**: `OPEN`
- **후보**: `operation_id`를 인증 UID + installation + immutable normalized-request fingerprint에
  결속한다. 동일 ID/동일 fingerprint만 replay하고, 동일 ID/다른 payload는
  `OPERATION_ID_PAYLOAD_CONFLICT`로 거부한다. receipt insert/outcome과 state transition은 하나의
  transaction에서 commit한다. historical outcome/applied revision과 current state/revision은
  응답에서 명시적으로 분리한다.
- **미결**: receipt가 축 7 lock set에 포함되는지, 보존/GC, deletion/PITR 뒤 replay 응답.
- **증거**: A0 적대 #2·#5 `REPRODUCED_POSTGRES`; 수정 후보 `MISSING`.
- **축별 기술 게이트**: `BLOCKED_BY_DECISION`.

## 축 4 — client generation·server revision·rollback

- **결정**: `OPEN`
- **후보**: client가 임의 절대값을 쓰지 않고 `expected_current → exact successor`만 제안하며,
  server revision은 서버가 발급한다.
- **필수**: 양수 domain, jump 금지, overflow/exhaustion 규칙, rollback·restore 뒤 resync, reinstall 복구.
- **증거**: A0 적대 #7·#8 `REPRODUCED_POSTGRES`; 수정 후보 `MISSING`.
- **축별 기술 게이트**: `BLOCKED_BY_DECISION`.

양수 하한과 상한만 두면 client가 허용 최대값으로 한 번에 점프해 lockout을 다시 만들 수 있다.

## 축 5 — installation lineage·reinstall·rekey

- **결정**: `OPEN`
- **후보**: client lineage / server-issued lineage / 별도 account-incarnation 결속을 비교한다.
- **미결**: 다른 UID·새 lineage가 retired token을 합법적으로 회수하는 규칙.
- **증거**: A0 적대 #4 `REPRODUCED_POSTGRES`; Firebase 재설치 실측은 `PLANNED`지만 “토큰 재사용
  없음”의 상한 증거가 될 수 없다.
- **축별 기술 게이트**: `BLOCKED_BY_DECISION`.

## 축 6 — account deletion barrier

- **축 결정**: `OPEN`
- **현재 선호 가설**: HMAC tombstone, 단 아래 비용을 제품·운영이 수용하는 경우에만.

| 후보 | 결정 상태 | 현재 선호 | 요구/비용 |
|---|---|---|---|
| HMAC tombstone | `PROPOSED` | 조건부 우세 | 장기 가명정보, 삭제 UID 재사용 금지, key custody·PITR 권위 |
| active-UID parent | `OPEN` | 현재 비선호 | account lifecycle authority·incarnation proof 신설 |

### HMAC tombstone hard gates

```text
장기 가명정보 보존 · 삭제 UID 재사용 금지
domain-separated HMAC · key version · key custody/backup
복원 영역 밖 삭제 권위로 reconciliation, 아니면 완료까지 fail-closed
account-delete는 serialization point 아래 같은 tx에서 barrier 생성 + 기존 device row 삭제
REGISTER는 요청 UID의 barrier를 mutation 시점에 같은 tx에서 검사 + UPSERT
```

⚠️ key loss는 저장된 digest를 물리적으로 없애지는 않지만, 새 요청 UID를 같은 digest로 계산하지
못하게 해 **장벽 조회·집행을 불가능**하게 한다. unknown/missing key는 fail-closed여야 한다.

또한 `HMAC_K1(uid)`만으로 `HMAC_K2(uid)`를 계산할 수 없다. indefinite tombstone을 택하면 단순
`key version + dual-read`만으로 retirement가 완성되지 않는다. 다음 중 하나를 선택해야 한다.

```text
과거 matching key를 필요한 기간 보존
비회전 stable lookup secret/control ID 사용
가역 UID 자료 보존(개인정보 비용 큼)
UID가 다시 등장할 때만 lazy migration(완전 migration 아님)
```

### active-parent 성립 조건

```text
1 일반 register가 missing parent를 자동 생성하지 않음
2 신뢰 가능한 lifecycle 경로만 epoch 발급
3 UID 소지가 아닌 non-replayable account-incarnation proof
4 요청은 원래 incarnation/epoch에 이미 결속되고, mutation tx에서 primary의 active AND epoch=eN 검사
5 account-delete · parent 비활성 · device row 삭제 원자화
6 replica/cache 결과로 write 인가 금지
```

parent는 권위 있는 lifecycle을 다른 이유로 도입할 때뿐 아니라, tombstone 보존/key-lifecycle을
거부하고 그 대안 비용을 지불하기로 할 때도 재평가한다.

- **증거**: 현행 resurrection은 A0 §3 `REPRODUCED_POSTGRES`; 두 후보 검증은 `MISSING`.
- **축별 기술 게이트**: `BLOCKED_BY_DECISION`.

## 축 7 — UID·lineage·token 직렬화와 transaction 경계

- **축 결정**: `OPEN`
- **증거**: A0 raw-SQL global advisory lock 18/18은 `REPRODUCED_POSTGRES`의 primitive evidence.
  v3.2 후보 검증은 `MISSING`.
- **축별 기술 게이트**: short-global mechanism은 `READY_FOR_SPIKE`; architecture 선택은
  `PENDING_EVIDENCE`.

| 후보 | 결정 상태 | 강점 | 위험 |
|---|---|---|---|
| short global xact lock | `OPEN` | gap·multi-key ordering 단순화 | 처리량·pool starvation·긴 transaction |
| fine-grained ordered | `OPEN` | 더 높은 동시성 가능 | ABBA·dynamic discovery·gap·livelock·lock identity |

UID-only lock은 후보가 아니다. `device_token`은 전역 unique라 cross-UID 경쟁을 직렬화하지 못한다.

### short-global 성립 조건

```text
pg_advisory_xact_lock 계열의 transaction-scoped lock
collision-audited 전용 namespace/constant
fresh Session의 하나의 outer Session.begin 안에서 첫 관련 SQL/row lock보다 먼저 획득
register/delete 내부 commit 제거; row+heads+receipt 단일 outer commit
account-delete는 8계열 첫 DML 전에 획득
lock 보유 중 외부 network/auth/FCM await 금지
모든 purge caller의 선행 DML과 commit ownership을 재설계
```

현 purge는 caller가 transaction을 소유하고 일부 owner는 alert/log DML 뒤 cleanup을 호출한다. helper
안에서 늦게 global lock을 잡으면 순서가 깨지고, caller 입구로 올리면 unrelated work까지 직렬화할 수
있다. 이 경계 자체가 A0.6A의 hard gate다.

### fine-grained를 택할 때 필요한 것

```text
각 class 내부까지 canonical sorted total order
optimistic owner discovery → rollback/release → complete lock-set 획득 → revalidation
bounded retry + 명시적 CONTENTION_RETRY_EXHAUSTED 안전 종료
absent-row gap mechanism(advisory/sentinel/conflict-first 중 하나)
HMAC rotation과 독립적인 stable lock identity
```

### retention 제약 — `PROPOSED`, 아직 ACCEPTED 아님

```text
bounded candidate scan은 control lock 밖
candidate 하나마다 짧은 tx: canonical control lock → 재조회 →
captured uid+lineage+token+generation+server_revision exact CAS
active row를 control lock보다 먼저 잠그지 않음
다중 worker는 fair claim/lease 또는 bounded retry/rescan
SKIP LOCKED는 throughput 도구일 뿐 non-starvation 보장이 아님
```

## 축 8 — purge·retention exact CAS와 손상 복구

- **결정**: `PROPOSED`
- **제안**: captured `uid + installation/lineage + token + generation + server_revision` 전부 exact CAS.
- **미결**: active/lineage/token-head rowcount drift 또는 손상 시 rollback·repair·quarantine 절차.
- **증거**: A0 첫 후보와 적대 #6 `REPRODUCED_POSTGRES`; 수정 후보 `MISSING`.
- **축별 기술 게이트**: `BLOCKED_BY_DECISION`; 축 4·7·9에 종속.

## 축 9 — 물리 schema와 모든 writer 권한

- **결정**: `PROPOSED`
- **선호 후보**: 기존 `user_devices`를 active delivery row로 두고 신규 head/barrier/receipt table을 분리.
- **미결**: dual-write 없이 일관되게 통합 가능한지, reader cutover, DB privilege/fence.
- **증거**: writer inventory는 `SOURCE_PROVEN`; 후보 통합은 `MISSING`.
- **축별 기술 게이트**: `READY_FOR_SPIKE`.

통합 대상은 5개 mutation family(register, query DELETE, purge, retention, account-delete)이며 purge
commit owner만 6갈래다. one-shot·수동 DML도 cutover 전에 재inventory한다.

## 축 10 — HMAC·회전·개인정보·GC

- **결정**: `OPEN`
- **제안**: raw UID/무키 hash가 아니라 domain-separated HMAC + version. HMAC도 익명정보가 아니라
  가명·연결 가능한 control data다.
- **미결**: indefinite tombstone과 key retirement, key loss fail-closed, stable lock identity, restore epoch.
- **증거**: 설계 논증 `STRUCTURAL`; 후보 runtime evidence `MISSING`.
- **축별 기술 게이트**: `BLOCKED_BY_DECISION`; 축 6·7·11에 종속.

## 축 11 — backup·PITR·restore

- **결정**: `OPEN`
- **후보**: (a) 복원 영역 밖 삭제 권위/ledger를 재적용하거나, (b) reconciliation 완료까지 mutation과
  delivery를 fail-closed하거나, (c) 둘을 결합한다.
- **미결**: restore epoch, authoritative source, rehearsal/runbook, receipt/head 재조정.
- **증거**: 위험은 `STRUCTURAL`; restore drill `MISSING`.
- **축별 기술 게이트**: `BLOCKED_BY_DECISION`.

외부 ledger가 **유일한** 해법은 아니지만, 외부 삭제 권위도 없고 fail-closed도 아니면 PITR 뒤
barrier 소실을 막을 수 없다. 무기한 tombstone만으로는 과거 snapshot restore가 해결되지 않는다.

## 축 12 — API versioning·legacy compatibility·cutover·rollback

- **축 결정**: `OPEN`
- **현재 선호 가설**: 현재 단일 main-service topology에서 maintenance quiescence를 한 번 사용하고
  claim 뒤 roll-forward-only.
- **증거**: legacy contract는 `SOURCE_PROVEN`, mixed legacy/v3 실패는 A0 적대 #3
  `REPRODUCED_POSTGRES`; cutover 후보는 `MISSING`.
- **축별 기술 게이트**: state-machine mechanism은 `READY_FOR_SPIKE`; rollout은
  `BLOCKED_BY_DECISION`.

### versioned API 최소 계약

```text
request body
  operation_id
  installation_id
  client_generation
  desired_state
  ACTIVE   → device_token + platform
  INACTIVE → expected_server_revision 또는 exact current-token evidence

identity
  UID는 request body가 아니라 인증 snapshot에서만 파생

stable typed outcome
  APPLIED · IDEMPOTENT_REPLAY · STALE_REJECTED · GENERATION_CONFLICT ·
  TOKEN_CLAIM_CONFLICT · DELETION_BARRIER_REJECTED · LEGACY_FENCED ·
  OPERATION_ID_PAYLOAD_CONFLICT · CONTENTION_RETRY_EXHAUSTED
```

outcome은 축 1·2·3·4·5·6·7·8·12의 provisional semantics가 동결될 때 함께 확정한다.

### serialization 후보와 독립적인 admission/drain gate

HALT는 모든 token lock을 획득하는 방식으로 구현하지 않는다. 두 lock 후보의 protocol을 섞거나
shared→exclusive upgrade하지 않는다.

```text
short-global mutation  exclusive admission/serialization xact lock을 첫 lock으로 한 번만 획득
                       (shared 선취·upgrade 없음)
fine-grained mutation  shared admission xact lock을 먼저 잡고 transaction 끝까지 유지한 뒤
                       per-key ordered locks 획득
HALT/CLAIM transition  두 후보 모두 exclusive admission xact lock 아래 mode 변경
```

이 gate가 없으면 HALT 전에 ACTIVE를 읽은 요청이 token lock을 기다렸다가 HALT 뒤 mutate할 수 있다.
shared lock 두 개를 잡은 뒤 둘 다 exclusive로 upgrade하는 구현도 교착하므로 금지한다.

### 권위적인 cutover state machine

```text
1 dormant schema/control 설치; claim OFF
2 claim-OFF compatibility build 준비
   - 5개 mutation family 모두 reducer transaction 경계 사용
   - legacy POST/DELETE도 claimed token이면 old DML 전에 reject
   - unclaimed token은 검사+mutation을 하나의 token-serialized tx로 유지
3 one-shot/manual DML 동결 후 compatibility build 배포; deployment/rollback lock 시작
   - 이 경계에서 아직 claim은 OFF; crossing old write는 최종 legacy state에 포함
4 pre-compat process/backend 0 증명
   - 9가 commit될 때까지 구 image 재기동·재접속을 deployment lock + old credential revoke/
     connection admission block으로 계속 금지하거나 DB DML fence 유지
   - point-in-time census만으로는 불충분
5 모든 writer와 공통인 exclusive admission gate 아래 durable HALT commit
   - 모든 mutation은 최소 shared gate(short-global이면 같은 key의 exclusive)를 얻은 뒤
     같은 tx에서 HALT를 fresh-read
   - HALT 전에 queue됐어도 lock을 나중에 얻으면 mutation 0
6 mode cache가 있으면 recreate 후 exact halt-generation fresh-boot ACK
   - 기존 AtomicQuiesce* 기전을 재사용·보강
   - ACK 단독이 아니라 4의 backend/drain proof와 결합
   - 현재 선례처럼 scheduler 시작 뒤 ACK를 쓰는 결선을 그대로 복사하지 않음
   - D21 admission closure/HALT 관측은 모든 user-device writer 시작보다 앞섬
7 HALT 중 최종 legacy state를 LEGACY_UNORDERED로 backfill하고 old/new shadow 비교
8 v3 endpoint·barrier·reader self-check; 아직 mutation HALT
9 machine gate가 4의 **지속 중인 재진입 차단**과 5·6·7·8을 확인한 뒤 exclusive admission gate tx에서
  exact HALT control + open quiesce session/generation을 fresh-read하고,
  HALT → CLAIM_ENABLED 전환 + quiesce session consume를 원자 수행
10 token별 first-claim canary; current owner 확인 또는 explicit transfer/drain/rekey
11 reader cutover 후 old-table reader/direct writer 0 확인, 점진 확대
```

기존 `AtomicQuiesceSession/AppAck/confirm_quiesce_drained`는 5·6·9의 선례다. 그대로 충분하다고
간주하지 않고 D21 writer family, old backend 증명, backfill/shadow 조건을 추가한다. 현 helper는
control generation은 exact 비교하지만 ACK의 observed generation은 `>=`를 허용하므로, D21이 exact
ACK를 요구하면 helper/계약을 보강하고 별도 시험해야 한다.

iOS도 legacy query DELETE를 사용하므로 같은 compatibility fence를 적용한다
(`DeviceService.swift:128`). 플랫폼 차이를 security fence로 사용하지 않는다.

claim/barrier가 하나라도 생긴 뒤 구 binary가 control state를 무시할 수 있으므로 기본은
roll-forward-only다. 구 binary rollback이 필수라면 DB trigger/permission/stored-procedure 같은
영구 write fence가 필요하다.

---

## 3축 상태표

| 축 | 결정 | 후보 증거 | 축별 기술 게이트 |
|---|---|---|---|
| 1 desired/effective | `PROPOSED` | `MISSING` | `BLOCKED_BY_DECISION` |
| 2 ownership/first claim | `PROPOSED` | `MISSING` | `BLOCKED_BY_DECISION` |
| 3 receipt/replay | `OPEN` | `MISSING` | `BLOCKED_BY_DECISION` |
| 4 generation/revision | `OPEN` | `MISSING` | `BLOCKED_BY_DECISION` |
| 5 lineage/reinstall | `OPEN` | `PLANNED`(제한적 플랫폼 실측) | `BLOCKED_BY_DECISION` |
| 6 deletion barrier | `OPEN` | `MISSING` | `BLOCKED_BY_DECISION` |
| 7 serialization | `OPEN` | `OBSERVED` (primitive only, `REPRODUCED_POSTGRES`) | `READY_FOR_SPIKE` |
| 8 cleanup CAS | `PROPOSED` | `MISSING` | `BLOCKED_BY_DECISION` |
| 9 schema/writers | `PROPOSED` | `MISSING` (writer inventory만 `SOURCE_PROVEN`) | `READY_FOR_SPIKE` |
| 10 HMAC/privacy | `OPEN` | `MISSING` | `BLOCKED_BY_DECISION` |
| 11 PITR/restore | `OPEN` | `MISSING` | `BLOCKED_BY_DECISION` |
| 12 cutover/rollback | `OPEN` | `MISSING` | mechanism만 `READY_FOR_SPIKE` |

`A0-inherited` 반례 등급은 이 표에 덮어쓰지 않고 각 축 본문에 별도로 유지한다.
이 표는 축별 기술 준비도만 보인다. 현재 전역 실행 게이트 `BLOCKED_BY_HANDOFF`가
모든 `READY_FOR_SPIKE`를 override하며, `HANDOFF_COMPLETE` 후에도 별도 실행 GO가 필요하다.

## 사용자 결정과 spike 입력의 시점

다음 두 제품·운영 선택은 **D21 plan ACCEPT/실구현 전** 필요하지만 A0.6A와 A0.6Q의
protocol-only 반증을 시작하기 위한 선행 승인은 아니다. spike에서는 test-only provisional
assumption으로 명시한다.
이 문장은 **제품 결정 선행 여부**만 다룬다. 현재 `BLOCKED_BY_HANDOFF`를 해제하거나
실행 GO를 대체하지 않는다.

```text
축 6   삭제 UID 재사용 금지와 장기 HMAC 장벽/key-custody 운영 비용을 수용할지
축 12  사전 계획·측정된 유계 maintenance quiescence와 first claim 뒤 roll-forward-only를 수용할지
```

key loss로 장벽 집행이 사라지는 위험을 “수용”하는 선택지는 두지 않는다. 운영이 수용하는 것은
fail-closed key custody/backup 부담이다.

축 7의 최종 global/fine-grained 선택은 A0.6C와 필요 시 D 뒤의 기술 결정이다. 사용자는 A0.6C
결과를 보기 **전에** 허용 지연, 처리량, 오류율, bounded-completion 기준을 고정한다.

---

# 다음 검증 — A0.6을 단계별로 분리한다

A0.6은 transaction 기전, cutover 경계, reducer 의미, 성능을 하나의 PASS/FAIL로 합치지 않는다.

## A0.6A — SQLAlchemy transaction/short-global feasibility

- **축별 기술 게이트**: `READY_FOR_SPIKE`.
- **현재 전역 실행 게이트**: `BLOCKED_BY_HANDOFF`. `HANDOFF_COMPLETE` 후 별도 실행 GO를
  받아야 착수한다.
- **목적**: 제품 의미를 승인하지 않고 실제 mapped ORM/service 경계에서 short-global transaction이
  성립하는지 반증 시도한다.
- **환경**: pinned server SHA의 `/private/tmp` 격리 복사본, lockfile의
  SQLAlchemy 2.0.49/psycopg 3.3.3, `postgres:17-alpine` 로컬 image ID
  `sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73`,
  production pool shape(`pool_size=3`, `max_overflow=2`, online `pool_timeout=10s`,
  statement timeout 60s). isolation은 시작 시 `SHOW transaction_isolation`로 고정·기록한다.
  production credential, data, network, repo 파일 변경 없음. `PYTHONDONTWRITEBYTECODE=1` +
  `python -B`를 쓰고 시작/종료 source manifest를 대조한다. 외부 pull/install은 금지하며 로컬
  image/dependency가 없으면 `BLOCKED_BY_INPUT`으로 종료한다.
- **사전 manifest**: 5 mutation family와 6 purge commit owner의 현재
  `DML → lock → commit` ownership을 고정한다.

실험:

```text
1 fresh Session의 outer Session.begin에서 collision-audited 전용 constant의
  pg_advisory_xact_lock을 첫 application SQL로 획득
2 mapped candidate model/service를 실제 사용해 flush·rollback·identity-map·autoflush=False·
  exception translation·txid 연속성 확인
3 register/delete inner commit 제거와 row+heads+receipt 단일 commit prototype
4 purge 6 owner의 선행 alert/log DML보다 먼저 lock을 올릴지, cleanup을 fresh isolated tx로 분리할지 비교
  - isolated 안은 notification commit→purge 미실행 crash, notification commit 뒤 purge 실패를 시험
  - purge가 notification commit보다 앞서지 않음을 증명
5 retention candidate-per-tx, account-delete first-of-8-DML-before-lock 금지
6 separate process/session의 mutual exclusion, absent-row create, exception after each DML,
  caller timeout, SIGTERM/SIGKILL, DB backend termination, COMMIT-ack loss
  - A 단계의 COMMIT-ack loss oracle은 committed/rolled-back DB state, lock release,
    MAY_HAVE_COMMITTED 구분까지. safe replay 의미는 축 3 동결 뒤 B에서 검증
7 rollback·lock release·pool recovery와 explicit bypass-writer positive control
8 lock holder가 statement 사이에서 멈추는 양성 대조; lock_timeout/PG17 transaction_timeout 후보 비교
9 pool 3+2에서 mutation burst 중 unrelated read probe로 pool starvation 측정
10 모든 waiter가 사전 고정된 시간 안에 완료하거나 typed lock-timeout으로 안전 종료하는지 검사
```

단순 `Session.execute(text(raw_sql))`로 A0 raw harness를 감싸는 것은 ORM integration evidence가
아니다. 그 범위는 `OBSERVED (SQLALCHEMY_DRIVER_ONLY)`다. mapped prototype만 직접 부르면
`OBSERVED (ORM_PROTOTYPE_ONLY)`다. integration을 주장하려면 temp worktree의 실제 register,
query DELETE, retention, account-delete, purge 6 owner 진입 경로를 모두 실행하고(Firebase 같은 외부
경계만 mock), SQL trace로 first-lock·single-tx·no-inner-commit을 확인해야 한다.

증거는 deterministic DB barrier, SQL trace/Session event, `pg_locks`/txid, raw stdout·stderr·exit code,
source/schema/image/dependency hash와 cleanup manifest로 남긴다.

판정:

```text
MECHANISM_REPRODUCED · INTEGRATION_BLOCKED · MECHANISM_FAILED
성능 수치 = OBSERVED (LOCAL_PERF_ONLY), 또는 미완이면 INCONCLUSIVE (LOCAL_PERF_ONLY)
```

이 단계는 architecture PASS나 fine-grained 불요를 선언하지 않는다.

`MECHANISM_REPRODUCED`는 **실제 진입 경로 전부**가 first-lock/single-tx를 지키고, rollback·kill·
timeout·bypass positive control·bounded completion을 모두 통과한 경우에만 준다. 기존 caller
transaction 의미를 보존하지 못하거나 한 경로라도 통합할 수 없으면 `INTEGRATION_BLOCKED`다.

## A0.6Q — compatibility cutover/quiescence proof

- **축별 기술 게이트**: protocol-only harness는 `READY_FOR_SPIKE`; integrated boundary 판정은
  A0.6A `MECHANISM_REPRODUCED`에 종속.
- **현재 전역 실행 게이트**: `BLOCKED_BY_HANDOFF`. `HANDOFF_COMPLETE` 후 별도 실행 GO를
  받아야 protocol-only harness도 착수한다.
- **목적**: 축 12의 state machine과 기존 AtomicQuiesce* 재사용 가능성을 실제 PostgreSQL 17,
  SQLAlchemy, subprocess/container 종료 경계에서 검증한다.
- **필수 case**: graceful stop, SIGKILL, pre-HALT queued request, old backend 잔존, one-shot writer,
  HALT generation mismatch, fresh-boot ACK 부재, cache refresh/scheduler start와 ACK 사이 writer 발화,
  zero census 뒤 pre-compat writer 재기동, shared→exclusive upgrader 2개, final backfill 뒤 late commit,
  control-transition COMMIT response loss와 idempotent 재시도.

protocol-only/mock state machine은 `QUIESCENCE_PROTOCOL_REPRODUCED_LOCAL`까지만 줄 수 있다.
`QUIESCENCE_BOUNDARY_REPRODUCED_LOCAL`은 A0.6A에서 살아남은 동일 admission/transaction gate를 사용하고
다음 oracle을 전부 만족해야 한다.

```text
모든 negative case에서 CLAIM_ENABLED 진입 불가
경계 전에 commit된 write는 최종 LEGACY_UNORDERED backfill에 포함
activation 뒤 old/pre-HALT transaction commit 0
control state·exact generation·quiesce session·ACK·backend/drain evidence 중
  하나라도 부재/불일치면 fail-closed
activation tx가 exact HALT를 fresh-read하고 session consume와 mode 전환을 원자 수행
raw stdout·stderr·exit code·pg_stat_activity·tx/lock trace·manifest 보존
```

- **판정**: `QUIESCENCE_PROTOCOL_REPRODUCED_LOCAL · QUIESCENCE_BOUNDARY_REPRODUCED_LOCAL ·
  DRAIN_PROOF_INSUFFICIENT · CUTOVER_FAILED`.

A0 §5.3 late-commit 양성 대조를 process-kill 증거로 재사용하지 않는다. local boundary 재현 뒤에도
실제 current/staging topology rehearsal은 `CURRENT_STACK_E2E_UNPROVEN`이며 operational readiness는
`PENDING_EVIDENCE`다.

## A0.6B — provisional reducer regression

- **축별 기술 게이트**: `BLOCKED_BY_DECISION`
- **선행**: 축 1·2·3·4·5·6·8·12의 expected semantics를 **test-only provisional contract**로 동결하고,
  A0.6A/Q에서 축 7·9의 provisional mechanism/boundary를 고정.
- **실행**: 실제 SQLAlchemy candidate에서 18 base + 8 adversarial 재실행.

재실행 전에 A0가 놓친 oracle부터 고친다.

```text
active ↔ lineage ↔ token-head의 exact bijection
owner · token · generation · server_revision · desired/effective · claim state · rowcount
barrier 존재 ⇒ active/device row 0
receipt historical outcome ⊥ current state/revision
```

각 field/row를 일부러 훼손하는 positive control이 반드시 oracle 실패를 일으켜야 한다.

적대 8개 중 #1·#2·#4·#5·#7·#8은 lock 판별 사례가 아니라 의미 계약 사례다. #3·#6만
serialization/invariant에 직접 닿고, #3은 축 2·12, #6은 축 8의 provisional contract가 필요하다.

판정은 다음처럼 분리한다.

```text
CONDITIONAL_ON_PROVISIONAL_CONTRACT
  26 case가 provisional expected outcome/state와 일치하고, 모든 corruption positive control이
  oracle failure를 일으킴
REDUCER_FAILED
  harness self-test는 유효하지만 하나 이상의 semantic outcome/state가 불일치함;
  최소 interleaving과 state diff를 보존
판정 없음 · evidence INCONCLUSIVE
  setup/non-determinism 또는 positive-control miss; harness를 고친 뒤 재실행
```

어느 성공 판정도 사용자 `ACCEPTED`나 production PASS가 아니다.

## A0.6C — topology/workload selection

- **축별 기술 게이트**: `BLOCKED_BY_INPUT`
- **선행**: 운영 mutation 빈도·burst·per-user deletion cardinality·purge batch와 사용자 SLO를
  결과 보기 전에 고정.
- **측정**: (a) 같은 candidate DDL/DML과 전부 다른 token의 workload에서 **global lock만 on/off**한
  쌍으로 순수 false-serialization 비용을 분리하고, (b) 사전 측정한 same-token/cross-token 혼합·
  cardinality·burst를 함께 재생한다. p50/p95/p99/max transaction·lock·pool wait, throughput,
  timeout/error, bounded-completion 실패율, unrelated-read latency를 잰다. 현 production 구현은 별도
  descriptive baseline으로만 적어 reducer refactor 비용을 lock 비용으로 오인하지 않는다.
- **환경 등급**: local disposable PG는 상대 비교와 mechanism evidence만 제공한다. 운영 RDS SLO를
  통과시킬 수 없으며 staging-like evidence가 별도로 필요하다.

판정:

```text
로컬        LOCAL_CONTENTION_PROFILE_OBSERVED · PERF_EVIDENCE_PENDING
staging-like SHORT_GLOBAL_SUITABLE_FOR_PINNED_PROFILE · NEEDS_FINE_GRAINED_EVALUATION
```

단일 실패를 semantic·wiring·harness·failure-recovery·contention으로 분류하고, **정확성 통과 뒤
contention/SLO 실패만** fine-grained 진입 근거로 쓴다. A0.6B가 실패하면 reducer 의미/구현을
고치는 것이지 lock granularity를 바꾸는 것이 아니다.

유한 benchmark가 starvation 부재를 증명하지는 않는다. A0.6A의 bounded completion 또는 typed
timeout은 안전 hard gate이고, C는 그 timeout 빈도와 예산 적합성을 측정한다.

`NEEDS_FINE_GRAINED_EVALUATION`은 fine-grained 선택이 아니다. 그 경우에만 별도 **A0.6D —
fine-grained mechanism/performance spike**를 설계하고 같은 안전·SLO 기준으로 비교한다.

## 실행 순서

```text
H0~H5 → HANDOFF_COMPLETE      현재 필수 선행
A0.6A + A0.6Q protocol-only   별도 실행 GO 후, 제품 의미는 test-only provisional hypothesis로 둠
A0.6A mechanism 통과
A0.6Q integrated boundary
임시 의미 계약 동결             축 1·2·3·4·5·6·8·12
A0.6B                  전체 reducer 회귀
workload/SLO 사전 고정
A0.6C                  pinned profile에서 short-global 적합성 판정
A0.6D                  C가 요구할 때만 fine-grained 비교
```

현재 **필수 선행**은 v3.2 자체 승인이나 D21 구현이 아니다. 목적지 승인을 기다리지 않고
H0/H1과 필요 시 HP를 먼저 수행한다. H1_PASS 뒤 H2에서 영속 목적지를 승인하고 H3~H5로
`HANDOFF_COMPLETE`를 만든 뒤, A0.6A와 A0.6Q protocol-only 실행을 별도 GO로 판단한다.

---

# 임시 산출물 영속 이관과 삭제 gate

이 문서와 관련 증거에 대해 **승인된 독립 재사용 가능 정본/standalone evidence
copy**가 현재 확인되지 않았다. 세션 JSONL·workflow 기록에 내용 조각이 남아 있을 수는
있지만, 이를 재사용 가능한 독립 사본으로 계수하지 않는다.

`A0.6` 완료를 기다렸다가 이관하지 않는다. **A0.6A/Q 실행이나 장기 중단보다 먼저**
영속 이관을 완료해야 한다. 승인된 목적지가 아직 없으므로 handoff lifecycle은
`HANDOFF_BLOCKED_BY_DESTINATION`이고, 현재 전역 실행 게이트는 `BLOCKED_BY_HANDOFF`다.
이 상태에서 A0.6 실행과 cleanup은 보류한다.

**응급 HP는 H0/H1의 완료를 기다리지 않는다.** H0/H1은 정본 이관을 위한 분류·감사 gate이고,
임시 손실을 막는 private byte copy의 선행조건이 아니다. 목적지 승인 전에는 read-only `P0`
preflight만 할 수 있고, 승인을 우회하는 zero-approval write/copy는 의도적으로 없다.

이 절의 “read-only”와 “어떤 write 전”은 source·destination·evidence namespace에 대한
task-owned create/write/rename/link/unlink/chmod/chgrp/xattr 같은 **의도적 mutation 전**이라는
뜻이다. C1/C2의 read가 filesystem policy에 따라 atime을 올릴 수 있는 현상과 고정 schema의
control stdout은 payload write로 세지 않으며, atime은 아래 lifetime 규칙으로 따로 결속한다.
stdout은 `ACCESS_CONTROLLED`이고 top-level exact root·aggregate·anomaly만 내보내며 per-entry
content나 불필요한 metadata를 출력하지 않는다.

### P0 verifier bootstrap — on-disk producer가 없는 기본 경로

P0 fd-hasher를 먼저 파일로 만들 필요는 없다. **첫 candidate 실행 전에** exact bootstrap UTF-8
bytes와 exact fd-hasher UTF-8 bytes를 read-only static review한다. reviewer는 digest만 보거나
자기 선언으로 PASS하지 않고, 검토한 exact source bytes와 schema/runtime closure를 byte-for-byte
열람해야 한다. 사용자에게도 같은 turn의 canonical UTF-8/base64 source block 또는 byte-identical
retrieval block, reviewer·verdict·두 source SHA-256·runtime trust boundary를 함께 제시한다.
source 원문 없이 digest와 verdict만 있는 `FD_HASHER_REVIEW`는 유효하지 않다.
그 exact fd-hasher source는 length-framed/base64 single argv data element로 전달하고, 검토된
작은 bootstrap이 length와 digest를 확인한 뒤 memory에서만 compile/execute한다.

```text
ARGV_BLOB = b"P0A1 FD_HASHER_SOURCE " + DEC_LEN + b" " + LOWER_SHA256 + b" " +
            RFC4648_PADDED_BASE64_NO_WHITESPACE
```

아래 `P0F1`과 같은 decoded-length/minimal-decimal/lowercase-hex/RFC-4648 규칙을 쓰되 argv
element라 terminal LF를 넣지 않고 NUL·CR·LF·추가 field/byte를 거부한다. bootstrap은 받은
`sys.argv` element의 ASCII bytes 자체를 먼저 검사한 뒤에만 source를 decode/compile한다.

interpreted 기본안은 selector가 아닌 실제 interpreter executable을 직접 invoke해 지원이 확인된
`-I -S -B`, fixed cwd, allowlist한 clean environment/locale, deterministic raw-path ordering과
canonical serialization을 사용한다. `/usr/bin/python3` 같은 selector/shim을 거치면 shim과 실제로
선택된 executable을 모두 결속한다. import한 non-builtin module·native library·helper closure도
digest/platform/ABI trust boundary에 포함한다. macOS dyld shared cache 안의 native image에는
존재하지 않는 standalone dylib path/SHA-256을 요구하지 않는다. 그 경우 fresh OS product/version/
build·kernel·architecture, dyld shared-cache UUID, image install-name/UUID, exact ctypes ABI
signature·상수와 on-disk Python/`_ctypes` closure digest를 함께 결속한다. fd 기반 security metadata는
`ctypes.CDLL(None)`의 `flistxattr`/`fgetxattr`와 `acl_get_fd_np`/`acl_to_text`/`acl_free`를 사용하며,
resource fork는 `com.apple.ResourceFork` xattr로 같은 open-fd/no-follow 경계에서 수집한다.
admission phase에는 temp source/binary, heredoc,
process substitution, pycache, subprocess/network와 task-owned filesystem output을 금지한다.
exact GO response 뒤에는 static-review된 process가 admission packet에 realpath·dev/inode·content
digest가 결속된 `/bin/cp`를 exact argv로 호출하고 승인된 publish/fsync write만 수행할 수 있으며,
그 밖의 subprocess와 network는 계속 0이다. exact argv byte transport와
`ARG_MAX` 여유, runtime/shared-cache closure 또는 exact source의 process-restart 재현성 중 하나라도
증명하지 못하면 inline으로 우회하지 않고, 위 local-only/private gate를 통과한 fresh non-temp
verifier root에 대한 별도 exact `P0_VERIFIER_BOOTSTRAP_GO`를 먼저 받는다. 그 GO는 HP copy를
허가하지 않으며 `/private/tmp` 안에 유일 verifier를 만들지 않는다. 단순히 ARG_MAX가 크다는
사실만으로 file-backed fallback을 닫지 않는다.

이 bootstrap이 만드는 control object는 다음 canonical encoding을 공통 사용한다.

```text
UTF-8 JSON object · ASCII schema key · duplicate key/float/NaN/Infinity 금지
object key는 UTF-8 byte 순서로 재귀 정렬 · insignificant whitespace와 trailing LF 없음
integer는 최소 decimal · boolean/null은 JSON literal · raw filesystem path bytes는 base64로 표현
array는 schema가 지정한 raw-byte canonical order를 유지
```

`FD_HASHER_REVIEW`·`P0_ADMISSION_PACKET`·`P0_TTY_ATTESTATION`·`P0_TRANSPORT_BINDING`·
`P0_APPROVAL_RESPONSE`·`P0_SNAPSHOT` object는 자기 digest field를 갖지 않는다.
각 `*_SHA256`은 **완성 object의 exact canonical bytes만** SHA-256하고 별도 control value/file로
둔다. review object는 reviewer/verdict, bootstrap/hasher exact source digest와 runtime closure를
포함하고, admission packet은 review digest를 포함한다. 이 규칙과 exact schema/version 자체도
static review 대상이다.

`P0-admission`의 full per-entry manifest와 최초 L0 lifetime table은 **동일한 장기 실행 P0
process의 immutable memory**에 둔다. process는 cryptographic session nonce·PID·process start
identity·exact executable/runtime digest를 포함한 `P0_SESSION_ID`를 만들고 다음을 담은 canonical
`P0_ADMISSION_PACKET`과 그 SHA-256을 memory에서 완성한다.

```text
exact top-level root 목록 · root/entry count와 bytes
full per-entry baseline digest · admission lifetime digest
observed (source_uid,source_gid,count,entry_set_digest) row 전부
total ownership map의 모든 source pair -> destination pair row
fresh numeric group-set 값 전부와 그 digest
anomaly · destination verdict/evidence digest · verifier/review/runtime digest · exact copier binding
P0_SESSION_ID · fixed schema/version
```

Codex 실행에서는 verifier를 `exec_command(tty=true, login=false)`의 foreground process로 띄우고,
tool이 돌려준 opaque `EXEC_SESSION_ID`를 버리지 않는다. process는 packet frame을 내기 전에
reviewed `termios` 경로로 자기 PTY를 **byte-transparent raw control mode**로 바꾼다.

```text
c_iflag = 0
c_oflag = 0
c_lflag = 0
c_cflag = (previous_cflag & ~(CSIZE | PARENB)) | CS8
cc[VMIN] = 1 · cc[VTIME] = 0
```

이는 `ICANON`/`ISIG`/`IEXTEN`, echo, CR/LF·strip·flow-control 입력 변환과 output post-processing을
모두 끈다. `tcsetattr(TCSANOW)` 직후 `tcgetattr`가 exact expected masks와 VMIN/VTIME을 돌려주지
않으면 packet을 내지 않고 `BLOCKED_BY_INPUT`이다. process는 actual fd identity·exact termios
state, frame limits, `P0_SESSION_ID`와 `P0_ADMISSION_PACKET_SHA256`을 canonical
`P0_TTY_ATTESTATION`으로 만들고 digest한다. 그 뒤 packet frame과 attestation frame을 순서대로
flush하고, 같은 actual termios state를 다시 확인한 뒤 전용 control stdin에서 멈춘다.

operator만 tool session handle을 안다. operator는 반환된 `EXEC_SESSION_ID`와 process가 낸 exact
attestation을 다음 canonical `P0_TRANSPORT_BINDING`으로 합성해 packet과 함께 사용자에게 제시한다.

```text
EXEC_SESSION_ID · P0_SESSION_ID · P0_ADMISSION_PACKET_SHA256
exact launch argv/cwd/allowlisted-environment digest · tty=true · login=false
exact P0_TTY_ATTESTATION bytes · P0_TTY_ATTESTATION_SHA256
packet/attestation frame 수신·digest 검증 완료 marker
control_wait_deadline(null 또는 exact absolute time)
```

`P0_TRANSPORT_BINDING_SHA256`은 이 exact canonical bytes의 digest다. Codex tool contract의
`write_stdin(EXEC_SESSION_ID, ...)`가 기존 unified exec session의 stdin에 후속 tool call의 bytes를
전달하므로, 정상적인 user-turn 경계 자체를 EOF로 간주하지 않는다. 현재 tool의 read-only 양성
대조에서는 `tty=false`가 stdin EOF를 즉시 돌려준 반면 `tty=true`는 session id를 유지하고
`write_stdin` 입력을 같은 process에 전달했다. 따라서 PTY는 control object의 저장 형식이 아니라
운반층일 뿐이다. packet·attestation·binding·response의 exact canonical bytes는 모두 다음 단일 ASCII frame으로
보내고, 수신자는 base64 decode 뒤 길이와 digest를 검증한다.

```text
FRAME = b"P0F1 " + KIND + b" " + DEC_LEN + b" " + LOWER_SHA256 + b" " +
        RFC4648_PADDED_BASE64_NO_WHITESPACE + b"\n"
KIND  = P0_ADMISSION_PACKET | P0_TTY_ATTESTATION |
        P0_TRANSPORT_BINDING | P0_APPROVAL_RESPONSE
```

`DEC_LEN`은 decoded canonical object byte 수의 최소 ASCII decimal이며 0 자체 외 leading zero를
금지한다. `LOWER_SHA256`은 decoded bytes의 lowercase hex 64자다. base64는 RFC 4648 canonical
padding을 사용하고 whitespace를 포함하지 않는다. parser는 KIND allowlist, exact field 수, 길이,
digest, padding과 terminal LF를 확인하고 embedded CR/LF, terminal LF 뒤 byte, noncanonical encoding,
중복 frame을 모두 거부한다. 수신 parser는 자기 stdin에서 읽은 원래 LF-terminated frame만
처리한다. control frame과 session handle은 payload가 아니지만 `ACCESS_CONTROLLED`로 다룬다.

```text
MAX_DECODED_BYTES
  P0_ADMISSION_PACKET   = 262144
  P0_TTY_ATTESTATION    = 32768
  P0_TRANSPORT_BINDING  = 65536
  P0_APPROVAL_RESPONSE  = 131072
MAX_FRAME_BYTES(kind) = exact ASCII header bytes + 4*ceil(MAX_DECODED_BYTES(kind)/3) + 1 LF
```

정적 검토된 parser는 raw-mode fd에서 fixed-size chunk로 읽되 KIND를 식별한 즉시 그 KIND의
`MAX_FRAME_BYTES+1`보다 더 축적하지 않는다. 상한 초과, deadline/EOF 전 terminal LF 부재,
terminal LF와 같은 read에 붙은 추가 byte, 두 번째 frame, partial/invalid base64는 즉시
fail-closed다. 승인 response는 한 frame만 허용하며, schema의 실제 decoded/frame 길이가 위 상한을
넘으면 잘라 보내지 않고 `BLOCKED_BY_INPUT`으로 새 schema/GO를 요구한다.
FIFO·Unix socket·`/proc`·
`nohup` fallback은 만들지 않는다. 후속 turn에 empty-input poll로 exact session handle이 여전히
열려 있음을 확인할 수 없거나 handle이 unknown/closed이면 응답을 다른 process에 보내지 않고
admission을 폐기한다. 이 session transport를 실행 환경이 보장하지 못하면 live-memory P0를
시작하지 않고 `BLOCKED_BY_INPUT`으로 남겨 별도 persistent-control-plane 설계와 exact GO를 받는다.

사용자가 수천 normal entry를 한 줄씩 수동 승인하는 절차는 아니지만, ownership class/map/group
값은 digest만이 아니라 실제 row/value도 보아야 한다. per-entry content/metadata는 출력하지 않고
full manifest digest로 결속한다. process가 살아 있는 동안 admission manifest/lifetime/source-review
bytes를 바꾸거나 재구성하지 않는다.

operator는 아래 필드를 모두 담은 canonical `P0_APPROVAL_RESPONSE` object와
`P0_APPROVAL_RESPONSE_SHA256`, 완성 `P0F1` frame을 먼저 만들고 exact packet·attestation·
session·transport binding과 함께 사용자에게 제시한다.

```text
P0_SESSION_ID · P0_ADMISSION_PACKET_SHA256 · FD_HASHER_REVIEW_SHA256
OWNERSHIP_MAP_SHA256 · NUMERIC_GROUP_SET_SHA256
exact canonical P0_TRANSPORT_BINDING bytes · P0_TRANSPORT_BINDING_SHA256
아래 emergency HP copy GO의 entire exact authorization scope
  (source roots/baseline · destination identity/leaf/mapping · event/staging prefix ·
   allowlisted create/write/link/unlink/fsync/copy operations · explicit exclusions ·
   PRESERVATION_ONLY_NO_OWNERSHIP_DISPOSITION clause와 expiry/guard)
decision=APPROVE_EXACT_GO · fixed schema/version
```

사용자가 이 exact response bytes와 `P0_APPROVAL_RESPONSE_SHA256`까지 승인한 뒤에만 operator가
같은 `EXEC_SESSION_ID`에 `write_stdin`으로 그 완성 frame을 byte-for-byte 전달한다. process는
`P0_SESSION_ID`·`P0_ADMISSION_PACKET_SHA256`·`FD_HASHER_REVIEW_SHA256`·
`OWNERSHIP_MAP_SHA256`·`NUMERIC_GROUP_SET_SHA256`·exact canonical `P0_TRANSPORT_BINDING`
bytes와 `P0_TRANSPORT_BINDING_SHA256` 및 frame의 decoded length/digest를 검증한다. response를
읽기 직전과 완전한 frame을 받은 직후 `tcgetattr`로 actual raw
termios state를 `P0_TTY_ATTESTATION`과 다시 비교한다. binding bytes의 digest, 포함된 exact
attestation bytes/digest, 내부 session/packet 필드와 authorization scope의 baseline/destination/
operation predicate까지 memory admission 및 execution recheck와 모두 일치해야 final snapshot에 exact
attestation·binding·approval-response bytes와 각 digest를 포함한다. user reply 자체를 자동으로 process
stdin에 연결하지 않고, operator가 exact GO와 binding을 검증한 뒤 relay한다. EOF, 명시된
`control_wait_deadline` 만료, process restart, PID/start/session mismatch,
response mismatch 또는 memory-state loss면 **아무것도 쓰지 않고** 그 admission을 폐기해 새
process/P0부터 다시 한다. 승인 뒤에도 같은 process가 execution L0/C1/C2와 destination recheck를
수행한다. 이 continuity가 없으면 “같은 verifier를 다시 실행해 재구성”하지 않는다.

execution recheck가 통과하면 같은 process가 final `P0_SNAPSHOT` canonical bytes를 memory에서
완성·freeze하고 `P0_SNAPSHOT_SHA256`과 아래 exact `P0_SHA256` envelope bytes를 계산한 **뒤에**
`HP_ATTEMPT_STARTED`를 publish한다. 그 뒤 fresh root에서 no-replace로 P0_SNAPSHOT/P0_SHA256,
payload, HP_MANIFEST/HP_SHA256을 만든다. exact admission/review/verifier source와
`P0_TRANSPORT_BINDING`·`P0_APPROVAL_RESPONSE` bytes와 각 digest는 P0_SNAPSHOT에 담고,
HP_MANIFEST는 그 object/envelope digest를
결속한다. persisted helper나
H2/H4/H5는 이 무파일 bootstrap의 선행조건이 아니다.

`P0-admission`은 위 task-owned mutation보다 먼저 L0를 고정한 뒤 이미 static-review된 exact
fd-hasher로 C1/C2까지 read-only 수행한다. exact source-root path 목록·각 root identity·root/entry
count/bytes뿐 아니라 모든 entry의 path/set/type/dev/inode/uid/gid/mode/flags/size/mtime/ctime/
security-metadata/SHA-256을 결속한 **full source-universe baseline digest**, destination 후보의 아래
local-only/private 증거, 실행할 fd-hasher의 exact source/review/command/runtime/environment digest를
사용자에게 제시한다. `PYTHONPATH`·`sitecustomize`·user-site 또는 ambient shell 설정이 의미를
바꾸게 두지 않는다. compiled verifier이면 binary digest·platform/ABI와 exact argv를 같은 수준으로
결속한다. 실행 직전 이 전부를 재검증한다.

P0는 observed numeric ownership class마다
`(source_uid, source_gid, count, entry_set_digest)`를 만들고, identity row까지 포함한 **total exact
`(source_uid,source_gid) -> (destination_uid,destination_gid)` map**을 제안한다. source uid가 current
uid가 아니면 `OWNER_ACTION_REQUIRED`; destination uid는 current uid이고 destination gid는 fresh
numeric group-set의 member여야 한다. 특정 시점의 `0 -> 20` 같은 값을 영구 규칙으로 박지 않는다.
fresh P0가 실제 class와 current numeric group-set을 도출하고 `OWNERSHIP_MAP_SHA256`과
`NUMERIC_GROUP_SET_SHA256`으로 결속한다.

사용자는 이 **이미 본** full baseline digest와 exact root path 목록, exact parent dev/inode,
absent root leaf, 두 attempt sibling prefix, **각 canonical event publish마다 publish·parent-fsync된
canonical과 dev+inode+hash가 모두 같은 해당 publish staging hard-link 정확히 하나의 unlink만**
묶는다. noncanonical·pre-existing·identity/hash 불일치 residue의 unlink는 허가하지 않는다. 이와
`P0_SESSION_ID`·`P0_ADMISSION_PACKET_SHA256`·`FD_HASHER_REVIEW_SHA256`·`OWNERSHIP_MAP_SHA256`·
`NUMERIC_GROUP_SET_SHA256`·`P0_TRANSPORT_BINDING_SHA256`을 묶은 exact authorization scope가
위 `P0_APPROVAL_RESPONSE.authorization`이다. response object는 자기
`P0_APPROVAL_RESPONSE_SHA256`을 내부에 넣지 않는다. operator가 exact response bytes와 digest를
제시하면 사용자는 **객체 밖의 승인 발화에서** 그 `P0_APPROVAL_RESPONSE_SHA256`을 명시해
`emergency HP copy GO`를 준다. response 밖의 요약 문구나 operator 재해석은 권한을 넓히지 않는다.
“실행할 때 P0가 찾는 모든 것”이나
wildcard/count만 승인 대상으로 쓰지 않는다. exact root list에 `UNRESOLVED` root가 있으면 같은 GO
안에서도 `PRESERVATION_ONLY_NO_OWNERSHIP_DISPOSITION` clause로 그 root와 digest를 **따로** 승인해야
한다. 경로가 목록에 우연히 들어간 것만으로 아래의 별도 보존 승인을 충족하지 않는다.

승인 뒤 실행 직전 L0→C1/C2를 다시 수행해 full baseline digest, source root set, verifier/review,
ownership class/map, numeric group-set 또는 destination evidence가 다르면 **첫 write 전에** 중단하고
새 packet/GO를 받는다.

admission L0의 regular-file `a0`와 entry별 cleaner boundary, `T_min/T_guard`는 별도
`admission_lifetime_digest`로 보존해 승인 packet과 GO에 결속한다. C1/C2 자체가 atime을 올릴 수
있으므로 atime은 source-stability full baseline equality key에 넣지 않는다. `P0-execution`은 첫
write 전에 새 L0와 `execution_lifetime_digest`를 채취하되, 각 entry와 전체 universe의 실제 보존
경계는 `effective_guard=min(admission_guard, execution_guard)`로 고정한다. admission 뒤 read로
늦춰진 atime이나 더 늦은 execution guard가 최초 경계를 소급 연장하지 못한다.

그 exact GO 안에서만 `P0-execution` → copy → 전수 검증 → HP envelope 생성을 수행한다. 이 GO는
H1 disclosure 승인, H2 destination 승인, H4/H5, cleanup GO를 대신하지 않으며 process signal,
VM suspend/terminate, TCC 설정 변경, evidence chmod/chgrp/xattr 삭제도 허가하지 않는다. 별도
producer pause가 필요하면 exact process/resource와 reversible action을 묶은 별도 GO를 먼저 받는다.

문서 작성 세션이 source universe 안의 파일을 계속 바꾸면 그 세션도 producer다. 최종 문서 편집과
static review가 끝난 뒤 더 이상 write admission이 없고 open writer 0임을 확인하면 별도 pause는
필요하지 않다. 그 상태를 닫을 수 없을 때만 위 별도 pause GO 또는 `OWNER_ACTION_REQUIRED`다.

**Pre-H1 local-only/private destination gate.** H1_PASS 전 TEMP_REQUIRED와 lifecycle provenance는
`UNREVIEWED/ACCESS_CONTROLLED`이므로 HP parent/root/두 attempt sibling prefix와
`LIFECYCLE_OUTPUT_ROOT`는 current-user·0700·non-temp일 뿐 아니라 **자동 외부 전송, File Provider
관리, cloud/backup sync와 local-byte eviction 대상이 아닌 local-only 경로**여야 한다. 이 gate는
`HP_ATTEMPT_STARTED` 또는 `ROOT_GENESIS` publish보다 먼저 통과한다. P0는 exact path와 ancestor의
lstat/realpath/dev/inode/mount를 고정하고, 알려진 iCloud/CloudDocs·File Provider·Dropbox/OneDrive/
Google Drive 등 provider root가 같은 dev/inode 또는 ancestor/subtree를 가리키는 inverse alias,
provider-domain/ubiquity/iCloud xattr·metadata, configured sync/backup coverage, placeholder/
`UF_DATALESS`를 함께 검사한다. 하나라도 양성이거나 판별이 불완전하면 `OWNER_ACTION_REQUIRED`다.
xattr 부재, dataless=0, sync daemon 미실행, 넉넉한 free space 중 하나만으로 local-only를 증명하지
않고 sync/Optimize Storage/TCC 설정을 에이전트가 바꾸거나 우회하지 않는다. H1 이후 승인은 이미
일어난 pre-H1 egress를 소급 정당화하지 않는다.

pre-H1 egress 또는 destination-policy incident는 기존 lifecycle event type으로 가장하거나
`LIFECYCLE_OUTPUT_ROOT`에 쓰지 않는다. 영속 기록이 필요하면 별도 exact write GO를 받은
local-only/private non-temp fresh 0700 `INCIDENT_RECORD_ROOT`를 같은 gate로 검증한다. pinned parent
identity와 provider-state를 root 생성 직전 재확인하고, root 생성 직후 첫 staging/record byte 전에
root 자체의 provider-domain/inverse alias·sync/backup scope·placeholder/`UF_DATALESS=0`·local
residency를 다시 검사한다. drift·양성·판별 불완전이면 byte를 쓰지 않는다.

이 root는 lifecycle chain으로 가장하지 않는 **H0/H1 밖 별도 new-byte generation**이다. exact
prefix-disjoint root 안에 fixed/minimized schema의 no-replace immutable `INCIDENT_RECORD.json` +
`INCIDENT_RECORD_SHA256` pair만 두고, incident evidence-input inventory/digest, incident scope·관측 evidence, 기본
`ACCESS_CONTROLLED` 분류, H1-equivalent confidentiality/license review와 reviewer/verdict, owner
disposition state를 결속한다. pair가 incomplete/mismatch면 그 root를 고치거나 재사용하지 않고 새
승인을 받는다. disposition이 pending인 record 뒤의 최종 결정은 predecessor digest를 가리키는 새
fresh incident root/pair로만 남기며, 모든 predecessor도 archive한다.
`INCIDENT_RECORD_SHA256`은 JSON byte만 hash하고 checksum file 자신은 그 digest에서 제외한다.

이하 `AUX_LOCAL_VERIFIED(kind, root, inventory_digest)`는 incident record와 아래 history staging
같은 H0/H1 밖 local auxiliary generation의 **current derived verdict**다. 매 derive/reuse에서 exact
parent/root local-only gate와 entry set을 pass 전후 재검증하고, 각 content open 전 no-follow stat 및
no-follow fd hash 전후 fstat로 identity/flags/size/mtime/ctime 불변·`UF_DATALESS=0`을 요구한다.
hydrate하지 않으며 provider 편입·eviction·unreadable byte·중간 drift면 `AUX_LOCAL_PENDING +
OWNER_ACTION_REQUIRED`다. H2는 exact auxiliary root/inventory/review/disposition·retention/cleanup
class를 승인하고, H4는 source pre/post continuity와 이 current evidence digest를 manifest/result에
결속하며, H5는 immutable destination에서 독립 retrieval한다. terminal owner disposition과 이
사슬이 끝나기 전 H4_COMPLETE를 내지 않는다. canonical incident record는 영구 보존하고 H5 뒤
남은 로컬 duplicate만 별도 CLEANUP_MANIFEST/GO로 정리한다. 승인된 safe record root가 없으면
incident는 즉시 사용자에게 report하되 영속화됐다고 주장하지 않고 H1/H4를 blocked로 둔다.
H2_APPROVED 뒤 **그 exact AUX inventory가 선택된 live H4 source이고 모든 required destination의
대응 H5 독립 retrieval이 끝나기 전**에는 root/inventory/review/disposition의 생성·변경·current-
verdict 하락이 새 source generation이다. 해당 handoff generation의 H2_APPROVED·exact H4 GO/
H4_RESULT와 downstream H5를 즉시 무효화하고 historical object는 고치지 않는다. 갱신한 exact AUX
inventory로 H2와 copy/upload GO를 다시 받거나, 고유 digest를 가진 별도 supplemental H2→H4→H5
generation으로 완결하기 전 기존 HANDOFF_MANIFEST에 append하거나 H4_COMPLETE를 내지 않는다.

모든 required H5가 exact AUX bytes를 독립 retrieval하면 `AUX_LOCAL_ARCHIVED` historical state다.
그 뒤 exact AUX digest/H5 object를 결속한 CLEANUP_MANIFEST와 별도 cleanup GO 아래 성공한
quarantine/delete는 앞선 H2/H4/H5를 무효화하지 않고 `AUX_LOCAL_CLEANED`로 전이한다.
CLEANUP_RECEIPT가 original AUX digest·각 H5 immutable object·승인 quarantine identity와 최종 local
absence를 결속해야 한다. 승인 cleanup 밖의 mutation/loss/provider drift는 historical H5를 고치지는
않지만 current AUX/cleanup verdict를 즉시 내리고 `AUX_LOCAL_PENDING + OWNER_ACTION_REQUIRED`로
두며 CLEANUP_COMPLETE를 금지한다.

P0의 TCC 검사는 parent directory open/traverse와 현재 policy만 read-only로 기록한다. 실제 write
가능성은 GO 아래 첫 no-replace attempt staging create에서 검증하며 `EACCES`/`EPERM` 또는 대화형
권한 요구가 나오면 root를 만들거나 다른 경로로 fallback하지 않는다. parent의 unrelated sibling
(credential 포함)은 content/hash/inventory 대상이 아니다. pinned parent fd에서 exact root leaf와
승인된 두 literal prefix만 `openat`/`fstatat` scope로 다루고 parent-wide content scan/hash/copy를
금지한다.

2026-09-05 이 호스트 확인에서
`~/Library/Mobile Documents/com~apple~CloudDocs/Documents`는 `~/Documents`를 가리키는 symlink이고
두 경로의 followed dev/inode가 같으며 Finder iCloud Documents 설정·File Provider/iCloud xattr와
dataless descendant가 존재했다. `fileproviderctl evaluate ~/Documents`도 `isUploaded=1`,
`isExcludedFromSync=0`, sync not paused와 upload-capable provider를 보고했다. 따라서
**`~/Documents`와 같은 설정의 `~/Desktop`은 pre-H1 HP/
lifecycle 목적지로 금지**한다. `~/Downloads`는 현재 provider xattr·dataless 관측이 없는
데 더해 같은 command가 `No item for URL`을 돌려준 **후보일 뿐**이며, 위 전체 gate와 fresh
P0-admission을 통과하기 전에는 승인·안전 확정으로 쓰지
않는다. 이 과거 음성 관측을 “오늘 이미 egress gate 충족”으로 재사용하지 않으며 parent 생성
직전과 root 생성 직후 첫 payload byte 전에 provider/inverse-alias/sync/backup/local-residency를
각각 새로 판정한다. 같은 Data volume의 후보는 tmp_cleaner/path-loss 방어일 뿐 device/APFS-volume loss
durability가 아니며 H4/H5를 계속 요구한다.

위 생산 경로는 정의됐지만, 현재 exact full source-universe admission packet과 static-reviewed
fd-hasher 전문/review digest는 아직 고정되지 않았으므로
`24 roots / 2220 files / 47.7 MiB` 같은 시점 한정 count를 넣은 copy GO는 **발행 준비가 되지
않았다**. 문서 편집을 멈춘 뒤 P0-admission을 새로 산출·검토하고 그 결과를 사용자에게 먼저
제시한다.

```text
P0 emergency preservation preflight
  source  승인 packet의 exact full source-universe baseline digest에 결속된 전체 scratchpad ·
          /private/tmp/fxi-* exact discovery 결과 ·
          /private/tmp/s1a_commit_message.txt 같은 아래 명시 standalone path
  freeze  알려진 producer/workflow admission 중지 + open writer 0 확인. read-only fd만 있다는
          이유로 process를 중지하지 않음; 이후 write admission 가능성을 닫지 못하면 별도 pause GO
          또는 OWNER_ACTION_REQUIRED. pause가 필요하면 PID 숫자만 믿지 않고 owner·executable·
          start time·open-path set을 재확인하며 pause/resume을 같은 승인과 검증 범위에 결속
  L0      content read 전에 lstat-only lifetime snapshot을 채취하고 entry별 최초
          cleaner boundary/T_guard를 고정. admission L0와 execution L0를 각각 결속하고
          effective_guard=min(admission_guard, execution_guard). 이후 read로 올라간 atime은
          이 guard를 늦추지 않음. atime은 stability equality digest에는 넣지 않음
  C1/C2   승인된 exact fd-hasher로 아래 H0 content-snapshot 알고리즘을 두 번 수행
          (path-based shasum은 대체재가 아니며 atime equality는 요구하지 않음)
  copy    fresh 0700 root 아래 source별 fresh direct-child에 /bin/cp -pRP exact-argument copy
  verify  source pre/post의 dev/inode와 비-atime metadata·SHA-256 안정성, target의
          아래 공통 `COPY_EQ`에 따른 payload·metadata 동등성을 전수 확인.
          target dev/inode/ctime은 기록하되 copy이므로 source와 같다고 요구하지 않음;
          source drift면 해당 attempt 폐기
  output  P0_SNAPSHOT.json/P0_SHA256과 HP_MANIFEST.json/HP_SHA256은 temp sidecar가 아니라
          승인된 HP root 안에서 바로 no-replace 생성. P0_SNAPSHOT은 H0의 source mapping,
          root identity, L0, C1/C2, 모든 regular file/directory metadata, cleaner eligibility/
          admission/execution lifetime digest·각 guard·effective_guard, freeze/open-writer 증거와
          full P0-admission canonical bytes, FD_HASHER_REVIEW와 exact bootstrap/hasher source,
          exact P0_TRANSPORT_BINDING·P0_APPROVAL_RESPONSE bytes/digest,
          verifier runtime/environment binding,
          observed ownership classes·total ownership map·
          numeric group-set 및 각 digest를 같은 schema로 빠짐없이 담음
```

checksum envelope는 다음 exact bytes로 고정한다.

```text
P0_SNAPSHOT_SHA256 = SHA-256(P0_SNAPSHOT.json exact canonical bytes)
P0_SHA256 bytes     = <64 lowercase hex><two ASCII spaces>./P0_SNAPSHOT.json<LF>
P0_SHA256_ENVELOPE_SHA256 = SHA-256(P0_SHA256 exact bytes)

HP_SHA256 bytes     = HP root의 모든 regular file 중 HP_SHA256 자신만 제외한 각 파일을
                      <64 lowercase hex><two ASCII spaces>./relative/path<LF>로 기록한 것.
                      raw relative-path byte의 LC_ALL=C 순서이며 P0_SNAPSHOT.json,
                      P0_SHA256, 모든 copied payload와 HP_MANIFEST.json을 반드시 포함
```

`P0_SNAPSHOT.json`과 `P0_SHA256`은 STARTED 전에 memory에서 exact bytes/digest가 freeze되고,
STARTED 뒤 root에 같은 bytes를 no-replace로 쓴다. envelope 파일은 자기 자신을 hash하지 않고,
HP_SHA256이 그 envelope bytes를 결속한다. filename 표현이 불가능한 newline/special path는 앞선
P0 gate에서 이미 FAIL이므로 checksum 형식에 임의 escape를 추가하지 않는다.

이하 모든 축약된 “source stability”와 “전수 equality”는 다음 공통 predicate를 뜻한다.

```text
SOURCE_STABLE
  L0a/L0b의 lstat-only key에 uid·gid·st_flags를 포함한다. ACL/xattr를 읽는 API는 L0에
  섞지 않는다. L0 채택 뒤 content hash보다 먼저 별도 security-metadata pass에서 ACL과
  xattr/resource-fork의 이름·value digest를 기록하고, C1/C2와 source pre/post에서 다시
  비교한다. set/type/dev/inode/uid/gid/mode/flags/size/mtime_ns/ctime_ns와 이 metadata digest가
  바뀌면 drift다. current user 소유가 아니거나 copy/verifier가 안전하게 보존·표현하지 못하는
  ACL·flag·setuid/setgid·special metadata가 있으면 OWNER_ACTION_REQUIRED.

COPY_EQ
  regular payload는 set/type/size/mode/mtime_ns/SHA-256, directory는
  set/type/mode/mtime_ns/empty, 지원되는 flags·ACL·xattr/resource-fork digest를 비교한다.
  각 source ownership pair에 대해 target uid/gid는 P0-admission에서 사용자가 본 total exact
  ownership map의 **유일한 expected pair와 정확히 일치**해야 한다. “source와 같거나 map” 같은
  암묵 fallback은 없다. target uid는 current uid이고 target gid는 승인·재검증한 numeric group-set의
  member여야 한다. remap 때도 parent/root 0700·setid 0·untrusted ACL 0으로
  외부 traversal을 막는다. undeclared uid/gid drift, mode 확대, untrusted ACL, setid,
  placeholder/UF_DATALESS 또는 unreadable byte는 FAIL이다. source/target dev·mount ID와
  same_device를 기록하고, same_device면 protection_scope는
  TMP_CLEANER_AND_PATH_LOSS_ONLY다.
```

뒤 절차의 더 짧은 필드 목록은 이 공통 predicate를 축소하지 않는다. `/bin/cp -p`가 권한상
source uid/gid를 보존하지 못해도 exit status가 이를 알리지 않을 수 있으므로 owner/group은 반드시
post-copy에서 검증·기록한다. verified evidence root 안의 mode/group/xattr를 나중에 “정리”하지
않는다. 예를 들어 disclosure 후보를 `chmod 600`으로 바꾸면 검증된 tree가 깨진다. sanitization이
필요하면 원본 HP generation을 immutable하게 두고 별도 승인된 derived artifact와 manifest를 만든다.

P0/C1/C2·fresh/no-replace·destination pre-write gate 밖에서 만든 `/bin/cp -pRP` + path-based
SHA-256 사본은, 별도 exact write GO가 있었더라도 `UNVERIFIED_RECOVERY_CANDIDATE`일 뿐이다.
copy 시점의 source identity/stability, symlink/TOCTOU 부재, overwrite 부재, metadata fidelity와
pre-first-byte egress를 사후에 만들어 내거나 `HP_ATTEMPT_STARTED`를 소급 기록할 수 없다. 원본이
남아 있으면 원본에서 새 fresh verified HP attempt를 수행하고, 원본이 사라졌다면 candidate를
보존하되 `HP_VERIFIED`·`TEMP_LOSS_PROTECTED_LOCAL`·`SOURCE_LOST_AFTER_P0_HP`·
`PROMOTED_HANDOFF_SOURCE`로 승격하지 않는다. 이 문서는 그런 선복사를 자동 허가하지 않는다.

목적지가 아직 없거나 copy GO가 없으면 `HP_REQUIRED + TEMP_LOSS_AT_RISK +
OWNER_ACTION_REQUIRED`다. 이는 자동 복사를 허용하지 않는 승인 경계의 결과이며, 상태 라벨만으로
보존됐다고 주장하지 않는다. H0/H1 manifest·sidecar 생성에는 아래 exact
`LIFECYCLE_OUTPUT_ROOT`를 묶은 수정 GO를 별도로 받는다. 이 root도 위 pre-H1 local-only/private
gate를 통과한 exact private **non-temp** parent 아래 absent fresh 0700 leaf여야 하며,
current-user owner·untrusted write ACL/group/world write 0·symlink ancestor 0을 확인한다.
`/private/tmp` 안의 lifecycle root는 승인하지 않는다.

## 호스트 수명 관측의 등급

2026-09-04 KST 이 호스트에서 확인한 `com.apple.tmp_cleaner`는 매일 `Hour=0`에 `/tmp`
(`/private/tmp`)를 검사한다. 실행 스크립트의 regular-file pass는 다음이다.

```text
/usr/bin/find -dx . -fstype local -type f
  -atime +3 -mtime +3 -ctime +3
  [아래 name exclusion] -delete -print

floor((now-atime)/24h) > 3 AND
floor((now-mtime)/24h) > 3 AND
floor((now-ctime)/24h) > 3
≈ now >= max(atime,mtime,ctime) + 96h 인 실행 기회
```

`/usr/bin/find`를 71h·73h·95h·97h 경계에서 직접 실행한 결과, 단위 없는 day 비교는 이
호스트 바이너리에서 **완료된 24시간 단위를 내림한 뒤** `+3`과 비교됐다. 73h와 95h는
`-atime +3`에 불일치하고 97h는 일치했다. man page의 “다음 단위로 올림” 문구와 다르므로,
운영 기준은 이 바이너리 실측이며 H0 때 같은 positive control을 다시 실행한다. shell의
`find`는 shim일 수 있으므로 항상 `/usr/bin/find`를 명시한다.

두 번째 pass는 같은 local filesystem 경계에서 `-type d -empty -mtime +3`인 빈 directory도
지운다. `.X*-lock`, `.X11-unix`, `.ICE-unix`, `.font-unix`, `.XIM-unix`, `quota.user`,
`quota.group`은 두 pass 공통 제외이고, `.vfs_rsrc_streams_*`는 directory pass에만 추가로
제외된다. H0는 cleaner 판정을 위해 empty-directory 여부와 상대경로·mode·mtime/exclusion을
기록하고, provenance tree를 위해 non-empty를 포함한 모든 directory metadata도 기록한다.

이는 **삭제 가능 시점**이지 보장된 삭제 시각이 아니다. 계속 깨어 있으면 실효 96시간 경계 뒤
첫 `Hour=0`이 다음 정규 실행 기회다. 그러나 `StartCalendarInterval`은 sleep 중 놓친 자정 실행을
wake 시 합쳐서 실행할 수 있다. 자정을 잠든 채 지나고 eligibility 뒤 깨어나면 표의 “다음 정규
실행”보다 먼저 그 wake에서 삭제될 수 있다. 따라서 정규 자정을 “실제 첫 삭제 시각”이나 안전
마감으로 부르지 않고, **각 항목의 실효 96시간 경계 전**을 보존 마감으로 삼는다.

2026-09-04 KST `lstat` snapshot은 다음과 같다. 이 표는 영구 규칙이 아니며, 접근 방식에
따라 atime이 바뀔 수 있으므로 H0에서 반드시 다시 산출한다.

| 범위 | 현재 가장 빠른 항목 | 실효 96시간 경계 | 계속 awake일 때 다음 정규 실행 |
|---|---|---:|---:|
| H0 발견 범위(분류 전) | `sysimg-google_apis.xml` | `2026-09-06 10:15:08.544 KST` 부근 | `2026-09-07 00:00` |
| 명시 handoff/D21 이력 | `D21_TEARDOWN_v3.md` | `2026-09-07 23:57:13.627 KST` 부근 | `2026-09-08 00:00` |
| `UNRESOLVED` hotfix audit | untracked `tests/test_device_token_purge_fence.py` | `2026-09-07 06:28:45 KST` 부근 | `2026-09-08 00:00` |
| A0 evidence | `a0_pg_harness.py` | `2026-09-08 03:38:09.004 KST` 부근 | `2026-09-09 00:00` |
| drift evidence | `generate.stderr` | `2026-09-08 12:50:51.395 KST` 부근 | `2026-09-09 00:00` |

H0 발견 범위의 첫 항목은 아직 D21 소유로 분류된 것이 아니다. 명시 payload와
조사 범위의 위험을 넘겨짚지 않기 위해 둘을 따로 적었다. 이 문서 편집은 자신의
mtime/ctime을 바꾸므로 current packet의 임계는 최종 편집 후 H0에서 재산출한다.

이 표의 “다음 정규 실행”은 continuous-awake projection일 뿐 actual deletion timestamp가 아니다.
H1 전에는 `OWNED_REPRODUCIBLE`/“대체 불가”도 확정하지 않는다. 특히 tracked file은 HEAD의 base를
복구할 수 있어도 미커밋 modified byte는 복구되지 않고 untracked file은 HEAD에서 복구되지 않는다.
현재 알려진 hotfix 변경 11개는 모두 `UNRESOLVED`; 이름이나 base 존재만으로 축소 copy에서 빼지
않는다. 위 untracked test의 eligibility는 D21 history보다 이르므로 “첫 대체 불가 손실은
`D21_TEARDOWN_v3.md`”라고 단정하지 않는다.

위 snapshot에서 H1 전 최초 `T_guard`는 `2026-09-05 10:15:08.544 KST` 부근이다.
driver가 나중에 `OWNED_REPRODUCIBLE`로 분류될 개연성만으로 이를 D21 파일의 더 늦은 guard로
바꾸지 않는다. **H1_PASS가 exact H0_SHA256에 결속해 required subset을 확정하기 전에는 전체
cleaner-eligible H0 universe의 이른 guard가 권위값**이다. 이 값도 실행 직전 L0에서 재산출한다.
`T_guard=T_min-24h`는 물리 삭제 예측이 아니라 wake·승인·재시도 여유를 확보하는 운영
contingency다. 이를 지나도 삭제가 일어났다고 주장하지 않지만, 의도한 24시간 여유를 소진했으므로
아래 위험 escalation과 보존 우선순위를 유지한다.

접근·수정·metadata 변경, sleep/wake, launchd 실행 상태에 따라 실제 위험은 달라지며,
실행 전 파일별 timestamp와 daemon 상태를 다시 산출한다. 이 호스트 probe에서는 `read(2)`
계열과 mmap/git 계열이 서로 다른 atime 갱신을 보였으므로, “읽기”를 하나의 규칙으로
일반화하거나 접근을 수명 연장 기전으로 쓰지 않는다. 현재 부팅에서
pre-boot `/private/tmp` 산출물이 남지 않은 강한 관측은 있으나 이를 모든 재부팅의 확정 동작으로
일반화하지 않는다. Time Machine destination/local snapshot은 현재 확인되지 않았지만, 이것이
다른 백업 수단의 부재까지 증명하지는 않는다. `touch`로 시각만 연장하는 것은 보존으로 인정하지
않는다.

## 이관은 정본과 원시 증거를 분리한다

사용자가 다음 두 목적지를 각각 승인해야 한다.

```text
canonical document  현재 OPEN/PROPOSED인 결정축과 이후 A0.6 판정을 담을 서버 계획 문서
raw evidence store  byte-identical 원시 증거와 검토 이력을 담을 Git-tracked evidence 또는
                    별도 off-host artifact store
```

현재 `ACCEPTED 0`이므로 바로 ADR로 승격하지 않는다. Android 계획서는 서버 ordering 결정의
정본이 아니라 승인된 서버 문서를 링크하는 소비자다. 이 문서는 특정 목적지를 임의로 정하지
않으며, 목적지가 정해지기 전에는 아래 gate를 통과한 것으로 간주하지 않는다.

원시 증거는 **이동하거나 고쳐 쓰지 않고 byte-identical copy**한다. 기존 `REPORT.md`, harness,
`EVIDENCE_SHA256.txt` 안의 `/private/tmp` 절대경로는 당시 provenance이므로 그대로 둔다.

이관 루트에 다음 두 파일을 새로 만든다.

```text
HANDOFF_MANIFEST.json
  source absolute path → archive-root 기준 POSIX 상대경로 mapping
  regular file은 class · disclosure · disposition · byte size · mode · SHA-256
  **모든 directory**는 상대경로 · type · mode · mtime_ns · empty 여부를 별도 entry로 기록
  directory payload는 regular-file count/bytes와 directory count(empty count 포함),
  canonical payload tree SHA-256도 기록

HANDOFF_SHA256
  "64개 lowercase hex + 공백 2개 + ./relative/path" 형식
  LC_ALL=C 경로 정렬
  모든 regular payload + HANDOFF_MANIFEST.json 포함. directory metadata는 manifest를 통해 결속
  HANDOFF_SHA256 자신만 제외
```

검증은 handoff root에서
`LC_ALL=C shasum -a 256 -c HANDOFF_SHA256`로 전수 실행한다. `HANDOFF_SHA256`
자신의 hash/immutable object identity는 canonical document의 commit tree 또는 artifact-store
metadata에 연결한다. H0에서 newline을 포함한 path, symlink, special file을 별도 계수하고,
이 형식으로 안전하게 표현할 수 없는 항목이 하나라도 있으면 H4를 중단한다.

directory payload tree SHA-256은 type-tagged canonical line을 archive-relative path로
`LC_ALL=C` 정렬해 연결한 byte에 SHA-256을 적용한다. regular file line은
`F␠mode␠size␠sha256␠./path\n`, 모든 directory line은
`D␠mode␠mtime_ns␠empty(0|1)␠./path\n`로 고정한다. `HANDOFF_MANIFEST.json`과
`HANDOFF_SHA256`은 이 directory aggregate에서 제외해 순환을 막고, 전체 handoff
검증에서는 위 규칙대로 manifest 자체를 `HANDOFF_SHA256`에 포함한다.

다음은 명시 handoff 후보이며 새 manifest가 직접 hash해야 한다.

```text
A0 evidence          /private/tmp/fxi-d21-a0.aWQ3tH 전체 7파일
                     (기존 §12 목록 밖 REPORT.md와 classes/*.class 포함)
contract drift       /private/tmp/fxi-contract-drift.3jE6zp 전체 211파일
                     (기존 EVIDENCE_SHA256.txt 14개 밖 generated-v2{,-r2} 포함)
decision history     A0_5_PACKET v1·v2·v3.1·현재본
D21 history          D21_TEARDOWN v3·v4·v6·v7·v8·v9
                     (v9가 v6을 이력·증명으로 명시 참조)
message drafts       S1A_MSG.txt · C2_MSG.txt · COMMIT_MSG.txt
supporting artifacts /private/tmp/fxi-s1a-files.txt (`OWNED_REPRODUCIBLE`)
                     /private/tmp/s1a_commit_message.txt (`OWNED_REQUIRED`, amend 전 이력)
                     /private/tmp/fxi-rehearsal-freeze.txt (`OWNED_REPRODUCIBLE`,
                       drift/IMAGE_FREEZE.txt와 byte-identical)
lifecycle provenance exact `LIFECYCLE_OUTPUT_ROOT` **전체 tree** — root-level
                     content-addressed GENERATION_EVENT chain, current/failed/invalidated/
                     crash-aborted/reconciled-orphan generation directory 전부,
                     내부 H0/H1·H1 boundary·lifetime event와 모든 directory metadata/aggregate
HP provenance        승인 parent의 exact HP attempt-registry prefix 아래 event/staging residue 전부 ·
                     verified/aborted HP attempt root 전체(directory aggregate 포함) ·
                     존재하는 HP_MANIFEST.json · HP_SHA256 envelope
session provenance   H1이 승인한 call-ID-bound command/combined-output event excerpts
cleanup mechanism    reviewed `safe_rename_macos.c` source와 pinned binary/build metadata ·
                     SAFE_RENAME_REVIEW.json · SAFE_RENAME_REVIEW_SHA256
```

lifecycle-generated files와 session-event excerpts는 생성 전/후를 막론하고 기본
`ACCESS_CONTROLLED`이다. H1은 excerpt를 만들기 전에 call ID·event type·byte length·SHA-256·
exact extraction rule로 **prospective bytes**를 검토하고, 생성 뒤 그 hash가 일치할 때만 H4에
넣는다. H1/H2 이후 생긴 manifest/receipt라고 해서 자동 공개 가능해지지 않는다.

위 A0/drift 개수는 2026-09-04 inventory snapshot이다. 이관 직전에 전체를 다시 열거한다.

message draft는 commit에서 byte 복구 가능하다고 묶어 취급하지 않는다. 현재 비교는 다음과 같다.

```text
S1A_MSG.txt    vs Android 538f149  raw commit body와 byte-identical
                 7072 bytes · SHA-256 7032cfefd8bf2438db87283225f2faf9a815095b7f675a72740a9d1efbcd4a62
COMMIT_MSG.txt vs server e312b43   raw commit body와 byte-identical
                 7202 bytes · SHA-256 c40de93fa4980576e180d77e44551953f3b6badf57317d197eb96ab0109b7e7a
C2_MSG.txt     vs server d35e0b3   byte-identical 아님: 4332 vs 4432 bytes,
                 diff hunk 1 deletion + 2 additions(+100 bytes); 양쪽 trailing LF
s1a_commit_message.txt             amend 전 draft; final commit body와 다름
```

비교에는 출력 개행을 덧붙이는 `git log --format=%B`를 쓰지 않고, `git cat-file commit`의
첫 빈 줄 뒤 raw payload를 사용한다. commit에서 복구 가능한 byte가 있더라도 이력 가치가 있는
draft는 H4에서 그 byte를 이관하고, 사용자가 명시한 disposition을 승인한 뒤에만 cleanup
후보로 낮춘다.

A0 7-file bundle에는 `javac/java` invocation이 없지만, 원 Codex session JSONL의 final call
`call_s5LMZ1E3FfeJCXGVZBggpMuT`가 실제 6-JAR classpath를 기록한다. command event는
line 39438(1648 bytes, SHA-256
`0ba26ad5842e0f7a302b2a3073bdadb4d9ff98b6aa2e795b5877386f1f9c58ef`), paired combined-output/
exit event는 line 39440(3222 bytes, SHA-256
`840c0c61ab125ae9e4f82404c89ce050d5f53d56c0fcb3d763bd812cb64e5efd`, exit 0)이다.
live session 전체 파일은 계속 변할 수 있으므로 전체 hash를 provenance로 삼지 않고, H1 검토
뒤 call ID로 찾은 두 event를 byte-exact excerpt로 이관한다. 원 session locator는 다음이다.

```text
/Users/jay/.codex/sessions/2026/08/29/
  rollout-2026-08-29T18-57-38-01a04cf4-10c8-78a0-9920-40632abfdd56.jsonl
```

여섯 JAR은 A0 디렉터리 밖의 `EXTERNAL_INPUT`이다. `REPORT.md` §12는 그 classpath 중
okhttp/mockwebserver 두 JAR만 hash했다. 나머지 넷은 원 command가 수동으로 포함한
dependency JAR이다. 특히 `okio-jvm 3.9.0`과 `kotlin-stdlib 1.9.0`은 OkHttp 4.12.0의
POM 선언(`okio 3.6.0`, `kotlin-stdlib-jdk8 1.8.21`)과 다른 local-cache substitution이다.
현재 local Gradle cache byte를 전수 확인한 snapshot은 다음과 같다.

```text
com.squareup.okhttp3:okhttp:4.12.0
  789531 bytes · b1050081b14bb7a3a7e55a4d3ef01b5dcfabc453b4573a4fc019767191d5f4e0
com.squareup.okhttp3:mockwebserver:4.12.0
  74739 bytes · 6784673687f4ac8f21679b9d4bc7cdb46e1a1ce1be9d3133b36bede59a741561
com.squareup.okio:okio-jvm:3.9.0
  372089 bytes · ddc386ff14bd25d5c934167196eaf45b18de4f28e1c55a4db37ae594cbfd37e4
org.jetbrains.kotlin:kotlin-stdlib:1.9.0
  1708006 bytes · 35aeffbe2db5aa446072cee50fcee48b7fa9e2fc51ca37c0cc7d7d0bc39d952e
junit:junit:4.13.2
  384581 bytes · 8e495b634469d64fb8acfa3495a065cbacc8a0fff55ce1e31007be4c16dc57d3
org.hamcrest:hamcrest-core:1.3
  45024 bytes · 66fdef91e9739348df7a096aa384a5685f4e875584cce89386a7a47251c4d8e9
```

H0/H1에서 `ARCHIVE_BYTES_WITH_LICENSE` 또는 `REFERENCE_ONLY_NETWORK_REQUIRED`를 선택한다.
후자면 exact offline rerun이 불가하고 네트워크 재취득이 필요함을 manifest에 적는다.

A0 PostgreSQL harness의 container image도 파일 inventory 밖의 `EXTERNAL_INPUT`이다.

```text
postgres:17-alpine
  linux/arm64 · sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73
```

H0의 외부 runtime inventory에 이 image를 추가하고, license와 함께 image byte를
보존할지 위 digest/reference만 남겨 network reacquisition을 요구할지 동일한 disposition으로
결정한다.

OkHttp 관측을 standalone E2E evidence로 과대해석하지 않는다. A0 bundle만으로는 invocation/
output/exit를 audit할 수 없다. H1 뒤 위 selected session events를 별도 immutable provenance로
이관하면 command·combined output·exit 0은 transcript audit할 수 있다. 다만 별도 stdout/stderr
stream과 exact `javac -version`은 미기록이다. 직전 session call
`call_jDqU8Ft1NMTvmfQtRLFdsHsL`(lines 39366/39368)은 당시 Java runtime
Temurin 17.0.18+8을 기록하고, class file major는 61이다. 이 범위와 한계를 handoff manifest에
함께 적는다.

같은 scratchpad의 다른 파일이나 `/private/tmp/fxi-*`는 이름만으로 이 작업 소유라고
추정하지 않는다. 모든 항목을 `OWNED_REQUIRED / OWNED_REPRODUCIBLE /
EXTERNAL_INPUT / FOREIGN / UNRESOLVED` 중 하나로 분류한다. `UNRESOLVED`는
`OWNER_ACTION_REQUIRED`를 내고 exact path·status/hash·timestamp 임계·필요 disposition을 통지하며,
삭제하지 않는다.

현재 알려진 exclusion은 `/private/tmp/fxi-hotfix-audit.pEkw69`다. HEAD `e48160a`에서
tracked modification 9개와 untracked file 2개가 있어 `UNRESOLVED / OWNER_ACTION_REQUIRED`다.
D21 archive에 임의로 포함하거나 cleanup하지 않는다. 소유·disposition이 결정되기 전
tmp 손실 임계가 다가오면 이 항목 역시 별도 사용자 승인을 받고 위 local-only/private gate를
통과한 non-temp
임시 사본으로 보호한다. broad glob cleanup은 금지한다.

정본 문서와 재사용 실행 도구는 `/private/tmp`의 **기존 named artifact**에 운영적으로
의존하지 않아야 한다. byte-identical raw evidence 안의 과거 절대경로는 예외이며,
정본에서 그 경로를 live link로 사용하지 않는다. pinned source/input에서 매번 새로
만들고 종료 시 버리는 재현 가능 ephemeral workspace/venv(예: `/tmp/fxi-contract-venv`)는
허용한다. 단, 유일 input/state가 그 경로에만 존재하면 이 예외가 아니다.

## handoff gate

```text
H0 exact inventory 동결:
   첫 discovery 전에 알려진 producer/workflow admission을 중지하고 open writer 0을 확인.
   source universe는 전체 scratchpad, `/private/tmp/fxi-*`의 exact discovery 결과, 그리고
   handoff 후보로 명시한 standalone `/private/tmp/s1a_commit_message.txt`를 포함한다.
   H0/H1/lifecycle-event의 모든 output은 discovery 전에 승인되고 위 local-only/private gate를
   통과해 원자 생성한 하나의 exact private non-temp fresh 0700 `LIFECYCLE_OUTPUT_ROOT` 아래에서만
   no-replace로 만들고, 두 discovery에서 이 root
   subtree 하나만 source universe에서 제외한다. root를 만든 직후 `.publish-staging` 또는 다른 첫
   provenance entry를 만들기 전에 parent provider-state 불변과 root 자체의 provider-domain/
   ubiquity/iCloud xattr·configured sync/backup coverage·placeholder/`UF_DATALESS=0`·local residency를
   다시 검사한다. 판별 불완전·양성·pre-create evidence와 drift면 root 안에 어떤 byte도 publish하지
   않고 `OWNER_ACTION_REQUIRED`로 멈춘다. 통과하면 어떤 generation child보다 먼저
   `root_event_seq=0`, `prev_event_digest=null`, schema/version, 승인 parent/root path·mount·dev·inode와
   pre-create/post-create destination local-only/provider/residency evidence digest를 결속한
   `ROOT_GENESIS`를 아래 publish primitive로 만든다. 이 event에는 generation이 아직 없으므로
   `generation_seq`·generation nonce·relative child path·H0/H1 digest·lifetime tip은 모두 explicit
   null이고 임의 기본값이나 직전 generation을 넣지 않는다. bootstrap 중 crash해 canonical genesis가 없고
   generation child도 없다면, 승인 root identity가 그대로이고 `.publish-staging` residue 외 entry가
   0일 때만 재시도한다. lifecycle single-writer/root lock 아래 **이번 publish staging leaf를 만들기
   전에** pre-existing residue set `R`을 전수 freeze/hash하고, genesis는 `R`과 새
   `publish_attempt_nonce`를 결속한다. 이번 genesis staging leaf는 self-envelope라 `R`에서 제외한다.
   full write/fsync 뒤 canonical `linkat` 직전에 current leaf를 제외한 `R`이 그대로인지 재검증하며,
   drift면 publish하지 않는다. crash로 남은 current leaf는 다음 attempt의 pre-existing `R`에 포함된다.
   정상 최초 genesis의 `R`은 empty다.
   다른 entry가 있으면 해당 root를 재사용하지 않고 `OWNER_ACTION_REQUIRED`다.

   **각 H0/H1 attempt 또는 재실행은 child를 만들기 전에** fresh
   `generation-<generation-seq>-<nonce>` planned path를 결속한 `GENERATION_STARTED` event를 먼저
   publish한다. 그 다음에만 그 exact direct-child를 no-replace로 만들고, 그 안에서만 고정명
   `H0_MANIFEST.json`/`H0_SHA256`, 뒤이은 `H1_REVIEW.json`/`H1_SHA256`, lifetime event를 만든다.
   invalidated generation을 수정·재사용하지 않는다. generation의 상태는 하나의 record를 나중에
   고치는 방식이 아니라 root-level append-only event chain으로 기록한다. lifecycle single-writer가
   `root_event_seq`와 `generation_seq`·nonce를 배정하고, 각 전이는 no-replace
   `GENERATION_EVENT.<root-event-seq>.<sha256>.json` 하나를 추가한다. filename digest는 완성된
   JSON byte의 SHA-256이고 별도 sidecar를 요구하지 않는다. event는 exact
   `generation_seq`·nonce·relative child path·event type·그 시점에 존재하는 H0/H1 digest·직전 root
   event digest·그 시점의 lifetime tip digest를 결속한다.

   lifecycle JSON은 canonical 이름에 직접 쓰지 않는다. exact root 아래 고정 `.publish-staging/`에
   no-follow/O_EXCL regular file로 전부 쓰고 `fsync`·schema·hash 검증을 끝낸 뒤, pinned directory
   fd끼리 같은 filesystem의 `linkat`으로 absent canonical leaf에 no-replace publish하고 directory를
   `fsync`한다. 성공 뒤 staging hard-link를 지우는 동작까지 exact lifecycle-root 수정 GO에 포함한다.
   crash로 staging leaf가 남으면 canonical event로 해석하지 않고 아래 reconciliation에서만 다룬다.
   이 publish primitive는 root `GENERATION_EVENT`와 `LIFETIME_EVENT`, H0/H1 JSON 및 checksum
   envelope 모두에 적용한다. H0/H1의 fixed-name JSON+SHA pair는 **둘 다 검증되고 해당 root state
   event가 append된 뒤에만 committed**다. 허용 state 전이는 다음과 같다.

   ```text
   ROOT_GENESIS | H0_FAIL | H1_FAIL | GENERATION_INVALIDATED | CRASH_ABORTED |
     GENERATION_ORPHAN_ABORTED → fresh GENERATION_STARTED
   GENERATION_STARTED → H0_COMPLETE | H0_FAIL | CRASH_ABORTED
   H0_COMPLETE → H1_PASS | H1_FAIL | GENERATION_INVALIDATED | CRASH_ABORTED
   H1_PASS → GENERATION_INVALIDATED
   any carried state → GENERATION_ORPHAN_ABORTED
   ```

   `STAGING_ORPHAN_RECORDED`는 state를 바꾸지 않는 audit event라 어느 유효 chain tip 뒤에도 올 수 있다.
   단 **모든** event의 `prev_event_digest`는 audit event를 포함한 즉시 직전 chain tip을 가리켜야
   하며 건너뛰지 않는다. state validator는 root event chain을 순서대로 replay하면서 audit event에서
   직전 state를 carry-forward하고, 다음 state transition의 허용 predecessor는 그 carried state로
   판정한다. 따라서 audit event 뒤 state event도 hash-chain fork를 만들지 않는다.

   H0가 끝났으나 H1을 아직 수행하지 않은 generation은 `H0_COMPLETE` tip인 pending generation이지
   seal된 terminal record가 아니다. H1 결과나 뒤이은 invalidation은 새 event로만 표현한다.
   비권위 `current` symlink/file은 만들지 않는다. current state는 **root 전체를 열거해
   unaccounted/unreconciled staging·orphan·incomplete pair가 0이고, root/lifetime filename-digest
   mismatch와 event-chain gap/fork가 0임을 확인한 뒤**, 유일한 최신 event-chain tip에서 산출한다.
   `ROOT_GENESIS`가 exact one-to-one으로 결속한 bootstrap staging residue 및
   `CRASH_ABORTED`/`GENERATION_ORPHAN_ABORTED`/`STAGING_ORPHAN_RECORDED`가 exact one-to-one으로
   결속한 opaque byte와 incomplete H0/H1
   pair는 accounted evidence라 이 zero 조건에서 제외한다. root/lifetime content-addressed event에는
   별도 `.sha256` pair를 요구하지 않는다.
   `GENERATION_STARTED`가 기록된 순간부터 그 시도는 current pending이며, 실패·crash 때문에 이전
   H1_PASS generation으로 fallback하지 않는다. STARTED 뒤 child 생성 전 crash는 dangling STARTED가 되어 우선
   `LIFECYCLE_LOCAL_PENDING`으로 분류한다. 다음 lifecycle 시작 시 single-writer reconciliation은
   root writer 0에서 전체 tree와 각 event namespace를 먼저 검사한다. event와 exact file/digest가 일치하는
   `GENERATION_STARTED` 또는 `H0_COMPLETE` tip만 같은 generation에서 재개할 수 있다. event에
   결속되지 않은 추가 byte, incomplete H0/H1 pair, unmatched child 또는 dangling STARTED는 기존
   byte를 고치지 않는다. current generation의 existing partial state는 exact path·dev·inode·directory
   aggregate를 `CRASH_ABORTED` state event에 결속한다. STARTED event만 있고 child가 없는 dangling
   시도는 planned exact path와 `observed_absent=true`, dev/inode/tree=`null`을 결속한다.
   정상 protocol 밖에서 STARTED 없이 나타난 unmatched `generation-*` child는 어느 carried state에서든
   exact path/dev/inode/tree digest를 `GENERATION_ORPHAN_ABORTED` **state event**에 결속해 current를
   terminal-aborted로 만든 뒤에만 fresh generation으로 진행한다. 따라서 기존 H1_PASS가 다시 current가
   되지 않는다. `.publish-staging`의 partial/duplicate byte는 `STAGING_ORPHAN_RECORDED` audit event에
   결속한다.
   canonical root/lifetime event는 publish primitive 때문에 partial byte로 나타날 수 없다. 이
   recovered generation과 accounted orphan payload도 H4 archive 대상이며 event/pair validator의
   `extra=0`에서 **명시적으로 결속된 opaque payload 예외**로 센다; 유효 state나 lifetime record로
   해석하지 않는다. root event chain 자체가 corrupt/conflicting하거나 exact tree를 안전하게
   결속할 수 없을 때만 `OWNER_ACTION_REQUIRED`로 멈춘다. 따라서 crash 흔적은 이전 H1_PASS로
   fallback시키지도, 영구 dead-end로 만들지도 않는다.
   H0 manifest는 root와 generation child의 exact path·owner·mode·dev·inode, ROOT_GENESIS의
   pre/post-create destination-evidence digest 및 이 exclusion을 기록한다.
   동적 output 이름을 개별 source extra로 취급하지 않는다.

   **L0 lifetime snapshot은 어떤 content read보다 먼저** `lstat`/`fstatat(...,
   AT_SYMLINK_NOFOLLOW)`만으로 `L0a→L0b` 두 번 채취한다. 두 pass의
   set·type·dev·inode·uid·gid·mode·st_flags·
   regular-file size·atime_ns·mtime_ns·ctime_ns, 모든 directory의 mtime_ns·ctime_ns·empty 여부가 모두 같을 때만
   L0b를 pre-read baseline으로 채택한다. empty 여부를 확인하는 directory read 자체가 directory
   atime을 올릴 수 있으므로 directory atime은 equality/deadline key로 쓰지 않는다. regular file은
   path·type·dev·inode·uid·gid·mode·st_flags·size와 `a0/m0/c0`, 모든 directory는
   path·type·dev·inode·uid·gid·mode·st_flags·`m0/c0`·empty 여부를 기록한다. regular file의 cleaner 경계는
   `max(a0,m0,c0)+96h`, **empty directory만** `m0+96h`다. entry별
   `cleaner_eligible`과 H1 전 eligible source universe의 최초 `T_min/T_guard`를 이 L0 값에서
   고정한다. 이후 hash/copy/read로 올라간 atime은 이 guard를 늦추지 못한다.

   **C1/C2 content-stability snapshot은 두 번 연속 수행한다.** 각 regular file은 no-follow로
   연 file descriptor에서 hash하고 전후 `fstat`의
   dev·inode·type·uid·gid·mode·st_flags·size·mtime_ns·ctime_ns가 같아야 한다. 각 pass는
   `SOURCE_STABLE` 전 필드와 SHA-256을 기록하고, 모든 directory에도 같은 stat/security-metadata
   stability predicate를 적용한다. L0의
   비-atime 구조 metadata와 C1/C2가 모두 같아야 한다. **atime은 post-read 관측값으로만 남기고
   content-stability equality key에서 제외한다.** atime-only 증가는 실패가 아니지만 content·set·
   type·dev/inode·uid/gid·mode/flags·security metadata·size·mtime/ctime drift 또는 hash mismatch는 실패다.

   현재 fresh generation의 immutable `H0_MANIFEST.json`에 L0와 C1/C2를 함께 finalize한 뒤 그 파일의 hash를 별도
   `H0_SHA256`에 기록한다; 어느 후속 단계도 H0 manifest를 수정하지 않는다. snapshot이
   불안정하거나 producer를 동결할 수 없는 source는 H0_COMPLETE가 아니며
   `OWNER_ACTION_REQUIRED`. `UNRESOLVED`도 소유 미확정과 별개로 writer-free snapshot을 만들 수
   있어야 H0_COMPLETE. H0는 handoff destination 승인 전에도 실행할 수 있지만, exact
   `LIFECYCLE_OUTPUT_ROOT` 수정 GO 없이는 output을 생성하지 않는다. H0/H1 invalidation·실패·
   rerun은 source가 같더라도 다음 fresh generation child에서 H0부터 다시 수행하며, 이전 고정명을
   덮어쓰지 않는다.
H1 disclosure/distribution gate: H0 manifest를 고치지 않고 별도 immutable
   `H1_REVIEW.json` + `H1_SHA256`을 생성하며 exact H0_SHA256에 결속. 모든 regular payload와
   EXTERNAL_INPUT에 다음 두 분류와 방법을 기록.
   기밀성     PUBLIC · ACCESS_CONTROLLED · EXCLUDE_WITH_REASON · UNREVIEWED
   배포/라이선스 REDISTRIBUTABLE · REFERENCE_ONLY · LICENSE_REVIEW_REQUIRED
   방법       file type/magic inventory + 값을 출력하지 않는 presence-only detector +
              수동 검토. detector ID·path·count·reviewer 판정은 H1_REVIEW에 기록
   PASS       판정을 exact H0_SHA256에 결속 ∧ UNREVIEWED=0 ∧
              LICENSE_REVIEW_REQUIRED=0 ∧ 모든 EXCLUDE_WITH_REASON에 owner·
              사유·재현성 영향·별도 disposition 존재. required payload의 별도 disposition이
              완료되지 않으면 H4_FAIL
   private key/credential/실 PII는 ACCESS_CONTROLLED. 로컬 절대경로는 provenance일 수 있어
   존재만으로 민감 판정하지 않음. binary/dependency/license는 기밀성과 독립 판정
   이름·mode만으로 비밀 유무를 확정하지 않는다. 알려진 `ops/systemd/fxi-topic-auth-capture.env`는
   H1에서 content-safe detector와 수동 검토를 거칠 후보이며, 원 HP evidence를 검증 뒤 chmod하는
   방식으로 처리하지 않는다.
   `.git/objects/info/alternates`가 있는 copied worktree는 working tree·index·refs의 raw snapshot일
   뿐 standalone Git history라고 주장하지 않는다. history가 required이면 H1/H2 disposition 아래
   외부 object closure를 exact commit/object에 결속해 별도 durable retrieval로 증명하거나, 별도
   승인된 standalone bundle/clone artifact를 만든다. 최초 materialize는 별도 exact staging-write
   GO 아래 pre-H1 local-only/private gate를 통과한 non-temp absent fresh 0700
   `HISTORY_STAGING_ROOT`에서만 한다. pinned parent 재확인과 root 생성 직후 첫 byte 전 root 자체의
   provider/sync/backup/placeholder/UF_DATALESS/local-residency 재검사를 통과해야 하며, Git/provider/
   외부 destination에 직접 pre-review materialize하지 않는다. materialize한 closure/bundle/clone은 current
   H0/H1 밖의 **새 byte generation**이므로 `AUX_LOCAL_VERIFIED` own immutable inventory/digest와 H1-equivalent
   confidentiality/license review, exact destination/write·commit/push/upload GO, H5 independent
   retrieval을 모두 거친다. H1-equivalent PASS 뒤에만 H2가 외부 destination을 승인하고 H4가
   staging에서 outward copy/upload한다. 이를 완료하기 전 history requirement는 `UNRESOLVED`이고
   `H4_COMPLETE`를 내지 않는다. staging local duplicate는 H5 뒤 별도 cleanup manifest/GO 대상이다.
   lifecycle-generated files와 prospective/created session excerpts는 기본 ACCESS_CONTROLLED.
   public export가 필요하면 fixed-schema allowlist로 별도 package를 만들고 package 완성 뒤
   disclosure review를 다시 수행. 그 review log와 원 lifecycle provenance는 package 밖 private
   evidence로 유지하며 public raw destination에 섞지 않음
   `REFERENCE_ONLY`이고 bytes를 이관하지 않는 EXTERNAL_INPUT은 disposition을
   `REFERENCE_ONLY_NETWORK_REQUIRED`로 명시해 두 namespace를 연결함.
HP temporary loss protection:
   H1_PASS 전에는 `TEMP_REQUIRED := P0/H0 temp discovery universe 전체`를 provisional
   `UNREVIEWED/ACCESS_CONTROLLED`로 둔다. H1_PASS 순간 exact H0/H1 분류에 따라
   `TEMP_REQUIRED := (required ∪ UNRESOLVED) ∩ temp discovery universe`로 전이한다.
   H1_FAIL이면 이 축소 전이를 하지 않고 provisional 전체 집합을 유지하며, `H1_BOUNDARY`도 그
   집합 digest를 기록한다. HP_VERIFIED와 TEMP_LOSS_PROTECTED_LOCAL은 applicable한 이 집합의
   byte 보존만 판정한다. exact private non-temp
   LIFECYCLE_OUTPUT_ROOT의 provenance는 HP coverage universe가 아니며, root owner/mode/dev/inode와
   root-level generation-event genesis→current tip 및 각 generation 내부 genesis→frontier의
   contiguous hash chain을 검증했을 때 별도
   `LIFECYCLE_LOCAL_VERIFIED`로 추적한다. 이는 historical terminal이 아니라 **current derived
   verdict**다. 산출하거나 다시 사용할 때마다 exact parent/root의 pre-H1 local-only/private gate,
   owner/mode/ACL, provider-domain·inverse alias·sync/backup scope, ROOT_GENESIS destination-evidence
   binding을 **검증 pass 전후 모두** 재검증한다. 각 content open 전 `fstatat(...,
   AT_SYMLINK_NOFOLLOW)`로 identity/flags와 placeholder/`UF_DATALESS=0`을 확인하고, no-follow fd의
   hash 전후 `fstat`에서도 dev/inode/type/flags/size/mtime/ctime 불변과 `UF_DATALESS=0`을 요구한다.
   pass 뒤 parent/root provider-state·residency와 entry set을 다시 확인한 뒤에만 모든 lifecycle byte의
   local readability/hash와 current chain을 인정한다. dataless byte를 검증하려고 hydrate하지 않는다.
   중간 drift나 hydration 가능성을 배제하지 못하면 PASS하지 않는다. provider 편입·eviction·unreadable byte·정책 drift면 historical event는 고치지 않고 위험
   root에 새 audit byte도 쓰지 않으며 `LIFECYCLE_LOCAL_PENDING + OWNER_ACTION_REQUIRED`로 내린다.
   이때 current TEMP_REQUIRED의 raw H5 또는 HP coverage도 없으면 `TEMP_LOSS_AT_RISK`를 함께 유지한다.
   pre-H1 external egress 증거가 있으면 위에서 정의한 별도 승인된 `INCIDENT_RECORD_ROOT`에
   incident와 owner disposition을 영속화하기 전
   H1_PASS/H4_COMPLETE를 금지한다. 이는 H4/H5 durability를 대신하지 않는다. HP_VERIFIED와
   LIFECYCLE_LOCAL_VERIFIED가 모두 성립하면 lifecycle H4/H5 미완료는 handoff gate를 계속
   막지만, 그것만으로 TEMP_LOSS_AT_RISK를 유지하지는 않는다.
   각 regular file은 `t=max(a,m,c)+96h`, empty directory는 `t=mtime+96h`.
   단 tmp_cleaner가 실제 도달하고 type/name exclusion에 걸리지 않는 entry만 deadline 후보임.
   H1_PASS 전 `T_min`은 H0 discovery universe의 cleaner-eligible subset 최솟값이고
   H0_MANIFEST에 기록. H1_PASS 뒤 `T_min`은
   `(required ∪ UNRESOLVED) ∩ cleaner-eligible`의 최솟값이며 exact H0_SHA256에 결속된
   H1_REVIEW에 기록. 둘 다 `T_guard=T_min-24h`.
   H1_PASS 뒤 subset guard도 새 lstat로 다시 시작하지 않고 **L0의 고정된 entry별 t를**
   `(required ∪ UNRESOLVED) ∩ cleaner-eligible`에 투영해 산출한다. 이후 timestamp 관측은
   immutable H0/H1을 고치지 않고, exact H0 digest·current lstat·entry별 current t와
   `effective_guard=min(기존 applicable guard, current guard)`를 담은
   `LIFECYCLE_OUTPUT_ROOT/<current-generation>/LIFETIME_EVENT.<lifetime-seq>.<type>.<sha256>.json`을
   위 publish primitive로 매번 fresh/no-replace 생성한다. filename digest는 완성 JSON byte의
   SHA-256이며 별도 sidecar를 만들지 않는다. event는 별도 `lifetime_seq`와 직전 lifetime-event
   digest를 넣어 hash chain을 만든다. 이 chain의 genesis는 **exact H0_SHA256 하나에만** anchor한다.
   H1이 terminal이 되면 기존 lifetime tip을 prev로 하는 immutable `type=H1_BOUNDARY` event에
   exact H1_SHA256·verdict·TEMP_REQUIRED-set digest·H0 L0에서 투영한 subset T_min/T_guard를 결속한다.
   그 boundary를 publish한 뒤 root의 `H1_PASS`/`H1_FAIL` event가 exact boundary digest를 결속해야
   H1 terminal state가 committed된다. H1 뒤 `type=SNAPSHOT` event는 이 boundary 다음에서만 이어진다. H1 전 snapshot과 H1 후 바뀐
   분류/guard 의미를 같은 genesis 주장으로 뭉개지 않는다. P0-only emergency HP는 HP root 안의
   P0_SNAPSHOT/HP_MANIFEST로 닫고 lifecycle generation의 lifetime chain으로 승격하지 않는다.
   seq는 namespace별로 분리해 `root_event_seq`·`generation_seq`·`lifetime_seq`라 부르며 각각
   단조·연속이고 단일 prev digest만 허용한다. 이전 event는 수정·삭제하지 않으며 latest
   pointer가 필요하면 비권위 index로만 둔다. 보존 판단은 기록된 모든 applicable guard 중 가장 이른 값을 사용해 단순
   조회·hash·copy로 deadline을 뒤로 미루지 않는다. discovery set/type/content/mode/size/mtime/
   ctime이 바뀌면 lifetime event로 덮지 않고 H0/H1 invalidation 규칙을 적용한다. atime-only 증가는
   lifetime event에 관측하되 H0 invalidation 사유가 아니다.
   HP copy scope는 분류 시점에 따라 고정: H1_PASS 전에는 cleaner eligibility와 무관하게
   **P0/H0의 temp discovery universe 전체**를 provisional `UNREVIEWED/ACCESS_CONTROLLED`로
   private HP에 복사한다. eligibility는 deadline 산출에만 쓰며, name exclusion 또는 아직
   늙지 않았다는 이유로 재부팅/기타 손실 보호 범위에서 빼지 않는다.
   현재 관측의 전체 byte량이 작다는 것은 이 보수적 범위를 줄일 근거가 아니라 전수 보존의
   비용이 낮다는 근거다. H1 전 “진짜 대체 불가” subset은 아직 존재하지 않는다. P0 승인자는
   exact top-level root 목록·aggregate/anomaly·전수 manifest digest를 보며, 정상 entry 수천 행을
   일일이 승인하지 않는다.
   H1_PASS 뒤에는 temp `(required ∪ UNRESOLVED)`만 복사한다. non-temp
   `LIFECYCLE_OUTPUT_ROOT`의 lifecycle/session output은 HP payload에 중복 복사하지 않고 H4/H5의
   별도 lifecycle source로 보존한다.
   Gradle cache/Docker 같은 EXTERNAL_INPUT bytes는 이 temp universe에 자동 포함하지 않고 별도
   disposition/GO를 요구함.
   H0_COMPLETE 여부와 무관하게 TEMP_REQUIRED bytes가 raw-evidence destination에서 H4
   copy와 H5 독립 retrieval까지 끝나지 않은 동안 HP 필요성은 살아 있음. destination이
   미승인이면 완료 가능성을 예측하지 말고 즉시 exact HP 경로 승인을 요청. destination이
   승인됐어도 raw H4/H5가 T_guard까지 끝나지 않았으면 HP_REQUIRED. HP는 T_guard 전 완료를
   운영 목표로 하며, 이 시각은 물리 삭제 시각이나 그 뒤 보존 불가능을 뜻하지 않음.
   이미 T_guard를 지났고 raw H4/H5 또는 HP가 미완료면
   `HP_REQUIRED + TEMP_LOSS_AT_RISK + OWNER_ACTION_REQUIRED`로 두고 다른 비-handoff 작업을
   중지한 채 보존만 진행; HP_VERIFIED 또는 raw H5_COMPLETE 전에는 위험 상태를 내리지 않음.
   HP의 다단계 copy가 crash해도 root를 미계수 상태로 남기지 않도록, 사용자는 exact non-temp
   private parent·absent fresh root leaf와 함께 그 parent의 exact sibling
   `<root-leaf>.HP_ATTEMPT_EVENT.*` 및 `.<root-leaf>.HP_ATTEMPT_STAGING.*` prefix를 승인한다. P0로
   source frontier를 메모리에서 동결한 뒤, root를 만들기 **전에** pinned parent fd에서 content-addressed
   `HP_ATTEMPT_STARTED` event를 staging full-write/fsync → hash → `linkat` no-replace → parent-fsync로
   publish한다. canonical filename grammar는
   `<root-leaf>.HP_ATTEMPT_EVENT.<attempt-event-seq>.<event-type>.<sha256>.json`이고 filename digest는
   완성 JSON byte의 SHA-256이다. 모든 event는 schema/version·attempt nonce·exact parent/root·
   `attempt_event_seq`·event type·semantic state·직전 event digest를 담는다. STARTED는 seq 0,
   `prev_event_digest=null`이며 `P0_SESSION_ID`·`P0_ADMISSION_PACKET_SHA256`·
   `FD_HASHER_REVIEW_SHA256`·`P0_TRANSPORT_BINDING_SHA256`·
   `P0_APPROVAL_RESPONSE_SHA256`·`P0_SNAPSHOT_SHA256`·
   `P0_SHA256_ENVELOPE_SHA256`,
   `coverage_baseline_kind=P0_SNAPSHOT|H0`와 그 exact digest, TEMP_REQUIRED path/digest set과 시작 전
   freeze한 pre-existing registry residue set의 exact path/digest aggregate도 결속한다. 각
   attempt-prefix는 독립 chain이다. **모든** attempt event publish에서 이번 staging leaf는 self-envelope라 시작 전 freeze한
   pre-existing registry residue set에서 제외하고, canonical `linkat` 직전에 current leaf를 제외한
   그 set의 불변을 재검증한다. 이 parent-prefix의 canonical event와 crash staging residue 전부를
   `HP_ATTEMPT_REGISTRY` universe로 간주해 H1/H4/H5에서 전수 분류·archive한다.

   STARTED 뒤에만 root를 원자 생성한다. STARTED에 terminal event가 없으면 다음 실행은 source/root
   writer 0에서 exact root tree를 동결한다. 완성된 HP_MANIFEST/HP_SHA256과 payload가 STARTED의
   baseline/mapping 및 아래 전수 equality를 모두 만족하면 `HP_ATTEMPT_VERIFIED`를 append할 수 있다.
   VERIFIED event는 exact root path·owner·mode·dev·inode·tree digest, HP_MANIFEST byte digest,
   HP_SHA256 envelope byte digest와 그 envelope가 선언한 aggregate digest, covered-path digest set,
   source pre/post snapshot evidence digest, destination local-only/provider/residency evidence digest와
   equality verdict를 **모두** 결속한다. 이 terminal event가
   publish·parent-fsync된 뒤에만 해당 root를 verified HP generation으로 세고 aggregate
   `HP_VERIFIED`를 derive/report할 수 있다. aggregate는 terminal event들에서 언제든 재계산하는
   비영속 verdict라 별도 파일·세 번째 sibling prefix를 만들지 않는다.
   그렇지 않거나 root가 없으면 `HP_ATTEMPT_ABORTED`에 reason과 existing root의 path·owner·mode·
   dev·inode·tree digest를 결속한다(root absent면 `observed_absent=true`, 나머지는 null). 모든
   STARTED 뒤 event의 prev는 audit를 포함한 즉시 직전 event digest다.
   `HP_ATTEMPT_STAGING_RECORDED`는 직전 semantic state를 그대로 carry하는 audit event다. prefix별
   validator는 `STARTED(prev=null) → zero or more audit → exactly one VERIFIED|ABORTED`만 허용하고,
   terminal 뒤에는 audit만 허용한다. STARTED·terminal의 중복, terminal 종류 중복, fork, seq gap,
   unknown event, state regression과 terminal 뒤 state-changing event는 모두 invalid다. canonical event와
   같은 dev+inode/hash인 publish hard-link residue는 original HP GO에 포함된 staging cleanup으로
   unlink하고, 그 밖의 residue는 이 audit event가 exact path/digest로 결속한다. aborted
   root byte는 수정·재사용하지 않는 `ACCESS_CONTROLLED` opaque H4 source이고 HP coverage에는
   산입하지 않는다. terminal attempt event 뒤의 root는 immutable하다. attempt-event publish 중
   남은 staging residue는 다음 state/audit event가 exact digest/path로 account하며, unaccounted residue가 있으면
   새 root 생성·HP_VERIFIED·H4_COMPLETE를 모두 막는다. H1/H4/H5 validator는 모든 발견 prefix의
   unique genesis/tip·연속 seq·prev/hash·state carry와 VERIFIED root/envelope/evidence binding을 다시
   검증한다. terminal 없는 STARTED가 하나라도 있으면 H1_PASS·H4_COMPLETE·H5_COMPLETE를 내지 않고
   먼저 VERIFIED 또는 ABORTED로 조정한다; accounted partial root라는 이유로 통과시키지 않는다.

   사용자는 위 pre-H1 local-only/private gate를 통과한 exact non-temp private parent와 absent fresh
   root leaf를 승인. parent는 lstat 기준 현재 사용자 소유·mode 0700·untrusted write
   ACL/group/world write 0·symlink/provider inverse-alias ancestor 0·충분한 공간이어야 함. 이
   destination 증거와 exact parent dev/inode/mount/provider-state digest는 STARTED·VERIFIED가
   모두 결속한다. fresh root는 pinned parent fd의 `mkdirat(..., 0700)` 또는 같은 parent
   dev:inode를 재확인한 subshell의 단일 `mkdir -m 0700`로 원자 생성하고 EEXIST면 실패하며,
   생성 직후 owner/mode/dev/inode뿐 아니라 root 자체의 provider-domain/ubiquity/iCloud xattr·
   UF_DATALESS/local-residency를 다시 확인해 첫 payload byte 전에 fail closed한다. original→child mapping은 injective하고 모든 child는
   copy 직전 absent여야 함. source마다 root 아래 fresh direct-child 하나를 써 분류가 다른 source를
   격리하며 existing directory와 merge하지 않음. `(모든 resolved source root ∪ non-temp
   LIFECYCLE_OUTPUT_ROOT)`와 `HP destination root subtree`는 서로 어느 방향으로도
   ancestor/descendant가 아니고 같은 dev+inode alias도 아니어야 한다. 승인된
   `HP parent → fresh root → fresh direct-child` 내부 nesting은 이 prefix-disjoint 규칙의 유일한
   예외다. macOS volume의 case-fold와 Unicode normalization을 반영한 canonical component 비교가
   불확실하면 copy를 시작하지 않는다.
   현재 regular-file/directory-only 범위는 `/bin/cp -pRP "$source" "$fresh_child"`와 같은
   **approved total ownership map 아래 `COPY_EQ`-selected-metadata-preserving** exact-argument copy를
   사용하고 nonzero면 즉시 중단. newline path,
   symlink, special file 또는 이 copy model로 표현할 수 없는 entry가 있으면 임의 fallback 없이
   `OWNER_ACTION_REQUIRED`로 중단하고 HP_VERIFIED를 내지 않음. source pre/post와 target에 공통
   `SOURCE_STABLE`/`COPY_EQ`를 적용하고 copy 뒤 timestamp·owner/group·flags·ACL/xattr/provider/
   local-residency를 다시 확인한다. 첫 실제 payload entry에서 expected target uid/gid·mode·mtime_ns와
   지원되는 security metadata가 보존되지 않으면 남은 source를 복사하지 않고 즉시 attempt를
   ABORTED로 닫는다. 별도 probe file 생성·삭제는 그 exact path/write/delete를 승인받지 않은 채
   실행하지 않는다.
   root의 `HP_MANIFEST.json`은 `baseline_digest`(H0_COMPLETE 전에는 exact
   `P0_SNAPSHOT_SHA256`,
   이후에는 exact H0_SHA256)·generation 시작 전 동결한 temp-source frontier·original→child
   mapping·분류·전수 metadata와 source/destination st_dev·mount, approved total ownership map,
   full P0-admission/FD_HASHER_REVIEW/P0_TRANSPORT_BINDING/P0_APPROVAL_RESPONSE/ownership-class/
   total ownership-map/numeric group-set digest,
   `P0_SNAPSHOT_SHA256`·`P0_SHA256_ENVELOPE_SHA256`,
   destination local-only evidence digest·provider state·all-target-local-residency verdict,
   `same_device` 및 protection_scope를 기록한다. exact review/source 원문은 P0_SNAPSHOT에만 두며
   HP_MANIFEST에 중복하지 않는다. `HP_SHA256`은 위에 정의한 대로 P0 pair·모든 copied payload·
   HP_MANIFEST를 hash하고 자신만 제외한다.
   `HP_ATTEMPT_VERIFIED`로 닫힌 root만 immutable **HP generation**이다. 그 generation의
   HP_MANIFEST는 같은 HP_SHA256으로
   검증하고, HP_SHA256 자체는 checksum envelope라 coverage target에서 제외한다. 이 self-envelope와
   이미 non-temp인 이전 HP root는 새 generation trigger가 아니며, H4/H5에서 exact envelope digest와
   root directory aggregate를 별도 영속화·검증한다. generation frontier 뒤 새/변경 temp source,
   TEMP_REQUIRED set 변경 또는 새 H0 digest가 생기면 기존 manifest를 고치지 않고 fresh
   root/manifest/hash로 다음 generation을 생성한다. non-temp lifecycle output의 증가는 HP가 아니라
   다음 lifecycle H4/H5 generation을 촉발한다.
   HP generation은 **temp-source만 닫는** 다음 finite-closure protocol을 쓴다. non-temp
   `LIFECYCLE_OUTPUT_ROOT`의 chain 완료나 writer pause는 HP PASS의 선행조건이 아니다.

   1 exact TEMP_REQUIRED source의 producer/open writer만 pause하고 path set을 동결
   2 current P0/H0 baseline digest와 각 TEMP_REQUIRED path의 baseline entry digest를 결속
   3 source에 대해 pre-copy C1/C2 안정성 snapshot을 수행; set/content/비-atime metadata drift면 중단
   4 fresh child로 copy한 뒤 source post-snapshot과 target 전수 equality를 검증
   5 이 관측과 exact covered-path digest set을 HP_MANIFEST 안에 기록하고 HP_SHA256을 검증한 뒤,
     exact root/envelope/evidence를 결속한 `HP_ATTEMPT_VERIFIED` terminal event를 먼저 publish·
     parent-fsync. 그 terminal을 재검증해 aggregate HP_VERIFIED를 derive/report한 뒤에만
     temp-source writer를 resume

   HP 도중 lifecycle event를 만들지 않는다. lifecycle chain tip을 정보용으로 기록할 수는 있으나
   HP coverage·PASS·다음 generation trigger에 사용하지 않는다. 이 protocol 뒤 독립적으로 생긴
   temp source/TEMP_REQUIRED-set/H0 digest 변화만 다음 HP generation trigger다. non-temp lifecycle
   output의 추가·미완료·chain 오류는 `LIFECYCLE_LOCAL_PENDING` 및 다음 lifecycle H4/H5 대상이지,
   이미 검증된 동일 TEMP_REQUIRED byte coverage를 스스로 무효화하지 않는다.
   aggregate `HP_VERIFIED(current_H0_or_qualified_P0_digest, covered_path_digest_set,
   generation_digest)`는
   현재 TEMP_REQUIRED 집합의 각 항목이 동일 baseline entry digest를 가진 verified HP generation에
   포함되거나 raw H5_COMPLETE일 때만 유지한다. H0 invalidation/rerun이나 TEMP_REQUIRED-set 변경은
   즉시 aggregate HP_VERIFIED와 TEMP_LOSS_PROTECTED_LOCAL을 현재 TEMP_REQUIRED 집합에 대해 내리고
   HP_REQUIRED로 복귀시킨다. 옛 generation은 historical evidence일 뿐 현재 coverage로 계수하지
   않는다.
   `HP_ATTEMPT_VERIFIED`는 검증 시점의 immutable historical terminal일 뿐 현재 local residency를
   영구 보증하지 않는다. aggregate HP_VERIFIED/TEMP_LOSS_PROTECTED_LOCAL을 derive하거나 다시
   사용할 때마다 exact parent/root에 local-only/private gate, owner/mode/ACL, provider enrollment,
   provider-state·entry set·local residency를 **검증 pass 전후 모두** 확인한다. 각 content open 전에
   no-follow path stat으로 identity/flags와 placeholder/UF_DATALESS=0을 확인하고, no-follow fd의 hash
   전후 `fstat`에서도 dev/inode/type/flags/size/mtime/ctime 불변과 UF_DATALESS=0을 요구한다. pass 뒤
   parent/root 상태까지 재검증한 뒤에만 모든 payload byte의 local readability/hash를 인정한다.
   dataless byte를 검증하려고 hydrate하지 않으며 중간 drift나 hydration 가능성을 배제하지 못하면
   current aggregate를 내리고 `OWNER_ACTION_REQUIRED`다. root가 이후
   provider/sync/backup scope에 편입되거나 local byte가 eviction/dataless가 되면 historical terminal은
   고치지 않되 current aggregate를 즉시 내리고
   `HP_REQUIRED + TEMP_LOSS_AT_RISK + OWNER_ACTION_REQUIRED`로 복귀한다. 위험 parent에는 새 audit
   byte를 쓰지 않는다. pre-H1 external egress 증거가 있으면 위에서 정의한 별도 승인된
   `INCIDENT_RECORD_ROOT`에 incident와 owner disposition을
   영속화하기 전 H1_PASS/H4_COMPLETE를 금지한다.
   정상 live-source H0_COMPLETE 순간 P0-bound aggregate는 historical-only로 내린다. P0 bytes를 새
   H0 inventory에 자동 승격하지 않으며, H0-bound fresh HP generation 또는 raw H5_COMPLETE 전까지
   HP_REQUIRED다. 단 아래 qualified P0-loss recovery로 만든 H0_COMPLETE는 그 H0가 exact P0/HP
   digest를 직접 결속하므로 같은 verified P0 HP를 current coverage/source로 유지한다.
   성공 시 TEMP_LOSS_PROTECTED_LOCAL. H1/HANDOFF_COMPLETE/cleanup GO를 대체하지 않음.

   **P0-loss recovery.** H0 전에 tmp 원본 일부 또는 전부가 사라져도 verified emergency P0/HP가
   있으면 곧바로 `UNRECOVERABLE_SOURCE_LOSS`로 만들지 않는다. 다음 predicate가 모두 참일 때만
   `SOURCE_LOST_AFTER_P0_HP`로 분류한다.

   ```text
   P0_SNAPSHOT/P0_SHA256 및 HP_MANIFEST/HP_SHA256과 HP_ATTEMPT_VERIFIED가 전부 유효
   P0가 위에 열거한 H0 필수 field/schema를 빠짐없이 포함
   각 live original은 P0 logical entry와 exact identity/content 안정, 각 lost original은 absent
   각 promoted HP child는 P0 logical payload의 regular-file set/type/size/mode/mtime/SHA-256 및
     모든 directory set/type/mode/mtime/empty와 일치하고 HP source 자체 pre/post identity 안정
   mapping은 injective, missing/extra 0, unaccounted attempt/registry byte 0
   ```

   통과하면 fresh lifecycle generation에서 normal `GENERATION_STARTED` 뒤 H0_MANIFEST를 새로
   만들되 `h0_origin=promoted_p0`, exact P0/HP/attempt-event digest, original별 continuity와 logical
   original path→HP child mapping을 기록하고 `H0_COMPLETE` event가 이를 결속한다. 이는 P0를
   H0라고 이름만 바꾸는 것이 아니라, P0 schema completeness와 현재 HP byte를 재검증해 H0의
   logical source snapshot을 재구성하는 경로다. 이후 H1은 promoted HP byte를 검토하고 H4/H5를
   계속한다. 필수 field 하나라도 없거나 copy가 불일치하면 `UNRECOVERABLE_SOURCE_LOSS`이며,
   임의 재구성은 금지한다.
H2 사용자 승인: H1 결과를 보고 두 목적지의 owner·destination coordinates·visibility/ACL·
   provider/sync/backup/eviction policy·immutability mechanism·retention/expiry·삭제 정책,
   exact H0/H1 digest, 승인 inventory와
   HANDOFF manifest schema를 확정.
   Git이면 repo·branch·base SHA·exact target path도 동결. 실제 immutable commit/tree/object/
   version ID는 H3/H4에서 생성된 뒤 H5가 기록·독립 retrieval로 검증하며, 이미 preallocated된
   object만 H2에서 ID를 기록함.
   ACCESS_CONTROLLED required payload가 하나라도 있으면 public destination을 승인하지 않고,
   모든 required payload 및 lifecycle/session provenance의 기밀성·배포 등급과 destination
   policy가 호환되어야 H2_APPROVED
   H2_APPROVED는 destination/policy 승인일 뿐 write 권한이 아님. H3 전 canonical edit GO,
   H4 전 exact inventory+destination에 결속된 copy/upload GO를 별도로 받음. H4에서 생성된 actual
   HANDOFF_MANIFEST/HANDOFF_SHA256 digest는 그 GO와 H4 result에 결속. 외부 store upload도
   별도 GO이고, Git commit GO·push GO·cleanup GO는 계속 각각 분리함.
   이하 `H2_DESTINATION_EQ`는 목적지별 승인 full vector — owner/operator, exact coordinates와
   identity/ancestor, payload 분류 호환성, visibility/ACL, provider/sync/backup/eviction,
   immutability mechanism, retention/expiry, 삭제 정책 — 가 모두 현재 관측과 일치함을 뜻한다.
   적용 불가 필드도 `N/A`와 근거를 명시하고, 검증 미지원·판별 불완전·한 필드 drift는 FAIL이다.
   immutable `H2_APPROVED` record는 exact H0/H1·승인 inventory·user approval reference, full vector와
   각 `N/A` 근거를 canonicalize한 `H2_APPROVED_VECTOR_DIGEST`를 결속한다. 각 equality checkpoint는
   관측 시각·실제 vector·verdict·evidence와 verifier source/command/runtime/environment digest를 담은
   별도 observed-vector evidence digest를 만든다. H2는 prospective H4_RESULT·H5 result·
   HANDOFF_RECEIPT의 fixed schema/namespace와 confidentiality/distribution class도 승인하되 write
   권한을 주지 않는다. H4 GO는 exact H2 record/vector digest를 결속한다.
H3 canonical semantic handoff: exact H0_SHA256·H1_SHA256을 결과에 기록. 현재 12축 상태·
   A0.6 단계/의존·baseline·상대 evidence locator·ACCEPTED 0·중단 경로를 1:1 checklist/diff로
   이관하고 live temp dependency 0을 검증
H4 raw byte handoff: 원본은 유지하고 byte-identical copy; 기존 내부 manifest는 수정하지 않음.
   단 HP 후 tmp 원본이 사라졌으면 아래 source-promotion 규칙을 통과한 HP copy만 사용.
   어떤 destination byte도 쓰기 직전 `H2_DESTINATION_EQ` 전체를 재검증한다. copy/upload 완료 뒤
   H4 result를 내기 전에도 같은 full vector와 실제 local-residency/immutability 상태를 다시 검증한다.
   H2와 drift하거나 판별이 불완전하면 해당 copy를 handoff로 사용하지 않고
   `H4_FAIL + OWNER_ACTION_REQUIRED`다. 실제 또는 배제할 수 없는 비승인 external egress가 있으면
   위 incident-record 규칙으로 영속 disposition을 남기기 전 해당 destination을 재사용하거나
   H4_COMPLETE를 내지 않는다.
   copy 전 canonical target과 raw destination은 서로, 그리고 모든 resolved live-source root,
   LIFECYCLE_OUTPUT_ROOT, HP generation root subtree와 어느 방향으로도 ancestor/descendant가 아니고
   같은 dev+inode alias도 아닌지 case-fold/Unicode-normalized component 기준으로 확인한다.
   promoted HP child와 그 owning HP generation root의 승인된 nesting은 source-side 관계이므로
   허용하되, child는 logical payload source로 한 번만 mapping/copy하고 owning root의 lifecycle
   archive entry와 중복 payload로 계수하지 않는다. 그 밖의 source-source overlap은 명시적 mapping
   없이 허용하지 않는다. raw destination leaf는 absent 상태에서 no-replace로 만들고 existing
   directory와 merge하지 않는다. 불확실하거나 destination과 겹치면 H4_FAIL이다.
   H4 preflight는 source continuity에 따라 분기한다.
   `SOURCE_PRESENT` entry는 H0와 동일한 live discovery universe를 다시 두 번 열거하고
   set/type/dev/inode/content/size/mode/mtime/ctime을 H0와 비교한다. `SOURCE_LOST_AFTER_HP` entry는
   old path의 예상된 부재를 extra/missing 오류로 세지 않고, 아래 promotion predicate를 먼저
   통과한 HP child를 source로 삼는다. copy에서 달라질 수밖에 없는 HP child의 dev/inode/ctime은
   H0와 같다고 요구하지 않고, H0 logical payload의 regular-file
   set/type/size/mode/mtime_ns/SHA-256 및 모든 directory의 set/type/mode/mtime_ns/empty 여부와
   일치하는지와 HP source 자신의 pre/post identity 안정성을 검증한다.
   `SOURCE_LOST_AFTER_P0_HP` entry도 먼저 위 qualified P0-loss recovery로 `h0_origin=promoted_p0`인
   H0_COMPLETE를 만든 뒤 같은 logical-payload/HP-source predicate를 적용한다. qualified H0가 아직
   없으면 H4로 건너뛰지 않는다.

   H0/H1/lifetime event의 개별 동적 이름이 아니라 H0 전에 고정한 exact
   `LIFECYCLE_OUTPUT_ROOT` subtree 하나를 source
   discovery에서 제외한다. 그 root는 invalidated generation을 포함한 별도 lifecycle source로
   전수 열거하고 root-level GENERATION_EVENT chain과 generation별 lifetime-event hash chain,
   HP envelope digest, ROOT_GENESIS 및 current `LIFECYCLE_LOCAL_VERIFIED`가 결속한 destination
   local-only/provider/residency evidence digest, root directory aggregate를 HANDOFF_MANIFEST에
   기록해 archive한다. H4는
   exact lifecycle frontier(`root_event_seq` + last-event digest 및 generation별 lifetime-event tip)를 먼저
   고정하고 그 frontier까지의 writer를
   멈춘 뒤 복사한다. frontier 뒤 생긴 lifecycle output은 H0/H1 source snapshot을 무효화하지 않고
   다음 lifecycle H4/H5 generation을 요구한다. H4도 genesis baseline anchor부터 선택 frontier까지
   root-level `root_event_seq`/prev digest 및 각 generation의 `lifetime_seq`가 각각 unique
   contiguous하고, canonical event filename digest=JSON byte hash·fork/gap/extra 0이며 모든 이전
   H4/H5 lifecycle
   anchor의 포함·연장을 PASS 조건으로 검증한다. `ROOT_GENESIS`가 one-to-one으로 결속한 bootstrap
   staging residue 및 `CRASH_ABORTED`/
   `GENERATION_ORPHAN_ABORTED`/`STAGING_ORPHAN_RECORDED`로 결속된
   generation/staging payload는 opaque evidence로 archive하되 current나 committed event/pair로
   쓰지 않는다. H0_COMPLETE/H1_PASS/H1_FAIL event가 결속한 H0/H1 JSON+SHA pair만 pair equality를
   요구한다. 아직 reconciliation되지 않은 unmatched child·staging leaf·dangling event·불완전 pair
   또는 current pending generation은 PASS를 막는다. 실패하면
   H4_COMPLETE를 내지 않고
   `LIFECYCLE_LOCAL_PENDING + OWNER_ACTION_REQUIRED`로 둔다. temp byte coverage도 별도로
   깨졌을 때만 HP_REQUIRED를 함께 낸다.
   `SOURCE_PRESENT`의 unexpected extra/missing/type/dev/inode/content/size/mode/mtime/ctime,
   또는 promoted HP logical payload/identity 안정성의 불일치가 하나라도 있으면
   H0_COMPLETE·H1_PASS·H3_COMPLETE·
   H4_COMPLETE와 양 목적지의 모든 H5_COMPLETE, 현재 집합의 aggregate HP_VERIFIED와
   TEMP_LOSS_PROTECTED_LOCAL을 무효화하고 H0/H1부터 재실행. H0_SHA256 또는 H1_SHA256이
   바뀌면 exact digest·inventory에 결속된 H2_APPROVED와 기존 H4 copy GO도 무조건 무효화한다.
   destination coordinate/ACL/retention policy 제안은 재사용할 수 있지만 새 digest에 대한 사용자
   재승인이 필요하다. H0/H1 digest가 그대로인 timestamp-only lifecycle event 추가는 이 예외이며,
   해당 lifecycle H4/H5 generation만 갱신한다.
   copy 전에 해당 source의 producer/agent admission을 중지하고 open writer 0을 다시 확인.
   live source 또는 promoted HP child를 copy 직전과 직후 각각 regular-file과 모든 directory의
   set·type·dev·inode·size·mode·SHA-256·mtime/ctime·empty 여부로 전수 snapshot한다. 두 source snapshot은
   서로 exact identity까지 같아야 하고, H0와의 비교는 위 continuity별 predicate를 쓴다.
   destination copy는 regular-file set·type·size·mode·mtime_ns·SHA-256과 모든 directory의
   set·type·mode·mtime_ns·empty 여부가 같아야 하며,
   copy에서 달라지는 destination dev/inode/ctime은 별도로 기록하되 source와 같다고 요구하지 않음.
   copy 도중 drift/missing/extra/symlink 변화가 하나라도 있으면 copy를 handoff로 쓰지 않음.
   immutable HANDOFF_MANIFEST는 **어떤 destination write보다 먼저 freeze**한다. exact
   H0_SHA256·H1_SHA256, 승인 inventory, canonical `H2_APPROVED` record의 원문 bytes+digest,
   `H2_APPROVED_VECTOR_DIGEST`, destination별 canonical pre-write observed-vector evidence의 원문
   bytes+digest·verifier/runtime binding만 포함한다. manifest 자신을 포함한 package를 최종
   copy/upload한 뒤에 생기는 post-copy/upload evidence를 이 manifest에 소급 삽입하지 않는다.
   대신 destination별 별도 no-replace content-addressed `H4_RESULT.<destination>.<sha256>.json`이
   HANDOFF_MANIFEST digest, actual destination inventory/equality verdict, canonical post-copy/upload
   observed-vector evidence 원문 bytes+digest·verifier/runtime을 결속한다. H4_RESULT는 자신이
   attestation하는 package inventory 밖의 completion object이며 자기 자신의 post-write 관측이나
   미래 object ID를 내용에 요구하지 않는다. H4 GO는 승인된 local destination에서의 exact
   no-replace H4_RESULT 생성과 namespace만 포함할 수 있다. H4_RESULT를 Git에 commit하는 GO,
   push GO, 외부 store upload GO는 **항상 각각 별도 explicit approval**이며 H4 GO나 서로를
   대신하지 않는다. H5가 그렇게 저장된 result object 자체를 독립 retrieval한다.
   모든 required H4_RESULT가 publish되기 전에는 H4_COMPLETE가 아니다.
   상대경로 manifest 전수 hash + 표본이 아닌 실제 retrieval/read 검증
H5 두 목적지의 durability를 각각 검증:
   Git이면 commit 직전 HEAD/base drift를 재확인하고 staged path 집합 == 승인 inventory,
   unrelated staged diff 0을 증명. commit GO와 push GO를 각각 받아 원격 commit/tree와
   manifest/hash를 검증. retrieval 뒤 `H2_DESTINATION_EQ` 전체를 재확인
   외부 artifact store이면 immutable version/object ID·retention/expiry·삭제 정책을 확인하고
   독립 retrieval과 전수 hash 뒤 `H2_DESTINATION_EQ` 전체를 재확인
   각 canonical H5 result는 exact H0_SHA256·H1_SHA256·H4 manifest와 해당 H4_RESULT digest,
   `H2_APPROVED_VECTOR_DIGEST`, post-retrieval observed-vector evidence 원문 bytes+digest·verifier/runtime과 실제 immutable
   commit/tree/object/version ID를 포함. 두 destination retrieval 결과를 아래 별도
   `HANDOFF_RECEIPT.json`에 영속화하고 그 receipt 자체를 독립 retrieval한 뒤에만 H5_COMPLETE;
   receipt는 canonical H2 record, H4_RESULT, H5 result와 각 embedded observed-evidence 원문
   bytes+digest를 포함하거나 exact immutable object로 가리켜 후속 감사가 digest를 재계산할 수 있게 한다.
   policy drift·판별 불완전·mismatch 또는 receipt 보존 실패면 H5_FAIL
```

canonical document와 raw evidence가 서로 다른 저장 방식인 혼합형도 허용한다. handoff
전이는 다음 조건으로 고정한다.

```text
H0_COMPLETE ∧ H1_PASS ∧ H2_APPROVED
  → HANDOFF_IN_PROGRESS

H0_COMPLETE ∧ H1_PASS ∧ H2_APPROVED ∧ H3_COMPLETE ∧ H4_COMPLETE ∧
두 목적지 H5_COMPLETE
  → HANDOFF_COMPLETE
```

H1_FAIL이면 H2를 승인해도 전이하지 않는다. H1은 형식적 secret marker 검색만으로
대체하지 않는다. H0/H1/HP는 `HANDOFF_BLOCKED_BY_DESTINATION` 상태에서도 실행할 수
있지만 그 상태를 자동으로 바꾸지 않는다.

HP 후 tmp 원본이 사라지면 H0 존재 여부에 따라 `SOURCE_LOST_AFTER_HP` 또는
`SOURCE_LOST_AFTER_P0_HP`를 기록한다. 후자는 위 recovery로 H0_COMPLETE를 먼저 만들어야 한다.
모든 required source에 대해 H0 logical source와 HP copy의 regular-file set·type·size·mode·mtime_ns·SHA-256 및 모든 directory의
set·type·mode·mtime_ns·empty 여부가 전수 일치하고 missing/extra 0이며, original-path → HP-child chain과
검증된 `HP_MANIFEST.json`/`HP_SHA256` digest가 남은 경우에만 그 child를
`PROMOTED_HANDOFF_SOURCE`로 승격한다. 그 사본에서 H4/H5를 계속할 수 있다. 한 항목이라도
불일치/누락하거나 HP 검증 전 원본이 사라지면
`UNRECOVERABLE_SOURCE_LOSS`로 종료하고 H4·cleanup을 금지한다.

H0/H1 manifest와 lifetime event, HP attempt event·HP_MANIFEST/HP_SHA256,
incident/history AUX inventory·review, canonical H2 record와 embedded pre-write evidence,
selected session-event excerpts는 **pre-H4 lifecycle provenance**다. H4 package와 영속
`HANDOFF_MANIFEST.json`에는 이들만 source/digest chain으로 포함하고 manifest를 destination write
전에 freeze한다. destination별 H4_RESULT·H5 result와 embedded post-write/post-retrieval evidence는
package 밖 **post-H4 provenance**이며 frozen manifest에 넣거나 소급 수정하지 않는다.
`HANDOFF_RECEIPT.json`은 이 post-H4 object의 canonical bytes/digest와 실제
commit/tree/object/version ID·retrieval 결과를 가리키는 별도 사후 객체로 보존한다. receipt를 Git 또는 artifact store에
쓰는 데도 해당 write/commit/push/upload GO와 독립 retrieval이 필요하다; receipt는 자기 자신의
미래 object ID를 내용에 요구하지 않아 self-attestation 무한회귀를 만들지 않는다. cleanup
receipt도 같은 사후 객체다.

HP root는 H5 뒤에도 자동 삭제하지 않으며 아래 exact cleanup lifecycle을 통과한다.
`UNRESOLVED` child는 다른 required payload와 분리해 계속 `OWNER_ACTION_REQUIRED`로 보존한다.

`HANDOFF_COMPLETE` 직후 전역 실행 게이트는 `BLOCKED_BY_HANDOFF`에서
`AWAITING_SEPARATE_GO`로만 전이한다. 사용자의 별도 실행 GO 없이 A0.6을 시작하지 않는다.

local-only/private gate를 통과한 로컬 non-temp copy나 local Git commit은 tmp cleaner·일반
재부팅 손실을 막는 **임시 보호**이지
`HANDOFF_COMPLETE`나 cleanup 충분조건이 아니다. Git 목적지는 원격 commit/tree retrieval까지,
artifact store는 off-host immutable object retrieval까지 H5에서 검증한다. Time Machine 복구
경로가 확인되지 않은 현재 상태에서 그 검증 전 원시 증거를 삭제하지 않는다.

## A0.6 중단 경로와 cleanup 순서

A0.6이 보류되면 정본에 `A0_6_DEFERRED + 사유 + OPEN 축 + 재개 조건`, 폐기되면
`A0_6_ABANDONED + 마지막 증거 + 미해결 위험`을 기록하고 같은 handoff gate를 거친다. A0.6이 끝나지
않았다는 이유로 `/private/tmp`의 유일본을 무기한 유지하지 않는다. A0.6 결과도 같은 방식으로
정본과 evidence store에 추가한다.

각 spike의 container·volume·network는 그 spike가 소유한 **정확한 이름**만 종료 시 정리하고
absence check를 남긴다. A0의 `fxi-d21-a0-pg-awq3th`는 이미 제거된 것으로 보고됐으며, 다른
container 또는 dangling volume을 이름 패턴만으로 A0 소유라 판단하지 않는다. 기존 A0에
없던 cleanup manifest를 사후에 조작하지 않고, `REPORT.md`의 종료 기록과 현재 exact-name
absence를 서로 구분해 보존한다. 이후 spike부터만 시작/cleanup manifest를 필수로 한다.

cleanup 전에 존재할 수 있는 것은 완료 receipt가 아니라 영속 exact `CLEANUP_MANIFEST`
(`status=CLEANUP_PENDING`)다. 이 manifest가 영속 `HANDOFF_MANIFEST.json`을 가리키고
exact immutable object/version·SHA-256, exact target parent/leaf/path·분류·archived source
hash·freeze 시 target의 expected dev/inode/type/mode/size와 directory tree digest·예상 후속
검증, 모든 descendant의 `st_dev == target-root st_dev`, target 아래 mountpoint 0인 mount-table
snapshot뿐 아니라 **사용자가 사전에 승인한 non-temp quarantine parent·fresh leaf·결합 exact
path와 provider/sync/backup/eviction policy**를 열거한다. quarantine은 H1 disclosure·H2
destination policy와 호환되어야 하며, 자동 외부 전송이 있는 목적지는 그 exact egress가 별도로
승인되지 않으면 허용하지 않는다. 이 quarantine destination/policy 승인은 목적지 좌표만 고정하며 move/delete
권한이 아니다. 그 승인 뒤 `CLEANUP_MANIFEST` 생성에는 exact inventory·destination을 묶은 별도
edit/write GO가 필요하고, Git이면 commit GO와 push GO를, artifact store면 upload GO와 독립
retrieval을 각각 거쳐 immutable object/version을 고정한다. 그 다음 사용자는 별도로 그 manifest의
exact object/version과 SHA-256을 명시한 `cleanup GO`를 줘야 한다. `HANDOFF_COMPLETE`와 이
승인된 manifest가 모두 있어야 다음 순서로 정리한다.

cleanup은 **reviewed/pinned macOS no-replace helper가 존재하고 positive control을 통과하기
전에는 시작하지 않는다.** H2 이후 별도 canonical edit GO로 `safe_rename_macos.c`를 영속
tools 위치에 둔다. 정확한 compiler/input/output/probe 경로를 묶은 별도 build/control execution
GO 뒤에만 binary compile과 disposable positive control을 실행하고 source와 compiled binary를
함께 검토·고정한다. `CLEANUP_MANIFEST`는
source/binary SHA-256, exact `clang --version`, build invocation, source/destination parent의
expected dev+inode를 포함한다. 임의 shell/Python wrapper나 `mv -n`으로 fallback하지 않는다.

검토 완료는 형용사가 아니라 영속 객체로 증명한다. H2 뒤, 어떤 실제 target도 옮기기 전에
exact output을 묶은 별도 review-object write GO로 `SAFE_RENAME_REVIEW.json`과
`SAFE_RENAME_REVIEW_SHA256`을 만들고 다음을 기록한다.

```text
source/binary SHA-256 · SDK/ABI와 사용 flag · exact compiler/version/invocation
reviewer · verdict · 네 positive-control의 exact input/pre/post manifest
stdout/stderr/exit status와 각 evidence digest
```

이 review 객체와 helper source/binary를 H4/H5로 영속·독립 retrieval한 뒤,
`CLEANUP_MANIFEST`가 그 immutable object/version과 digest를 정확히 가리킬 때만 cleanup을
시작한다. helper/review가 현재 H4 freeze 뒤 만들어졌다면 기존 H4 manifest를 소급 수정하지 않고
별도 lifecycle handoff generation과 H5를 완료한다.
source edit GO · build/control execution GO · review-object write/commit/push/upload GO ·
cleanup GO는 서로를 대신하지 않는다.

helper의 최소 계약은 다음과 같다.

```text
입력       source parent path + single-component source leaf
           destination parent path + single-component fresh leaf
parent     O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW_ANY로 열고 fstat;
           manifest의 dev+inode와 일치해야 함
precheck   fstatat(..., AT_SYMLINK_NOFOLLOW)로 source dev+inode+type exact 일치;
           **source target st_dev == source parent st_dev == destination parent st_dev**;
           destination은 ENOENT. leaf는 '/', '.', '..', NUL을 허용하지 않음
mutation   renameatx_np(srcfd, srcleaf, dstfd, dstleaf,
             RENAME_EXCL|RENAME_NOFOLLOW_ANY|RENAME_RESOLVE_BENEATH) 정확히 1회
postcheck  source는 ENOENT, destination dev+inode+type은 pre-source와 exact 일치
failure    한 조건이라도 실패하면 다른 rename/copy/delete fallback 0
```

approved disposable probe에서 다음 네 positive control을 모두 통과해야 한다.

```text
성공             동일 filesystem rename 성공 + inode 보존
destination 충돌 EEXIST + source/destination 둘 다 불변
symlink          source/ancestor symlink 거부 + 양쪽 불변
cross-device     st_dev 불일치를 mutation 전에 거부 + 양쪽 불변
```

검증할 별도 filesystem probe를 안전하게 만들 수 없으면 cross-device control은 통과한 것으로
간주하지 않는다. ram disk/disk image/별도 volume 생성은 probe의 exact path·size·mount·cleanup을
묶은 별도 execution GO 없이는 하지 않는다. 그런 승인된 disposable second filesystem이 없으면
stub/unit branch test로 대체해 PASS를 주장하지 않고 cleanup을 `CLEANUP_PENDING`으로 유지한다.
helper/compile/control이 **어떤 실제 target도 옮기기 전** 실패하면
`CLEANUP_PENDING + OWNER_ACTION_REQUIRED`; 일부 target 뒤 실패하면 `CLEANUP_PARTIAL`이다.

삭제 직전 TOCTOU를 막기 위해 각 exact target에 다음 절차를 쓴다.

```text
1 해당 path의 producer/agent admission을 중지하고 open writer 0을 확인
2 regular-file set·type·size·mode·mtime_ns·SHA-256과 모든 directory의
  set·type·mode·mtime_ns·empty 여부를 재산출해 HANDOFF_MANIFEST.json의 archived
  source와 전수 비교. 모든 descendant의 st_dev가 target root와 같은지, 현재 mount table에서
  target 아래 descendant mountpoint가 0인지도 freeze manifest와 재검증. 하나라도 다르면
  CLEANUP_PENDING으로 중단
3 quarantine parent가 사전 destination/policy 승인과 cleanup GO가 가리키는 manifest 양쪽에
  동일하게 결속된 non-temp directory이고 승인된 provider/sync/backup/eviction state가 유지되는지
  확인. parent는 current-user
  owner·mode 0700·untrusted write ACL/group/world write 0이며 helper가 pinned dev+inode를 확인.
  source와 같은 filesystem이고 symlink ancestor 0이어야 하며, exact leaf는 승인 parent의
  direct child이자 `lstat`상 존재하지 않는 fresh single-component name이어야 함
4 일치할 때만 pinned helper로 exact target을 manifest의 exact quarantine path로
  **no-replace atomic rename**. 평범한 `mv`의 overwrite/nesting 동작, broad glob,
  사전 생성 destination은 금지
5 rename 뒤 resolved actual path가 승인 exact path와 같은지, old path에 새 항목이 생기지 않았는지,
  quarantine에 open writer 0인지 확인한다. content를 열기 전에 quarantine parent/root의 exact
  provider-domain/inverse-alias·sync/backup/eviction policy와 local residency,
  placeholder/`UF_DATALESS=0`을 post-rename 상태에서 다시 검사한다. drift·판별 불완전·dataless면
  hydrate하거나 rehash/delete하지 않고 `CLEANUP_PARTIAL + OWNER_ACTION_REQUIRED`로 보존한다.
  통과한 경우에만 quarantine을 재열거·rehash해 archived source와
  두 번째 전수 비교. descendant same-device와 descendant mountpoint 0도 새 exact path 기준으로
  다시 검증
6 두 비교가 모두 일치하면 신뢰된 parent를 다시 확인한 같은 subshell에서 pinned parent로
  `cd -P`하고 `stat -f '%d:%i' .`가 manifest의 dev:inode와 일치하는지 확인한다. rehash 뒤 delete
  직전 승인된 provider/sync/backup/eviction·inverse-alias policy와 parent/root local residency를
  다시 검사하고, 모든 entry를 no-follow로 재열거해 set/identity/flags 불변과
  placeholder/`UF_DATALESS=0`을 확인한다. 이 pre-delete pass 전후의 provider/root evidence도 같고,
  마지막 same-device 전수 검사와 mount-table descendant 0 검사가 다시 일치할 때만
  `/bin/rm -rf -- "./$approved_single_component_leaf"`를 실행. leaf는 `.`/`..`가 아닌
  single component이며 `./`를 붙여 option 해석을 막음. caller cwd의 bare leaf, glob,
  재귀 parent 삭제·경로 재해석 금지. 이 pass에서 drift·dataless·판별 불완전이면 hydrate/delete를
  하지 않고 `CLEANUP_PARTIAL + OWNER_ACTION_REQUIRED`. nonzero 또는 post-delete 잔존도 CLEANUP_PARTIAL
```

drift/missing/extra/symlink/mode 변화, 시작 전 이미 없어진 path, open writer, 재생성된 old path,
destination 선점/race·경로 불일치, 또는 비교 불일치가 하나라도 있으면 그 target을 삭제하지
않는다. source에 남아 있으면 그대로 보존하고, quarantine이 이미 되었다면 그 안에 보존한 채
`CLEANUP_PARTIAL / OWNER_ACTION_REQUIRED`로 종료한다.

```text
1 manifest가 `OWNED_REPRODUCIBLE`로 증명하고 H4/H5가 끝난 모든 standalone support/cache/
  compiled output (`fxi-s1a-files.txt`, `fxi-rehearsal-freeze.txt` 포함)
2 raw handoff·retrieval 검증이 끝난 superseded/predecessor decision·teardown·message draft
  (v3.1 임시 사본, C2_MSG.txt, s1a_commit_message.txt 포함)
3 정본·원격 또는 외부 store 검증이 끝난 현재 packet과 H0/H1/lifetime lifecycle output의
  임시 사본, helper의 비정본 build output. 영속 manifest/receipt·canonical HP attempt event와 canonical helper
  source/binary는 제외. **모든 HP attempt/generation root 하위 entry는 1~4단계에서 제외하고
  5단계의 attempt class 또는 generation별 mode로만 처리**
4 이관·검증된 A0와 contract-drift raw evidence 디렉터리 — 원 `/private/tmp` source 중 마지막
5 HP attempt material
  a `HP_ATTEMPT_ABORTED` root와 noncanonical registry staging residue — 아래 opaque 규칙
  b persistent HANDOFF_MANIFEST가 HP source/digest chain을 포함하고 대응 H5가 끝난 verified
    HP generation — 아래 A/B mode 중 하나만 선택
```

`HP_ATTEMPT_ABORTED` root와 event가 결속한 registry staging residue는
`HP_ATTEMPT_OPAQUE` target class다. coverage나 verified-generation A/B mode로 세지 않는다. H1이
disposition을 끝내고 exact opaque tree/event chain을 H4 archive한 뒤 대응 H5까지 끝난 경우에만,
root 또는 residue 하나씩 별도 CLEANUP_MANIFEST·별도 cleanup GO로 generic archived-root 전수
equality와 같은 quarantine/delete 절차를 적용한다. HP_MANIFEST/HP_SHA256의 존재를 요구하지 않는다.
canonical `HP_ATTEMPT_*` event가 승인·crash 이력의 영속 유일본이면 삭제하지 않고, H4/H5가 보존한
정본 외 local duplicate만 같은 gate로 정리한다.

verified HP generation root cleanup mode는 **generation별·manifest별로** 상호 배타적이다. 하나의
`CLEANUP_MANIFEST`는 HP generation 하나와 mode 하나만 다루며, 서로 다른 generation의
mode A/B target도 한 manifest에 섞지 않는다.

```text
A all-at-once
  모든 child disposition이 동시에 승인되면 child를 별도 target으로 열거하지 않고 HP root
  하나만 target. H4 HANDOFF_MANIFEST가 그 HP generation root 전체를 directory source entry와
  canonical tree aggregate로 보존했을 때만 generic step 2의 archived-root 전수 equality를 사용

B childwise
  mixed/순차 disposition이면 승인 child를 서로 다른 prefix-disjoint manifest로 개별 처리하고
  receipt를 누적. root는 모든 child가 resolved될 때까지 target이 아님. 마지막에는
  HP_MANIFEST/HP_SHA256 영속 사본 검증 + 모든 prior child CLEANUP_RECEIPT + root dev/inode +
  exact remaining regular-file/directory set·type·size·mode·hash/mtime·empty 여부와
  current-root digest를 묶은
  **새 immutable root-only CLEANUP_MANIFEST와 별도 cleanup GO**를 받음.
  이 final root action은 original archived-root equality를 주장하지 않고 receipt-chain + exact
  current-state predicate를 generic step 2 대신 사용한 뒤 같은 atomic quarantine/delete를 적용
```

같은 generation에서 mode A의 root와 child, 또는 mode B의 child와 root를 같은 manifest에 함께
넣지 않는다. 서로 다른 generation도 각각 별도 manifest·별도 cleanup GO를 사용한다.

source target과 quarantine path를 합친 전체 집합은 **prefix-disjoint**여야 하며, source와
quarantine이 어느 방향으로도 ancestor/descendant 관계이면 manifest를 거부한다. 부모 raw-evidence
directory가 target이면
그 아래의 compiled/cache child(예: A0 `classes/OkHttpRetryHarness.class`, drift의
`generated-v2{,-r2}`)는 부모의 일부로만 4단계에서 처리하고 1단계 별도 target으로 먼저
삭제하지 않는다. 겹치는 parent/child target이 있으면 manifest를 거부한다.

HP root에 `UNRESOLVED`, `FOREIGN`, 아직 disposition이 없는 child가 하나라도 남으면 root 전체는
삭제 대상이 아니다. prefix-disjoint인 승인 child만 개별 처리하고 나머지는
`OWNER_ACTION_REQUIRED`로 보존한다.

`FOREIGN`, `UNRESOLVED`, `EXTERNAL_INPUT`, disposition 미승인 `OWNED_REQUIRED`, 또는 H4에서
제외된 required payload는 위 순서에 진입하지 않는다. 특히 공유 Gradle cache의 6개 jar와
Docker image는 별도의 exact owner/disposition 및 cleanup GO 없이는 삭제·prune 대상이 아니다.

삭제 직후 old path·quarantine exact-path absence, 관련 repo status, 실패 항목을 확인하고
`CLEANUP_RECEIPT`를
`CLEANUP_COMPLETE` 또는 `CLEANUP_PARTIAL`로 작성한다. receipt를 Git에 둘 경우 그 사후 기록의
commit GO와 push GO도 각각 받고 원격을 검증한다. 외부 store이면 업로드 뒤 독립
retrieval/hash를 검증한다. 영속
handoff/cleanup manifest와 receipt는 삭제하지 않는다.
`CLEANUP_RECEIPT`는 exact CLEANUP_MANIFEST/GO identity와 pre-rename,
post-rename/pre-hash, post-hash/pre-delete의 destination-policy/local-residency/entry-flags evidence
digest 및 verifier/runtime binding을 모두 결속한다. AUX target이면 original AUX digest·대응 H5
immutable object·승인 quarantine identity·최종 local absence도 결속한다. 어느 checkpoint도
생략되면 CLEANUP_COMPLETE나 AUX_LOCAL_CLEANED가 아니다.
`/private/tmp` 삭제는 일반적으로 복구를 전제하지 않는다.

### 작업 종료 뒤 문서 보존 원칙

관련 문서를 전부 지우는 것이 아니라 **역할별로** 처리한다.

```text
영구 보존  승인된 current canonical document · 원시 evidence package ·
           HANDOFF/HP/HELPER_REVIEW/CLEANUP manifest·hash·receipt · 승인/판정 이력
이력 보존  canonical 문서가 근거로 참조하는 superseded packet/teardown/message draft의
           byte-identical archive copy
삭제 가능  위 두 부류의 H5 retrieval과 cleanup manifest/GO가 끝난 뒤 남은 /private/tmp
           working copy · 재현 가능한 cache/build output · 중복 draft
삭제 금지  FOREIGN · UNRESOLVED · 유일본 · 아직 H4/H5가 끝나지 않은 source ·
           영속 manifest/receipt 또는 canonical helper source/binary
```

즉 “관련 작업 완료”는 코드 구현 완료가 아니라 **정본·이력·원시 증거의 독립 retrieval 검증과
해당 exact temp copy의 cleanup GO까지 완료**됐다는 뜻이다. 그 전에는 문서가 superseded라는
이유만으로 삭제하지 않는다.
