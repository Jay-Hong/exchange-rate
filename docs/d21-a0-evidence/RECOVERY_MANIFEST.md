# D21 A0 증거 복원 매니페스트

2026-09-05 09:26:48 KST 재부팅으로 `/private/tmp/fxi-d21-a0.aWQ3tH` 가 소실된 뒤,
Codex 세션 로그에 남은 **성공한 apply_patch 이력만** 순서대로 재생해 복원했다.
원본 파일을 복사한 것이 아니라 생성 이력을 재생한 결과다.

## 출처

```text
패치 이력   ~/.codex/sessions/2026/08/29/
              rollout-2026-08-29T18-57-38-01a04cf4-10c8-78a0-9920-40632abfdd56.jsonl
              성공 패치 13건 적용, 실패한 line 39617 REPORT.md 생성은 제외
참조 해시   REPORT.md §12 — Claude 세션 로그에 별도 보존된 사본에서 회수
              ~/.claude/projects/-Users-jay-Downloads-Projects-FXi-exchange-rate/
                6e397967-5e2b-4fe2-b1af-08ec50201854.jsonl
              REPORT.md 자신의 해시는 패치 실행 당시 도구 출력(log line 39654)
```

## 적용한 패치 (로그 순서)

```text
line  39228  Add     a0_pg_harness.py
line  39254  Update  a0_pg_harness.py
line  39272  Add     a0_reducer_v3.py
line  39310  Update  a0_reducer_v3.py
line  39327  Add     a0_timeout_barrier.py
line  39339  Update  a0_timeout_barrier.py
line  39396  Add     OkHttpRetryHarness.java
line  39420  Update  OkHttpRetryHarness.java
line  39432  Update  OkHttpRetryHarness.java
line  39513  Add     a0_reducer_v3_adversarial.py
line  39546  Update  a0_reducer_v3_adversarial.py
line  39622  Add     REPORT.md
line  39654  Update  REPORT.md
```

## 검증

| 파일 | bytes | SHA-256 | 참조값 출처 |
|---|---:|---|---|
| `a0_pg_harness.py` | 29511 | `cafd6c100b5dc3fd3be0a0e4962c44db9b847a955932990e6ff8bf17d3270664` | REPORT §12 (Claude 로그 보존본) |
| `a0_reducer_v3.py` | 27391 | `8d778886e59ca3e2f7050a01e7dbcd20b715f16430db408215fb16b2f8ffffa6` | REPORT §12 (Claude 로그 보존본) |
| `a0_reducer_v3_adversarial.py` | 7409 | `917f6da15cda8be65ea6118959f5cfb6548e0c55d2ee0aa1434f38099f044f59` | REPORT §12 (Claude 로그 보존본) |
| `a0_timeout_barrier.py` | 6193 | `07ac24370f0bfd316dcf3100c2eed67fcbe83e5bedd95da4242368ab3d0fff45` | REPORT §12 (Claude 로그 보존본) |
| `OkHttpRetryHarness.java` | 7840 | `bbfd6ae570204e2730bc1903fd86ffd35f9f6071211c9274a61763f685871a8c` | REPORT §12 (Claude 로그 보존본) |
| `REPORT.md` | 20366 | `18cfb6f9fba909ef4e7e50711806b7238dfb4669e5941c883c975bb400db3cb7` | 도구 출력 log line 39654 |

여섯 파일은 모두 끝에 LF(`0x0a`)가 있다. `REPORT.md`를 포함한 여섯 파일 모두
소실 전 기록된 SHA-256과 byte 크기에 일치한다.

## 검증된 것과 안 된 것

```text
BYTE_CONTENT_RECOVERED       A0 harness 5종 · REPORT.md
RECOVERY_AUDIT_REQUIRED      A0 실행 stdout / exit code
RECOVERY_AUDIT_REQUIRED      fxi-hotfix-audit.pEkw69 audit package
RECOVERY_AUDIT_REQUIRED      D21_TEARDOWN v3~v9 초안 최종 bytes
RECOVERY_OR_NEW_GENERATION   fxi-contract-drift.3jE6zp — 재생성은 복구가 아니다
```

참조 해시는 독립 계산값이 아니라 A0 당시 측정의 두 번째 보존본이다.
복원된 REPORT.md 밖에 보존돼 있어, 잘못 복원된 REPORT 가 자기 참조값을 함께
바꾸는 순환은 막는다. 그 이상의 독립성은 주장하지 않는다.

`docs/` 는 이 리포에 없던 디렉터리이며 이 복원을 위해 신설했다.

## 함께 보존한 설계 패킷

| 파일 | lines | bytes | SHA-256 | 상태 |
|---|---:|---:|---|---|
| `A0_5_PACKET_v3_2.md` | 2152 | 154782 | `b754cc221f5ab709f3f43a8c36a0df12d8505845486782dbef081be467edbbc7` | `DESIGN_OPEN / ACCEPTED 0` |

이 파일은 Phase A0 실행 산출물이 아니라 D21 server-ordering 설계 패킷이다.
성공한 세션 패치 이력으로 복원된 bytes를 함께 보존한 것이며, 이 기록은 패킷을
정본이나 승인된 설계로 승격하지 않는다.
