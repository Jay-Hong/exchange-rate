# collection_expected — 작업 미실행 감지 계약 (SOURCE_HEALTH_PLAN §7.6)

**상태: 설계 합의(2026-09-22, Claude·Codex). 구현·배포 승인이 아니다.** 본문(r3)과 부록 A(=설계 폴더의 부록 A4)는 Codex 작성, Claude 가 합격 기준·코드 인용 대조로 검증했다.
경과: r1·r2(Claude) → REVISE → r3(Codex 작성) → Claude 가 S0-O1 대안(APScheduler 실행 로그 필터)·Tier 분할 제기 → 부록 A1~A3(Claude) 각각 REVISE → A4(Codex 작성·Claude 검증).

**S0 열린 결정의 결론**
- **O1(실행 연결 경로)**: Tier 1 은 부록 A 의 실행기 INFO 레코드 필터(E) — 호출 직전 사건 `executor_pre_call` 과 래퍼의 `run_entered` 를 분리하고, 관측 필터를
  실행기 로거의 **첫 필터**로 고정해 스레드 로컬 문맥으로 결속한다. 본문 §4.2 의 A(실행기 확장)는 완전한 제출 연결이 필요할 때의 후속 선택지.
- **O2(설정 효력)**: `commit_ack_observed_at`(`db.commit()` 반환 바로 다음 줄) 채택. known/unknown 구간은 부록 A §4.
- **O3(완결성·보존)**: Tier 1 = 원시 사건 + 현재 관측 요약만(부재는 전부 `insufficient_evidence`, `stall_suspected` 는 진단). 본문 §7.2 완결 증명·§9.3 ledger 는 Tier 2.
- **선행 수정**: 큐형 래퍼가 worker 스레드에서 asyncio 큐를 조작하는 결함(부록 A §7) — 래퍼를 `async def` 로 바꾸는 수정·회귀 검증이 큐 계측보다 먼저.

**인용 읽는 법**: 본문·부록의 `*.verdict.txt:줄`, `handoff_*.txt`, `collection_expected_r*.md`, `switch_jobs_table_*.txt`, `/private/tmp/…whl` 은 리포 밖 설계 검토
기록(작성 시점 해시 명시)이며 출처 표시다. 리포 안 근거는 `app/…:줄`(master `77f8dd4` 기준).

---

## 본문 (r3)

작성: Codex, 2026-09-22. 상태: **검토용 계약안 — S0 열린 결정이 있으며 구현 승인을 뜻하지 않는다.**

⛔ 이번 산출물은 이 새 문서 하나다. 수집 코드·스케줄·설정·r1/r2·추출 표·판정 원문을 변경하지 않는다.
범위는 은행 9종 + investing + dxy의 수집 `task_*`다. 거래소 5종·KRX 수집기, 수집 성공/유효성 계약, 알림·API/UI는 이번 계약의 구현 대상이 아니다.

### 0. 기준, 증거 등급, 읽는 방법

- 코드 기준: `/Users/jay/Downloads/Projects/FXi/exchange-rate`, 사용자 지정 `master 77f8dd4`(전체 SHA `77f8dd41a605789e62969acab2ffbe95aed7f4c6`). 아래 `app/...:행`·`tests/...:행`·문서 경로는 이 루트 상대 경로다.
- 인계서: `handoff_r3.txt:1–27`, SHA-256 `c57f05ed56e6c480c6171fb18fb9ade8f68018f9228a942d08be7a802389ab34`.
- 출발점: `collection_expected_r2.md`, SHA-256 `b1d9b54ca1cf3b8ab837ba8586048b1318a639e6aaa4d8352ce8156398ca70bb`.
- 정책 대조 표: `switch_jobs_table_77f8dd4.txt`, SHA-256 `f4f9a786150f148b70d3217f65272eeba11d097fd6d4a976c3a6bad4dfd86349`.
- 판정 원문: `collection_expected_r1.verdict.txt:82–137`, `collection_expected_r2.verdict.txt:71–131`.
- **[현행 사실]**은 이번에 읽은 코드·문서의 file:line으로 뒷받침한다. **[r3 계약]**은 앞으로 구현할 규칙이며, 현재 존재하는 기능이라는 뜻이 아니다. **[추정/미측정]**은 증명·측정하지 않은 부분이다. 이하 별도 사실 표기가 없는 규칙·필드·이벤트는 모두 r3 제안이다.

#### 0.1 APScheduler 버전과 검증 범위

기준 버전은 `requirements.lock.txt:4`의 **3.11.2**다. 설치된 로컬 3.11.0과 구분한다. 이번에는 다음 보관 휠을 설치·압축 해제 없이 읽고, `python3 -B`의 import 경로 맨 앞에 넣어 메모리 내 재현에 사용했다.

`/private/tmp/claude-501/-Users-jay-Downloads-Projects-FXi-exchange-rate/3b41f41d-dace-450a-a034-23934b6e8485/scratchpad/aps/apscheduler-3.11.2-py3-none-any.whl`

SHA-256: `ce005177f741409db4e4dd40a7431b76feb856b9dd69d57e0da49d6715bfd26d`.

이하 **`APS312/파일:행`은 이 휠 내부 `apscheduler/파일:행`**을 뜻한다. `APS310/`은 `/Users/jay/miniconda3/lib/python3.13/site-packages/apscheduler/`다. 임시 휠 경로의 장기 보존은 보장하지 않으므로 재검증 때 동일 해시의 아티팩트를 사용한다.

직접 대조 결과 `executors/asyncio.py`와 `schedulers/base.py`는 두 버전이 동일하다. 3.11.2의 `executors/base.py:106–155`에는 예정 시각별 grace 검사 후 `job.func(*job.args, **job.kwargs)` 호출이 있다. `job.py:141–155`와 `triggers/cron/__init__.py:205–222`에는 UTC 기준 비교/가감 변경이 있다. 같은 폴더의 `apscheduler_3110_vs_3112.txt:12–19`는 참고 기록이며, 이번 결론은 휠 직접 대조·§12 재현에 근거한다.

r2 판정의 **“20회 중 19회 진입→제출 관측”은 3.11.0에서 얻은 과거 표본**이다(`collection_expected_r2.verdict.txt:75,131`). 3.11.2의 빈도 측정으로 인용하지 않는다. 3.11.2 소스상 순서 보장이 없다는 사실과, 새 실행 연결 구현이 동작을 보존한다는 주장은 별개다.

### 1. r2 판정 6건의 처리표 — A1

| r2 지적 | 독립 확인과 r3 처리 | 닫힘/남은 조건 |
|---|---|---|
| ① 실행별 슬롯 연결·등록 세대·자식 시작 | §4: 인자 없는 동기 래퍼를 유지하는 실행기 확장과 래퍼 역산 비교. 세대·제출 묶음·개별 실행·큐·재시도 연결. §4.4: 실제 spawn 성공과 dequeue 분리 | 계약 경로 구체화. **S0-O1** 실행기 확장 채택·3.11.2 동등성 검증은 열림 |
| ② 설정 효력을 재등록 종료에 묶음 | §3: DB commit 확인 관측 시각을 정책 효력으로 권고. 캐시·개별 등록 반영은 별도 증거. OFF→ON 등록 실패에도 기대 유지. 초기 스냅샷·연속 토글·종료 누락 규칙 | 효력/적용 분리 해소. **S0-O2** 효력 기준 채택 및 shadow 이력 한계 수용은 열림 |
| ③ transition 정의와 ADR-044 모순 | §5: 해당 정책 슬롯을 생성할 수 있는 상태인지 판정. ID 존재와 분리. 호출 전후 구간으로 경계 불확실성 표시. ADR 두 슬롯 직접 설명 | 정의·대표 사례 해소. 과거 raw 증거 전체를 이번에 재집계한 것은 아님 |
| ④ 성공값 오류·pause·정책 밖 실행 | §6: `completed/raised` 그대로 사용. next_run_time의 missing/null 구분, pending·scheduler.state, 구조/실행 비교. 귀속 불가 실행도 보존 | 계약상 해소. 스냅샷 사이 상태를 완전 복원한다는 보장은 하지 않음 |
| ⑤ 단일 결과·관측 정상·늦은 증거 | §7–8: 발생/수신 시각, 증거 완결 조건, 실행별 충돌과 슬롯별 우선순위, `entered_late` 정의 | 선택 규칙 해소. **S0-O3** 무유실 증명 경로·파라미터 검증은 열림 |
| ⑥ 서로 다른 deadline·정정 | §9: 생성 cursor와 미확정 집합 분리, deadline 순서 처리, 판정 ID·revision·중복 제거·보존 만료 처리 | 알고리즘 해소. 보존·도착 유예의 실측값은 **S0-O3**에서 확정 |

### 2. 기대 슬롯과 정책의 독립성

#### 2.1 기대 계산

`collection_expected`는 실제 등록·실행 성공과 독립이다. 정책 후보는 `(job_id, due_at)`마다 만들고, 다음을 due 시각 기준으로 적용한다.

1. `mode = get_market_mode(due_at in Asia/Seoul)`.
2. 독립 선언 정책 표에서 해당 mode/job의 트리거 발화인지 검사한다.
3. §3의 그 시각 유효 설정을 적용한다. 확실한 OFF는 `admin_disabled`, 정책 슬롯 자체가 없으면 `scheduled_off`다. 둘이 겹치면 표시 우선은 `scheduled_off`이며 두 조건은 보존한다.
4. 설정을 모르면 기대를 false로 만들지 않는다. `expectation_state=unknown`인 후보를 남겨 `insufficient_evidence/config_history_unknown`으로 보고한다. 이 후보는 확정 expected 분모에 섞지 않는다.

정상 제외는 위 두 가지뿐이다. 등록 누락·전환·재시작·큐 거부는 기대를 없애지 않는다. 공휴일은 `market_expected`에만 사용한다. 현재 모드 함수는 요일·시간을 사용한다(`app/market_mode.py:42–78`); 기존 계획도 이 분리를 요구한다(`SOURCE_HEALTH_PLAN.md:137–161`). DXY fallback용 `get_dxy_policy_state()`를 이 슬롯 시계로 사용하지 않는다(`app/market_mode.py:9–19`).

슬롯 레코드: `slot_id, crawler, job_id, due_at_utc, mode, policy_revision, config_revision, policy_grace_s, dispatch_deadline`.
`slot_id = H(schema_version, policy_revision, job_id, due_at_utc)`이며 프로세스·실제 등록 세대·설정 revision은 ID에 넣지 않는다. 동일한 정책 기회를 재시작·늦은 설정 증거 때문에 중복 생성하지 않기 위해서다. 설정 보정은 같은 후보의 판정 revision으로 남긴다. 정책 개정은 발효 구간을 겹치지 않게 선언하고 과거 슬롯을 새 정책으로 재발행하지 않는다.

`dispatch_deadline = due_at + policy_grace_s`. 슬롯 생성 때 mode·grace·정책 revision을 고정한다. 설정 정보가 늦게 확정돼도 grace를 현재 모드 값으로 바꾸지 않는다. 예정 시각은 UTC 정규화, 표시와 정책 계산은 KST다. `now < due_at`은 미래 슬롯이므로 아직 판정하지 않는다.

#### 2.2 현행 정책 표 요약

아래는 모두 활성일 때다. 모든 등록 선언의 `max_instances=1`; `coalesce`는 미지정이고 현행 기본값은 True다(`app/scheduler.py:45`, `APS312/schedulers/base.py:906–912`). 원본 표의 `coalesce=None`은 **미지정 표기**이지 유효값 None이 아니다.

| mode | 등록 수 | 작업별 초/추가 조건과 grace(초) | 코드 근거 |
|---|---:|---|---|
| IN | 11 | investing `7,17,27,37,47,57`/5; dxy `1,11,21,31,41,51`/5; kb `9,19,29,39,49,59`/10; hana `2,12,22,32,42,52`/10; woori `14,44`/30; bs `33`/30; citi `13`/30; shinhan `18`/30; ibk `34`/30; nh `54`/30; sc `58`/30 | `app/scheduler.py:772–916` |
| BREAK1 | 10 | IN과 같은 초/유예에서 sc 제외. woori `hour=19-23,0-5`, shinhan `hour=19-23,0-2` | `app/scheduler.py:918–1061` |
| BREAK2 | 9 | investing/dxy/kb/hana/bs/citi/nh는 같은 초/유예. `task_ibk_terminal` 화~토 06:00:34·06:01:34/30. `task_woori` 화~토 06:00~06:03 `:14/:44` + 06:04:53의 OR/30 | `app/scheduler.py:1063–1201` |
| OUT | 7 | 매분 investing `08`/30, nh `10`/30, dxy `21`/120, kb `28`/30, shinhan `30`/30, hana `38`/30, bs `51`/30 | `app/scheduler.py:1203–1320` |

`task_ibk`·`task_ibk_terminal`의 crawler는 ibk, 나머지는 `task_` 접미 이름이다. **귀속은 crawler 이름만으로 합치지 않고 job_id까지 일치시킨다.** BREAK2의 등록 9개는 하루 슬롯 9개가 아니다. 월요일에는 우리·IBK terminal이 등록돼도 당일 발화가 0회다. 화~토에는 우리 9회·IBK 2회다(위 코드 및 §12의 3.11.2 재현).

모드 구간은 반열린 구간으로 적용한다: OUT 토 07:00~월 06:00, BREAK1 평일 19:00~다음 날 06:00, BREAK2 월~금 06:00~08:00 및 토 06:00~07:00, IN 평일 08:00~19:00(`app/market_mode.py:60–78`). 창 경계에서 기존 모드의 grace가 이어져도 슬롯의 mode는 due 시각으로 고정된다.

정책은 구현 시 별도 선언한다. 런타임 `get_jobs()`나 등록 코드의 AST 추출물을 기대값 생성기로 재사용하지 않는다. AST 표는 기준 확인 자료이고, 독립 정책↔가짜 스케줄러 등록 결과의 구조 일치 시험을 둔다. 비교 대상은 트리거 종류·필드·timezone·start/end·jitter·OR 자식 구조·유효 grace/coalesce/max_instances·crawler 매핑이다. 문자열만 비교하지 않는다.

### 3. 관리자 설정: 정책 효력과 실제 적용 — A3

#### 3.1 현행 한계와 효력 제안

현재 `CrawlerConfig`에는 최신 `enabled/updated_at`만 있고 이력 revision은 없다(`app/models.py:57–64`). `updated_at`은 `db.commit()` 전에 대입한다(`app/crud.py:1765–1768`); commit 시각이나 과거 효력 구간으로 사용할 수 없다. DB commit 뒤에도 CRUD 로그가 실행되므로 CRUD 함수 반환만 보면 “DB 실패”와 “commit 후 기록 실패”를 구분하지 못한다(`app/crud.py:1768–1776`). 토글은 DB→캐시→전환 잠금→재등록 순서이며, DB·캐시 쓰기는 잠금 밖이다(`app/scheduler.py:276–292`).

**권고 효력 기준:** `policy_effective_at = commit_ack_observed_at` — 해당 변경의 `db.commit()` 성공 반환을 호출자 코드가 처음 관측한 시각. 이는 DB 내부 commit 순간의 주장도, API 성공 응답 시각도 아니다. `commit_started_at`과 함께 관측 구간을 남기되, r3 정책 자체는 이 명시적 관측 시각을 경계로 정한다. `due_at < effective_at`은 구값, `due_at >= effective_at`은 신값이다. 등록·캐시·종료 기록 성공은 이 경계를 미루지 않는다. 관측 실패로 시각을 잃으면 설정 unknown이며 구값 유지로 대체하지 않는다.

**S0-O2 열린 결정:** 위 관측 경계와, DB 내구 commit의 선형화 순서/시각을 효력으로 삼는 대안을 선택해야 한다. 후자는 DB와 원자적인 revision 이력 또는 동등한 영속 증거가 필요하고, 현재 모델로 정확히 복원할 수 없다. r3는 shadow에서 전자를 권고한다. 이 선택은 관측 계약의 의미이므로 S1 전에 확정한다. 실제 토글 제어 흐름·락을 추가해 순서를 바꾸는 일은 이 권고에 포함하지 않는다.

#### 3.2 초기 스냅샷과 revision

- `config_baseline`은 11개 crawler 각각의 존재 여부·DB 값·읽기 시작/종료·캐시 관측값·기준 ID·증거 완결성을 남긴다. 캐시 폴백을 DB 사실로 취급하지 않는다. 조회 실패·행 누락은 해당 crawler unknown이다. 현행 폴백은 빈 캐시에서 True를 반환한다(`app/scheduler.py:220–249`).
- 초기 읽기와 변경 관측의 사이를 비우지 않는다. 변경 관측기를 먼저 준비하고 읽기 전후의 변경 세대를 비교한다. 동시 변경이 있으면 일관된 구간을 재구성하거나 baseline을 재시도한다. 안정된 시점을 증명하지 못하면 unknown으로 시작한다. baseline의 유효 시작 이전을 최신 값으로 소급하지 않는다.
- `config_event_id=(process_instance_id, producer_id, seq)`는 사건 ID다. `config_revision=(baseline_id, crawler, ordered_revision)`은 **입증된 효력 순서**다. 두 값을 혼동하지 않는다. `before/after, parent_revision, commit_call_id, commit_started_at, commit_ack_observed_at, recorded_at, received_at`을 기록한다. before를 모르면 unknown으로 표기하며 캐시값을 DB before로 조작하지 않는다.
- 순차 OFF→ON→OFF는 각각 별 revision과 반열린 효력 구간을 가진다. 같은 timestamp면 입증된 revision 순서를 사용한다. commit 호출 구간이 겹치고 서로 다른 값의 순서를 입증할 수 없으면 수신 순서로 revision을 발명하지 않는다. 관련 구간은 `config_order_ambiguous`; 일관된 후속 baseline 이후만 다시 확정한다. 병렬 토글을 직렬화하는 새 락은 관측 추가가 아니라 동작 변경이므로 별도 검토 대상이다.
- 프로세스 재시작 시 새 baseline/epoch를 만든다. 과거 이력을 보관 로그로 복원할 수 있는 경우만 이전 revision과 연결한다. 외부 DB 수정 등 관측하지 못한 경로가 발견되면 마지막 확인~새 baseline 사이를 unknown으로 열고, 새 현재값으로 과거를 덮지 않는다.

#### 3.3 적용 증거와 실패

`cache_applied_at`, `cache_revision`, `switch_requested_at`, `switch_id`, `registration_generation`, 각 설정 guard가 읽은 `enabled/config_revision`, 개별 add/remove 완료 구간을 별도로 남긴다. `requested_config_revision`은 토글 요청의 귀속값이며, 전환이 실제로 읽은 revision과 같다고 가정하지 않는다. 시작/종료의 전체 snapshot 외에 **각 guard read의 revision**이 필요한 이유는 현행 본문이 작업마다 `is_enabled()`를 따로 호출하기 때문이다(`app/scheduler.py:778,789,806,989,1165,1185`). 기록은 실제로 읽은 값을 동반하며 재조회로 대신하지 않는다.

| 사례 | 기대값과 관측 결과 |
|---|---|
| OFF→ON commit 확인, cache/add 실패 또는 전환 finished 없음 | effective_at 이후 정책 슬롯을 계속 생성. 실행 없으면 §8 판정. 설정 적용 실패/`admin_change`를 문맥으로 붙임 |
| ON→OFF commit 확인, 제거 실패로 구 작업 실행 | 이후 슬롯은 admin_disabled. 실행 증거는 §6.3 `unexpected_execution/admin_disabled`로 보존. 이전 due의 이미 제출된 실행은 이전 슬롯에 연결 |
| 토글 A를 기다리는 동안 B가 cache에 먼저 적용 | A의 switch_id를 최초 적용 시점으로 단정하지 않음. 개별 guard의 실제 revision으로 연결; A revision이 등록에 사용되지 않았다면 `superseded_before_registration` |
| commit 실패가 확실함 | 정책 revision을 만들지 않음. 단 네트워크 오류 등으로 commit 결과 자체가 불명확하면 성공/실패를 추정하지 않고 새 baseline 필요 |
| commit 성공, 기록 또는 프로세스 종료로 이후 이력 소실 | known effective 증거가 있으면 그 효력 유지. 없으면 공백 unknown. 종료 이벤트 부재가 이전 OFF를 영구 유지시키지 않음 |

`admin_state_mismatch`는 동일 revision/일관된 관측 구간에서만 확정한다. 적용 지연 타이머는 **effective_at부터** 시작하며 finished를 기다리지 않는다. `admin_apply_timeout_s` 경과 시 종료 누락도 지연 사건으로 남긴다. 이 값은 정책 grace와 별개인 미측정 파라미터다. 새 revision이 생겨도 이전 지연 사건을 삭제하지 않고 superseded/해소 시각을 기록한다.

### 4. 실행별 슬롯 연결 — A2

#### 4.1 연결 키와 인과관계

기대 슬롯과 실행의 ID를 분리한다. 실제 실행 원천에는 `process_instance_id, scheduler_instance_id, jobstore_alias, executor_alias, job_id, registration_generation, dispatch_batch_id, scheduled_run_time, execution_id`를 남긴다. `registration_generation`은 **개별 등록 시도 전에** 예약해 생성하는 래퍼 closure에 결속하고, 성공/pending/실패 상태를 따로 기록한다. add 반환 뒤 현재 job을 찾아 세대를 붙이면 반환 전 발화 또는 remove/add와 경합한다. 이미 제출된 구 세대의 완료는 신세대로 옮기지 않는다.

`execution_id`는 제출 묶음 내 개별 예정 시각을 가리키며, 재전달 이벤트 ID와 다르다. 서로 다른 프로세스·세대·제출 묶음은 예정 시각이 같아도 다른 실행일 수 있다. 동일 기대 슬롯에 여러 실행이 연결되면 `duplicate_dispatch`를 남기고 슬롯 분모는 1개로 유지한다.

증거→정책 연결은 `job_id + scheduled_run_time`의 정확한 일치와 그 시각 정책 revision으로 수행한다. actual mode/grace가 달라도 동일 슬롯의 수행 증거는 인정하고 등록 mismatch를 별도 남긴다. 시간 창 안의 임의 시작이나 “가장 가까운 슬롯”은 연결 근거가 아니다. 구 `task_ibk`를 crawler가 같다는 이유로 `task_ibk_terminal`에 붙이지 않는다.

#### 4.2 전달 경로 선택지 비교

현행 요청형·큐형 래퍼는 인자 없는 동기 함수다(`app/scheduler.py:320–334,738–744`). AsyncIOExecutor는 동기 함수를 이벤트 루프의 기본 실행기로 보내며, native coroutine은 이벤트 루프 task로 실행한다(`APS312/executors/asyncio.py:31–52`). 제출 이벤트는 executor 제출 호출 이후 전달되므로 래퍼 진입보다 먼저라는 보장이 없다(`APS312/schedulers/base.py:1189–1244`).

| 선택지 | 예정 시각의 개별 결속 방법 | 실행 위치·misfire·coalesce·max_instances 영향 | 판단 |
|---|---|---|---|
| **A. AsyncIOExecutor 경로 확장 + 예정 시각별 runner 관측** | 제출 때 Job/세대와 run_times를 불변 envelope로 캡처. 동일 스레드 풀에서 실행하는 runner의 각 run_time에 대해 기존 grace 검사 후, `job.func()` 호출 구간에 thread-local 실행 문맥을 set/reset. 래퍼는 인자 없이 해당 문맥을 읽음 | 동기 위치 유지 목표. 기존 한 묶음 제출/완료와 job.id 카운터 유지. grace 검사·예외/반환·coalesce 이후 리스트를 그대로 보존해야 함. private runner 확장과 계측 지연 때문에 **동작 불변은 미입증** | **권고, S0-O1** |
| **B. 래퍼 내부에서 트리거 역산** | 래퍼 closure에 등록 세대/트리거를 담고 진입 시각 이전의 후보 due를 열거. 정확히 하나임을 추가 증거로 입증할 때만 연결 | 동기 위치 유지. 라이브러리 misfire/coalesce/max_instances를 직접 바꾸지 않지만 역산 비용이 진입을 지연시킴. executor가 언제 어떤 run_time을 제출했는지는 알 수 없음 | 전체 슬롯 연결에는 부적합. 모호하면 미귀속 보조 증거만 |
| C. submitted 리스너가 job별 큐에 due 저장 | 래퍼가 큐에서 due를 꺼냄 | 래퍼가 먼저 올 수 있음. 기다리게 하면 스레드 대기·grace 이후 지연을 새로 만듦. coalesce·다중 세대·skip 때 1:1 소비도 보장 못 함 | 전달 경로로 제외. 사후 사건 대조는 가능 |
| D. `job.kwargs`에 due 주입 | 등록시 정적 kwargs 또는 공유 job 수정 | APScheduler는 동적 scheduled_run_time을 자동 주입하지 않음. 인자 없는 래퍼는 kwargs를 받지 못함. 공유 kwargs 변경은 이미 제출된 실행과 경합. async로 바꾸면 실행 위치도 바뀜 | 제외 (`APS312/job.py:173–203`, `executors/base.py:131`) |

B의 구체 반례: DXY OUT 10:00:21을 제출한 스레드가 10:01:22에 진입하면 120초 grace 후보에 10:00:21과 10:01:21이 함께 있다. “마지막 trigger due”는 잘못된 회차를 선택할 수 있다. coalesce=True도 이 문제를 없애지 않는다. 최신 슬롯 선택은 **스케줄러가 due 목록을 만든 시각** 기준이며, 래퍼 진입 전 새 슬롯이 생길 수 있기 때문이다. 트리거 시간대만 맞는다는 것은 결속 증명이 아니다.

#### 4.3 권고 A의 구현 경계

1. 기존 default executor의 확장 경로 안에서 대상 `task_*`만 계측한다. 다른 alias로 구·신 세대를 갈라 max_instances 카운터를 분리하지 않는다. `_instances[job.id]` 검사는 `APS312/executors/base.py:58–75`에 있다. 같은 `task_woori` ID를 유지하는 현행 이유도 `app/scheduler.py:984–988,1180–1183`에 명시돼 있다.
2. envelope를 executor 제출 전에 준비한다. max_instances 거부도 그 Job/세대/envelope로 기록하고 원래 예외를 그대로 전달한다. `submitted` 이벤트 리스너가 나중에 현재 `get_job(id)`로 세대를 조회하는 방식은 금지한다. bare APS 이벤트만 있고 세대가 유일하지 않으면 미귀속 처리한다.
3. 기존 `run_job`에는 개별 실행 문맥 hook이 없다(`APS312/executors/base.py:114–131`). 버전에 맞춘 runner adapter/국소 확장이 필요하다. run_times를 실행별 **여러 future로 쪼개지 않는다**. 기존 묶음의 instance 증가/감소·callback·cancel·예외 변환·반환값 처리 시점을 보존한다. 다른 job.func나 전역 `run_job`을 실행 중 갈아끼우는 monkey patch는 채택하지 않는다.
4. thread-local은 **worker 스레드 안에서** 각 실제 함수 호출 전후 set/reset한다. 스케줄러 스레드의 ContextVar가 자동 전달된다고 가정하지 않는다. 미설정·reset 실패는 관측 결손으로 보고하며 다음 실행에 이전 ID를 재사용하지 않는다. 기존 수집 함수의 Exception/BaseException 처리 범위는 바꾸지 않는다.
5. 제출 전에 coalesce로 제거된 due는 executor에 오지 않는다(`APS312/schedulers/base.py:1189–1192`). A만으로 `coalesced` 증명은 불가능하다. 정확한 pre/post run_times와 처리 cycle/세대를 캡처하는 추가 scheduler hook을 선택할 수 있으나 별도 private 확장이다. **기본 권고는 이 확장을 보류**하고 직접 증거가 없는 생략을 coalesced로 추정하지 않는 것이다.
6. 관측 실패가 본 실행을 막지 않는 격리를 구현하되, 해당 관측 구간을 정상이라고 선언하지 않는다. 관측 오류를 삼키고 손실 표식마저 사라졌으면 §7의 정상 증명도 실패한다. 계측 비용 0·완전한 시간 동등성은 주장하지 않는다.

#### 4.4 큐·재시도·자식까지 보존

요청형은 래퍼 진입을 `run_entered`로 기록한다. 이 이름은 **dispatch의 요청형 도착점**이며, 실제 네트워크 요청·유효 수집·저장 성공을 뜻하지 않는다. 큐형은 `wrapper_entered`와 `queue_accepted/queue_rejected`를 분리한다. 현행 큐 거부는 처리 중/이미 대기 중, 압력 `qsize>=20`, QueueFull이며, 앞 둘은 DEBUG·압력/QueueFull은 WARNING이다(`app/scheduler.py:631–684`). 코드 reason은 `worker_processing/already_in_queue`를 보존하고 요약만 `duplicate_in_flight`로 묶는다.

성공한 put에 `queue_item_id, execution_id, slot_id(or unresolved), enqueued_at, attempt_no=0`를 결속한다. 현재 정렬키 `(priority, timestamp, bank_name, is_retry)`와 동률 비교를 유지하는 payload/envelope를 권고한다. ID를 정렬키 앞에 끼워 순서를 바꾸지 않는다. item별 metadata 수명은 put 성공~task_done/명시 폐기까지이며, 적재 실패 시 성공 item을 남기지 않는다. worker의 unpack·중복 순회도 함께 검증한다(`app/scheduler.py:539–574,603–608,663–674`).

재시도는 같은 원 실행/슬롯에 `attempt_no=1, parent_queue_item_id`를 연결하고 새 queue_item_id를 만든다. 현재 +1000 우선순위·최대 1회 규칙을 유지한다(`app/scheduler.py:563–571`). 원 슬롯에 새 expected를 만들거나 재시도 때문에 최초 dispatch_deadline을 연장하지 않는다. 다음 정기 슬롯의 duplicate 거부를 이전 슬롯 성공으로 채우지 않는다.

`queue_dequeued`는 꺼냄일 뿐이다. `child_started`는 **부모가 subprocess 생성 성공을 관측한 지점**으로 한정한다. legacy는 `await create_subprocess_exec()` 성공 반환 직후(`app/scheduler.py:363–367`), IBK는 `subprocess_exec()` 성공 반환 직후(`app/ibk_subprocess_capture.py:164–173`)의 별도 hook이 필요하다. IBK `cleanup_blocked` 분기는 spawn 자체가 없다(`app/ibk_parent_runner.py:179–187`). 여기는 `child_not_started/cleanup_blocked`, spawn 오류는 `child_not_started/spawn_error`로 남긴다. collector 진입은 별도 child-side 증거 없이는 주장하지 않는다. 기존 IBK run_id와 슬롯/queue_item_id는 부모에서 연결하고 IBK 결과 프로토콜의 의미를 바꾸지 않는다.

### 5. transition과 등록 경계 — A4

#### 5.1 슬롯 생성 가능 상태

슬롯 s에 대해 `can_generate(s, registration_state)`는 동일 job_id의 실제 세대가 다음을 충족하는지 나타내는 **true/false/unknown**이다.

- 해당 trigger가 s.due_at을 발화 시각으로 포함한다. 트리거의 mode 이름이 정책 mode와 같은지는 필수 조건이 아니다.
- pending이 아니고 scheduler가 실행 가능한 상태이며 job이 pause 상태가 아니다.
- 그 세대의 `next_run_time`/처리 이력이 해당 due를 이미 건너뛰지 않았다. due 뒤에 새로 add된 트리거가 달력상 그 시각을 포함하더라도 초기 next_run_time이 더 뒤면 지난 슬롯을 복원한 것이 아니다.
- 상태 증거의 유효 구간·완결성이 해당 시각을 덮는다. 상태를 모르면 false가 아니라 unknown이다.

max_instances·coalesce·executor 대기·grace 판정은 이 상태가 생성한 기회를 처리하는 다음 단계다. `can_generate=true`가 함수 진입을 보장하지 않는다. 반대로 이미 제출된 실행은 제거·pause 후에도 구 슬롯 증거가 될 수 있다.

`context_reason=transition`은 정책 mode 경계와 관련된 remove/add 또는 구 trigger 잔류 때문에 해당 슬롯을 만들 수 없는 상태였음이 확인될 때 붙인다. `transition_evidence`에 경계·switch_id·세대·불능 사유·확실성을 담는다. due에 상태가 불명확하면 `transition_status=possible`이지 확정 transition이 아니다. due에는 가능했지만 제출 전에 제거됐음이 증명되면 그 기회 상실도 transition 문맥으로 남긴다. 단순히 `due < switch_finished_at`이거나 작업 ID가 없다는 것만으로 단정하지 않는다.

관리자 변경·수동 pause 등 별도 이유는 각각 `admin_change`·등록 상태 사건으로 기록한다. 정책 전환 증거 없이 모두 transition으로 뭉치지 않는다. `late_switch`는 전환 진입/개별 등록이 정책 경계보다 늦은 사실이며, 경보 임계와 다르다. 현행 제어 job은 매분 :01에 등록된다(`app/scheduler.py:1980–1990`). 몇 초까지 정상인지의 측정값은 이 문서에 없다.

#### 5.2 operations[].at의 한계

현재 `operations[].at`은 **add/remove 호출이 반환되거나 예외를 잡은 뒤** `_note_job_op`에서 기록한 시각이다. 내부 변경의 선형화 시각이 아니다(`app/scheduler.py:1395–1405,1415–1434`). 따라서 r3는 `call_started_at, call_returned_observed_at, recorded_at(기존 at), monotonic_start/end, operation_id, generation, result, exception_type`을 추가 제안한다. 실제 변경점은 보통 호출 전후 구간 안에 있고, 후행 기록과 같은 시각이라고 확정하지 않는다. 실패 호출은 부분 변화 가능성을 배제하지 말고 다음 snapshot으로 확인한다.

| due와 관측 구간 관계 | 판단 |
|---|---|
| 성공 add 완료 관측 이후, 이후 mutation 없음이 확인되고 next_run_time이 해당 due를 포함 | 그 세대는 슬롯 생성 가능 후보 |
| remove 호출 시작 이전의 완전한 상태 구간 | 이전 세대로 평가 |
| due가 remove/add 호출 구간과 겹침 | 경계 불확실. 직접 실행 증거는 인정하되 상태만으로 transition을 확정하지 않음 |
| 과거 기록에 at만 존재 | at을 상한 관측으로 사용. 직전 신뢰 상태/호출 범위를 찾을 수 없으면 하한 미상. 정밀한 불능 구간이나 초 단위 손실 수를 발명하지 않음 |
| 전체 finished만 있거나 operations_complete=false | 작업별 준비 경계 미확인. 기록 결손 사건과 슬롯 판정을 분리 |

실제 전환은 모든 `task_*` 제거 후 순차 등록이다(`app/scheduler.py:767–779`). 전체 switch 종료를 모든 job의 동시 준비 시각으로 쓰지 않는다. 전환 전후 snapshot도 수집 시작/끝·도중 mutation 여부를 남기며 원자적 전역 상태라고 가정하지 않는다.

#### 5.3 ADR-044와 정상 전환 중 실행

ADR 기록에서 2026-09-18 06:00:14·06:00:44에는 `task_woori` 자체는 존재했지만 BREAK1 `hour=19-23,0-5` trigger가 남아 있었다. 이는 06시 정책 슬롯을 만들 수 없는 상태다. BREAK2 trigger는 06:01:01 remove/add 뒤 설치됐고 지난 두 due를 복원하지 않았다(`DECISIONS.md:7202–7222`). 따라서 이 두 슬롯은 **transition·late_switch 문맥을 갖는 미실행**이며 우리 job 자신의 misfire/max_instances로 바꾸지 않는다. 현재 코드의 두 trigger도 이 기전과 일치한다(`app/scheduler.py:989–995,1185–1198`; §12 재현).

기록된 ADR 결론을 재현하는 완전한 시험 fixture에서는 `not_executed + transition,late_switch`가 정답이다. 이번 읽기는 ADR 서술·코드 대조이며 당시 raw 전체를 다시 감사한 것은 아니다. 구 로그에 r3의 ready/seq/watermark가 없다는 이유로 ADR의 확인된 사건을 부정하지 않되, 자동 비교기가 임의 과거 구간을 같은 확실성으로 채워서는 안 된다. 과거 판정에는 `evidence_basis=legacy_adjudicated/ADR-044`를 붙여 live r3 완결 증거와 구별한다.

BREAK1→BREAK2의 DXY는 두 trigger 모두 `:01/:11/...` 슬롯을 만들 수 있다(`app/scheduler.py:939–945,1086–1092`). 구 세대 실행이라도 정확히 06:00:01에 귀속되고 도착점이 deadline 안이면 `entered`다. 전환이 끝나지 않았다는 이유로 이를 미실행 처리하지 않는다. 실제 불능 구간이 입증되지 않으면 transition을 자동 부착하지 않는다.

### 6. 등록 비교기와 귀속되지 않는 실행 — A5

#### 6.1 전환 사건의 신뢰도

현행 `mode_switch_finished.outcome`은 **`completed` 또는 `raised`**다(`app/scheduler.py:1488–1500`). `ok`로 정규화하지 않는다. `operations_complete=false`는 관측 기록 불완전을 뜻한다(`app/scheduler.py:1407–1412`). 이 값과 본문의 성공 여부는 별개 축이다.

| 입력 | 등록 비교기의 처리 |
|---|---|
| completed + complete=true + 필수 구조 필드/세대 완비 | 개별 operation을 순서대로 재구성. 전환이 실제 읽은 설정 revision과 당시 정책 기대를 대조. 이후 현재 상태는 snapshot으로 별도 확인 |
| raised | `registration_uncertain/switch_raised`. 성공한 앞쪽 operation 사실은 보존하되 전체 등록 성공으로 해석하지 않음. 후속 snapshot으로 확인된 missing/extra 등은 추가 보고 가능 |
| complete=false 또는 구조 필드 부족 | `registration_record_incomplete`. 기록에서 빠진 add를 곧바로 실제 등록 누락으로 판정하지 않음 |
| started만 있고 종료 기록 없음 | `switch_record_missing` 사건. `switch_record_timeout_s` 이후 보고; 슬롯 기대·판정은 기다리지 않음. 종료 여부와 로그 전달 여부를 단정하지 않음 |
| finished만 있거나 schema/세대 연결 실패 | `registration_record_incomplete/orphan_finish`. 완전한 전환 이력을 가정하지 않음 |

설정/정책이 전환 중 바뀌었다면 “요청한 revision에 맞는가”와 “현재 정책에 맞는가”를 분리한다. 전환이 구 revision에 충실했다는 이유로 현재 mismatch를 감추지 않는다. 빈 기대 집합도 유효하다. 전부 OFF면 실제 잔존 작업은 extra이며, 비교기를 skip하지 않는다.

#### 6.2 실제 상태 snapshot

현재 operations에는 trigger 문자열·ID·at·예외 종류만 있다(`app/scheduler.py:1396–1405`). 다음 구조가 필요하다.

`snapshot_id, process/scheduler_instance_id, sampled_from/to, consistency, scheduler.state(STOPPED/RUNNING/PAUSED), scheduler.running, jobstore_alias, job_id, registration_generation, pending, next_run_time_state(missing|null|value), next_run_time, trigger(type/fields/timezone/start_date/end_date/jitter/OR children), effective_misfire_grace_time, effective_coalesce, effective_max_instances, executor_alias, wrapper_kind/crawler, config_revision_read`.

시작 전 job의 미설정 필드를 null로 뭉개지 않는다. `pending=true`에서 아직 없는 next_run_time과, 등록된 job의 `next_run_time=None`은 다르다. pause는 next_run_time=None으로 표현되며(`APS312/schedulers/base.py:611–620`), `running`은 paused도 True이므로 state를 함께 봐야 한다(`APS312/schedulers/base.py:274–281`). pending의 정의는 `APS312/job.py:129–135`다.

출력 사건은 `registration_missing/extra/mismatch`, `registration_pending`, `registration_paused`, `scheduler_stopped/paused`, `registration_state_uncertain`이다. pending은 실행 가능 등록으로 세지 않는다. 시작 전에는 pending 사실로 보고하고 §10 준비 신호로 실행 가능 여부를 분리한다. 과거 next_run_time은 정상 backlog일 수도 있으므로 그것만으로 pause나 누락을 단정하지 않는다. next_run_time 진척 이상은 동일 세대의 처리·mutation 이력과 함께 비교한다.

전환 직후와 주기 snapshot을 함께 쓴다. 주기 5분은 **후보값/미측정**이며 완전 탐지 보장이 없다. 추가→실행→제거가 두 snapshot 사이에 끝날 수 있고, 이미 제출된 실행은 `get_jobs()`에서 사라질 수 있다. operation 관측으로 모르는 외부 변경은 별도 불확실성으로 남긴다.

#### 6.3 실행 증거의 역방향 비교

모든 대상 실행 증거를 기대 후보로 역조회한다. 맞는 슬롯이 없더라도 버리지 않는다.

- 키·예정 시각·세대는 확실하지만 정책상 OFF/시간표 밖이면 `unexpected_execution`과 `reason=admin_disabled|scheduled_off|unknown_task_job`.
- due가 같아도 다른 job_id면 `job_id_mismatch`; 임의 crawler 매핑으로 terminal 슬롯을 채우지 않는다.
- scheduled_run_time/세대/정책·설정 이력이 불명확하면 `unattributed_execution`과 후보 목록/누락 필드. “정책 밖 실행”으로 확정하지 않는다.
- 구 세대가 제거 후 실행돼도 원래 due 슬롯이 있으면 그 슬롯으로 귀속한다. 뒤 슬롯을 채우지 않는다. 프로세스 경계를 넘어 후보가 여러 개면 사실이 확정될 때까지 미귀속 상태를 유지한다.
- 같은 슬롯의 여러 실제 실행은 `duplicate_dispatch`; 같은 event_id 재수신은 중복 전달이다. 둘을 구별한다. unknown `task_*`의 관측은 가능하지만 거래소·KRX의 기대 정책을 이번 계약에 추가하지 않는다.

### 7. 증거, 시각, 관측 정상의 증명 — A6

#### 7.1 이벤트 envelope와 시각

새 이벤트 공통 필드: `schema_version, event_id, producer_id, seq, process/scheduler_instance_id, job_id, registration_generation, dispatch_batch_id, execution_id, scheduled_run_time, occurred_at, recorded_at, received_at, monotonic_at, clock_epoch, evidence_quality` 및 연결 가능한 slot/queue/attempt ID.

`occurred_at`은 의미 있는 사건 경계의 생산자 시각, `recorded_at`은 기록 시도 시각, `received_at`은 비교기 수신 시각이다. 전송 재시도는 동일 event_id를 유지한다. seq는 기록 성공 뒤가 아니라 **관측 시도 시점부터** 부여해 실패/누락을 추적한다. 이벤트 배열의 수신 순서를 실행 순서로 쓰지 않는다. 제출·skip의 bare APS 이벤트에는 실제 발생 wall-clock이 없으므로 리스너 수신 시각을 원래 발생 시각이라고 표시하지 않는다(`APS312/events.py:73–76,91–107,120–134`). 정확한 시각이 필요하면 §4의 발생 hook에서 캡처하고, 그렇지 않으면 관측 구간/시각 unknown을 남긴다.

같은 프로세스의 duration/지연은 monotonic을 보조 사용한다. 서로 다른 프로세스의 monotonic 값은 직접 비교하지 않는다. wall clock 역행·오차 또는 발생 hook 지연 구간이 deadline에 걸치면 `timeliness=unknown`; received_at으로 on_time을 만들어내지 않는다. 원시 시각은 보존한다.

도착점은 요청형 `run_entered`, 큐형 **최초 시도 `queue_accepted`**다. child_started는 run 보조 증거이며 큐 대기 허용치를 policy grace에 더하지 않는다. 성공한 dispatch는 collector 결과의 success/partial/failure와 독립이다.

#### 7.2 “관측 정상”은 구간·채널별 증명

로그가 한동안 보였거나 listener 등록 1건이 있다는 것만으로 부재를 확정할 수 없다. `observation_complete(slot, cutoff)`는 다음의 결합을 요구한다.

1. **정의된 coverage:** 해당 process epoch에서 scheduler 제출/skip, 요청형 진입 또는 큐 수락/거부, 세대 연결 채널의 설치·schema·활성 구간이 확인돼 있다. 관측 준비와 실제 scheduler 준비를 구분한다. due 이전 준비가 없거나 재시작 공백이 있으면 결손이다.
2. **손실 상태:** 생산자별 seq 연속성, dropped/overflow/serialization_failed/hook_failed 누적 계수와 그 epoch가 확인된다. 실패한 기록을 재전송할 수 없으면 손실을 영속적으로 표시하거나 다음 정상 checkpoint에 sticky counter로 싣는다. 성공 로그만 세는 counter는 증명이 아니다. counter 자체의 전달이 끊기면 정상 증명도 중단한다.
3. **수신 완료:** 생산자의 coverage fence/checkpoint가 cutoff까지의 관측 시도를 봉인하고 생산 seq 범위를 선언한다. 비교기는 그 범위를 모두 받거나 손실 표시를 확인한다. 병렬 worker의 진행 중 기록은 fence보다 늦게 완료될 수 있으므로, fence는 이미 발생한 미완료 관측까지 포함해야 한다. scheduler에서 만든 제출 envelope registry와 worker 기록을 합쳐 barrier를 확인한다. 단순 heartbeat나 `now > deadline + L`은 watermark가 아니다.
4. **발생 시각의 신뢰:** cutoff 이전에 발생한 증거가 cutoff 이후로 잘못 찍힐 수 있는 미확정 구간이 없어야 한다. 진입 발생 hook을 건너뛸 수 있는 경로·수동 호출·예외 경로가 있으면 해당 채널은 불완전하다.
5. **판정 입력의 완결:** 정책/설정 revision과 증거 연결이 확정돼 있고, 같은 슬롯에 귀속될 가능성이 남은 미귀속 실행이나 해결되지 않은 충돌이 없다.

fence는 수집 실행에 대기 barrier를 거는 방식이 아니라 관측기가 비동기로 완료를 확인하는 장치다. 구현상 입증하지 못하면 unknown을 유지한다. **S0-O3:** 손실이 자기 자신도 숨길 수 있는 일반 로그만으로 이 조건을 만족한다고 선언하지 않는다. seq+sticky 손실 표식+수신 watermark의 실제 전달/보존 경로를 선택하고 장애 주입으로 검증해야 한다.

직접 연결된 확실한 `entered/missed/...`는 관련 없는 채널의 공백 때문에 지우지 않는다. 다만 coverage가 없으면 “다른 중복 실행도 없었다”거나 “전체 관측 정상”은 주장할 수 없다. 부재를 근거로 하는 `not_executed/submitted_not_entered`에는 위 증명이 필요하다. 후속 미확정 실행 후보가 결론을 바꿀 수 있으면 §8에 따라 insufficient다.

#### 7.3 늦은 도착과 늦은 실행

| 실제 발생과 수신 | 판정 의미 |
|---|---|
| due ≤ 도착점 occurred_at ≤ deadline, received_at만 늦음 | `entered`, `timeliness=on_time`, `late_evidence=true`. 과거 판정은 §9 정정 |
| 도착점 occurred_at > deadline | **`entered_late`**, `timeliness=late`, 지연값 보존. 늦은 실행을 정상시각 진입으로 정정하지 않음 |
| occurred_at이 없거나 오차 구간이 deadline과 겹침 | 진입 사실은 보존하되 `insufficient_evidence/timeliness_unknown`. received_at으로 대체하지 않음 |
| occurred_at < due | 예정 시각/시계/귀속 충돌. 오차 근거 없이 조기 정상 실행으로 인정하지 않음 |

grace와 같은 경계(`==deadline`)는 on_time이다. APS312 runner도 `difference > grace`일 때 missed로 분기한다(`APS312/executors/base.py:117–127`). 단 이 검사는 함수 호출 직전보다 앞에 있으므로, 통과했더라도 스레드 지연으로 실제 도착점이 deadline을 넘을 수 있다. `entered_late`는 scheduler가 반드시 잘못 동작했다는 뜻이 아니다. 정책상 도착 지연 사실이며 실제 적용 grace도 별도 남긴다.

### 8. 단일 dispatch_outcome 선택 규칙 — A6

#### 8.1 실행 단위의 정규화와 충돌

먼저 event_id로 전달 중복을 제거하고, execution_id/최초 시도별 증거를 집합으로 모은다. 수신 순서가 바뀌어도 같은 집합의 결과는 같아야 한다. `submitted`와 진입은 둘 다 보존하며, 뒤늦게 submitted가 왔다고 entered를 되돌리지 않는다.

| 같은 실행의 증거 조합 | 처리 |
|---|---|
| submitted + missed, 도착점 없음 | `missed`. 제출은 실제 진입이 아님 |
| submitted + 요청형 run_entered | 발생 시각에 따라 entered/entered_late |
| 큐형 wrapper_entered + queue_rejected | `queue_rejected`; wrapper 진입만으로 큐형 entered를 만들지 않음 |
| queue_accepted + child_not_started(cleanup_blocked/spawn_error) | dispatch는 entered/entered_late 유지. 자식 미시작은 run 계층의 별도 사실 |
| queue_accepted + 재시도 | 최초 수락의 결과/시각 유지. 재시도는 같은 슬롯의 후속 시도 |
| missed 또는 max_instances 또는 coalesced + 같은 실행의 도착점 | 정상 경로상 양립 불가. `insufficient_evidence/evidence_conflict`, 원시 양쪽 증거 보존 |
| 동일 queue_item의 accepted와 rejected, 또는 같은 execution의 서로 다른 terminal skip | 동일하게 conflict. 어느 로그를 나중에 받았는지로 선택하지 않음 |
| child_started는 있으나 최초 queue_accepted 시각/연결이 없음 | 실제 자식 사실 보존. dispatch 시각을 추정하지 않고 insufficient. not_executed를 선택하지 않음 |
| 제출만 있고 진입 채널 손실/미준비 | `insufficient_evidence`, submitted_not_entered를 선택하지 않음 |

coalesced는 scheduler가 **같은 세대·처리 cycle의 pre-coalesce 목록에 해당 due를 포함했고 post 목록에서 제거했다는 직접 증거**가 있을 때만 만든다. grace 중첩·제출 한 건·옛 next_run_time만으로 추정하지 않는다. coalescing 자체는 해당 생략 슬롯의 “실행 시도”가 아니므로 별도 synthetic disposition ID로 연결한다.

#### 8.2 슬롯 단위 결정표

슬롯은 여러 실행을 가질 수 있다. 서로 다른 실행의 `missed`와 `entered`는 모순이 아니다. `duplicate_dispatch`와 모든 실행별 결과를 보존한다. 다음 표를 **위에서부터** 적용해 단일 `dispatch_outcome`을 선택한다.

| 우선 | 조건 | dispatch_outcome |
|---:|---|---|
| 0 | 기대/귀속/시각 결론을 바꾸는 충돌 또는 미확정 설정. 충돌한 실행은 정상 증거로 사용할 수 없음 | `insufficient_evidence` + 상세 reason |
| 1 | 독립적으로 신뢰 가능한 도착점이 하나 이상 deadline 안에 존재 | `entered` |
| 2 | 신뢰 가능한 도착점은 있으나 모두 deadline 이후이고 on_time 증거 없음 | `entered_late`; 관측 공백이 있으면 `earliest_entry_complete=false`로 남겨 향후 on_time 정정 가능성 표시 |
| 3 | 도착점 없음. 다른 후보 실행 또는 채널 공백 때문에 **deadline까지의 도착 여부가 직접 terminal 증거나 완결 coverage로 닫히지 않음** | `insufficient_evidence` |
| 4 | 도착점 없고 연결된 queue_rejected 증거가 있음 | `queue_rejected` + reason set |
| 5 | 위 해당 없고 연결된 missed 증거 있음 | `missed` |
| 6 | 위 해당 없고 연결된 max_instances 증거 있음 | `max_instances` |
| 7 | 위 해당 없고 직접 coalesce 생략 증거 있음 | `coalesced` |
| 8 | 제출/큐형 wrapper 진입은 있으나 도착점 없음, deadline까지 관측 정상 증명 | `submitted_not_entered` |
| 9 | 연결된 제출·도착점·확정 skip이 없고 deadline까지 관측 정상 증명 | `not_executed` |
| 10 | 나머지 | `insufficient_evidence` |

우선 0은 슬롯 전체의 결정에 영향을 주는 충돌에 적용한다. 충돌 실행 외에 독립적인 정상시각 도착점이 확실하면 우선 1로 entered를 증명할 수 있으나, `evidence_conflict`·중복 여부·coverage 불완전 표식은 남긴다. “정상 도착점 존재”와 “슬롯 전체 관측 무결”을 분리한다.

우선 4~7의 확정 terminal 증거는 그 실행의 직접 증명이다. submitted+missed만 있는 실행은 missed로 닫히며, 진입 로그가 없다는 이유로 다시 unknown으로 내리지 않는다. 다만 결론을 바꿀 수 있는 **구체적인 다른 후보 실행**이 미확정이면 우선 3이 먼저다. 단순히 future가 아직 끝나지 않았더라도 deadline까지 진입 부재의 coverage가 완결됐으면 우선 3 대상이 아니고 우선 8로 간다. 서로 다른 실행의 복수 실패가 확정됐으면 **큐 단계 거부 > runner misfire > 제출 전 max_instances > coalesce 생략** 순서로 대표값을 고르고 `attempt_outcomes[]`에 나머지를 남긴다. 같은 queue_rejected의 여러 reason도 set으로 남긴다. 단일 대표값 때문에 원인별 실행 수를 잃지 않는다.

`submitted_not_entered`와 `not_executed`는 **deadline까지 도착점이 확인되지 않았다는 판정**이지 앞으로 영원히 실행되지 않는다는 선언이 아니다. 큐형 wrapper가 수락/거부 전에 예외로 끝나면 에러 사실도 별도 보존한다. 후속 늦은 도착점은 entered_late로 정정한다. 아직 deadline 이전이면 lifecycle=`pending`이며 위 최종 결과를 조기에 확정하지 않는다.

`context_reason[]`의 transition/restart_gap/admin_change/late_switch는 이 우선순위를 바꾸지 않는다. `entered`도 수집 성공·healthy를 뜻하지 않는다. health 집계 분모/임계는 D7·D8로 남긴다(`SOURCE_HEALTH_PLAN.md:691–692`).

### 9. 비교 창·미확정 보존·정정 — A7

#### 9.1 두 가지 진행 위치

`enumerated_until`은 정책 후보 **생성 완료**의 반열린 구간 끝이다. 후보를 미확정 map에 넣은 뒤에만 전진한다. `pending_by_slot_id`와 deadline 순서 heap은 별도다. `(dispatch_deadline + evidence_arrival_allowance_s, slot_id)` 순서로 평가하며 due 순서 cursor 하나로 판정 완료를 표현하지 않는다.

비교 시각이 여러 주기 늦어져도 `[enumerated_until, target)`을 빠짐없이 열거한다. 경계의 정책·설정 revision을 모두 반영하고 동일 slot_id 재열거는 idempotent다. `target < enumerated_until`(시계 역행)이면 삭제/역전하지 않고 clock 사건을 남긴다. 빈 기대 집합에서도 생성 진행 위치와 정상 제외 집계는 전진시킨다. 실제 미등록 여부는 생성 진행과 무관하다.

도착 유예 `L=evidence_arrival_allowance_s`는 **판정 수신 대기**에만 사용한다. deadline·misfire grace·on_time 경계는 늘리지 않는다. due+grace+L이 지나도 필요한 watermark가 없으면 부재를 확정하지 않는다. `evidence_wait_timeout_s`까지 pending으로 두고, 이후 `insufficient_evidence/watermark_missing`을 판정하되 미해결 기록은 정정 대상으로 보존한다. 무한 대기나 조용한 폐기 대신 timeout 사건을 남긴다. L/timeout은 §11 S0-O3에서 선택할 미측정값이다.

#### 9.2 월요일 DXY의 확정 순서

| 슬롯(KST) | mode/grace | dispatch_deadline | 같은 L일 때 판정 가능 시점 |
|---|---|---|---|
| 월 05:59:21 | OUT / 120초 | 06:01:21 | 06:01:21 + L 이후, 필요한 watermark 확보 |
| 월 06:00:01 | BREAK2 / 5초 | 06:00:06 | 06:00:06 + L 이후, 필요한 watermark 확보 |

정책 근거: `app/market_mode.py:63–75`, `app/scheduler.py:1086–1092,1251–1259`. **뒤 슬롯을 먼저 확정해도 05:59:21은 pending map에 그대로 남는다.** 한 진입 증거는 정확한 scheduled_run_time에만 귀속하므로 120초 창이 겹친 여러 슬롯을 한 번에 채우지 않는다. 모드 전환 뒤 구 OUT 실행이 실제 제출됐는지도 세대로 확인하며, 제출되지 않았다고 기대 OUT 슬롯을 삭제하지 않는다.

#### 9.3 판정 ID와 정정 프로토콜

- 기본 `decision_id=H(contract_version, slot_id, evaluation_stream_id)`는 슬롯 판정의 안정 ID다. 첫 판정은 `decision_revision=1`. 이후 의미가 달라질 때만 revision을 1씩 올린다. 내용이 같은 재평가·이벤트 재수신은 새 revision을 만들지 않는다.
- 최초 이벤트 ID는 `(decision_id, 1)`. 정정 이벤트는 `(decision_id, new_revision)`을 ID로 가지며 `corrects_event_id=(decision_id, previous_revision)`, before/after 결과·context·시각·원인·추가 evidence_event_ids·config/policy revision을 담는다. 원 판정을 삭제하지 않는다.
- 근거 집합은 event_id로 중복 제거하고 payload hash를 함께 검증한다. 같은 ID/다른 payload는 collision 사건이다. 재집계 때도 같은 근거 집합·contract version은 같은 결과를 내야 한다. 새 평가 알고리즘은 별 evaluation_stream_id로 기록하고 기존 stream을 조용히 재정의하지 않는다.
- 수신자는 decision_id별 revision ledger를 유지한다. out-of-order 정정이 오면 이전 revision/참조를 기다리거나 이력 누락으로 표시한다. 집계는 최신 유효 revision의 **이전 기여 제거 + 새 기여 추가**를 한 번 적용한다. 정정을 새 슬롯 1개로 더하지 않는다.
- 예: not_executed(v1) 뒤 due+2초의 run_entered가 늦게 수신되면 entered(v2), late_evidence=true. 실제 진입이 deadline+2초라면 entered_late(v2)다. 전자의 경우 기존 watermark가 완결을 선언했다면 **관측 계약 위반 사건**도 함께 남긴다. 단순 정정으로 무유실 실패를 숨기지 않는다.
- `raw_evidence_retention`, `decision_ledger_retention`, `dedupe_retention`, `online_correction_horizon`을 별도로 명시한다. `dedupe_retention >= online_correction_horizon + 최대 허용 replay 겹침`, `raw/decision retention >= correction_horizon`을 만족해야 한다. 실제 보존이 더 짧으면 지원 horizon을 줄이거나 근거 불충분으로 표시한다. 무한 정정 보장은 하지 않는다.
- horizon이 지난 증거는 `late_evidence_outside_horizon`으로 보존하고 기본 집계를 임의 변경하지 않는다. 원 판정과 근거를 확보한 별도 replay stream에서만 재평가한다. 보존 만료/메모리 압력으로 pending을 내보낼 때도 `pending_evicted/evidence_retention_gap` 사건과 insufficient 판정을 남긴다. 지원 기간/상한 미확정 상태로 S4의 장기 관측 완결성을 승인하지 않는다.

### 10. 시작·종료·재시작

현행 시작 순서는 config load → 여러 작업 등록 → `control_job(cause="startup")` → `scheduler.start()`다(`app/scheduler.py:1887–1900,2206–2210`). startup의 completed는 pending 등록 완료일 수 있다. 첫 finished만으로 수집 준비가 됐다고 판단하지 않는다.

- `process_instance_id`는 프로세스 시작마다 새로 만든다. `observation_ready`는 채널/schema/seq/fence 준비, `scheduler_ready`는 start 성공·state 확인, 큐형의 `queue_ready`는 큐와 worker 경로 준비를 각각 뜻한다. 시작 직후 훅과 준비 신호가 경합하면 경계 구간을 unknown으로 남긴다. r3 관측기가 현재 코드를 막아 이 순서를 강제로 바꾸지 않는다.
- 준비 전이라도 정책상 ON이면 expected는 유지된다. 직접 실행 증거가 있으면 그것을 보존하고, 부재만 있는 슬롯은 `insufficient_evidence + restart_gap`이다. ready 신호를 기대 생성 시작점으로 삼아 공백 자체를 삭제하지 않는다.
- 정상 종료에는 최종 seq/checkpoint·종료 구간을 기록한다. 비정상 종료 또는 기록 유실이면 마지막 수신 시각을 실제 종료 시각으로 단정하지 않는다. 이전 epoch의 마지막 완결 fence~다음 epoch의 준비 구간은 관측 공백이다.
- 이전 raw/판정/정책·설정 이력을 실제 확보한 범위만 replay한다. 이력이 없으면 `restart_gap(range_unknown)`이며 슬롯 수·정확한 downtime을 추정하지 않는다. due 목록만 계산 가능한 경우에도 설정을 모르면 expected 개수로 확정하지 않는다.
- 동시에 살아 있는 두 process epoch가 같은 슬롯을 실행하면 같은 slot_id의 별 실행으로 연결해 중복을 보고한다. 이 계약이 multi-worker 제어/영속 소유권을 해결했다는 뜻은 아니다. 저장소 및 multi-worker 결정은 D4로 남긴다(`SOURCE_HEALTH_PLAN.md:146,688`).

### 11. S0 열린 결정과 단계 — A8

#### 11.1 열린 결정: 선택지·권고·종료 조건

| ID | 선택지 | r3 권고와 선택 이유 | S0 확정 사항 / 후속 검증 gate |
|---|---|---|---|
| **S0-O1 실행 결속** | A 실행기/runner 국소 확장 / B 래퍼 역산만 / 제출 로그 사후 대조만 | A를 권고. 실제 run_time을 worker의 개별 호출에 넘길 수 있다. B/C는 모호한 슬롯을 완전히 닫을 수 없음. pre-coalesce scheduler 확장은 우선 보류하고, 직접 증명 못 한 생략은 원인 미상으로 남김 | **S0:** 3.11.2 adapter 범위·세대/skip 결속·큐 payload 표현·시험 기준 합의. **S1b:** §11.3 동등성 시험 통과. 확장을 채택하지 않으면 완결을 주장하지 않고 부분 관측 계약으로 범위를 명시적으로 축소 |
| **S0-O2 설정 효력/이력** | commit 확인 관측 경계+재집계 로그 / DB 원자 이력+내구 commit 경계 | shadow에는 전자를 권고. 등록 성공과 무관하며 DB migration 없이 관측 가능. 이력 소실/겹친 commit은 unknown으로 남기는 한계를 수용해야 함. 후자를 고르면 별도 스키마·트랜잭션 변경 검토 필요 | **S0:** 효력 시각, 초기 baseline, 겹친 토글 순서, commit 후 기록 실패/재시작 공백 정책 합의. **S1a/S4:** 실제 이력 전달·재구성을 주입 시험으로 검증 |
| **S0-O3 완결·유예·보존** | seq/checkpoint/손실 표식+연속 보존 로그 / 단순 best-effort 로그만 | 전자를 권고. 후자만 가능하면 부재 판정은 계속 insufficient이며 not_executed 탐지가 완성됐다고 말할 수 없음. L은 관측 지연 분포를 보고 고르되 deadline을 변경하지 않음 | **S0:** 채널별 fence 의미·손실 검출/보존 경로·파라미터 결정 방법과 미측정 시 제한 합의. **S4 전:** scan/도착 유예/각 timeout/보존·메모리 상한 값과 근거 확정, 완결·replay·중복 제거 시험 통과 |

파라미터 선택의 대안은 **고정 보수 후보값**과 **측정 분포+명시 여유로 정한 값**이다. 후자를 권고한다. 예를 들어 등록 snapshot 300초·비교 tick 60초는 시험 출발 후보일 뿐, 탐지 SLA나 운영 안전값으로 승인한 수치가 아니다. 도착 유예·손실 timeout·정정 보존 일수는 이번에 측정하지 않았으므로 숫자를 발명하지 않는다. shadow ≥7일 요구와 원시 로그의 연속 보존 필요성도 함께 검증한다(`SOURCE_HEALTH_PLAN.md:146,161,375–380`). O3를 닫기 전에도 직접 증거의 기록은 가능하지만 “부재 확정” 품질은 보장하지 않는다.

#### 11.2 단계별 산출물과 gate

| 단계 | 범위 | 완료 조건 |
|---|---|---|
| **S0 계약 확정** | r3 의미 검토, O1/O2/O3 선택 및 검증 방법·한계 확정 | 열린 결정의 선택 결과와 남은 검증 gate를 문서화. 설계 승인과 운영 적용 승인을 구분 |
| **S1a 기존 신호 확장** | 제출·skip 리스너, 전환 구조/호출 구간, 설정 관측/초기 baseline, scheduler/관측 준비·손실 신호 | 실제 코드 outcome 유지. 정확한 실행 결속이 없는 채널은 unknown 표기. 로그만으로 슬롯 진입을 입증했다고 하지 않음 |
| **S1b 실행 연결** | 선택한 실행기 경로, 요청형/큐형 래퍼→queue item→retry→spawn 연결 | 3.11.2에서 §11.3 회귀·주입 시험과 독립 검토. bare 이벤트 역순·세대 교체에서도 오귀속 0을 시험으로 확인 |
| **S2/S3 정책·슬롯 생성기** | 독립 정책 선언, 구조 일치 시험, 순수 슬롯 생성/설정 timeline | S1보다 먼저 또는 병행 가능한 별 작업. 4모드·경계·휴일·빈 집합·시간 역전 시험. 실제 scheduler 등록을 기대값 입력으로 쓰지 않음 |
| **S4 비교기 통합** | 등록/슬롯 비교기, 증거 완결, 미귀속 실행, 정정/replay | 결과 우선순위·손실/재시작·deadline 역전·중복 제거·보존 만료 통과. log-only; API/UI·알림 효과 없음 |
| **S5 ≥7일 shadow** | 주간 전체 모드 사이클 + 주입 공휴일·장애 | 오탐률뿐 아니라 연결률, unknown 비율/사유, 주입 장애 탐지율, 정정률, 지연/메모리/로그량 측정. 이후 D7/D8·D4로 판단 |

S1 전체의 **“동작 불변”은 목표**다. 현재 완료 사실 또는 log-only라는 이름의 귀결이 아니다. 실행 위치와 제어 의미를 보존하는 시험 결과, 비용 측정, 실패 격리 증거가 있어야 주장할 수 있다. 이 문서는 S1 코드를 구현하거나 배포하지 않는다.

#### 11.3 S1 동작 보존을 입증할 시험

1. **실행 위치·호출 형태:** 요청형/큐형 래퍼가 여전히 인자 없는 동기 함수이고, 기존 default executor의 worker 스레드에서 실행되는지 확인. async로 바뀌지 않음, 대상 밖 async job 경로 동일, callback/cancel/shutdown 처리 동일.
2. **misfire 경계:** due 전·정확히 grace·grace 직후, 제출 후 worker 대기→misfire, grace 통과 후 실제 늦은 진입을 비교. 관측 지연을 강제로 넣어 부작용도 측정. 예외·반환·기존 crawler_stats 의미가 바뀌지 않아야 함.
3. **coalesce/instance:** raw run_times 여러 개에서 같은 선택 목록·실행 개수·이벤트가 나오는지 검증. coalesce=False의 다중 run_time도 방어 시험. 제출 묶음 카운터와 같은 ID remove/add 중 max_instances 유지, 다른 ID가 가진 별 카운터를 임의 병합하지 않음. `_run_job_success/error`를 중복 호출하지 않음.
4. **귀속·역순:** 진입→submitted, missed→submitted 순서, 동일 job_id 신·구 세대 교체, 두 process 겹침, 같은 due 복수 실행, metadata 없는 수동 호출, submit 실패/거부를 주입. 시간 창의 다른 슬롯으로 보정하지 않고 필요한 경우 unattributed/insufficient로 남겨야 함.
5. **큐 동작:** 기존 priority/timestamp/동률 순서, qsize 19/20/25 경계·QueueFull, worker 처리 중/이미 대기 중 거부, +1000 재시도와 최대 1회, task_done·worker 취소·재시작·빈 큐, IBK gate on/off의 기존 결정/통계/예외를 보존. metadata 누락이 큐 순서나 재시도 조건을 바꾸지 않아야 함.
6. **자식 시작:** dequeue만 있고 cleanup_blocked인 IBK, spawn 실패, spawn 직후 종료/취소를 분리. 성공한 spawn 지점에서만 child_started 발생. collector 진입을 추정하지 않음.
7. **설정·전환:** OFF→ON commit 후 cache/add 실패, ON→OFF remove 실패, 초기 DB 조회 실패/행 누락/전부 OFF, 빠른 연속·겹친 토글, guard 사이 revision 변경, commit 후 로그 실패, start/finish/operation 기록 실패, pause/resume·pending·종료 누락. 기대값이 등록 실패 때문에 사라지지 않아야 함.
8. **관측 실패 격리:** 각 hook·serialization·전송·버퍼 포화·fence 손실·thread-local cleanup에 오류를 주입. 기존 예외/취소를 삼키거나 새로 퍼뜨리지 않음, 손실은 표시되고 부재 판정은 insufficient로 내려가야 함. 동일 오류에서 수집 동작과 관측 품질을 별도로 검증.
9. **비용 측정:** 동일 부하에서 수집 시작 지연·executor 대기·전환 시간·CPU/메모리·로그량을 관측 on/off로 비교. 허용 한도는 사전 합의하며, 이번에는 성능 overhead/유실률/SLA를 측정하지 않았다.

기존 회귀 출발점은 `tests/test_switch_jobs_windows.py:55,312,404,433`, `tests/test_control_job_transition.py:82,197,428,529`, `tests/test_scheduler_skip_telemetry.py:20,40,69`다. 기존 시험의 존재는 새 계측 경로의 통과 증거가 아니다.

S2~S4에는 추가로 금 18:59:58→19:00, 화 02:59:18/05:59:44→06시, 토 07:00, 월 06:00·08:00, 우리 9회·IBK 2회와 월 0회, 공휴일 주입 불변, DXY 120초 중첩, ADR 두 슬롯, deadline 역전, 미확정 보존, 정정 역순·중복·ID 충돌·horizon 만료·생성 범위 역전의 결정적 fixture가 필요하다.

### 12. 이번 작성에서 실제 수행한 검증 — A9

네트워크·설치·앱 import 없이 §0.1의 3.11.2 휠을 `python3 -B`로 사용했다. 프로젝트 함수는 AST에서 `_switch_jobs_body`만 추출해 fake scheduler/manager와 호출되지 않는 crawler 대역에 연결했다. 실제 DB·큐 worker·수집기·자식 프로세스는 실행하지 않았다. 별도 시험 파일은 만들지 않았으며 결과는 다음과 같다.

| 확인 | 입력/방법 | 실제 결과와 한계 |
|---|---|---|
| 입력 정본 | 인계서·r2·기계 표 SHA-256 계산 | 지정값 일치 |
| 버전 | 보관 wheel SHA-256 및 import 위치/version 출력 | **3.11.2**, 지정 wheel 내부 import 확인. 설치된 3.11.0으로 대체되지 않음 |
| 표/코드 대조 | fake 등록 결과 vs 기계 표의 실제 Cron/OR 인스턴스. type·fields·timezone·start/end·jitter·OR 구조·grace·max·coalesce 미지정 비교 | IN 11 / BREAK1 10 / BREAK2 9 / OUT 7, 37건 일치. 모드별 전부 비활성도 0건. 모든 개별 토글 조합을 전수 시험한 것은 아님 |
| 마무리 슬롯 | 2026-09-21(월), 22(화), 26(토), KST 06:00≤t<07:00 trigger 열거 | 우리/IBK = 월 0/0, 화 9/2, 토 9/2 |
| transition 기전 | 2026-09-18 06:00:14에 BREAK1/2 우리 trigger 비교. 06:00:01 DXY BREAK1 비교 | 우리 BREAK1은 해당 due 불포함, BREAK2는 포함. DXY 구 trigger도 해당 due 포함 |
| coalesce | 수동 구동 BaseScheduler 대역과 capture executor, 고정 now, OUT DXY 예정 3개 | raw 3 → submitted 1, missed 0. **실행기 확장 A를 구현한 시험은 아님** |
| pause | 같은 job을 pause 후 구조와 next_run_time 비교 | trigger/grace/coalesce/max 동일, next_run_time=None |
| misfire 경계 | 3.11.2 `run_job`의 clock을 메모리에서 고정, grace=120 | 지연 121초: missed·함수 호출 0. 지연 120초: 함수 진입 |
| kwargs | 인자 없는 lambda에 scheduled_run_time kwargs 등록 | ValueError로 거부됨 |

§12 결과는 재현한 경계 사례만 증명한다. 전체 pytest, S1 adapter 동등성, 실제 운영 성능, 관측 유실/보존, 7일 shadow는 **미수행**이다. 3.11.0의 기존 제출 순서 빈도는 §0.1처럼 버전을 표시했다. 3.11.2의 위 일부 경계 재현으로 전체 일주일·DST/모든 환경의 등가성을 주장하지 않는다.

### 13. 인계 합격 기준 대응

| 기준 | 대응 위치 |
|---|---|
| A1 r2 판정 6건 연결 | §1; 남은 선택지는 §11.1 |
| A2 실행별 예정 시각 결속·선택지·동작 변경·3.11.2 | §0.1, §4.1~4.4, §11.3, §12 |
| A3 정책 효력/실제 적용·초기/연속/종료 누락 | §3.1~3.3, §10 |
| A4 슬롯 생성 가능 등록 상태·at·ADR 두 슬롯 | §5.1~5.3 |
| A5 completed/raised·next_run_time/pending/state·미귀속 | §6.1~6.3 |
| A6 증거 우선순위/충돌·관측 정상·발생/수신·늦은 실행 | §7.1~7.3, §8.1~8.2 |
| A7 deadline 역전·미확정·ID/revision/중복/보존 | §9.1~9.3 |
| A8 S0/S1a/S1b/S2·S3/S4·S5·동작 보존 시험 | §11.1~11.3 |
| A9 file:line·실측/추정 구분 | §0의 표기 규칙, 각 절 근거, §12 검증 범위 |

검토자에게 남기는 S0 선택은 **O1 실행기 확장, O2 설정 효력/이력, O3 관측 완결/파라미터/보존** 세 묶음이다. 이 열린 항목을 승인 완료라고 대신 선언하지 않는다. 본 계약의 의미를 확정한 뒤 각 단계 gate에 필요한 증거로 진행 여부를 판단한다.


---

## 부록 A — Tier 1(부분 관측) 범위 (설계 폴더의 부록 A4, Codex 작성)

A3 대비 변경 목록:

- **§2-5:** seq 발급·deque 추가·용량 퇴출 계수를 같은 짧은 임계구역으로 직렬화하고, 배출도 같은 잠금으로 보호한다. 로깅은 잠금 밖이다. 수신 seq 틈을 유실로 확정하는 규칙을 철회하고 `pre_call_dropped`의 계수 범위를 정의했다.
- **§2-7·§3(c):** 관측 필터를 실행기 로거의 **첫 필터(또는 유일한 필터)**로 기동 때 설치·검증하고, 래퍼 진입 때 객체 동일성·순서를 재검증한다. 위반 시 슬롯은 unknown이다.
- **§3:** `Filterer.filter`의 `if not result: return False`까지 포함해 조건부 결속 논증을 다시 썼다. `pre_call` 뒤 `run_entered` 없음으로 오귀속을 감시한다는 문구를 철회했다. 직접 관측 가능한 설정 위반 신호와 탐지하지 못하는 잔여 조건을 구별했다.
- **§2.1·§3.1·§3.2:** 두 결함의 대조 재현, 실제 수신, 기존 반례 10행의 A4 회귀 결과를 추가했다. **§1·§4·§5·§6·§7의 의미는 유지**하며, §4 ack 위치와 §7 (a)·수정 선행 순서를 되돌리지 않는다.

⛔ **Tier 1은 r3 S4 완료가 아니다.** 산출물은 **원시 사건 + 현재 관측 요약**뿐이다. 슬롯의 `not_executed`/`submitted_not_entered` 확정 판정을 내지 않으며 부재는 `insufficient_evidence`다. 확정 부재 판정에는 Tier 2의 r3 §7.2 완결 증명·§9.3 ledger가 필요하다. 아래는 설계 계약이며 구현 완료·배포 승인 주장이 아니다.

기준·인용 표기:

- 기준 A3 SHA-256: `ebc50cf9068a336fdcfb32f79cdaaa12a31827e4f4aeb2bdbc268544699a320e`.
- 기준 r3 SHA-256: `f6f553b384d33a8a5ab12b0dd10111fd9ba61b327b5a93bb97bc1d31d658d75a`.
- 요청서 SHA-256: `747793ad0eebefe1c0f070ad54e467f4c77f00604a60cda8f85c04205d32d472`.
- 판정 원문은 같은 폴더의 `r3_claude_review.verdict.txt`(`01a0f5a6…`), `collection_expected_r3_addendum_a1.verdict.txt`(`dd68b724…`), `collection_expected_r3_addendum_a2.verdict.txt`(`cb6ddd92…`), `collection_expected_r3_addendum_a3.verdict.txt`(`af5c44b7…`)이며 지정 해시와 일치했다. A3의 통과 항목은 A3 판정:51–84, 남은 두 결함은 :88–133이다.
- 리포 경로 기준은 `/Users/jay/Downloads/Projects/FXi/exchange-rate/`이며 `scheduler.py`·`crud.py`는 각각 `app/scheduler.py`·`app/crud.py`의 약칭이다. 요청서의 리포 기준은 master `77f8dd4`다. 이번에는 git을 사용하지 않았으므로 현재 HEAD 일치 여부는 **확인 필요**이며, 코드 인용은 이번에 직접 읽은 작업 파일 기준이다.
- `APS312/`는 지정된 `/private/tmp/claude-501/-Users-jay-Downloads-Projects-FXi-exchange-rate/3b41f41d-dace-450a-a034-23934b6e8485/scratchpad/aps/apscheduler-3.11.2-py3-none-any.whl` 내부 `apscheduler/`다. 휠 SHA-256 `ce005177f741409db4e4dd40a7431b76feb856b9dd69d57e0da49d6715bfd26d`를 확인했다. 설치·압축 해제 없이 직접 import했다.
- `PYLOG`는 이번 로컬 Python 3.13.5(Anaconda)의 `/Users/jay/miniconda3/lib/python3.13/logging/__init__.py`다. 운영 Python의 동일 동작은 **확인 필요**다. Dockerfile:4,21의 `python:3.13-slim`만으로 로컬 구현과의 동일성을 단정하지 않는다.

### 1. 사건 네 개와 도착점

| 사건 | 발생 위치(스레드) | 시각 | 뜻 |
|---|---|---|---|
| `executor_pre_call` | `apscheduler.executors.default`의 Running INFO를 필터가 가로챔(`APS312/executors/base.py:129`, 동기 작업은 worker) | `record.created` | grace 통과 뒤 **호출 직전**. 실제 진입 증거가 아니다. 로그 호출 뒤 실패나 지연이 있을 수 있다 |
| `run_entered` | 요청형 래퍼 첫 줄(`scheduler.py:320`) | 래퍼가 직접 잰 시각 | **요청형의 도착점** |
| `queue_accepted` / `queue_rejected(reason)` | `enqueue_selenium_job`(`scheduler.py:612`, 호출 래퍼 :738) | 수락 = put 성공 직후(:674). 거부 = 각 결정 지점: `worker_processing`(:632), `already_in_queue`(:640), `pressure`(:654)는 put 이전, `queue_full`은 :678의 QueueFull | **큐형의 도착점 = 최초 `queue_accepted`**. 재시도 적재는 같은 슬롯의 재시도 |
| `missed` / `max_instances` | 기존 리스너(`scheduler.py:48–86`, job ID·예정 시각 이미 기록) | 리스너 호출 시각 | 직접 skip 사실. 세대 귀속은 §3 규칙 |

`executor_pre_call`로 도착 타이머를 초기화하지 않는다. 큐형 wrapper 진입도 큐 수락을 대신하지 않는다. `EVENT_JOB_SUBMITTED`는 Tier 1에서 구독하지 않는다. r3 A의 완전한 제출 연결은 후속 선택지다.

### 2. 필터 계약 (E)

1. 실행기 로거를 INFO로 설정하되 **관측 필터의 제한 대상은 INFO**다. WARNING 이상은 비대상 작업까지 통과시켜 기존 misfire·실패 기록을 보존한다. 현행 실행기 로거는 WARNING이다(`app/logging.py:110–114`). 비대상 INFO(broadcast·mirror·executed successfully 포함)는 버린다.
2. Running 식별은 **메시지 템플릿 `Running job "%s" (scheduled at %s)` 동일 + `levelno == INFO` + args 형태 `(Job, datetime)`**를 모두 검사한다. missed도 인자가 둘이나 두 번째는 timedelta이므로 args 개수만으로 식별하지 않는다(`APS312/executors/base.py:126,129`). 템플릿·형태를 3.11.2 회귀 시험으로 잠근다.
3. 필터 안에서는 `job.id`, due ISO, `job.func`의 불변 job ID·등록 세대, `record.created`, thread ID 등 스칼라만 복사한다. Job 객체를 후행 배출기로 넘기지 않는다.
4. 필터는 첫 동작으로 TLS 문맥을 지우고, 본문 전체를 예외 격리한다. 실패 시 TLS를 다시 비우고 실패 계수를 남긴 뒤 레코드를 버린다. 정상적인 관측 오류를 runner에 전파하지 않는다. 필터 예외가 새면 `logger.info`가 `job.func` 호출 앞에서 실패할 수 있다(`PYLOG:865–871`, `APS312/executors/base.py:129–132`). 프로세스 강제 종료 등까지 예외 격리가 보장된다는 뜻은 아니다.
5. **필터 안에서 로깅하지 않는다.** 로컬 구현의 `Logger.handle`은 공통 TLS의 `in_progress`를 설정하고, `isEnabledFor`는 재진입 중 비활성으로 처리한다(`PYLOG:1676–1688,1772–1788,1826–1829`). 다른 로거로 바꿔 호출해도 필터 안 발행은 예외 없이 0건일 수 있다. 다음 경로를 사용한다.
   - 필터는 TLS clear → 식별·스칼라 복사 → 버퍼 기록 → TLS 문맥 저장(마지막) → 원본 `False` 순서다. `collections.deque(maxlen=N)`의 **N은 양의 정수**로 기동 때 검증한다.
   - 모든 생산자와 배출기가 공유하는 **동일한 짧은 잠금** 아래에서 ① 프로세스별 단조 `pre_call_seq` 발급 ② 현재 버퍼가 가득 찼는지 확인 ③ `(seq, job_id, generation, due, record.created, thread)` append ④ append 성공으로 확정된 퇴출 계수 갱신을 수행한다. seq를 먼저 발급하고 잠금을 풀었다가 append하는 것은 금지한다. 스칼라 준비는 잠금 전, TLS 저장은 성공 후다. 잠금 안에 로깅·I/O·await·사용자 콜백을 넣지 않는다.
   - **배출기**는 로깅 호출 밖에서 도는 비대상 주기 작업(예: 10초, `task_*` 아님)이다. 동시 실행이 겹치지 않는 단일 배출기가 같은 잠금으로 배치 복사·deque 비우기·계수/최종 발급 seq snapshot을 마친 뒤, **잠금을 풀고** 관측 로거 INFO로 `executor_pre_call`을 발행한다. snapshot은 로컬 버퍼 상태이며 수신 완결 fence가 아니다.
   - **`pre_call_dropped`의 정확한 뜻:** 해당 `process_instance_id`에서 append에 성공해 버퍼에 들어갔으나, 배출기가 꺼내기 전에 **후속 append가 가득 찬 deque의 맨 앞 항목을 퇴출시킨 건수의 누적값**이다. 한 퇴출당 정확히 1을 같은 잠금 안에서 더한다. `len == N`을 봤다는 이유만으로 append 성공 전에 증가시키지 않는다. 재시작은 새 process epoch이며, 계수 snapshot 재수신은 같은 손실을 다시 더하지 않는다.
   - append 전 실패로 **미적재가 확실한** 시도는 별도 `pre_call_enqueue_failed`, seq 발급 전 식별·복사 실패는 `pre_call_hook_failed`로 구별한다. seq를 이미 발급했으면 재사용하지 않는다. append·계수 갱신 중단으로 결과를 확정할 수 없으면 `buffer_accounting_unknown`을 유지하고 손실 0으로 간주하지 않는다. 이후 snapshot도 이 불확실성을 지우지 않는다.
   - **“seq 틈 → dropped”는 철회한다.** 배출 후 직렬화·핸들러·수집 경로의 손실, 재전달·역순 수신, 프로세스 종료로 잃은 항목은 이 버퍼 퇴출 계수에 포함하지 않는다. 수신 틈은 미확인 공백이며, 이 계수만으로 전체 무유실을 선언할 수 없다. 버퍼 퇴출은 이후 배출이 없어도 생산 시점의 계수에 반영되지만 계수 자체의 전달도 끊길 수 있다. Tier 1의 부재는 계속 insufficient다.
   - `run_entered`는 로깅 호출이 끝난 뒤 래퍼에서 직접 발행하고 유효한 `pre_call_seq`를 싣는다. 짝 키는 **`(process_instance_id, pre_call_seq)`**다. entered가 pre_call보다 먼저 수신될 수 있으며 수신 순서를 실행 순서로 쓰지 않는다. 두 출력 경로의 같은 사건 중복 수집도 이 키와 사건 종류로 구별한다. unknown은 의심스러운 잔존 seq를 유효 짝 키로 재사용하지 않는다.
   - 출력은 기존 루트 핸들러 경로를 사용하되 관측 발행 실패는 수집 결과와 격리한다. 현행 console/app 핸들러에도 레벨 제한이 있다(`app/logging.py:64–98`). 기동·배출 때 관측 로거와 수집 대상으로 정한 핸들러의 INFO 통과 가능 여부를 점검하고, 불가하면 `collection_observe_disabled` 상태와 WARNING을 남긴다. WARNING 자체의 수신도 보장되지 않으므로 경고가 없음을 정상 증거로 삼지 않는다. 출력 경로가 불능이면 해당 구간은 insufficient다.
   - **실제 수신 시험 필수:** 루트 포착 핸들러에 pre_call·entered가 도착했음을 단언한다. 예외가 없다는 것만으로 통과하지 않는다. 필터 안 로깅 방식은 로컬 재진입 방지 시험에서 pre_call 0건으로 실패해야 한다. 핸들러의 조용한 누락·외부 수집 중단은 별도 잔여 조건이다.
6. 비대상 INFO에도 레코드 생성·필터 비용이 발생한다. async 경로도 Running 로그를 내므로 이벤트 루프 비용이 있다(`APS312/executors/base.py:179–181`). 새 잠금의 경합·배치 크기·N·배출 주기의 운영 비용은 **미측정/확인 필요**다. 배포 전 비용과 배포 후 루프 지연을 측정한다.
7. **기동 순서·설정 불변 계약:** 작업을 실행하기 전에 실제 실행기 로거 객체에 관측 필터 객체를 **첫 위치 또는 유일한 위치**로 설치하고 `logger.filters[0] is observation_filter`를 검증한다. 단순 `addFilter()` 후 부착 여부 확인으로 끝내지 않는다. 기존 필터를 유지한다면 관측 필터 뒤에 두고 WARNING 이상 기존 경로가 유지되는지 시험한다. 실패하면 `binding_ready=false`이며 수집 자체를 중단하지 않고 귀속을 unknown으로 둔다. 실행 중에는 로거 객체·INFO 활성 상태(상위 레벨·전역 disable 포함)·disabled·필터 순서/구현을 바꾸지 않는다. 새 구성은 정지·재기동 후 새 관측 epoch에서 검증한다.

#### 2.1 발행·유실 회귀 결과 (이번 메모리 내 재현)

| 시험 | 결과 |
|---|---|
| 실제 `run_job`으로 동기 대상 두 번, 이후 실제 `run_job`으로 비대상 배출기 실행 | 루트 수신 `entered(1), entered(2), pre_call(1), pre_call(2)` — 각각 2건 |
| 실제 `run_coroutine_job`으로 코루틴 대상 두 번, 로깅 밖에서 배출 | pre_call 2건·entered 2건 수신, seq 일치 |
| A2 방식으로 필터 안에서 관측 로거 발행 | pre_call **0건**, entered 1건. 대조 조건 유지 |
| 비대상 작업의 실제 misfire WARNING | 루트에서 수신 |
| A3: A가 seq=1 발급 후 append 전에 정지, B가 seq=2 append, 배출 후 A 재개 | 수신 `[2, 1]`, N=64, 실제 유실 0. 중간 틈을 세면 거짓 손실 1 |
| A4: A가 잠금 안에서 seq=1 발급 뒤 정지, B·배출기가 같은 잠금에 대기, A 재개 | 수신 `[1, 2]`, dropped 0. 발급 뒤 미적재 상태를 배출기가 추월하지 못함 |
| N=2, 배출 전 5건 append | `[1,2,3]` 실제 퇴출, `[4,5]` 배출, dropped **3** |
| 빈 버퍼 재배출 / N=1에 두 건 / N=0 또는 -1 | 빈 배출은 계수 불변 / 퇴출 1건 / 기동 검증에서 거부 |
| N=2가 찬 상태에서 seq=3 발급 후 append 전 실패 강제 주입 | 기존 `[1,2]` 보존, dropped 0, enqueue_failed 1. 일반 append의 모든 실패 형태를 입증한 시험은 아님 |

### 3. 문맥 전달과 조건부 결속

- 등록 때 새 래퍼 객체에 불변 `_fxi_job_id`·`_fxi_generation`을 `add_job` 호출 **전에** 붙인다. 이미 제출된 구 Job은 구 래퍼의 세대를 사용한다. runner는 전달받은 Job의 함수를 호출한다(`APS312/executors/base.py:106–131`; 구 세대 재현 근거: `r3_claude_review.verdict.txt:65`) — 나중의 현재 Job 조회로 바꾸지 않는다.
- 필터는 첫 동작으로 TLS를 지우고, §2의 버퍼 기록에 성공한 대상일 때만 마지막에 `(job_id, generation, due, pre_call_seq, written_mono)`를 쓴다. 필터 실패·비대상 Running도 이전 문맥을 남기지 않는다.
- 래퍼는 첫 줄에서 문맥을 **꺼내며 지우고**, 다음을 모두 만족할 때만 슬롯에 결속한다. 하나라도 어긋나면 `run_entered(slot=unknown, bind_failure=<사유>)`를 남기며, 잔존 seq는 유효 연결 키로 쓰지 않는다.
  - **(a)** 문맥이 있다.
  - **(b)** job ID·generation이 자기 불변 `_fxi_*`와 같다.
  - **(c)** 기동 검증에 성공한 같은 실행기 로거·관측 필터이며 `binding_ready=true`, 알려진 구성 위반이 없다. 래퍼 진입 시 `isEnabledFor(INFO)`가 참이고 disabled가 아니며, **필터 목록이 비어 있지 않고 `logger.filters[0] is observation_filter`**다. 단순 부착 여부 검사로 대체하지 않는다. 실패 시 이유를 `filter_order`, `filter_missing`, `logger_disabled`, `binding_disabled` 등으로 분리한다.
  - **(d)** `0 ≤ time.monotonic() − written_mono ≤ 1.0초`. 음수·초과는 unknown이며, 이 나이 상한은 보조 조건이다.

**(c)의 논증과 적용 전제:** 지정 휠의 동기 runner는 Running `logger.info`가 정상 반환한 뒤 `job.func`를 호출한다(`APS312/executors/base.py:129–131`). 표준 경로는 `Logger.info`의 INFO 활성 검사(`PYLOG:1521–1522`) → `_log`의 레코드 생성·`handle` 호출(:1665–1667) → `handle`의 disabled 검사·필터 호출(:1676–1683)이다. 그런데 **필터 연쇄는 목록 순서로 진행하며 `if not result: return False`로 즉시 종료한다**(`PYLOG:865–871`). 따라서 “관측 필터가 붙어 있다”만으로는 이번 호출에서 실행됐다고 할 수 없다.

기동부터 Running 호출·래퍼 진입 사이까지 §2-7 구성이 고정되고 표준 로깅 경로를 그대로 사용한다는 전제 아래, 관측 필터가 첫 위치이면 그 앞에서 연쇄를 끊는 필터가 없다. 그 필터가 TLS를 먼저 지우므로 래퍼가 읽는 문맥은 이번 필터가 성공적으로 쓴 값 또는 빈 값이다. 대상 INFO는 관측 필터가 `False`를 반환하므로 **뒤 필터와 원본 핸들러로 가지 않는다**(`PYLOG:870–871,1681–1686`). 이 전제에서 앞 실행의 로그 반환 후·함수 호출 전 실패로 남은 문맥은 다음 필터가 지운다. INFO 비활성·disabled·필터 제거·순서 위반 상태가 유지되면 래퍼 (c)가 실패하여 unknown이다.

**감시 문구 철회:** “`pre_call` 뒤 `run_entered` 없음 개수로 이 오귀속을 간접 감시한다”는 A3 문구를 철회한다. 잘못 재사용된 seq에도 entered가 붙을 수 있어 누락 계수가 0인 채 오귀속이 발생한다. 미짝 pre_call 수는 호출 전 실패·지연·로그 유실의 진단 값일 뿐, 오귀속 탐지나 안전성 증거가 아니다.

대신 기동·래퍼 진입·배출 주기에서 **직접 관측한** 필터 위치/객체·활성 상태의 불일치를 `binding_config_violation`으로 계수한다. 한 번 확인한 위반은 해당 epoch에서 sticky `binding_ready=false`로 유지하며, 이후 정상 모양으로 돌아와도 자동 복구하지 않는다. WARNING과 상태 snapshot은 필터 밖에서 발행한다. 이 검사는 지속되는 위반과 검사 순간의 위반을 포착하며, 두 검사 사이에 바뀌었다 복구된 상태까지 탐지한다고 주장하지 않는다.

**잔여 조건(0이 아님):**

- (c)는 현재 상태 검사다. 다른 스레드가 로거 레벨·전역 disable·disabled·필터 순서/객체를 Running 호출 때만 바꾼 뒤 래퍼 검사 전에 복구하면 이를 놓칠 수 있다. “운영 중 변경하지 않음”은 결속의 운영 전제이며 위반을 전부 감시로 증명하는 장치는 아니다. 의심/위반 구간을 확인하면 unknown으로 내린다.
- Logger 메서드·`LogRecordFactory`·필터 구현 교체, trace/signal에 의한 중간 경로 변조, 직접/재진입 래퍼 호출 등 표준 runner→로깅→래퍼 경로를 벗어난 실행은 이번 결속 증명의 대상이 아니다. 이를 허용하려면 호출별 명시적 토큰/runner 경계 등 별도 결속이 **확인 필요**하며, 지원 여부가 확인되지 않은 경로는 슬롯 unknown이다. 로깅 전체의 임의 변경에도 안전하다는 주장은 하지 않는다.
- 프로세스 종료·메모리 실패·버퍼 포화·배출 중단·핸들러/외부 수집 누락은 남는다. 필터 first 조건과 정확한 버퍼 퇴출 계수는 수신 완결성이나 운영 오귀속률 0을 증명하지 않는다. 구현 시 TLS 초기화/저장 실패·출력 실패 격리도 검증해야 한다.

A2의 “모드 전환 경계 ≥11초”, “INFO 미호출이면 문맥 나이 ≥10초” 철회를 유지한다. A2 판정:103–141의 과거 2주 열거에서는 전체 최소 due 간격 10초, 전환별 10/11/24초였으나 **due 간격 ≠ 문맥을 쓴 실제 시각의 간격**이다. 이번 §3.1에서도 약 0.2초의 잔존 문맥을 재현했다. 1초 제한은 결속 증명이 아니다. A2의 “핸들러 재진입 주입이면 둘 다 unknown”도 되살리지 않는다. 원본 INFO가 버려지면 해당 핸들러의 재진입 경로 자체가 실행되지 않는다.

코루틴 작업(§7의 async 큐 래퍼)은 Running 로그 다음 `await job.func()`로 들어간다(`APS312/executors/base.py:179–181`). 래퍼는 **첫 await 전에** 같은 루프 스레드의 TLS를 꺼내며 지워야 한다. 큐형은 꺼낸 문맥을 `enqueue_selenium_job(bank, slot_ctx)`로 넘겨 수락·거부에 싣는다. 큐 항목 튜플의 정렬 키는 바꾸지 않는다. missed·max_instances 세대는 나중의 `get_job(id)`로 채우지 않고, 유일성을 입증하지 못하면 원시 skip만 보존하고 세대 unknown이다.

#### 3.1 선행 필터 반례와 A4 처리 (이번 재현)

지정 휠의 실제 Job·`run_job`, worker 1개에서 두 due를 실행했다. 앞 due=`00:00:09`는 제어한 grace 검사 시각 `00:00:18.900`에 통과시켰다. 관측 필터가 seq=1을 쓴 뒤 로그 반환 직후·래퍼 호출 전에 trace로 실패를 강제 주입했다. 약 0.2초 뒤 같은 worker에서 due=`00:00:19`를 실행하며 grace 검사 시각은 `00:00:19.100`으로 두었다. 선행 필터는 두 번째 Running에만 `False`를 반환한다. 각 실행 안에서 구성은 바꾸지 않았다.

| 구성·검사 | 관측 필터 호출 | 두 번째 실행 결과 |
|---|---:|---|
| `[선행 필터, 관측 필터]`, A3의 부착 여부 검사 | 1회 | **이전 due 00:00:09·seq=1에 오귀속**, 문맥 나이 약 0.201초 |
| 같은 잘못된 순서, A4의 기동 검증 + (c) | 1회 | 기동 `binding_ready=false`, 진입 `unknown/filter_order`, 나이 약 0.210초여도 거부 |
| `[관측 필터, 후행 필터]`, A4 | 2회 | 기동 검증 통과, 올바른 due **00:00:19·seq=2** |
| 정상 기동 후 순서 위반을 주입하고 유지, 잔존 문맥 있음 | 해당 Running에서 0회 | 래퍼 재검증으로 `unknown/filter_order` |

잘못된 기동 구성을 시험에서 계속 실행한 것은 **귀속이 fail-closed인지** 확인하기 위해서다. 실제 계약은 그 상태를 관측 준비 완료로 선언하지 않는다. 첫 필터 대조군에서도 모든 임의 장애에 대한 보장을 주장하지 않는다.

#### 3.2 기존 반례 10행 유지 (이번 A4 재실행)

| 조건 | A4 결과 |
|---|---|
| 1. 같은 worker에서 정상 두 번 | 각각 올바른 due |
| 2. 앞 실행 원본 핸들러 실패 주입 → 다음 필터 정상 | 원본 Running 핸들러 호출 0회, 두 실행 정상 |
| 3. 위 조건에서 다음 필터 실패 | 다음 실행 unknown |
| 4. 사이에 비대상 작업 삽입 | 다음 필터 실패 실행 unknown |
| 5. 잔존 문맥과 job ID 또는 generation 불일치 | 각각 unknown; 새 필터가 덮어쓰지 않는 소비 검사도 별도로 수행 |
| 6. 다음 실행 INFO 비활성 | unknown |
| 7. 원본 핸들러에서 재진입 주입 | Running 핸들러 호출 0회, 재진입 발생하지 않음 |
| 8. 로그 반환 직후 실패 → 다음 필터 정상 | 새 due로 정상 결속 |
| 9. 로그 반환 직후 실패 → 다음 필터 실패 | unknown |
| 10. 로그 반환 직후 실패 → INFO 비활성·disabled·필터 제거 상태 유지 | 세 조건 모두 unknown |

재현 범위·회수: 애플리케이션 전체를 import하지 않고 문서 규칙을 메모리에서 구현했다. 로깅·10행·필터 순서 시험은 실제 3.11.2 runner를 사용했고, seq 경쟁 시험은 동일 버퍼 규칙을 독립 모형으로 강제 교차 실행했다. 실행 소스는 별도 파일로 저장하지 않았다. 자식마다 **15초 alarm + 부모 20초 timeout**, 대기 barrier·future는 2초 한도이며 timeout 시 프로세스 그룹 종료 후 `communicate`로 회수하도록 했다. 두 자식 PID **52536, 53574** 모두 **exit 0·timeout 없음·회수 완료·PID 소멸**, 각 자식의 남은 worker 스레드 **0개**를 확인했다. 운영 성능·발생률, 실제 서비스 수집 경로는 **미측정/확인 필요**다.

### 4. 최소 설정 계약 (Tier 1에도 필요) — S0-O2 확정 제안 유지

- `crawler_config.updated_at`은 commit 전 시각(`crud.py:1766→1768`)이고 캐시 미스는 True 폴백(`scheduler.py:249`)이다. 둘 다 확정 설정 이력으로 쓰지 않는다.
- **효력 경계 = r3 §3.1의 `commit_ack_observed_at`**. `db.commit()`이 **반환한 바로 다음 줄**에서 잡는다(`crud.py:1768`). CRUD 함수 반환은 뒤의 로그(:1770–1776)를 포함해 다른 경계다. `due < effective_at`은 구값, `due ≥ effective_at`은 신값이다.
- 캐시 적용·remove/add·전환 완료는 적용 증거일 뿐 효력 경계를 미루지 않는다. ack 관측을 잃으면 해당 crawler 설정 unknown이며 구값 유지로 대체하지 않는다.
- **known:** r3 §3.2의 기동 `config_baseline` 유효 시작 이후와 효력 순서가 입증된 revision 사이. 변경 관측기를 먼저 준비하고 읽기 전후 변경 세대를 비교하며 안정 시점을 입증하지 못하면 unknown으로 시작한다.
- **unknown:** baseline 이전, 이력 공백, 순서를 입증하지 못한 겹친 commit 호출(`config_order_ambiguous`, 다음 일관 baseline까지), commit 결과 자체가 불명확한 토글. unknown 슬롯은 확정 기대 개수·stall 누적에서 제외하고 `insufficient_evidence`로 둔다. 토글 제어 흐름·락 변경은 이 계약 범위가 아니다.

### 5. `stall_suspected` (진단 전용)

- 뜻은 “도착 증거가 오래 관측되지 않았다”이며 미실행 확정이 아니다. 관측 유실도 원인일 수 있음을 표시한다. `dispatch_outcome`을 덮거나 degraded/dead를 만들지 않는다.
- 작업(job ID)별로 **정책·설정이 확인되고 deadline이 지난 기대 슬롯**이 마지막 도착점 이후 N개 이상일 때 신호를 낸다. 요청형은 `run_entered`, 큐형은 최초 `queue_accepted`다. OFF·기대 0 구간을 누적하지 않으며 늦게 받은 옛 사건을 “방금”으로 쓰지 않고 발생 시각을 쓴다. 슬롯 unknown인 진입으로 확정 슬롯의 도착을 만들어내지 않는다.
- 최초 도착 이전 기준은 **`max(스케줄러 준비 신호, 설정 known 시작)`**이다. 준비 신호가 없거나 설정 unknown이면 기준 없음·신호 없음이다. 처음부터 실행되지 않은 작업도 이 기준 이후 관측한다. job별 귀속 후 crawler로 요약하며 IBK 두 작업을 이름만으로 합치지 않는다.
- N 초안은 요청형 3·큐형 3이며 각 작업 grace가 지난 슬롯을 센다. ≥7일 shadow로 조정한다.

### 6. 순서

S2 정책 표 + S3 순수 슬롯 생성기 + §4 최소 설정 관측 → §2·§3(E·문맥 전달) → Tier 1 원시 사건·현재 요약 + §5 stall 관측 → ≥7일 shadow → 필요 시 Tier 2. 큐형 수락 계측은 **§7의 큐 수정·회귀 검증이 선행**한다.

### 7. 큐 적재의 스레드 경계 위반 — 정확성 결함, 운영 긴급성 미측정

- 현행 큐형 래퍼는 동기 함수(`scheduler.py:738–739`)다. AsyncIOExecutor는 동기 함수를 기본 실행기로 보내고 native coroutine은 루프 task로 실행한다(`APS312/executors/asyncio.py:41–49`). 동기 래퍼에서 큐 내부 읽기(`scheduler.py:604,640`)와 `PriorityQueue.put_nowait`(:674)를 수행하는 경계 문제가 있다.
- **기존 재현 근거 유지:** A1 판정:113–136, A2 판정:155–170의 Python 3.13.5·기본 루프/uvloop 0.21.0 시험이다. 일반 APS 경로에서는 완료 통지가 깨우기 누락을 가렸지만 완료가 지연되면 소비도 지연됐다. debug에서는 put 뒤 `RuntimeError: Non-thread-safe operation`·APS `EVENT_JOB_ERROR`, qsize=1인데 소비자가 계속 대기했다. 강제 교차 실행에서 `task_done() called too many times`와 unfinished 불일치도 나타났다. 이는 비원자성의 강제 재현이며 자연 발생 lost-update나 운영 발생률의 입증이 아니다. 이번 A4에서는 이 큐 시험을 다시 수행하지 않았다.
- 운영 영향은 미측정이다. 기존 판정의 uvloop 0.21.0과 리포 lock의 0.22.1(`requirements.lock.txt:90`)을 동일 환경으로 취급하지 않는다.
- **수정 방향 (a) 유지: 큐 래퍼를 `async def`로 바꾸어 중복·압력 검사부터 실제 put까지 모두 큐 소유 루프에서 수행한다.** 래퍼는 첫 await 전에 문맥을 소비한다. A2 판정의 주입 시험에서 (a)는 put 예외가 APS `EVENT_JOB_ERROR`로 전달됐다. 단순 `call_soon_threadsafe`인 (b)는 APS `EVENT_JOB_EXECUTED`와 별도 루프 콜백 오류로 분리됐다. 콜백 등록 성공은 큐 수락이 아니다.
- `queue_accepted`는 **큐 소유 루프의 실제 put 성공 직후** 기록한다. 비차단 put이어도 동기 로깅 비용은 남으므로 루프 지연 측정을 유지한다. **순서: 큐 수정 + 회귀 검증 → 실제 put 직후 수락 계측 → shadow.** 이 부록은 리포 코드를 수정하지 않는다.
