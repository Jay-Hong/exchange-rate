# D21 A0 임시 증거 손실 사고·정정 부록

작성 시각: 2026-09-05 10:20 KST 이후  
상태: `POST_LOSS / CORE_A0_BYTES_RECOVERED / REMAINDER_AUDIT_OPEN / R6_OPERATIONAL_PATH_SUPERSEDED`

이 문서는 `A0_5_PACKET_v3_2.md`의 원래 bytes를 고치지 않는다. 그 문서는 사고 전
상태를 나타내는 역사 자료로 보존하고, 이 부록이 그 문서의 보존 실행 절차와 현재 상태
판정을 대체한다.

## 1. 확인된 사고 사실

호스트는 2026-09-05 09:26 KST에 부팅됐다. 현재 `/private/tmp`의 birth time은
2026-09-05 09:28:19 KST다. 부팅 뒤 다음 원본 경로는 모두 존재하지 않았다.

```text
/private/tmp/fxi-d21-a0.aWQ3tH
/private/tmp/fxi-contract-drift.3jE6zp
/private/tmp/fxi-hotfix-audit.pEkw69
```

계획에서 후보로만 거론된 다음 목적지도 생성되지 않았다.

```text
/Users/jay/Documents/FXi-D21-emergency-20260905
/Users/jay/Downloads/FXi-D21-emergency-20260905
```

따라서 알려진 두 후보 목적지에서는 응급 HP, 단순 선복사, H4/H5 완료를 확인할 수 없다.
별도 목적지의 완료 증거도 현재 제시되지 않았으므로 이 소실을 `HP_VERIFIED`,
`TEMP_LOSS_PROTECTED_LOCAL`, `HANDOFF_COMPLETE` 또는 성공한 cleanup으로 표현하지 않는다.

## 2. 확인되지 않은 것

- 정확한 삭제 시각과 삭제를 수행한 주체는 관측하지 못했다.
- `/usr/libexec/tmp_cleaner`가 위 경로를 삭제했다고 단정하지 않는다.
- 사고 전 문서에 적힌 96시간 eligibility 경계가 이번 소실 원인이라고 주장하지 않는다.
- 원시 증거 세 루트가 다른 곳에 byte-identical하게 보존됐다고 주장하지 않는다.

관측과 일치하는 최소 설명은 **재부팅 전 `/private/tmp`에만 있던 자료가 재부팅 뒤 남지
않았다**는 것이다. 원인 귀속은 이보다 넓히지 않는다.

## 3. 복원된 것과 복원되지 않은 것

`A0_5_PACKET_v3_2.md`는 영속 Codex session JSONL에 기록된 최초 Add File과 성공한
후속 patch만 기계적으로 재적용해 원래 경로에 복원했다.

```text
복원 경로
/private/tmp/claude-501/-Users-jay-Downloads-Projects-FXi-exchange-rate/
6e397967-5e2b-4fe2-b1af-08ec50201854/scratchpad/A0_5_PACKET_v3_2.md

검증값
2152 lines
154782 bytes
SHA-256 b754cc221f5ab709f3f43a8c36a0df12d8505845486782dbef081be467edbbc7

복원 근거
/Users/jay/.codex/sessions/2026/08/29/
rollout-2026-08-29T18-57-38-01a04cf4-10c8-78a0-9920-40632abfdd56.jsonl
```

이는 **문서 복원**일 뿐이다. A0 report/harness, corpus drift 산출물, hotfix audit 산출물의
원래 파일·metadata·stdout·exit code를 복원한 것이 아니다. 세 원시 루트는 현재
`SOURCE_ABSENT / RECOVERY_AUDIT_REQUIRED / BYTE_IDENTICAL_RECOVERY_UNPROVEN`으로 다룬다.
영속 로그에서 일부 또는 전부를 재구성할 가능성까지 반증한 것은 아니므로
`UNRECOVERABLE`이라고 단정하지 않는다. 세션·workflow 로그의 조각을 검증 없이 원본
package와 같은 증거 등급으로 승격하지 않는다.

### 3.1 사고 후 recovery audit에서 추가 확인한 것

Claude session transcript만 검색해 “A0 harness 소스 본문이 없다”고 결론내리는 것은
불충분하다. 위 Codex session JSONL에는 다음이 실제로 들어 있다.

```text
JSONL line 39228  a0_pg_harness.py 전체 Add File patch 시작
JSONL line 39617  REPORT.md 전체 Add File patch 시작
```

같은 로그에는 `a0_reducer_v3_adversarial.py`, `a0_timeout_barrier.py`,
`OkHttpRetryHarness.java`, `D21_TEARDOWN_v3.md`부터 `v9`까지의 파일명과 작업 기록도 다수
남아 있다. 이것만으로 각 파일의 최종 bytes가 복원됐다고 주장할 수는 없지만,
**harness source가 실제로 소실됐다는 판정은 반증됐다.** 각 creation/update call의 성공
여부를 순서대로 재생하고 보고서 §12의 SHA-256과 대조하는 recovery audit가 먼저다.

hotfix 쪽은 별도로 구분한다. `wt-fcm-cleanup`은 `d35e0b3`에서 clean이고, 다음 두 파일은
현재 HEAD에 존재한다.

```text
tests/test_device_token_purge_fence.py
tests/test_fcm_token_cleanup_classification.py
```

이는 hotfix audit 사본에 있던 **코드 bytes의 일부가 커밋에 착지했음**을 보인다. 그러나
audit package의 실행 로그, manifest, metadata, 중간 상태까지 복원한 것은 아니다. 따라서
“hotfix 작업 전체가 유실”도 “hotfix audit에는 고유 손실이 전혀 없음”도 둘 다 금지한다.

contract drift fixture는 현재 exporter로 새로 만들 수 있지만, 재생성은 소실된 실행의
stdout/stderr, image/dependency freeze와 manifest를 되살리는 것이 아니다. 새로 실행하면
별도 evidence generation이다.

## 4. 첨부 답변에 대한 정정 판정

### 살아남는 주장

- 이 호스트에서 `ctypes.CDLL(None)`을 통한 fd 기반 xattr/ACL 접근은 실행 가능한 후보다.
  확인된 심볼과 실호출은 해당 구현 가능성을 지지한다.
- reviewer/author/operator/approver 역할과 독립성 규칙이 문서에 없던 것은 실제 거버넌스
  결함이다.
- configured sync/backup coverage를 판정하면서 admission 내부 subprocess를 전면 금지한
  것은 실행 계약의 공백이다.
- 승인 대기 deadline과 attempt 수가 유계가 아니고, 실패 시 checkpoint 없이 P0부터
  재시작하는 정책은 실제 보존 실패 위험이었다.
- 검증 등급보다 낮은 `UNVERIFIED_RECOVERY_CANDIDATE` 경로조차 별도 GO 아래 즉시 실행할
  수 있도록 정의하지 않은 것은 실제 복원력 결함이었다.

### 정밀화가 필요한 주장

- static review와 runtime closure 사이에 필연적 순환이 있는 것은 아니다. 미래 설계는
  `STATIC_SOURCE_REVIEW`와 실행 시작 시 같은 프로세스가 내는 `RUNTIME_ATTESTATION`을
  분리하고, 후자가 전자의 exact source/schema/ABI 허용 범위를 만족하는지 검증한다.
- `EXEC_SESSION_ID + write_stdin`은 범용 사람 터미널 절차가 아니라 Codex 실행기의 한
  control-plane 구현이다. 이 문서가 그 경로만 지원한다면 `CODEX_EXECUTOR_ONLY`라고
  명시해야 한다. 다른 환경의 미지원은 Codex 경로 자체의 반증이 아니라 별도
  `BLOCKED_BY_INPUT`다.
- 프로세스는 opaque `EXEC_SESSION_ID`를 독립 검증할 수 없다. 그 연결은
  `OPERATOR_ATTESTED`이고, process가 증명하는 것은 자기 nonce/PID/start/packet 결속까지다.

### 사고로 무효가 된 주장과 선택지

- “`~/Downloads`로 지금 복사”는 세 source root가 이미 없으므로 실행 불가다.
- “egress gate는 오늘 충족”은 성립하지 않는다. `Downloads`는 후보였고 configured
  sync/backup coverage와 fresh pre-write 판정이 완료되지 않았다.
- 09-07/09-08 cleaner eligibility 시간표는 이번 source에 대한 남은 실행 여유가 아니다.
- A/B/C 선택지는 모두 폐기한다. 특히 C의 “09-07부터 순차 소실”은 이미 발생한 상태
  변화보다 뒤처진 설명이다.

## 5. 증거 등급에 미치는 영향

원본 package가 필요한 A0 관측치는 현재 recovery audit 또는 재실행 없이 독립 감사할 수
없다. 보고서 문구나 세션 transcript에서 재구성한 값은 원래 stdout/exit code와 metadata를
자동으로 대신하지 않는다. 다만 exact creation/update 기록과 기대 SHA-256이 모두 남아 있다면
byte recovery 자체는 검증 가능하므로 먼저 그 가능성을 닫는다.

따라서 A0.6 또는 D21 결정에서 다음 원칙을 적용한다.

```text
원본 package가 필요한 주장  SOURCE_ABSENT / RECOVERY_AUDIT_REQUIRED
복구 hash가 기대값과 일치  BYTE_CONTENT_RECOVERED (metadata/runtime evidence와 별도)
복구 불가 또는 hash 불일치  RE-RUN_REQUIRED
소스 코드만으로 다시 증명 가능한 주장  현재 SHA에서 별도 재검증
일반 positive control       CURRENT_STACK_E2E로 승격 금지
복원 문서의 설계 제안       PROPOSED 유지; evidence PASS로 승격 금지
```

## 6. 향후 보존 절차의 필수 정정

다음 revision은 기존 R6 절차를 부분 수정해 실행 재개하지 않는다. source가 존재하는 새
generation에서 아래 요구를 먼저 만족해야 한다.

1. `AUTHOR`, `INDEPENDENT_REVIEWER`, `OPERATOR`, `USER_APPROVER`를 정의한다. reviewer는
   동일 작성 주체가 아닌 독립 검토 주체여야 하고 identity/session과 verdict를 기록한다.
   self-review로 gate를 닫지 않는다.
2. `STATIC_SOURCE_REVIEW`와 `RUNTIME_ATTESTATION`을 분리한다. runtime attestation은
   static review 뒤 실행된 exact process가 생성한다.
3. destination coverage는 pre-admission read-only 단계에서 allowlist한 exact system command와
   executable digest, argv, stdout/stderr, exit code를 결속해 조사한다. 완전 판정이 안 되면
   local-only를 주장하지 않고 사용자에게 unknown을 제시한다.
4. Codex 전용 PTY 경로라면 `CODEX_EXECUTOR_ONLY`를 명시한다. transport handle 연결은
   `OPERATOR_ATTESTED`로 표시한다.
5. `control_wait_deadline`은 null을 금지하고 absolute deadline을 요구한다. admission 실패
   재시도 횟수와 총 wall-clock budget을 제한한다.
6. 정규 `HP_VERIFIED`와 별도로, exact 별도 GO 아래 실행하는
   `UNVERIFIED_RECOVERY_CANDIDATE`를 정의한다. 최소한 source pre-copy identity/hash,
   fresh no-replace private destination, exact copy, post-copy source/target hash와 명시적
   비승격 manifest를 남긴다.
7. 정규 경로가 deadline 안에 준비되지 않으면 자동 재시도를 계속하지 않는다. 승인된
   degraded copy를 실행하거나 명시적으로 abort한다.
8. 고유 evidence와 current canonical document의 유일본을 `/private/tmp`에만 두지 않는다.
   생성 직후 승인된 non-temp 보존처와 독립 retrieval 검증을 완료한다.

## 7. 문서 삭제 정책

관련 작업이 끝나도 문서를 전부 삭제하지 않는다.

```text
영구 보존  current canonical decision/report, 이 사고 부록, 승인·판정 이력,
           원시 evidence package와 검증 manifest/receipt
이력 보존  정본이 인용하는 superseded 문서의 byte-identical archive
삭제 가능  영속 보존과 독립 retrieval 검증이 끝난 뒤의 temp working copy,
           재현 가능한 cache/build output, 의미 없는 중복 draft
삭제 금지  유일본, 원시 증거, 아직 재현되지 않은 source, 영속 manifest/receipt
```

이번 사고에서는 원래 temp source가 이미 없어 cleanup할 대상이 없다. 현재 복원된
`A0_5_PACKET_v3_2.md`는 다시 `/private/tmp`에만 있으므로, 이 부록이 존재한다는 이유로
삭제하지 않는다. 영속 archive 위치와 검증 절차를 별도로 승인·완료한 뒤에만 temp 사본을
cleanup 대상으로 분류한다.

## 8. 현재 다음 단계

1. Claude와 Codex의 영속 JSONL에서 각 소실 파일의 successful creation/update sequence를
   복원하고, 알려진 파일 SHA-256·행수·바이트 수와 대조한다.
2. hotfix audit는 committed source와 audit-only 산출물을 분리해 고유 손실 범위를 산정한다.
3. contract drift는 복구 가능한 원본 기록과 새로 재생성해야 하는 evidence를 구분한다.
4. recovery audit로 닫히지 않으며 의사결정에 필수인 항목만, 명시적으로 고정한 source SHA와
   기록된 환경에서 새 evidence generation으로 재실행한다.
5. 복원된 패킷과 이 부록의 영속 archive 목적지를 정하고 byte-identical retrieval을 검증한다.
6. 새 보존 프로토콜은 위 §6 요구를 반영한 뒤 별도 검토와 GO를 받는다.

이 부록은 복사·삭제·process control·commit·push 권한을 부여하지 않는다.

## 9. 2026-09-05 복원 체크포인트

§8의 1번 가운데 A0 핵심 소스 5종과 `REPORT.md`의 byte 복원은 완료됐다.

```text
영속 로컬 경로  docs/d21-a0-evidence/
복원 파일        a0_pg_harness.py · a0_reducer_v3.py ·
                 a0_reducer_v3_adversarial.py · a0_timeout_barrier.py ·
                 OkHttpRetryHarness.java · REPORT.md
검증             소실 전 SHA-256 및 byte 크기 6/6 일치
기록             RECOVERY_MANIFEST.md
Git 상태          아직 untracked — commit/push 전에는 로컬 파일일 뿐
```

이 체크포인트는 A0 전체 evidence package가 복구됐다는 뜻이 아니다. 실행 stdout/exit code,
hotfix audit package, D21 teardown 초안의 최종 bytes는 계속 감사 대상이다. contract-drift를
현재 revision에서 다시 생성하는 것은 과거 산출물 복원이 아니라 새 evidence generation이다.

또한 `A0_5_PACKET_v3_2.md`의 검증된 복원본은 아직 `/private/tmp`에만 있으므로 별도 영속
안착이 필요하다. 따라서 §8의 나머지 항목과 §7의 삭제 조건은 계속 유효하다.
