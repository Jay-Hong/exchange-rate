# Investing 보고 로그 보존·집계

`investing_observe_extract.py`는 Docker 로그를 읽어 Git 밖에 보존한다.
`investing_observe_aggregate.py`는 보존본을 검증하고 KST 날짜별 JSON 집계를 stdout으로 출력한다.
두 스크립트는 Python 3.11+ 표준 라이브러리만 사용하며 앱·DB 모듈을 import하지 않는다.
보고 계약은 [SOURCE_HEALTH_PLAN §7.10](SOURCE_HEALTH_PLAN.md#710-investing-슬라이스-1-보고-전용-구현),
발행 구현은 [investing_report.py](app/crawlers/investing_report.py)다.

## 실행

아래 추출 명령은 Docker를 읽을 수 있는 **승인된 실행 호스트**에서 사용한다.
이 변경으로 운영 접근·배포·cron 설치가 승인되거나 실행되는 것은 아니다.

```bash
python3 /absolute/path/exchange-rate/scripts/investing_observe_extract.py \
  --container exchange-rate-app \
  --output-dir "$HOME/logs/investing-observe"

python3 /absolute/path/exchange-rate/scripts/investing_observe_aggregate.py \
  --archive "$HOME/logs/investing-observe" \
  --start-date 2026-09-16 --end-date 2026-09-17 \
  > "$HOME/logs/investing-observe-summary-2026-09-16_17.json"
```

날짜 범위는 양끝을 포함한다. `--archive`는 **모든 날짜의 이벤트**를 읽고 나서
날짜를 골라 출력한다. 따라서 자정을 넘은 회차의 시작과 종료도 함께 해석한다.
위 집계 출력 파일명은 예시이며 기존 증거를 덮어쓰지 않도록 새 이름을 사용한다.
이벤트 JSONL 경로들을 위치 인자로 넘길 수도 있다. 이 경우 이력이 없으면 부분 관측이다.
구 scratchpad의 이벤트 envelope(`ts/container_id/event`)도 v2 구조가 유효하면 읽는다.
구 추출기의 `runs.jsonl`은 내구성·보존창 증거가 부족하므로 새 이력으로 취급하지 않는다.

기본 보존 경로는 `~/logs/investing-observe`다. CLI는 `~/logs/` 아래만 허용하고,
저장 로직은 리포 안으로 향하는 경로(심볼릭 링크 포함)를 거부한다.
로컬 시험은 임시 디렉터리와 Docker 대역을 사용한다.

```bash
pytest -p no:asyncio tests/test_investing_observe.py tests/test_investing_report.py -q
pytest -p no:asyncio
```

## 저장 프로토콜과 실패 확인

```text
~/logs/investing-observe/
  extract.lock
  cursor.json
  runs/<run_id>/
    started.json          실행 시작 (lock 획득 전)
    request.json          확정 컨테이너 ID·요청 시간창·이전 커서·스크립트 SHA-256
    stdout.raw            Docker stdout 원문 바이트
    stderr.raw            Docker stderr 원문 바이트 (줄 경계를 합치지 않음)
    rejects.jsonl         파싱 실패의 raw_file/raw_line/사유
    prepared.json         원문·이벤트·rejects 내구 저장 완료, 파일별 SHA-256
    result.json           성공/실패·실패 단계·예외·커서 갱신 완료 여부
  events/<원래 로그의 KST 날짜>/<run_id>.jsonl
```

1. `flock` 비차단 잠금으로 같은 보존 경로의 중복 실행을 막는다. 잠금 경쟁 실행도
   독립된 `run_id`의 실패 이력을 남긴다. 한 컨테이너에는 보존 경로 하나를 사용한다.
2. 이름으로 `docker inspect`를 **한 번** 호출해 ID와 StartedAt을 함께 받는다.
   환경변수 등 전체 inspect 정보는 받지 않는다. 이후 `docker logs`는 그 ID만 사용한다.
   도중 교체되어 기존 ID가 사라지면 실패로 남기며 새 이름으로 재조회하지 않는다.
3. 마지막 커서보다 10분 앞에서 재독한다. 최초 실행·교체·재시작은 최근 24시간을 요청한다.
   깨진 커서나 미래 커서는 오류이며 조용히 초기화하지 않는다.
4. 원문 전체를 디스크에 직접 쓰고 flush/fsync한다. timeout 시 받은 부분도 남긴다.
   파싱 실패는 원문 바이트와 줄 위치를 보존한다. 이벤트 파일은 추출 실행일 대신
   **Docker 로그 시각의 KST 날짜**로 나눈다. 앱 message 안의 시각은 귀속 기준이 아니다.
5. 이벤트·rejects·`prepared.json`을 저장한 뒤에만 커서를 교체한다.
   JSON/JSONL은 같은 디렉터리의 임시 파일 → 파일 fsync → atomic replace →
   디렉터리 fsync 순서다. 커서 갱신도 같은 방식이다.
6. 마지막으로 `result.json`을 저장한다. 종료 이력 저장 실패 또는 강제 종료로 이 파일이
   없더라도 커서에 대응하는 `prepared.json`은 이미 존재한다. 집계는 미완료로 표시한다.
   이력 경로 자체를 쓸 수 없는 실패는 stderr의 `EXTRACT_HISTORY_FAILED`로 알린다.
   모든 저장 장치가 실패했을 때 파일 이력까지 보장할 수는 없다.

추출 종료 코드: **0** 보존 성공, **1** 파싱 실패 원문까지 보존한 부분 성공,
**2** 추출/저장 실패, **3** 잠금 경쟁. `EXTRACT_RESULT`는 run_id·실패 단계·건수를 출력한다.
0도 하루 전체 관측이나 앱 보고의 무유실을 보장하지 않는다.
파싱 실패 원문·rejects 저장에 실패하면 커서를 전진시키지 않는다.
커서 이후 종료 이력 저장만 실패하면 prepared 이력을 남기고 2를 반환한다.
재실행은 겹치는 이벤트를 만들 수 있지만 집계에서 제거한다. 실패 이력은 삭제하지 않는다.

원문에는 Investing 이외의 앱 로그도 포함된다. 새 보존 디렉터리는 0700,
원문·JSON 파일은 0600으로 생성한다. 원문·커서·이력을 Git에 추가하지 않는다.
자동 삭제·로테이션은 하지 않으므로 디스크 사용량은 운영 검토에서 관리해야 한다.
파싱 실패 복구는 원문을 보존한 채 수정된 파서로 **새 보존본**을 만들고 독립 검토한다.

## 집계 의미

- 이벤트 키는 `(container_id, round_id, event, attempt_id)`다. 같은 키의 JSON payload를
  정규화하여 변형별로 한 번 센다. A·B·B와 A·B/B·A는 같은 결과다.
  충돌 payload·각 로그 시각은 `conflicts`에 모두 남긴다. 충돌 회차 전체는 확정 회차
  지표에서 제외한다. 다른 컨테이너의 같은 round_id는 별도 회차다.
- 구조·schema_version·참조 attempt_id·통화 키·숫자·writer·execution 등을 검증한다.
  알 수 없는 형식, malformed JSON, 중복 JSON 키, NaN/Infinity는 `invalid_events`에
  파일/줄/사유로 남기고 세지 않는다. 입력 원본을 수정하지 않는다.
- detail의 통화 상태는 `collection_attempts[pair]`가 가리키는 원본을 사용한다.
  생략 가능한 미도달/불필요 시도만 `not_attempted/not_started`로 복원한다.
  따라서 1차 valid → 2차 unknown/timeout이어도 valid 3을 보존한다.
- 통화·execution·writer·telemetry 회차 지표는 **충돌 없는 종료 확인 회차**만 분모로 한다.
  FX 스냅샷 반복을 요청 횟수로 세지 않는다. `format_by_event`는 이벤트 종류별이며
  충돌 키는 제외한다. `event_variants`에는 충돌 변형도 포함한다.
- `writer_returned_count_attempts`는 반환값별 **시도 수**, `writer_called_attempts`는
  명시된 writer 기록별 호출 상태 수다. 미도달 생략 시도는 여기에 더하지 않는다.
  `writer_call_count_rounds`는 0/1/2회 호출이 확인된 **회차 수**이고 계측 불명은 unknown이다.
  반환 null은 반환값 histogram에서 제외한다. 정수 0은 그대로 세며 저장 성공·정책 차단·
  변경 불필요로 해석하지 않는다. DB 상태를 추정하는 지표는 없다.
- 이벤트는 원래 로그 날짜, 회차는 시작 이벤트 날짜로 귀속한다. 시작이 없으면 최초
  관측 이벤트 날짜에 임시 귀속하고 `provisional_date_rounds`로 센다. 뒤늦게 시작 증거가
  추가되면 귀속이 달라질 수 있다. 중복 payload의 시각이 여러 개면 가장 이른 시각을 쓴다.
- `start_only`, `finish_only`, `fx_only`를 구분한다. 종료 미확인은 실패가 아니다.
  모든 이벤트가 없는 회차 수는 로그로 식별할 수 없어 `not_identifiable`로 출력한다.

`coverage.partial`은 요청한 **각 KST 하루**에 대해 계산한다. 종료된 추출의 원문·이벤트
해시를 검증한 뒤 `[실제 첫 로그 시각과 StartedAt 및 since 중 최댓값, until]`을 합친다.
`--since`만으로 Docker가 그 기간의 로그를 보유했다고 가정하지 않는다. 반환 로그가 없으면
그 실행은 coverage를 늘리지 않는다. 겹치는 구간은 한 번만 세고 빈 날짜도 출력한다.
창 공백, 실패/미완료 추출, 파싱 실패, 교체/재시작 위험 구간, 충돌, 입력/이력 오류를
부분 관측 사유로 남긴다. 날짜를 알 수 없는 손상 입력은 요청 날짜 전체에 보수적으로 표시한다.
이후 재추출이 공백을 덮어도 실패 이력 표식은 자동으로 해소하지 않는다.

`partial=false`도 **보존된 로그 창의 연속성**만 뜻한다. 발행기 자체 계측 유실,
Docker 내부 유실, 로그가 한 건도 없는 회차의 존재는 증명하지 못한다.
집계 종료 코드: **0** 입력·충돌·추출창 문제가 없는 요청 날짜,
**1** 부분 관측/충돌/검증 오류를 포함한 집계 출력, **2** 입력 파일 I/O 실패.

## 배포·cron 후속 검토 묶음

이번 작업은 작업 트리 구현·대역 시험까지다. cron 설치는 배포와 같은 검토 묶음에 넣되
승인 범위와 rollback을 별도 항목으로 둔다. **앱 배포 승인은 cron 설치 승인을 겸하지 않는다.**
최종 검토에는 새 배포 후보 SHA, 두 스크립트 SHA-256, 실행 사용자·Python/스크립트 절대 경로,
1시간 주기, 내장 잠금과 단일 보존 경로, stderr 수집 위치, 위 실패 코드 확인 방법,
해당 cron 행을 제거하는 해제 절차를 명시한다. 기존 증거 파일은 해제 시에도 보존한다.
기존 컨테이너의 로그가 필요하면 **컨테이너 재생성 직전에 추출**하고 결과를 확인한다.
새 스크립트 커밋으로 SHA가 바뀌므로 배포 후보를 새 SHA로 재검토한다.
