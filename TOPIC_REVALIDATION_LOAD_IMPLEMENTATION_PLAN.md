# Topic Revalidation Load Implementation Plan

## 문서 역할

**상태: Draft — 구현 순서와 인계용이며, 구현·배포 GO가 아니다.**

이 문서는 45초 재검증 폭주를 흡수하기 위한 **비규범 구현 순서와 인계 경계**다. 확정 계약의
정본은 다음 RID에 있으며, 충돌하면 그 문서가 우선한다.

- 서버 흡수 계약: [R-LOAD-3](spec/revalidation-and-load.md#r-load-3)
- 즉시거절 금지: [R-LOAD-4](spec/revalidation-and-load.md#r-load-4)
- snapshot 종결·deadline: [R-HAND-14](spec/topic-snapshot-handoff.md#r-hand-14),
  [R-HAND-16](spec/topic-snapshot-handoff.md#r-hand-16),
  [R-HAND-4](spec/topic-snapshot-handoff.md#r-hand-4)
- 클라이언트 jitter·재시도 상한·cooldown:
  [R-CLI-24](spec/ios-topic-state-machine.md#r-cli-24)

여기서 쓰는 `LOAD-S3` 같은 이름은 과거 `R-GATE-1`의 subscribe-load 계측 슬라이스 `S3~S7`과
**다른 namespace**다. bare `S3`로 부르지 않는다.

### 폐기 조건

이 문서는 영구 정본이 아니다. `LOAD-S4`의 통합 활성화와 합격 검증이 land되면 같은 변경에서
이 파일을 삭제한다. 삭제 전에 구현 과정에서 확정된 결정 중 기존 RID가 소유하지 않는 내용만
`DECISIONS.md` 또는 해당 spec의 정본 계약으로 승격한다. 이미 정본에 있는 계약과 단계별 구현
상세는 중복 이관하지 않는다. 구현 상세와 검증 결과는 각 슬라이스의 코드·테스트·커밋 기록이
소유한다.

따라서 완료 뒤 이 문서를 `Completed` 상태로 보존하거나, 삭제 시점을 별도 후속 작업으로 미루지
않는다. `LOAD-S4`가 구현됐더라도 활성화·합격 검증이 land되지 않았다면 아직 폐기하지 않는다.

## 현재 경계

### 완료된 바닥

- `LOAD-S1`: WS/REST Firebase 인증이 별도 executor lane과 named app을 사용한다.
- `LOAD-S2-DB`: PostgreSQL pool 대기, 물리 연결, statement 실행에 각각 유한 상한을 전달한다.
- `LOAD-S2-REDIS`: snapshot sync Redis는 read/connect 1초, pool 대기 1초, 최대 50 connection으로
  제한한다. caller 취소 직후에는 `to_thread` worker가 계속 돈다는 반대 상태와, read timeout 뒤
  connection이 pool로 돌아오는 상태를 실제 stalled RESP 서버로 함께 검증한다.
- `LOAD-S3`: ACK 전 예산, ACK 후 snapshot 총예산, 요청 전체 absolute deadline을 한 monotonic
  시간축에 결속했다. caller 취소·deadline 뒤 worker는 현재 bounded I/O를 끝낸 다음 checkpoint에서
  멈추고, 새 Redis/DB phase를 시작하지 않는다. post-ACK deadline/transient는 1013, fatal은 1011로
  terminal payload 없이 종결하며 REST twin의 deadline은 no-store 503으로 변환한다.
- `LOAD-S5`: WS/REST가 공유하는 snapshot build를 topic generation 단위 single-flight로 합치고,
  성공 결과만 최대 1초 cache한다. authorization은 공유 전에 끝나며 각 waiter는 독립된 payload
  복사본과 요청 budget을 가진다. leader 취소는 남은 waiter의 shared build로 전파되지 않고,
  마지막 waiter가 사라진 경우에만 shared build를 취소한다.
- mutation runner 8개는 공유 worktree를 직접 변이하지 않고 격리 worktree에서 실행한다.

`LOAD-S2-DB`는 statement 하나와 pool/connect phase를 유한하게 만들 뿐이다. 여러 statement의 합,
여러 topic의 합, caller 취소 뒤 계속 도는 `to_thread` worker의 전체 수명은 제한하지 않는다.
`LOAD-S2-DB` 완료 표시는 **PostgreSQL만** 가리켰다. 후속 감사에서 WS와 REST가 공유하는
`build_snapshot_observed()`가 sync builder를 default executor에 보내고, FX/USDT/KRX payload가 모두
`latest_rates_cache._get_sync_client()` 하나를 거쳐 Redis-first read한다는 호출 그래프를 확인했다.
기존 read/connect 1초는 유한했지만 pool connection 수가 사실상 무제한이고 취소 후 반환 양성대조가
없었다. `LOAD-S2-REDIS`가 이 두 누락을 닫았고, `LOAD-S3`가 여러 Redis/DB 호출의 합과 취소된
worker의 다음 phase 진입을 제한했다. `LOAD-S5`는 그 bounded build를 같은 key의 연결들이
공유하게 해 동시 작업량을 연결 수가 아니라 활성 key 수에 가깝게 줄였다. `LOAD-S6/S7`은 FIFO
admission과 서버 실패 cooldown을, iOS `45a8a12`는 `R-CLI-24` jitter·retry cap·client cooldown을
구현했다. 서버 `R-LOAD-3`과 클라이언트 `R-CLI-24` 구현은 land됐지만 waiter queue의 **개수** hard
cap은 없고 LOAD-S4 부하 리허설·통합 활성화도 남아 있다. 따라서 `자원 상한 완료`, end-to-end 폭주
완화 완료 또는 운영 활성화 완료라고 쓰지 않는다.

### 운영 상태는 별도 재확인

코드 land와 운영 활성은 다른 상태다. 배포 전에는 cron의 maintenance profile, online profile,
실제 서버 `statement_timeout`, 롤백 이미지, 현재 rollout pin을 다시 확인한다. 이 문서는 특정
호스트의 현재 상태를 정본으로 기록하지 않는다.

## 구현 순서

```text
LOAD-S2-DB (PostgreSQL phase 상한, 완료)
  -> Redis I/O 상한 감사·누락 보강 (완료)
  -> LOAD-S3 (요청 전체 예산 + worker 협력 중단, 완료)
  -> LOAD-S5 (topic single-flight/cache, 완료)
  -> LOAD-S6 (bounded wait + 1013 종결)
  -> LOAD-S7 (서버 실패 cooldown)
  -> LOAD-S4 (통합 활성화)
```

번호 순서가 실행 순서가 아니다. `LOAD-S4`는 최종 activation slice라 마지막이다.

## LOAD-S3 — 요청 전체 예산과 협력 중단

### 목표

ack 전 예산, ack 후 snapshot 총예산, 요청 전체 absolute deadline을 **한 monotonic 시간축**에서
관리한다. caller timeout만 거는 것으로 끝내지 않고, 이미 실행 중인 sync worker도 다음 blocking
phase를 시작하지 않게 한다.

### 계약

- 요청당 absolute deadline은 한 번 만들고 topic loop와 worker에 전달한다.
- snapshot이 실제로 쓰는 Redis 경로는 connect/read가 유한하고 caller 취소 뒤 socket을 반환한다는
  양성대조를 먼저 갖는다. async `wait_for` 존재만으로 내부 I/O 해제를 추정하지 않는다.
- async caller는 남은 예산까지만 queue/build/send를 기다린다.
- `to_thread` 취소가 worker를 멈춘다고 가정하지 않는다.
- worker는 DB/Redis 호출 **사이**와 다음 statement 시작 전에 deadline/cancel state를 확인한다.
- 실행 중인 SQL은 `LOAD-S2`의 `statement_timeout`이 끝낸다.
- 다른 thread에서 SQLAlchemy `Session.close()`나 psycopg connection을 강제 조작하지 않는다.
- post-ACK 총예산 초과는 terminal payload 추가 전송 없이 close 1013으로 끝낸다.
- fatal build 오류의 1011 계약과 transient/deadline 1013 계약을 합치지 않는다.

### 필수 검증

- 첫 statement 뒤 deadline이 끝나면 두 번째 statement를 시작하지 않는다.
- caller 취소 직후 worker connection이 반환됐다고 거짓 단언하지 않는다. 실행 중 statement는
  S2 상한 안에 끝나고 그 뒤 pool checkout이 0으로 돌아와야 한다.
- queue에서 만료, worker 실행 중 만료, topic 사이 만료가 각각 같은 absolute deadline을 쓴다.
- timeout/cancel 경로에서 session rollback·close와 executor ledger가 누수 없이 복귀한다.
- close 1013은 한 번만 발생하고 post-ACK terminal frame과 중복되지 않는다.

### 비범위

- topic single-flight, cache, waiter admission은 `LOAD-S5/S6`가 소유한다.
- cross-thread 강제 취소는 이번 단계에서 도입하지 않는다.

## LOAD-S5 — topic single-flight와 짧은 cache

### 목표

같은 topic의 동시 snapshot build를 하나로 합쳐 DB/Redis 작업량을 연결 수가 아니라 **활성 key 수**에
가깝게 만든다. authorization 판단은 공유하지 않고 build 결과만 공유한다.

### 계약

- key에는 payload를 바꾸는 모든 입력과 generation이 들어간다. 사용자별 정책 결과는 cache하지 않는다.
- leader task의 caller 취소가 남은 waiter의 공유 build를 취소하지 않는다.
- 마지막 waiter가 사라졌을 때의 정리 소유권을 하나로 둔다.
- 성공 cache와 실패 cooldown은 별개다. transient/fatal/auth failure를 성공 cache에 넣지 않는다.
- cache TTL과 invalidation은 publisher 최신성 계약보다 느슨해질 수 없다.
- single-flight 내부 build도 `LOAD-S3` deadline과 `LOAD-S2` I/O 상한을 그대로 지킨다.

### 필수 검증

- N개 동시 waiter가 실제 builder를 한 번만 호출하고 모두 동일한 immutable 결과를 받는다.
- leader caller만 취소해도 다른 waiter는 완료된다.
- build 실패·취소 뒤 registry entry가 남지 않고 다음 호출이 재시도할 수 있다.
- 서로 다른 topic/generation은 합쳐지지 않는다.
- payload aliasing으로 한 caller의 수정이 다른 caller에 전파되지 않는다.

### 구현 결과 (2026-08-19)

- process-local key는 `(topic, generation, supported, enabled)`다. 중앙
  `topic_dispatcher.publish_topic*()`가 새 live payload를 받을 때 topic generation을 올리고,
  build 완료 시 generation이나 availability gate가 바뀌었으면 그 결과를 cache하지 않는다.
- single-flight registry와 성공 cache는 event loop별 상태로 분리한다. shared task는 caller와
  독립된 `SnapshotRequestBudget`을 쓰며, waiter는 자기 남은 예산까지만 `asyncio.shield()`로 기다린다.
  실패·취소 registry 제거는 shared task의 `finally` 한 곳이 소유한다.
- 성공 payload만 cache-owned deep copy로 최대 1초 보관하고, 모든 waiter/cache-hit 반환에서 다시
  deep copy한다. `None`·예외·취소는 cache하지 않는다. 실패 cooldown은 여전히 `LOAD-S7` 소관이다.
- 고정 cardinality 계측 `snapshot_singleflight`가 요청을 leader/join/cache-hit으로 분리하고 실제
  cache 저장 수를 별도로 센다. 계약 버전은 `subscribe-load/7`이다.
- 20 waiter→builder 1회, leader 단독 취소, 마지막 waiter 취소, 실패 후 재시도, topic/generation/gate
  분리, payload aliasing, TTL·설정 fail-closed를 실행형 테스트로 잠갔다. `shield`·generation·deepcopy·
  registry 정리를 각각 제거한 변이는 모두 해당 테스트를 red로 만든다.

## LOAD-S6 — bounded wait와 예산 기반 1013

### 목표

동시 도착을 바로 거절하지 않고 짧은 queue에서 흡수하되, 요청 deadline 밖으로 무한 대기시키지 않는다.

### 계약

- semaphore의 즉시 `locked -> reject` 패턴을 쓰지 않는다.
- waiter는 `LOAD-S3`의 남은 예산까지만 기다린다.
- queue wait가 예산을 소진한 뒤에만 post-ACK close 1013을 사용한다.
- queue wait, worker start, execution을 별도 계측해 용량 부족과 느린 I/O를 구분한다.
- 공정성 정책(FIFO 또는 동등하게 설명 가능한 정책)을 테스트로 고정한다.
- waiter 수의 hard cap이 필요하면 `즉시거절 금지`와 양립하는 admission 설계를 별도 결정한다.
  시간 상한만 두고 메모리까지 bounded라고 주장하지 않는다.

### 필수 검증

- 짧은 선행 작업 뒤 자리가 나는 경우 queued request가 거절되지 않고 성공한다.
- deadline을 넘긴 waiter는 builder를 시작하지 않는다.
- timeout·취소·builder 오류 모든 경로에서 waiter/queued gauge가 0으로 복귀한다.
- 서로 다른 topic의 장시간 작업이 한 topic의 registry lock 때문에 직렬화되지 않는다.

### 구현 결과 (2026-08-20)

- 새 shared flight task만 process-local FIFO admission을 통과한다. flight를 registry에 먼저 게시해
  같은 key의 후속 요청은 admission에 중복 대기하지 않고 즉시 그 shared task에 join한다. 기존 flight
  join과 성공 cache hit는 slot을 소비하지 않는다.
- 동시 build 기본값은 `WS_TOPIC_SNAPSHOT_MAX_CONCURRENT_BUILDS=4`다. dormant 메커니즘 값이며
  LOAD-S4 부하 리허설 전 운영 승인값으로 간주하지 않는다. permit은 leader caller가 아니라 shared
  task가 소유해 caller 하나의 취소가 살아 있는 build의 slot을 조기 반환하지 않는다.
- 각 leader 후보는 자기 LOAD-S3 남은 예산으로 FIFO slot을 기다린다. 예산 소진 전 즉시거절은 없고,
  queue에서 만료된 요청은 builder를 시작하지 않은 채 기존 deadline 경로(WS post-ACK 1013 / REST
  503)로 합류한다.
- `subscribe-load/9`는 `snapshot_singleflight.waiters_now/max`와 `snapshot_admission`의
  queued/in-flight gauge·admission wait를 별도로 싣는다. 기존 `snapshot_build.queue_wait`는 executor
  제출→worker 시작을, `execution_ms`는 worker 본문을 재므로 admission 포화·executor 포화·느린 I/O를
  섞지 않는다.
- FIFO 성공, queue deadline의 builder 0회, queue cancel·builder 오류 뒤 gauge 0, 서로 다른 topic의
  병렬 시작을 행동 테스트로 잠갔다.

⚠️ 대기열 **개수**에는 hard cap이 없다. 이 슬라이스가 유계로 만든 것은 동시 build 수와 각 caller의
대기 시간뿐이다. 여기서 동시 build 수는 **admission permit을 가진 shared task 수**다. 취소된
`to_thread` worker는 다음 협력 checkpoint까지 executor thread를 잠시 더 점유할 수 있으므로 실제
thread 점유의 순간 상한이라고 쓰지 않는다. 메모리 상한이나 ingress 상한 완료라고도 쓰지 않는다.

## LOAD-S7 — 서버 실패 cooldown

### 목표

single-flight 하나의 transient 실패가 모든 waiter를 동시에 깨운 뒤 즉시 같은 build로 재진입시키는
재동기화를 막는다. 클라이언트 jitter와 **함께** 작동해야 하며 어느 한쪽만으로 완료라 하지 않는다.

### 계약

- cooldown은 topic/key별 transient build 실패에만 적용한다.
- fatal 오류, 인증/인가 거부, programming error를 transient cooldown으로 접지 않는다.
- cooldown 중에는 새 leader build를 시작하지 않는다.
- cooldown 만료는 고정 시각에 대규모 재시도를 다시 정렬하지 않도록 R-CLI-24 jitter와 함께 검증한다.
- failure state는 성공 cache와 별도이며 성공 시 즉시 정리된다.
- 관측에는 key, failure class, cooldown remaining, suppressed build 수를 남기되 SQL/토큰은 남기지 않는다.

### 필수 검증

- 하나의 transient 실패 뒤 동시 재시도가 builder 한 번 이상으로 증폭되지 않는다.
- cooldown 만료 뒤 정상 build가 가능하다.
- fatal/auth 오류가 cooldown cache에 들어가지 않는다.
- 다수 client의 retry가 R-CLI-24 적용 후 시간축에 분산되는 통합 테스트가 있다.

### 서버 구현 결과 (2026-08-20)

- 실제 shared builder가 낸 deadline·transient DB·Redis 실패만
  `(topic, generation, supported, enabled)` key별 cooldown에 넣는다. admission queue deadline,
  caller 취소, `None`, fatal/programming error, 영구 DB 오류, 인증·인가는 넣지 않는다.
- cooldown 중 새 flight·admission·builder는 시작하지 않고 WS는 기존 1013 transient 종결, REST twin은
  기존 retryable 503으로 합류한다. 성공 payload cache와 failure state는 별도 map이다.
- `WS_TOPIC_SNAPSHOT_FAILURE_COOLDOWN_SECONDS=1.0`은 dormant 메커니즘 기본값일 뿐 운영 승인값이
  아니다. 실제 값은 R-CLI-24가 포함된 LOAD-S4 부하 리허설에서 정한다.
- `subscribe-load/9`는 arm/suppression/expiry 누계, 고정 failure class, 마지막 suppression의 안전한
  key·남은 시간·해당 key suppress 횟수를 노출한다. SQL·토큰·예외 문자열은 싣지 않는다.
- transient 실패 뒤 동시 retry의 builder 0회, expiry 뒤 회복, generation 격리, fatal·영구 DB 음성
  분류, WS 1013·REST 503을 행동 테스트와 mutation gate로 잠갔다.

⚠️ 서버 절반은 구현됐고 iOS `45a8a12`에 R-CLI-24 jitter·retry cap·client cooldown도 land됐다.
그러나 다수 client 시간축 통합 테스트와 운영값 승인은 아직 없으므로 폭주 완화 전체나 LOAD-S4
activation을 완료라 하지 않는다.

## LOAD-S4 — 통합 activation

`LOAD-S4`는 구현 기능명이 아니라 **활성화 게이트**다. 다음 조건 전에는 켜지 않는다.

- `LOAD-S3`, `LOAD-S5`, `LOAD-S6`, `LOAD-S7`의 행동 테스트와 mutation gate가 green이다.
- R-CLI-24 jitter·retry cap·client cooldown이 실제 client build에 포함된다.
  **충족(source pin):** iOS `45a8a12`; 배포·운영값 승인은 아래 부하 리허설이 소유한다.
- 45초 동시 재구독 부하에서 queue wait, pool timeout, 1013, cooldown suppression을 함께 관측한다.
- rollback image와 server feature flag/safety-stop 절차가 검증돼 있다.
- activation 전후 결과를 같은 dashboard/log schema로 비교할 수 있다.

### GO / NO-GO

- **GO**: 짧은 queue가 실제로 성공을 흡수하고, timeout은 예산 뒤에만 발생하며, 실패 fan-out이
  재동기화되지 않는다.
- **NO-GO**: 즉시거절, 무한 waiter 증가, caller 종료 뒤 orphan worker의 장기 pool 점유,
  auth/fatal 오류의 cooldown 오분류, client jitter 미배포 중 하나라도 관측된다.

## 숫자 결정 규율

이 문서에서 deadline, cache TTL, queue wait, cooldown 값을 고정하지 않는다. 값은 다음 순서로 정한다.

1. 운영과 동형인 PostgreSQL/Redis 환경에서 현재 분포를 측정한다.
2. 45초 동시 도착 시나리오와 정상 traffic을 분리한다.
3. p95/p99와 관측 최대값, 표본 수를 함께 기록한다. 작은 표본을 p99라고 부르지 않는다.
4. 값 하나를 바꿀 때 용량과 timeout을 동시에 바꾸지 않는다.
5. 값과 근거를 같은 변경에서 테스트·runbook에 결속한다.

## 인계 체크리스트

- 현재 단계는 `LOAD-S*`로 적었는가. `R-GATE-1 S*`와 섞지 않았는가.
- normative 변경이 필요하면 먼저 해당 RID 문서를 고치고 C1/C2 결속을 다시 수행했는가.
- 코드 land와 운영 deploy/activation을 별 상태로 보고했는가.
- 실제 PostgreSQL/Redis가 필요한 검증을 SQLite-only green으로 대체하지 않았는가.
- mutation runner는 격리 worktree 안에서만 production twin을 변이하는가.
- 부분 테스트가 아니라 관련 배터리, 전체 suite, provenance preflight를 각각 확인했는가.
