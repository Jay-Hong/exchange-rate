# D21 아키텍처 검증 spike — Phase A0 OFFLINE 보고서

- 실행 시각: 2026-09-04 03:53 KST
- 실행자: Codex
- 목적: Android D21 FCM 등록/해제의 순서 역전, 계정 삭제 후 부활, retry/timeout 열화 분기와 server ordering 대안을 리포 수정 없이 검증
- 결론: **현행 FAIL · client-only FAIL · 실험용 server reducer v3 FAIL · 수정된 server-ordering 아키텍처 계열 PENDING_EVIDENCE**

## 1. 범위와 안전 통제

실행에 사용한 것은 다음뿐이다.

- 읽기 전용 server worktree: /Users/jay/Downloads/Projects/FXi/wt-fcm-cleanup
- 읽기 전용 Android repo: /Users/jay/Downloads/Projects/FXi/android
- 읽기 전용 iOS repo: /Users/jay/Downloads/Projects/FXi/ios
- 증거·harness: /private/tmp/fxi-d21-a0.aWQ3tH
- disposable PostgreSQL container: fxi-d21-a0-pg-awq3th
  - immutable local image: sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73
  - image tag: postgres:17-alpine
  - 실제 container PostgreSQL: 17.11 (server_version_num=170011)
  - 운영과 major 17만 일치하며 minor parity 주장은 하지 않는다.
  - network none, mount/volume 0, tmpfs DB, synthetic UID/token만 사용
- OkHttp 검증: 기존 local cache의 고정 okhttp-4.12.0.jar와 mockwebserver-4.12.0.jar
- localhost 이외 external network, production API/DB/Firebase/EC2/credential/실사용 데이터 사용 0
- image pull, prune, commit, push, deploy 0
- Python 실행은 PYTHONDONTWRITEBYTECODE=1 python3 -B

베이스라인:

| 대상 | HEAD | tree | 상태 |
|---|---|---|---|
| server isolated worktree | d35e0b311ad264ade4716f9abd01447f2349cd90 | 391e3e3dcd3b59d2f1c64b3b2a8f57daa8d6246f | clean |
| Android | 98012cdcbc9d53d2f32887f01fcda21b4223a623 | 65f00e2f2f5f44495f745df976e84aae7a129eaf | clean |

실행 중 origin/master가 361597985d12ee4082944e20c9b7ce4c00030d54로 전진했지만, 고정 worktree HEAD/tree는 이동하지 않았다. 이는 UPSTREAM_ADVANCED일 뿐 A0 증거 오염이 아니다.

## 2. 증거 등급

| 등급 | 의미 |
|---|---|
| REPRODUCED_POSTGRES | disposable PostgreSQL 17에서 해당 최종 상태를 실제 관측 |
| COMPONENT_RUNTIME_CONFIRMED | 고정 OkHttp 4.12.0 + MockWebServer에서 요청 수/메서드 등을 실제 관측 |
| SOURCE_PROVEN | 고정 리포·라이브러리 소스/설정으로 성질이 확정 |
| GENERIC_POSITIVE_CONTROL | 현상 가능성은 실증했지만 실제 Android→Nginx→Uvicorn→FastAPI E2E는 아님 |
| STRUCTURAL | 현재 계약으로 두 사건을 구분/거부할 정보가 없다는 설계 논증 |
| CURRENT_STACK_E2E_UNPROVEN | 실제 전체 경로의 runtime 재현은 아직 하지 않음 |

## 3. 현행 구현 hard-gate 결과

현행 SQL과 동일한 UPSERT/UID+token DELETE/purge/retention/account-delete 의미를 PostgreSQL에 옮겨 도착 순서를 통제했다.

| 시나리오 | 기대 | 관측 | 판정 |
|---|---|---|---|
| POST(B,T) → 늦은 POST(A,T) | B 유지 | 최종 A:T | **FAIL · REPRODUCED_POSTGRES** |
| POST(A,T,g1) → POST(A,T,g2) → 늦은 DELETE(A,T,g1) | g2 유지 | 행 삭제 | **FAIL · REPRODUCED_POSTGRES** |
| account-delete(A) → 늦은 POST(A,T) | 행 부활 금지 | 최종 A:T | **FAIL · REPRODUCED_POSTGRES** |
| owner-fenced purge(A,T) after token moved to B | B 유지 | purged=0; B:T | PASS · 기존 FCM fence 범위 |
| FCM purge(A,T) → 늦은 POST(A,T) | 죽은 행 부활 금지 | 최종 A:T | **FAIL · REPRODUCED_POSTGRES** |
| B 자격 DELETE(A의 T) | A 부재 증명 금지 | 404이지만 A:T 존속 | **확인됨** |
| DB commit 뒤 응답 유실 | client 실패=무변경 금지 | client unknown, A:T commit | **확인됨** |
| retention 양성 대조 | 새 행 보존, 8일 행 삭제 | 그대로 관측 | PASS |

근거가 되는 현행 코드:

- 등록은 token conflict마다 user_id를 무조건 덮는 UPSERT이고 내부에서 commit한다: app/crud.py:1791-1816.
- commit 뒤 SELECT·로그·응답 생성이 남아 있어 client 실패는 rollback 증거가 아니다: app/crud.py:1811-1839, app/main.py:3741-3754.
- 삭제는 caller UID + token으로 지우고 내부 commit한다: app/crud.py:1860-1877.
- 계정 삭제는 UserDevice 포함 8계열을 지우고 한 번 commit하지만 UID barrier가 없다: app/main.py:4732-4797.
- UserDevice.user_id는 평범한 문자열이고 FK는 없다: app/models.py:71-80. 삭제 뒤 늦은 요청이 같은 UID 행을 다시 만드는 것을 DB가 막지 않는다.

### 단순 barrier의 TOCTOU 양성 대조

barrier 확인 → sleep → UPSERT와 barrier 기록 + 기존 행 삭제를 다른 transaction으로 실행했다.

~~~text
register: barrier 없음 확인 후 정지
account delete: barrier 기록 + 삭제 commit (약 0.10s)
register: 재개 후 UPSERT commit (약 1.08s)
최종: A:T 부활
~~~

검사와 mutation이 같은 직렬화 지점/transaction이 아니면 barrier 자체가 TOCTOU를 다시 만든다.

## 4. server reducer 실험

### 4.1 첫 후보 — 단순 generation/head

기본 stale POST/DELETE, UID barrier, legacy fence, 두 barrier stop-point는 통과했다. 그러나 다음 반례가 재현됐다.

~~~text
FCM send snapshot: A,T,g1
더 최신 등록:       A,T,g2
늦은 purge:         owner+token만 대조
결과:               g2 행 삭제, head TOKEN_RETIRED:g2
~~~

owner+token만으로는 부족하며 purge/retention도 installation + generation + server_revision exact CAS가 필요하다.

### 4.2 v3 후보 — desired/effective 분리 + exact cleanup CAS

v3는 UID barrier, token ordering head, installation lineage head, desired/effective state, active row, operation receipt, exact cleanup CAS, 단일 advisory transaction lock을 사용했다.

기본 18개 순차·동시성 케이스는 모두 통과했다.

- stale POST/DELETE
- 동일 op replay / generation conflict
- stale purge
- purge 후 새 intent
- retention positive control
- account-delete barrier와 두 pause point
- claimed-token legacy fence / unclaimed iOS legacy 허용
- 서로 다른 lineage의 token 충돌·swap
- purge↔fresh registration 양방향

그러나 적대적 케이스 8개를 추가하자 **0/8**이었다.

| 반례 | 실제 관측 | 영향 |
|---|---|---|
| INACTIVE가 current head와 다른 임의 T2를 전달 | T2까지 RETIRED claim; 미래 B가 T2 claim 불가 | token poisoning / liveness |
| account-delete 뒤 옛 op-id replay | IDEMPOTENT_REPLAY:APPLIED_ACTIVE, 현재 ACCOUNT_DELETED | historical receipt가 현재 상태를 위장 |
| 더 최신 legacy B 뒤 늦은 최초 v3 A claim | 최종 A:T | cutover hard-gate FAIL |
| account-delete 뒤 새 installation I2가 같은 T 회수 | 영구 TOKEN_CLAIM_CONFLICT | bounded recovery 없음 |
| external purge 뒤 옛 op-id replay | 과거 APPLIED_ACTIVE, 현재 FCM_UNREGISTERED | current state 은폐 |
| token head 손상 뒤 purge | purge 성공; legacy가 T 재등록 | token-head CAS/rowcount 불완전 |
| client generation=BIGINT_MAX | 적용 후 미래 진행 불가 | lineage 영구 lockout |
| generation=0 | 정상 ACTIVE 적용 | 입력 domain 미검증 |

판정:

~~~text
v3 스크립트 그대로: FAIL

수정된 architecture family: PENDING_EVIDENCE
- INACTIVE는 새 token claim을 만들지 않고 current head에서 파생하거나 exact-match
- 최초 legacy→v3 claim ordering 규칙
- receipt의 historical outcome과 current state/revision 분리
- token-head exact CAS + 완전한 3자 invariant
- generation domain/durability/recovery
- reinstall/rekey, HMAC rotation/GC, production lock 설계
~~~

18/18을 PASS로 승격하지 않는 이유:

- 하나의 global lock이 동시 케이스를 직렬화했을 뿐 production lock granularity/부하는 검증하지 않았다.
- retention 전체 scan도 같은 lock을 잡아 모든 사용자의 등록·로그아웃·계정삭제를 막는다.
- invariant oracle가 token-head의 CLAIMED/generation/revision/rowcount drift를 놓쳤다.
- 임시 DDL의 MD5는 synthetic placeholder다. production에는 keyed/versioned HMAC과 key rotation이 필요하다.
- 실제 SQLAlchemy 예외/rollback, process kill, mixed-version writer, PITR restore는 범위 밖이었다.

## 5. retry·timeout 결과

### 5.1 고정 OkHttp 4.12.0 component runtime

| 응답/조건 | 관측 |
|---|---|
| POST 408 + Retry-After: 0 | POST 2회 |
| DELETE 408 + Retry-After: 0 | DELETE 2회 |
| POST 503 + Retry-After: 0 | POST 2회 |
| POST 503, Retry-After 없음 | POST 1회, 최종 503 |
| POST 307 same-host | POST 2회, 경로 변경, body 길이 유지 |
| DELETE 308 same-host | DELETE 2회, 경로 변경 |
| POST 401, Authenticator.NONE | POST 1회 |
| retryOnConnectionFailure(false) + 408:0 | POST 1회 |
| 같은 flag + 503:0 | **POST 2회** |

판정:

- 위 요청 수는 COMPONENT_RUNTIME_CONFIRMED.
- 307/308은 메서드와 body 길이 보존을 관측했다. byte 완전동일·DELETE query 보존은 미실증이다.
- retryOnConnectionFailure(false) 하나로 mutation follow-up을 모두 막을 수 없다.
- disconnect-after-request의 1 request + ConnectException은 내부 connection attempt를 계측하지 않아 INCONCLUSIVE다.
- 앱 builder 미설정과 OkHttp 4.12.0 default retry=true는 SOURCE_PROVEN이다. harness는 true를 명시했으므로 default 자체의 runtime 증명은 아니다.

### 5.2 whole-call hard bound

NO_HARD_BOUND는 현재 소스/설정으로 확정된다.

- Android는 connect/read/write timeout만 15초이고 callTimeout은 설정하지 않는다: NetworkModule.kt:58-77.
- auth interceptor의 Tasks.await(getIdToken(false))에 deadline이 없다: NetworkModule.kt:39-53.
- server auth executor queue는 무제한이고 caller cancellation 뒤 running worker가 계속된다는 계약이 있다: app/auth_executor.py:29-31,315-319,383-397.
- Nginx API proxy timeout은 10/10/30초지만 upstream은 하나다: nginx/conf.d/default.conf:11-14,115-132.
- Uvicorn CMD에는 request execution deadline이 없다: Dockerfile:131.
- DB statement_timeout은 statement 단위이지 요청 전체 상한이 아니다: app/database_settings.py:62.

이는 실제 요청이 늘 오래 걸린다는 뜻이 아니라, 모든 in-flight mutation이 N초 뒤에는 절대로 commit할 수 없다는 enforcement가 없다는 뜻이다.

### 5.3 timeout 이후 late commit 양성 대조

localhost http.client → ThreadingHTTPServer → PostgreSQL barrier에서 POST와 DELETE 각각 다음 순서를 관측했다.

~~~text
handler barrier 도달 < client TimeoutError < PostgreSQL commit
~~~

이는 GENERIC_POSITIVE_CONTROL이다. 즉 client timeout 사실만으로 server mutation 부재를 증명할 수 없다. 실제 Retrofit→Nginx→Uvicorn→FastAPI→SQLAlchemy late commit은 CURRENT_STACK_E2E_UNPROVEN이며 운영 사고 발생 주장도 하지 않는다.

## 6. mutation inventory와 production 통합 요구

| 계열 | 현재 경계 | ordering 도입 시 필수 변경 |
|---|---|---|
| 등록 UPSERT | crud.py:1749-1857, 내부 commit :1811 | 내부 commit 제거; row+head+receipt 단일 transaction |
| UID+token DELETE | crud.py:1860-1884, 내부 commit :1877 | versioned transition/reducer; legacy 우회 금지 |
| FCM purge | crud.py:1934-2052, caller commit | send 시점 uid+installation+token+generation+revision, exact CAS |
| retention | scheduler.py:1220-1262, 독립 session/commit | head+token+active row CAS; legacy 정책 별도 |
| account-delete | main.py:4732-4797, 8계열 단일 commit | UID barrier+heads+active row+기존 8계열을 같은 transaction/lock |

FCM purge commit 소유자는 여섯 갈래다.

1. sync push: app/main.py:179-182
2. legacy FX: app/crud.py:2761-2765
3. legacy source: app/crud.py:3921-3925
4. Source backend: app/notifications/alert_storage_backend.py:191-202
5. FX canary backend: app/notifications/alert_storage_backend.py:445-454
6. Comparison evaluator: app/notifications/comparison_evaluator.py:435-466

모든 producer가 exact cleanup snapshot을 운반해야 한다. 별도 active_delivery_rows를 만들면 기존 user_devices reader/writer의 dual-write/shadow/read cutover가 필요하므로, 현 테이블 확장안과 비교해야 한다.

## 7. client-only 판정

client-only는 최소 PREPARED → DISPATCHED_MAY_HAVE_MUTATED → COMMIT_CONFIRMED 또는 NO_MUTATION_PROVEN 상태, 모든 요청 전 write-ahead, durable hold/drain, crash 복구가 필요하다.

그러나:

1. timeout/응답 유실 뒤 commit 여부를 client가 알 수 없다.
2. read-back은 현재 순간만 보여주며 이미 전송된 요청의 미래 도착을 배제하지 못한다.
3. hard end-to-end 요청 수명 상한이 없다.
4. FCM rekey는 T의 delivery effect quarantine만 증명하며 계정삭제 뒤 DB 행 부활을 막지 못한다.
5. server-side UID barrier/FK가 없어 client가 account-delete resurrection을 차단할 수 없다.

| client-only 선택 | 판정 |
|---|---|
| MAY_HAVE_MUTATED를 무시하고 account-delete/B 활성화 | **safety FAIL** |
| 영원히 hold | **liveness FAIL**, 삭제 복구 불능 및 중대한 심사·준수 위험 |
| rekey만 수행 | 전달 위험 일부 격리, DB 행 부활 hard gate는 FAIL 유지 |

**client-only 최종 판정: FAIL.** Phase A1 Firebase rekey는 이를 hard-gate PASS로 만들 수 없으므로 A0 결과만으로 자동 진입하지 않는다.

## 8. 권장 server architecture family

이는 구현 확정안이 아니라 다음 검증 단계의 최소 후보 집합이다.

### Control/data 분리

1. UID-keyed deletion barrier: keyed/versioned HMAC, deletion id, key version, lifetime/GC/PITR runbook
2. token-keyed ordering/claim head
3. installation-keyed lineage head: max generation, token HMAC, UID HMAC, desired/effective state, server revision
4. active delivery row: plaintext FCM token은 전달 활성 동안만
5. idempotent operation receipt: historical outcome/applied revision과 current state/revision 분리

### Reducer 불변식

- UID barrier → lineage → 정렬된 token key의 하나의 직렬화 규칙
- missing-row SELECT FOR UPDATE만 믿지 않고 advisory/sentinel/unique-conflict로 gap 직렬화
- account delete barrier 기록 + 기존 모든 행 삭제 원자화
- REGISTER barrier 확인 + UPSERT 원자화; 이미 인증을 통과한 요청도 mutation 시점 barrier 확인
- ACTIVE만 새 token claim 가능
- INACTIVE는 current head에서 파생하거나 exact current token+revision일 때만 허용
- purge/retention은 captured uid+lineage+generation+server revision exact CAS
- active row·lineage head·token head가 하나라도 어긋나면 transaction rollback
- generation의 양수/상한/rollback/reinstall 복구 계약

### API 방향

versioned body endpoint 요청 최소 필드:

~~~text
operation_id
installation_id
client_generation
desired_state
device_token / platform (ACTIVE)
expected_server_revision 또는 exact current-token 증거 (INACTIVE)
~~~

UID는 인증 snapshot에서만 파생한다. stable outcome은 APPLIED, IDEMPOTENT_REPLAY, STALE_REJECTED, GENERATION_CONFLICT, TOKEN_CLAIM_CONFLICT, DELETION_BARRIER_REJECTED, LEGACY_FENCED 등을 구분한다. acting UID echo는 이미 일어난 잘못된 mutation을 되돌리지 못하므로 fence가 아니다.

## 9. migration / rollback matrix

| 단계 | 동작 | 필수 evidence |
|---|---|---|
| Schema dormant | control 테이블/컬럼만 explicit migration | database.py:49-64 create-all 제외, behavior change 0 |
| Compatibility writer | 5개 mutation 계열 모두 reducer lock/barrier, claim OFF | old direct writer 0 |
| Legacy backfill/shadow | 기존 row를 LEGACY_UNORDERED로 반영, 가짜 generation 금지 | old/new reader 대조 |
| 최초 v3 claim canary | current owner 확인 또는 명시적 transfer/drain/rekey | legacy↔v3 양방향 반례 0 |
| Barrier activation | account-delete 8계열과 UID barrier 원자 통합 | 두 pause point, response loss, stale POST |
| Reader cutover | 모든 발송 reader가 active/effective state 사용 | old-table reader/direct writer 0 |
| 확대 | invariant·부하·backup restore·rollback drill | hard gate 전항 |

Rollback은 비대칭이다.

- claim/barrier 활성화 전에는 feature OFF와 구 binary rollback 가능. schema는 남긴다.
- token claim/UID barrier가 하나라도 생긴 뒤에는 구 binary가 control state를 무시하므로 binary rollback 금지, roll-forward 기본.
- 구 binary rollback이 필수라면 DB trigger/permission이 old direct writer까지 fence해야 한다.
- head/barrier/receipt drop 또는 과거 DB snapshot만 복원하는 rollback 금지.
- post-claim 구 Android는 등록이 거부돼 push가 끊길 수 있다. 안전한 실패지만 UX 비용이다.
- iOS/unclaimed legacy는 현 계약 유지가 가능하지만 플랫폼 차이를 security fence로 사용하지 않는다.

## 10. hard-gate 최종 verdict

| 대안 | verdict | 근거 |
|---|---|---|
| 현행 | **FAIL** | 소유권 역전·새 등록 삭제·계정삭제 후 부활 재현 |
| client-only durable journal/hold | **FAIL** | account-delete resurrection 해결 불가, bounded recovery 없음 |
| check-then-write UID barrier | **FAIL** | TOCTOU 부활 재현 |
| 단순 generation/head 후보 | **FAIL** | stale purge가 newer generation 삭제 |
| 실험용 v3 reducer 그대로 | **FAIL** | 적대적 8개 반례 전부 실패 |
| 수정된 server ordering + UID barrier 계열 | **PENDING_EVIDENCE** | 기본 직렬화 가능성은 확인, cutover·reclaim·HMAC/GC·실제 ORM/부하/restore 미실증 |

**Phase A0에서 PASS인 대안은 없다.**

## 11. 다음 단계

1. Claude가 A0 report와 harness를 독립 검토한다.
2. frozen D21 개정 전에 다음 결정을 좁힌다.
   - INACTIVE exact semantics
   - 최초 legacy→v3 claim/transfer
   - installation/generation reinstall 복구
   - UID barrier HMAC 수명·key rotation·PITR
   - global lock 부하 vs fine-grained lock ordering
   - 현 user_devices 확장 vs 별도 active table
3. 그 뒤에만 실제 server/Android 동반 slice와 current-stack E2E를 별도 GO로 설계한다.
4. Firebase rekey A1은 delivery quarantine 증거가 필요할 때 별도 GO로 하되 account-delete ordering 대체재로 취급하지 않는다.

이번 A0만으로 D21 정책을 client-only 상태기계로 확정하거나 durable retry를 구현하면 안 된다.

## 12. 재현 파일

- a0_pg_harness.py — 현행 반례·첫 reducer 후보·barrier pause point
- a0_reducer_v3.py — desired/effective + exact cleanup CAS 후보, 기본 18개
- a0_reducer_v3_adversarial.py — v3 적대적 반례 8개
- a0_timeout_barrier.py — timeout 뒤 late commit generic positive control
- OkHttpRetryHarness.java — 고정 OkHttp 4.12.0 component retry/follow-up

SHA-256:

~~~text
cafd6c100b5dc3fd3be0a0e4962c44db9b847a955932990e6ff8bf17d3270664  a0_pg_harness.py
8d778886e59ca3e2f7050a01e7dbcd20b715f16430db408215fb16b2f8ffffa6  a0_reducer_v3.py
917f6da15cda8be65ea6118959f5cfb6548e0c55d2ee0aa1434f38099f044f59  a0_reducer_v3_adversarial.py
07ac24370f0bfd316dcf3100c2eed67fcbe83e5bedd95da4242368ab3d0fff45  a0_timeout_barrier.py
bbfd6ae570204e2730bc1903fd86ffd35f9f6071211c9274a61763f685871a8c  OkHttpRetryHarness.java
b1050081b14bb7a3a7e55a4d3ef01b5dcfabc453b4573a4fc019767191d5f4e0  okhttp-4.12.0.jar
6784673687f4ac8f21679b9d4bc7cdb46e1a1ce1be9d3133b36bede59a741561  mockwebserver-4.12.0.jar
~~~

## 13. 범위 밖 / 미실행

- production incident 존재 여부
- 실제 Android→Nginx→Uvicorn→FastAPI→SQLAlchemy late commit E2E
- Firebase rekey
- 실제 repo 코드·문서·설정 수정
- commit/push/deploy

## 14. 종료 검증

- disposable container fxi-d21-a0-pg-awq3th 제거 완료; 동일 이름 container 잔존 0.
- server isolated worktree HEAD/tree는 시작값과 동일하고 status clean.
- server의 기존 pycache/pytest-cache artifact는 시작·종료 모두 465개, 합성 SHA-256도 다음으로 동일:
  - 66570205b8898866f0d5c7634e9b5b8c6c42e2aa7b24607b569ec80ca64b0b1e
- Android HEAD/tree는 시작값과 동일하고 status clean.
- iOS status clean.
- root worktree는 다른 세션이 전진시킨 HEAD 361597985d12ee4082944e20c9b7ce4c00030d54이며, 기존 untracked 계획서 1개와 stash 1건을 그대로 보존했다.
- 리포지토리 파일 변경, commit, push, deploy는 모두 0.
- /private/tmp evidence 디렉터리는 Claude 독립 검토를 위해 보존한다.
