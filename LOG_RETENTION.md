# Docker 로그 보관기 — B v1

B v1 은 **Docker 에 도달한 stdout/stderr** 를 보존한다. Tier C 자식(nh·shinhan·sc·ibk 큐 자식)의 파일 로그와
그 실행의 양성 증거는 포함하지 않는다.

구현은 [scripts/docker_log_archive.py](scripts/docker_log_archive.py), 시험은
[계약 v2](tests/test_docker_log_archive_contract.py)와 [구현 측 시험](tests/test_docker_log_archive.py)이다.
Python **3.12.3 호스트**를 대상으로 표준 라이브러리만 사용하며 앱·DB를 import하지 않는다.
이번 작업은 로컬 구현·시험이며 서버 설치·크론 변경을 실행하지 않았다.

## 두 보존소의 역할

| 보존소 | 용도 | 삭제 정책 |
| --- | --- | --- |
| `~/logs/docker-archive` | Docker stdout/stderr 각각의 원문 gzip, 상시 장애 조사 | 성공 manifest의 `until < 현재 UTC−14일`만 만료 |
| `~/logs/investing-observe` | Investing 관측 원문·이벤트·집계 증거 | 기존 관측 계약 유지, B가 접근·삭제하지 않음 |

[관측 보존소](INVESTING_OBSERVE.md)의 `:23` 크론 종료·퇴역과 B의 `:41` 크론은 별개다.
양쪽에 같은 로그가 저장될 수 있다. 원문·보존소·크론 출력은 리포지토리 밖에 둔다.
새 보존 루트·하위 디렉터리는 0700, 데이터·메타데이터 파일은 0600이다.

## 수집과 내구성

한 보존소는 한 컨테이너 이름에 대응한다. 매회 제한된 JSON `inspect`로 전체 ID와 `StartedAt`을
함께 읽고, `docker logs --timestamps --since … --until … <확정 ID>`를 호출한다.
두 호출 모두 timeout 120초다. 최초 수집·ID 교체·동일 ID 재시작은
`max(현재−24시간, StartedAt)`부터, 같은 실행의 후속 수집은 마지막 성공 `until−10분`
(StartedAt보다 앞서지 않음)부터 요청한다. Docker가 이미 회전·삭제한 로그는 복구할 수 없다.
앱 메시지의 시각 대신 Docker 접두 시각을 사용하며, 원문은 디코딩·마스킹·줄 병합 없이 저장한다.
메타데이터 시각은 UTC이고, Docker 나노초 접두는 Python datetime의 마이크로초 정밀도로 해석한다.
원문 바이트와 해시는 그 변환의 영향을 받지 않는다.

저장 순서는 **gzip footer 종료 → 두 gzip 파일 각각 flush/fsync → 작업 디렉터리 fsync →
최종 run 이름으로 rename·부모 디렉터리 fsync → manifest 임시 파일 fsync·rename·디렉터리 fsync →
커서도 같은 원자 교체 절차**다. manifest에는 두 스트림 각각 원문/압축 SHA-256·크기와
요청창·ID·재시작/교체 여부를 기록한다. 이 순서와 파일/디렉터리 fd 구분, 올바른 압축 해시에
틀린 원문 해시를 넣었을 때의 거부는 구현 측 시험이 담당한다(계약 R26/R8b의 한계 보완).

`runs/<run_id>/manifest.json`이 있어야 완료 run으로 열거한다. 실패는 `failures/<run_id>.json`에
단계와 시각만 기록하며 예외 문자열·원문은 출력하지 않는다. 실패한 수집의 원문/부분 gzip은
`.pending-<run_id>` 또는 manifest 없는 run 안에 남을 수 있다. 커서 전에 실패하면 커서는 그대로이며,
커서 rename 뒤 디렉터리 fsync 실패라면 이미 검증 가능한 완료 run을 가리킬 수 있다.
최초 초기화의 일시적 저장 실패는 잠금을 다시 잡고 정책·실패 이력을 남긴 뒤 2를 반환한다.
이력 장치까지 계속 쓰기 불가능하면 `ARCHIVE_FAILURE_HISTORY_UNAVAILABLE`만 stderr에 남는다.
정전·SIGKILL로 실패 이력조차 없는 부분 산출물은 자동 삭제하지 않고 운영자가 확인한다.

## 보존창과 미보존 꼬리

`coverage`는 두 해시와 gzip 복원을 검증한 run들의 `[첫 Docker 로그 시각, until]` 합집합을
`archived`로 보고한다. 요청 시작부터 첫 로그 이전은 보존됐다고 추정하지 않는다.
빈 출력 또는 요청 범위 안의 유효한 접두 시각이 없는 출력은 `unknown`이다.
`gaps`는 보존창 사이 공백에서 unknown 구간을 제외한 것이며, 요청한 10분 겹침보다 첫 로그가 1초
늦다는 이유만으로 만들지 않는다.
이 정보는 애플리케이션이 모든 로그를 발행했다는 증명이나 실제 로그 유실량의 추정치가 아니다.

**미보존 꼬리**는 옛 ID의 마지막 성공 `until` 이후부터 옛 컨테이너 종료까지 발생할 수 있는
미수집 구간이다. B v1은 종료 시각을 알 수 없어 **교체를 처음 본 수집 시각**을 보수적 상한으로
`unarchived_tail`에 기록한다. 새 컨테이너가 먼저 시작돼 두 컨테이너의 실행이 겹칠 수 있으므로
새 `StartedAt`은 옛 꼬리의 끝으로 사용하지 않는다. 새 ID를 수집해도 옛 꼬리는 복구되지 않는다.
inspect에서 교체를 확인한 뒤 logs·저장이 실패해도 실패 이력에 첫 관측을 남겨, 재시도 때 상한이 늦춰지지 않는다.
동일 ID의 `StartedAt` 변경은 교체와 구분해 `restarted=true`로 기록한다.

## 만료와 경로 안전성

수동 수집·정기 수집·switch 직전 수집·만료는 동일한 비차단 `flock`을 사용한다.
다른 프로세스가 잠갔으면 collect는 3, expire API는 `ArchiveError`를 반환한다.
진행 중인 수집의 산출물을 만료가 건드릴 수 없다.

전용 정책 마커가 없는 기존 비어 있지 않은 루트는 인수하지 않는다. expire는 미소유 루트를
거부한다. `investing-observe` 자체·그 하위·그 보존소를 포함하는 상위 디렉터리도 거부한다.
**루트 자체 및 루트 내부의 심볼릭 링크는 거부**한다. 루트의 상위 경로는 한 번 정규화한
실제 경로를 사용한다. 따라서 macOS `/var → /private/var`와 상위 alias는 허용하되,
alias가 관측 보존소를 가리키면 정규화 후 거부한다. 경로를 바꿀 수 있는 다른 사용자와
보존 루트를 공유하지 않는다(0700 소유자 전용; 악의적인 동시 경로 교체까지 방어하는 샌드박스는 아님).

만료 기준은 mtime이 아닌 manifest `until`이고 **정확히 14일인 run은 유지**한다.
현재 커서의 근거 run은 14일을 넘어도 유지하므로, 수집이 멈추면 마지막 성공은 남는다.
커서·근거 manifest 불일치나 근거 gzip 손상은 수집과 만료를 모두 중단하며 조용히 초기화하지 않는다.
자신의 알려진 파일만 지우며 루트나 run 안에 운영자가 둔 다른 파일은 남긴다.
실패 이력과 그 이력에 연결된 부분 파일도 14일 초과 시 정리한다.
삭제는 `deletions/<run_id>.json`에 `pending` 의도를 내구 저장한 뒤 수행하고 `deleted`로 갱신한다.
삭제 중 중단되어 `pending`이 남으면 운영자가 파일 잔존 여부를 확인한다. 삭제 감사 기록은 자동 만료하지 않는다.

```bash
/usr/bin/python3 /home/ubuntu/fxi-host-tools/docker_log_archive.py expire --dry-run
/usr/bin/python3 /home/ubuntu/fxi-host-tools/docker_log_archive.py expire
```

## 정기 실행 래퍼와 점검

래퍼는 별도의 Git 밖 셸 파일을 만들지 않고 **동일 스크립트의 `cycle` 명령**으로 관리한다.
수집·만료·용량 가드를 함께 시험하고 호스트마다 로직이 달라지는 일을 피하기 위한 결정이다.
잠금 아래 **만료를 먼저 실행**하므로 용량 가드·용량 측정 오류로 수집을 막아도 정리는 수행된다.
만료 자체가 실패하면 신규 수집도 중단한다.

기본 가드는 보존소 4 GiB, 파일시스템 여유 5 GiB다. 원문·실패·진행 중·남의 파일을 포함한
전체 파일 크기(논리 크기와 할당 블록 중 큰 값)에, 한 회 최대 원문 256 MiB의 **3배+16 MiB**를
더해 압축 중 최대 사용량을 예약한다. 여유 공간에서도 같은 예약량을 뺀 뒤 가드를 판정한다.
실제 Docker 실행기는 두 pipe를 동시에 읽으며 두 스트림 합계 256 MiB에서 중단하고 자식을 회수한다.
압축 전 원문과 gzip이 함께 존재하는 순간을 포함하는 보수적 예산이다. 다른 프로세스의 동시
디스크 사용까지 예약하는 파일시스템 quota는 아니므로 쓰기 실패 경로는 여전히 필요하다.
임계값은 `--max-archive-bytes`, `--min-free-bytes`로 조정할 수 있다.
가드는 신규 수집을 중단하며 14일 이내 데이터를 용량 때문에 지우지 않는다.

| CLI 종료 코드 | 의미 |
| --- | --- |
| 0 | 수집·검증·요청 작업 성공 |
| 1 | `check`에서 누락 슬롯·오래된 성공·손상 발견 |
| 2 | Docker·저장·측정·검증·경로 오류 |
| 3 | 잠금 경쟁 |
| 4 | `cycle` 용량 가드로 신규 수집 중단(만료는 수행) |

**점검 담당과 주기는 아직 정하지 않았다**(자동 알림 없음 — 사용자 결정 사항). 권장: 매일 1회와 배포 직후 다음을 실행해
마지막 성공과 누락 슬롯을 확인한다.
설치 시 첫 정기 실행 예정 시각을 정확히 기록하고 아래 `--first-slot`에 넣는다(예시는 예시일 뿐이다).
`check`는 복원 해시가 맞는 성공만 인정한다. 수동 collect는 정기 슬롯을 채우지 않는다.

```bash
/usr/bin/python3 /home/ubuntu/fxi-host-tools/docker_log_archive.py check \
  --first-slot 2026-09-23T00:41:00Z
/usr/bin/python3 /home/ubuntu/fxi-host-tools/docker_log_archive.py coverage
```

슬롯 판정 창은 `[now−10분−24시간, now−10분]`이고 그 뒤의 슬롯은 `pending_slots`다.
마지막 성공이 70분보다 오래됐으면 `stale`이다. 실패·가드·크론 미실행은 해당 정기 성공이
없으므로 누락으로 잡힌다. `coverage`의 unknown/gaps/tail도 함께 확인한다.
**자동 알림은 없다.** 실행되지 않은 크론은 자신을 점검할 수 없으며, 하루 점검을 건너뛰면
24시간 창 밖의 누락은 별도 이력 조사가 필요하다. 크론 출력 파일도 운영 점검 때 크기를 확인한다.

## 설치·해제 절차 (승인된 호스트 작업에서 실행)

1. 검토된 스크립트의 SHA-256과 호스트 사본을 대조한다. 호스트 `/usr/bin/python3 --version`이
   3.12.3인지, Docker 읽기 권한과 여유 공간을 확인한다. 전용 루트는 기본값
   `/home/ubuntu/logs/docker-archive`로 두고 관측 보존소와 분리한다.
   **실행 파일은 서버 checkout 이 아니라 Git 밖 `/home/ubuntu/fxi-host-tools/docker_log_archive.py` 에 둔다.**
   서버 checkout 은 배포된 코드와 같아야 하는데(배포 기준선), 2026-09-21 기준 예정된 두 배포의 대상(09-22 `34ef52c5`,
   09-29 E0 `4ffda34`) 모두 이 스크립트를 포함하지 않는다. 설치를 위해 checkout 을 옮기면 실행 중인 이미지와 어긋나고,
   checkout 안을 가리키면 이후 배포가 크론이 부르는 코드를 조용히 바꾼다. 검토된 커밋의 blob
   (`git show <커밋>:scripts/docker_log_archive.py`)을 전송해 호스트에서 SHA-256 을 대조한 뒤, 0700 디렉터리에
   0700 파일로 원자 설치한다. 새 버전도 같은 절차로 교체하고 설치 기록에 커밋·해시를 남긴다.
2. `collect`를 한 번 실행하고 종료 0, `runs`의 새 run, 아래 복원 검증을 확인한다.
   실패·unknown이면 원인을 확인하고 설치 성공으로 취급하지 않는다.
3. `crontab -l` 성공 결과를 보관하고 원본의 모든 행을 유지하는 후보에 아래 **정확한 한 행**을
   추가한다. 적용 직전 현재 crontab이 원본과 바이트까지 같은지 재확인하고 후보 파일로 설치한다.
   설치 후 다시 읽어 후보와 동일한지 확인한다. 읽기 실패를 빈 crontab으로 취급하지 않는다.
   첫 `:41` 예정 시각을 UTC로 별도 설치 기록에 남긴다. 출력 파일은 `umask 077`로 만든다.

```cron
41 * * * * umask 077; /usr/bin/python3 /home/ubuntu/fxi-host-tools/docker_log_archive.py cycle --root /home/ubuntu/logs/docker-archive >> /home/ubuntu/logs/docker-archive-cron.log 2>&1
```

4. 연속 정기 실행의 종료 0·누락 없음·양쪽 스트림 복원을 확인하고 첫 24시간의 실제 크기를 측정한다.
   gzip 비율이나 14일 예상 용량은 설치 전 추정치로 운영 합격을 대신하지 않는다.
   설치 목표는 09-23, 완료 조건은 **2026-10-02 00:00 UTC(09:00 KST) 전에 연속 정기 실행과 복원 검증**이다.

**해제:** 현 crontab을 성공적으로 읽어 저장하고, 위 **정확한 전체 행이 하나일 때만 그 `:41` 행을 제거**한
후보를 만든다. 현재 값이 snapshot과 같은지 재확인 → 후보 설치 → 바이트 비교 순으로 확인한다.
중복·불일치면 적용하지 말고 확인한다. `:23` 관측 크론과 다른 행은 유지한다.
**보존본·실패/삭제 이력·스크립트·크론 출력은 남긴다.** 해제하면 자동 만료도 멈추며,
별도 수동 만료나 전체 보존소 폐기는 별도 운영 결정이다.

## switch 직전과 복원 검증

[배포 절차](DEPLOYMENT.md#8-코드-업데이트)의 실제 컨테이너 교체 명령 **바로 직전**, 아래를
실행하고 종료 0과 `ready=true`를 확인한다. 실패하면 switch를 진행하지 않는다.
명령은 옛 ID로 수집하고 양쪽 해시·복원을 확인한 뒤 이름의 ID와 StartedAt이 그대로인지 다시 확인한다.
컨테이너 중지·재생성은 이 명령이 수행하지 않는다. 확인 후 지연되면 다시 실행한다.

```bash
/usr/bin/python3 /home/ubuntu/fxi-host-tools/docker_log_archive.py pre-switch \
  --root /home/ubuntu/logs/docker-archive --container exchange-rate-app
```

출력의 run_id·container_id·until을 배포 기록에 남긴다. 관측 기간에는 별도로 필요한
관측 추출도 기존 절차에 따라 수행한다. 교체 뒤 collect와 coverage로 새 ID와 미보존 꼬리를 확인한다.
직전 수집을 해도 `until` 이후 옛 종료까지의 꼬리가 남으며 이를 “수 초”나 무손실로 단정하지 않는다.
종료 후 옛 컨테이너를 최종 수집하는 배포 도구 변경은 B v1 범위 밖이다.

```bash
/usr/bin/python3 /home/ubuntu/fxi-host-tools/docker_log_archive.py runs
/usr/bin/python3 /home/ubuntu/fxi-host-tools/docker_log_archive.py verify --run-id <run_id>
```

`verify`는 stdout/stderr 각각 **압축 SHA-256 → gzip CRC/footer 및 압축 해제 → 원문 SHA-256·크기**를
확인하고 원문 없이 결과만 출력한다. 하나라도 다르면 실패다. 원문 복원은 Python API
`restore(root, run_id) -> (stdout_bytes, stderr_bytes)`를 사용한다. 필요할 때만 승인된 로컬 분석 위치의
새 0600 파일에 각각 `xb`로 저장하고, 터미널·대화·메타데이터에 원문을 출력하지 않는다.
겹치는 run은 의도적으로 같은 로그를 포함하므로 단순 연결은 중복을 만든다.

## 로컬 검증

독립적인 stdlib 도구라 앱 fixture를 로드하지 않고 다음처럼 시험한다. bytecode·pytest 캐시는 만들지 않는다.

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/jay/.cache/fxi-venv-lock/bin/python -m pytest \
  --noconftest -p no:cacheprovider -p no:asyncio \
  tests/test_docker_log_archive_contract.py tests/test_docker_log_archive.py -q
shasum -a 256 tests/test_docker_log_archive_contract.py
```

계약 파일의 고정 해시는 `e21f851214d5b38594f8b6be437ec4a6104f0f23b353e3eea23621b1fa3247e6`이다.
지정 잠금 환경(Python **3.13.5**)과 별도 로컬 **Python 3.12.14** 환경에서 두 시험 파일 81건이 모두 통과했다(2026-09-21).
호스트의 **3.12.3** 실행은 설치 때 확인한다(패치 버전 차이는 대신 증명하지 않는다).
