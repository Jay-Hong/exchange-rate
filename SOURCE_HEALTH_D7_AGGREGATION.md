# D7 집계 결정 — 결과 보고 shadow 집계 계약 (SOURCE_HEALTH_PLAN §7.4)

**상태: 설계 합의(2026-09-22, Claude·Codex). 구현·배포 승인이 아니다.** 초안 r1~r3(Claude) 은 각각 REVISE, r4 는 작성 주체를 Codex 로 바꿔 Claude 가 합격 기준 A1~A6·인용 표본 대조로 검증했고 Codex APPROVE_DESIGN(r4 그대로). 이 문서는 r4 본문(설계 폴더 `d7_draft_r4.md`, sha256 `b808d36229c40487fa392f5c3da83592dc8cff5beea7ac67163c0576a5e6a2c5`)을 리포로 옮기며 머리말·§0 인용 문장·§8 후속 표·§9 자기 대조만 고쳤다.

코드 근거는 master `77f8dd4` 의 로컬 파일을 직접 읽은 위치다. 운영 상태·실제 메모리 사용량은 확인 필요다.

## 0. 범위와 코드 사실

**D7-1 구 통계 동결.** 구 필드·계산·화면 색은 유지한다. 문서상 의미는 “루틴 정상 반환 비율(구)”이며, 독립 호출 수나 검증된 수집 성공률로 사용하지 않는다. §7.4의 쿨다운 규칙은 새 집계에 적용하고 구 통계에는 이행 예외가 남는다. 새 산출물은 **log-only shadow 집계**다. API·화면 형태, 경보 발송, 재시도·폴백·크롤러 동작은 이 결정에서 변경하지 않는다.

| 확인한 사실 | 직접 확인한 근거 |
|---|---|
| 요청형은 `job_func()` 반환 후 success 기록·debug 로그를 같은 try 안에서 처리한다. 로거 예외가 뒤따르면 failure도 기록할 수 있으므로 success+fail은 호출 수가 아니다. 일반 큐 경로의 success는 자식 exit 0이다. | `app/scheduler.py:320-330`, `app/scheduler.py:374-407` |
| 구 success는 마지막 성공시각을 갱신하고 연속 실패를 0으로 만든다. 구 비율의 분모 0은 0.0이다. | `app/admin/crawler_stats.py:39-45`, `app/admin/crawler_stats.py:70-79` |
| IBK typed 경로는 자체 counts를 기록하며 구 crawler_stats와 중복 집계하지 않는다. | `app/ibk_parent_runner.py:1-7`, `app/ibk_parent_runner.py:119-129` |
| 보고 객체 초기화 실패는 `None`이며 보고 round_id를 얻지 못할 수 있다. 현재 발행의 종착점은 JSON 로깅이다. | `app/crawlers/investing_report.py:46-53`, `app/crawlers/investing_report.py:248-288`, `app/crawlers/bank_report.py:124-131`, `app/crawlers/bank_report.py:680-706` |
| Investing 쓰기는 writer 제출 통화만 `unknown/per_currency_write_unverified`, 미제출은 `not_attempted/not_submitted_to_writer`다. writer 시작 계측 유실이면 미제출처럼 보이는 통화도 `unknown/telemetry_error`다. 쿨다운은 수집·쓰기 모두 미시도다. | `app/crawlers/investing_report.py:179-184`, `app/crawlers/investing_report.py:201-213` |
| bs·citi는 쓰기의 `performed`, `no_change_needed`, `policy_blocked`, `not_attempted`, `unknown`을 구분한다. 수집은 후보 관측이 있어도 V2 근거 미확정이면 unknown이다. 두 보고의 최종 DB는 `not_checked`다. | `app/crawlers/bank_report.py:175-219`, `app/crawlers/bank_report.py:593-610`, `app/crawlers/bank_report.py:631-660`, `app/crawlers/investing_report.py:261-262` |
| 이번 첫 연결 대상 investing·bs·citi는 요청형이며 각 job ID의 `max_instances=1`이다. 보고 종료 호출은 크롤러 반환 전에 있다. 요청형 래퍼에는 회차 전체를 15분에 종료하는 경계가 없다. | `app/scheduler.py:320-330`, `app/scheduler.py:777-859`, `app/scheduler.py:927-1017`, `app/scheduler.py:1074-1139`, `app/scheduler.py:1239-1247`, `app/scheduler.py:1288-1295`; `app/crawlers/investing.py:234-238`, `app/crawlers/bs.py:80-88`, `app/crawlers/citi.py:85-93` |
| 관리자 API는 구 collector 결과를 그대로 반환하고, 화면은 최상위 키가 0개일 때 빈 상태 안내를 낸다. 새 최상위 키를 더하는 것만으로 호환을 보장할 수 없다. | `app/main.py:1559-1582`, `templates/admin.html:1216-1243` |

R1 표본(bs·citi `bank_round_finished`, 2026-09-21 21:18Z~09-22 12:16Z)의 1,797회차·수집 `unknown/v2_evidence_unconfirmed` 5,391건·쓰기 performed 2,033건/no_change_needed 3,358건은 Claude·Codex 가 보존 원문에서 각각 재집계해 일치한 값이다(`(source, round_id)` 중복 0). 수집 unknown을 이유로 확보된 쓰기 증거를 낮추지 않는다는 판단을 유지한다.

## 1. D7-2 호출 등록과 동일 cohort 보존식 — r3 결함 1

### 1.1 독립 입력과 연결

Tier 1에서 단일 in-process 집계기에 다음 연결부를 추가하도록 결정한다. 아래는 신규 설계이며 현재 구현 사실이 아니다.

1. **래퍼 진입 첫 관측 지점**에서, 보고 객체 생성·로깅·크롤러 호출보다 먼저 `(process_epoch, invocation_seq)`를 독립 발급한다. 소스·job ID·`invoked_at`(UTC)·단조시계 시작값·예상 계약·보고 지원 여부를 등록한다. `invocation_seq`는 epoch 안에서 증가하며 재사용하지 않는다. 로그 성공 여부와 무관하게 직접 등록한다.
2. 보고 객체가 생성되면 `round_started(invocation_id, source, round_id, validity_contract, report_schema, started_at)`를 연결한다. 초기화 실패는 **같은 invocation_id**로 `report_init_failed`를 전달한다. round_id를 가짜로 만들지 않는다. 호출 context는 요청형 래퍼 안에서 설정·해제하며 크롤러 인자·반환값을 바꾸지 않는다.
3. 보고 종료는 로깅과 별개로 정규화 스냅샷을 전달한다. 래퍼 바깥쪽 `finally`에서 `wrapper_exited(invocation_id, exited_at)`를 전달한다. 관측 훅 실패는 기존 예외 처리 바깥에서 격리해 구 success/failure·원래 예외·반환·재시도를 바꾸지 않는다. `BaseException`으로 빠져나갈 때도 관측 때문에 삼키거나 다른 예외로 교체하지 않는다.
4. 등록·연결·중복 검사·기여 반영·상태 이동·출력 스냅샷은 같은 잠금 경계에서 원자적으로 처리한다. 같은 호출의 시작 중복은 같은 연결이면 무효 연산이다. 한 호출에 두 round_id, 한 round_id에 두 호출 연결은 식별 충돌로 격리한다. `unsupported` 소스와 `reporting` 소스의 보고 유실은 별개다.

**등록 유실을 숨기지 않는다.** 등록 훅 자체 실패는 고정 크기 오류 카운터 `registration_errors`와 `coverage_complete=false`에 남긴다. 식별되지 않은 finish는 호출을 역으로 생성하지 않고 `orphan_finish`로 제외한다. 집계기와 오류 계수까지 함께 실패하거나 프로세스가 죽는 경우의 완전 관측은 보장하지 않는다. 따라서 아래 등호는 **등록에 성공한 호출 집합**에 대한 불변식이며 스케줄 예정 횟수와의 등호가 아니다. 미실행·dispatch 감지는 `SOURCE_HEALTH_PLAN.md:238-246`의 별도 범위다.

### 1.2 cohort와 두 보존식

`C = {등록된 reporting 호출 | source=s, epoch=e, a ≤ invoked_at < b}`를 고정하고, 조회시각 `as_of`에서 같은 C의 상태만 센다. 종료가 b를 넘더라도 시작 cohort를 옮기지 않는다. 등록 때 예상 계약을 고정하므로 초기화 실패도 소스·계약 경계에 귀속할 수 있다.

연결 분류는 정확히 하나다: `started`(시작 연결 성공), `init_failed`(초기화 실패 확인), `unbound`(둘 다 확인 못함). `started`는 종료 여부와 별개이며 한 번 연결된 호출은 후속 충돌에도 started에 남는다. init_failed 뒤 모순된 시작이 오면 초기 분류는 보존하고 식별 충돌로 격리한다.

```text
registered_invocations(C) = started(C) + init_failed(C) + unbound(C)
registered_invocations(C) = awaiting_report(C) + in_flight(C) + overdue(C)
                         + report_unavailable(C) + finalized(C)
                         + conflicting(C) + contract_mixed(C)
```

첫 식은 연결 분류, 둘째 식은 **현재 상태의 상호 배타적 분할**이다. 진단 누적 `ever_overdue`, `ever_unavailable`, `late_finish_accepted`, 계측 오류 등은 두 식에 더하지 않는다. `init_failed` 호출의 현재 상태는 `report_unavailable`이다. 초기화 실패를 별도 현재 상태로 중복 가산하지 않는다. 지원 없는 소스는 두 식에 포함하지 않고 `unsupported_invocations`로 구분한다.

구 통계 증분은 `legacy_success_delta/legacy_fail_delta`로 별도 출력할 수 있지만, cohort 구성·누락 수 계산·허용오차의 근거로 쓰지 않는다. reset이나 종료시각 차이까지 있어 직접 등가 비교할 수 없다.

**판정 예 1.** 정상 호출 A 한 번 뒤 구 debug 로거만 실패하면 구 값은 success=1/fail=1이어도 C에서는 `1 = started 1 + init_failed 0 + unbound 0 = finalized 1`이다. 이번 실제 래퍼 AST 격리 시험에서 구 값의 이중 증가를 확인했다(`app/scheduler.py:320-330`). A가 시작·초기화 기록 없이 반환하고 B가 실행 중이면, C={A,B}에서 `2 = started 1 + unbound 1 = unavailable 1 + in_flight 1`이다. B가 A의 공백을 상쇄하지 않는다.

## 2. 현재 상태와 늦은 종료 — r3 결함 2

`awaiting_report`는 등록 후 시작 연결 대기, `in_flight`는 시작 연결 후 종료 대기다. **15분은 잠정 overdue 진단 임계이며 실행 timeout이 아니다.** 단조시계로 경과를 계산한다. 시간 초과만으로 수집 missing·실행 timeout·반환 완료를 만들지 않는다.

| 입력/조건 | 현재 상태 전이 | 유지할 진단·집계 규칙 |
|---|---|---|
| 시작 연결 | awaiting_report → in_flight | 연결 분류 unbound → started |
| 초기화 실패 | awaiting_report → report_unavailable | 연결 분류 init_failed, 전 등록 통화 불확실성 |
| 시작 후 또는 시작 대기 중 15분 경과 | in_flight/awaiting_report → overdue | 이전 연결 분류 유지, `ever_overdue` 회차당 한 번 |
| 종료 없이 wrapper_exited 확인 | awaiting_report/in_flight/overdue → report_unavailable | 실제 실행 결과를 보고 없이는 추정하지 않음; `ever_unavailable` 보존 |
| 동일 직렬 job의 다음 래퍼 진입 | 앞 호출의 종료 미확인 상태 → report_unavailable | 앞 래퍼가 끝났다는 보조 증거. **동일 epoch·job ID·직렬 실행 전제**에만 사용. 큐·병렬·수동 실행에는 일반화 금지 |
| 시작 연결이 있는 호출의 정상 종료 스냅샷 수락 | in_flight/overdue/report_unavailable → finalized | 이전 현재 상태를 제거하고 한 번만 반영. 늦은 종료면 `late_finish_accepted` 보존 |
| 종료 내용 상충 | 해당 호출 → conflicting | 열린 기여 취소, 파생 상태 무효화(§5). 이후 최초 내용 재전달로 복권하지 않음 |
| 시작/종료 또는 회차 내부 계약 혼합 | 해당 호출 → contract_mixed | 어느 계약 비율에도 넣지 않음. 충돌도 있으면 현재 상태는 contract_mixed가 우선이고 두 진단은 보존 |

시작 연결 자체가 늦으면 연결 분류만 unbound → started로 옮기며, 이미 overdue인 현재 상태는 유지한다. 반환 확인 뒤 unavailable인 호출도 시작만 왔다고 실행 중으로 되돌리지 않고 실제 종료를 기다린다. 종료만 오고 시작 연결이 없으면 `report_unavailable/start_unrecorded`로 격리한다. Tier 1은 시작을 추정 복원하지 않는다. 이미 `conflicting/contract_mixed`인 호출은 나중 단일 정상 payload로 finalized에 돌아가지 않는다. 단순 wrapper_exited나 시간 경과가 finalized·conflicting·contract_mixed를 덮지 않는다.

위 종료 수락은 §5의 식별·시간·계약 검증을 통과한 경우다. 늦은 **첫 종료**의 버킷이 이미 닫혔다면 생명주기는 finalized로 옮기되 `inclusion=post_close_excluded`로 기록하고 수집 수치에는 넣지 않는다(§6). 이것도 이전 unavailable/overdue 상태를 남겨 이중 계산하지 않는다.

**판정 예 2.** A가 10:00 시작해 계속 실행 중이면 10:15에는 `I=1=overdue 1`이다. 10:16 종료를 받아 열린 버킷에 반영하면 `I=1=finalized 1`, 현재 overdue=0, `ever_overdue=1`, `late_finish_accepted=1`이다. valid라면 아래 불확실성 규칙에 따라 잠정 공백을 해소할 수 있다. 10:15에 반환 증거로 unavailable이 됐던 경우도 열린 버킷의 늦은 종료를 받으면 unavailable=0으로 옮기되 `ever_unavailable=1`은 남긴다.

## 3. D7-3·4·5·8·9 지표 의미와 입력 검증

| 결정 | 유지하는 규칙 |
|---|---|
| D7-3 단위 | 수집·쓰기·최종 DB는 회차×통화, 실행·partial은 회차당 한 번, 폴백은 회차×경로. 내부 URL 재시도·폴백은 같은 회차다. |
| D7-3 큐 재시도 | 기본 수집률은 **1차 회차만**. 큐 재시도는 별 비율. `recovered_by_retry`는 같은 계약의 원회차 해당 통화 missing + 명시적으로 연결된 재시도 valid일 때 원회차×통화당 한 번이다. unknown을 실패로 바꾸거나 시간 인접으로 연결하지 않는다. 현재 큐의 `is_retry`만으로는 연결할 수 없다(`app/scheduler.py:539-541`, `app/scheduler.py:563-571`). `root_round_id/retry_of` 도입 전에는 재시도 별 버킷만 가능하고 회복값은 확인 불가다. 현 세 소스의 내부 재시도를 큐 재시도로 오해하지 않는다. |
| D7-4 조건부 수집률 | `collection_rate=V/(V+M)`, 분모 0이면 null. `determinable_ratio=(V+M)/(V+M+U)`, 분모 0이면 null. 모든 unknown 사유를 U에 넣고 사유 분포를 따로 보존한다. 잠정 0.9 미만이면 `insufficient_evidence`; 이 이상도 완전 성공의 보증은 아니다. V·M·U·미시도 수·보고 완결성·무결성을 항상 함께 출력한다. |
| D7-4 제외와 부분 | 선택된 통화별 수집 상태를 그대로 쓴다. not_attempted는 수집 비율 분모 밖에 사유별로 보존한다. partial은 일부 통화만 valid인 회차 수이며 통화별 분모를 대체하지 않는다. 공식 무고시·개장 전 제외는 보고에 명시된 판정 근거가 있어야 한다. preserved는 다섯 번째 수집 상태가 아니다. |
| D7-5 쓰기·DB | §0의 Investing 제출/미제출/계측 유실 구분, bs·citi 쓰기 분포를 그대로 보존한다. 쓰기 비율·최종 DB 비율은 보류한다. performed를 최종 DB 일치나 하류 전달 성공으로 올리지 않는다. 현 final_db=not_checked를 보존한다. |
| D7-8 운영 판단 | 일반 큐의 `should_retry = not success`와 IBK 전용 `decision.should_retry`를 그대로 둔다(`app/scheduler.py:551-557`). 집계·로그 오류는 재시도·폴백·writer 입력·수집 시간표·기존 예외 전파를 바꾸지 않는다. |
| D7-9 버킷 키 | 통화 지표 `(source, pair, validity_contract, aggregation_rule_version, process_epoch, round_kind)`, 실행 지표는 pair 없이, 폴백은 pair 대신 path. 여기에 §6의 시간 버킷 번호를 붙인다. 보고 schema와 집계 schema는 별개다. dedup 키에 계약을 넣어 같은 회차를 여러 번 세지 않는다. |

`V=9,M=0,U=1`이면 **판정된 9회 중 100%, 보고로 얻은 대상 10회 기준 90~100%**다. 보고 공백이 추가로 있으면 이 범위를 전체 래퍼 호출의 성공 범위라고 부를 수 없다. `determinable_ratio=1`이어도 보고 완결성이 부족하면 전체 소스가 정상이라는 요약은 보류한다.

폴백 생략 사유로 회차를 승격하지 않는다. 예를 들어 bs 공식 실패 뒤 MIBANK만 신뢰창 밖 생략이면 missing 선택을 유지한다(`app/crawlers/bank_report.py:606-629`; 선택 순서 `app/crawlers/investing_report.py:27-29`). D9는 이 수집 요약 순서 문제로 해소된 항목이며 쓰기 증거의 후속 과제로 되돌리지 않는다(`SOURCE_HEALTH_PLAN.md:693-695`). IBK PRESERVED를 설명할 때도 보존 사유·DB 완전만 보지 않고 쓰기 오류/DB 미확인·의도값 미설명·retained/submitted 불일치 선행 분기를 통과해야 한다(`app/ibk_result_builder.py:429-451`). IBK enum은 다른 은행에 그대로 이식하지 않는다.

### 3.1 입력·축별 값 검증 — 닫힌 report_incomplete 요구 유지

종료 스냅샷은 invocation 연결, `(source, round_id)`, 시작·종료 계약/schema, **최초 finished_at**, 회차 종류와 재시도 연결(해당 시), 통화별 수집·쓰기·DB 상태와 사유, 실행 결과, 경로별 폴백 상태, 전체 시도의 계측 오류를 가진다. DOM 원문·로그 문자열을 다시 파싱해 상태를 만들지 않고 보고 객체의 선택 요약을 정규화한다.

필수 통화는 payload와 독립인 등록표로 정한다. D7의 9은행+Investing 등록표는 `usd-krw/jpy-krw/eur-krw`, Tier 1의 보고 지원 소스는 investing·bs·citi이며 나머지는 unsupported다. 현 세 소스의 실제 통화 입력 근거는 `app/crawlers/investing.py:32-35`, `app/crawlers/bs.py:34-37`, `app/crawlers/citi.py:42`다. 다른 은행의 D7 어댑터 연결은 후속이다.

각 등록 통화의 **각 축**을 검증한다. 키 없음·null·빈 객체·status 없음·허용 enum 밖이면 해당 축만 `unknown/report_malformed`로 보충한다. 수집 enum은 `valid/missing/not_attempted/unknown`; 쓰기는 §7.2의 `performed/no_change_needed/policy_blocked/failed/not_attempted/unknown` 중 해당 어댑터가 증명하는 값만 허용한다. 현재 bs·citi 어댑터가 failed를 만들어내지는 않는다(`app/crawlers/bank_report.py:175-179`). DB의 현재 `not_checked`는 명시적 미확인값으로 보존한다. reason 누락은 `reason_unrecorded`로 표시하고 오류 계수에 남기되 이미 확인된 다른 축을 지우지 않는다.

`malformed_axis_items`와 `malformed_rounds`, **어느 시도든** 계측 오류가 있었던 `telemetry_error_rounds`를 별도로 유지한다. selected summary가 valid여도 무결성 오류를 지우지 않는다. 이 오류 계수는 수집 비율 기여와 독립이다. 식별/계약/시간 같은 envelope 오류는 임의 키나 시각을 만들지 않고 회차 격리·완결성 공백으로 남긴다.

예: EUR 수집 값이 `{}`이고 USD/JPY는 valid면 `V=2,M=0,U=1`이다. EUR 쓰기의 performed가 적법하면 그대로 유지한다. 키 누락·null·status 누락·이상 enum에도 같은 축별 규칙을 적용한다.

## 4. D7-6 파생 시각·누락 수·보고 공백 — r3 결함 5

파생값도 `(source, pair, validity_contract, aggregation_rule_version, process_epoch, round_kind)`별로 둔다. `last_valid_round_finished_at`은 **회차 종료 시각**이지 통화 관측시각이 아니다. `missing_since_last_valid`는 마지막 valid 이후의 **확인된 누락 판정 횟수**이며 공백 없는 연속 실패 횟수가 아니다. baseline이 known일 때 missing만 +1, unknown/not_attempted는 값 유지, 유효한 다음 valid는 시각 갱신·0으로 만든다. epoch·계약 변경은 `baseline=unknown`, 두 값은 null에서 시작하며 재시작을 복구로 해석하지 않는다.

**규칙: `report_init_failed`, `start_unrecorded`, `report_unavailable`, 등록/집계 유실, 용량 중단, 계약 혼합, 식별 충돌 등 확인된 보고 공백은 해당 소스의 등록 통화 전체에 `uncertain=true`를 전파한다.** 통화를 아는 개별 수집 unknown은 그 통화에 적용한다. overdue는 `pending_gap` 사유의 잠정 불확실성이다. 첫 불확실 사건시각과 발견시각, 사유를 구분해 보존한다. round_id가 없어도 invocation_id와 등록표로 전파한다. **보고 없는 회차를 수집 missing이나 가짜 통화 unknown payload로 만들어 V/M/U에 넣지 않는다.** 완결성 공백으로 별도 출력한다.

초기화 실패·unknown은 이미 확인된 마지막 valid·누락 횟수를 보존하되 불확실 표식을 켠다. 시간상 다음 valid가 오면 그 이후의 새 기준을 세울 수 있다. **그 valid보다 앞서 끝난 호출의 과거 unavailable 기록은 cohort 진단에 남아도 새 기준의 불확실성을 영구 고정하지 않는다.** 반대로 valid보다 뒤의 공백, 실행 종료가 아직 확인되지 않은 다른 호출, 지속 중인 용량 중단이 있으면 불확실 표식을 유지한다. overdue의 정상 종료 수락은 pending_gap을 취소하지만 `ever_overdue` 진단을 삭제하지 않는다. 종료가 unknown이면 해당 불확실성으로 교체하고, missing이면 누락 판정으로만 반영한다. `post_close_excluded`인 늦은 첫 종료는 현재 baseline을 정상으로 복원하는 근거로도 사용하지 않는다.

충돌·계약 혼합으로 기여의 신뢰를 잃으면 r3처럼 보수적으로 `baseline=unknown`, 시각·횟수=null로 내린다. `recovered_by_retry`도 의존한 원회차/재시도를 신뢰할 수 없으면 확인 불가로 내린다. **부분 역산으로 이전 last_valid를 복원하지 않는다.** 도착 순서가 종료 순서보다 뒤집힌 과거 종료/공백이 오면 역시 파생 baseline을 무효화하고, 무효화 발견 이후 종료한 다음 정상 valid부터 재개한다. 과거 valid 재전달로 재기준화하지 않는다. 늦은 사건이 얼마나 과거에 영향을 줬는지 정확히 복원하는 일은 후속이다.

**판정 예 5.** 09:00 valid → 09:01 report_init_failed → 09:02 missing이면 해당 소스의 세 통화 모두 `last_valid=09:00`, `missing_since_last_valid=1`, `uncertain=true`, 첫 공백 09:01이다. 초기화 실패를 missing 한 번으로 더하지 않는다. 09:03에 정상 valid가 오고 다른 미해결 공백이 없으면 새 기준 09:03/0으로 시작하되 과거 초기화 실패 진단은 남는다.

## 5. D7-7 종료 식별·충돌·확정성

논리 dedup 키는 epoch 안의 `(source, round_id)`다. invocation 연결도 함께 검사하며 contract·finished_at은 키에 넣지 않는다. 정상화 스냅샷의 의미 필드·계약/schema·최초 종료시각을 포함한 canonical digest를 저장한다. 로깅 시각·전달 횟수·집계 수신시각은 digest에서 제외한다. malformed 입력도 검증 오류 종류를 digest에 포함해 조용히 정상 입력과 동치로 만들지 않는다.

`finished_at`은 **보고 종료 연결부가 최초 최종화 때 한 번 채운 시각**으로 고정한다. 현재 JSON에 새 시각이 이미 있다고 가정하지 않는다(현 payload: `app/crawlers/investing_report.py:248-288`, `app/crawlers/bank_report.py:680-704`). 전달 재시도는 최초 envelope를 재사용한다. 단조시계 종료값도 고정해 시작 이전·미래 종료·시계 역전이 있으면 시간 무결성 오류로 격리하고 값을 추정하지 않는다. 벽시계 급변 시 창 coverage를 미확인으로 표시하고 검증 없이 창을 재사용하지 않는다.

- 최초 정상 종료: 열린 귀속 버킷에 한 번만 기여한다. “finalized”는 종료 스냅샷 수락 상태이며 실제 실행 성공을 뜻하지 않는다.
- 같은 digest 재전달: `duplicate_finish`만 증가한다. 수집·실행·partial·폴백·파생값은 변화 없다.
- 다른 digest 또는 바뀐 finished_at: `conflicting_finish`. 열린 버킷에서는 저장된 **그 회차의 모든 결과 기여**(수집·쓰기·DB·실행·partial·폴백·회복)를 제거하고 현재 상태를 conflicting으로 이동한다. 첫 payload를 승자로 유지하지 않는다. 무결성·진단은 남기며 §4의 파생 baseline을 무효화한다.
- 시작 계약과 종료 계약의 불일치 또는 회차 내부 계약 혼합: contract_mixed. 열린 기여를 전부 제외하고 양쪽 계약 어느 쪽에도 넣지 않는다. 기존 오프라인 집계기의 혼합 제외 원칙과 같다(`scripts/investing_observe_aggregate.py:319-322`).
- 종료 이후 추가 **개별 증거**: `late_evidence` 진단만. 기존 스냅샷을 보강해 비율에 넣지 않는다. 이 증거를 반영한 새로운 종료 payload가 전달되면 상충 종료 규칙을 적용한다.

**닫힌 버킷에는 수정하지 않는다.** 닫힌 뒤의 동일 재전달은 `post_close_duplicate`, 상충 종료는 `post_close_conflict`, 이전 종료가 없던 늦은 첫 종료는 `post_close_finish`로 구분한다. post_close_duplicate는 duplicate_finish의 하위 진단이므로 둘을 회차 수처럼 합산하지 않는다. 충돌이면 현재 생명주기는 conflicting으로 이동하고 현재 파생값도 무효화하되, 닫힌 누적 수치는 **닫힐 때의 판정**으로 남긴다. 출력에 `frozen_at_close`, `post_close_*`, `cumulative_evidence_uncertain=true`를 붙인다. 현재 상태 보존식과 과거 닫힘 당시의 수치가 다른 시점이라는 사실을 명시한다. 불변 수치가 무조건 진실이라는 보장은 하지 않는다.

파생 상태 반례 유지: `A valid → B missing → C valid → D missing → C 충돌`이 모두 열린 같은 창이면 비율 기여는 `V=1,M=2`, 현재 last_valid·누락 수는 baseline unknown이다. `last_valid=C`, 횟수 1을 남기지 않는다. 하나·우리 확장 때는 부모가 자식 프레임을 수락한 뒤 최종화한다는 경계를 유지한다(구현 예정 계약 `SOURCE_HEALTH_PLAN.md:482-503`).

## 6. 최근 창·확정 누적·상세 기록 만료 — r3 결함 3

기본 최근 창은 출력 경계 T(UTC 분 경계)의 `[T-60분,T)`이다. 실행 중인 현재 분은 다음 출력에 포함한다. 창 경계는 모두 **왼쪽 포함·오른쪽 제외**다. 최초 유효 finished_at으로 회차가 속할 **겹치지 않는 UTC 1분 버킷** `B_k=[k분,(k+1)분)`을 한 번 정한다. started cohort는 §1, 결과 시간 버킷은 이 절을 사용하며 두 분모를 혼용하지 않는다.

초기 보존값 W=60분, 추가 여유 G=10분으로 정한다. 버킷의 `end=e_k`, 논리 닫힘 시각 `close_at=e_k+W+G`다. 따라서 상세 기여 기록의 수명은 종료 후 70~71분이고, 열려 있는 최근 창의 충돌을 수정할 수 있다. **닫힘·상세 기록 만료 시각은 동일한 close_at**이다. 실제 timer 정리가 늦어도 입력마다 먼저 만료 여부를 검사해 닫힌 규칙을 적용한다. 닫힘과 동시인 입력은 `received_at ≥ close_at`이므로 post-close다. 원자적 닫힘이 실패하면 기록을 버리거나 watermark를 앞당기지 않는다.

닫을 때 버킷을 한 번만 확정 누적에 더한 뒤 `closed=true`와 연속 닫힘 watermark를 원자적으로 갱신한다. 재호출은 무효 연산이다. 회차 상세 스냅샷/기여는 이때 해제하지만 **§7의 최소 식별 기록은 해제하지 않는다.** 아직 종료가 없는 호출은 start 버킷이 오래됐다는 이유로 삭제하지 않는다. 작은 lifecycle 기록을 epoch까지 보존한다.

```text
cumulative_end(T) = epoch 시작 이후 연속해 닫힌 1분 버킷들의 마지막 end
                 (지연 없는 경우 floor_minute(T - 70분))
cumulative_counts(T) = Σ counts(B_k, at_close), e_k ≤ cumulative_end(T)
recent_counts(T) = Σ current_counts(B_k), B_k ⊂ [T-60분, T)
```

epoch 시작 분은 `aggregation_started_at` 이후만 관측한 부분 버킷으로 표시한다. 닫힌 버킷이 없으면 누적 수=0, 비율=null, cumulative_end=null이다. 빈 분도 명시적으로 닫아 watermark를 연속 진행한다. 저장은 확정 누적 counters와 watermark만으로 충분하고 과거 모든 분 버킷을 메모리에 쌓지 않는다. **이동 창 출력 여러 개를 합산하지 않는다.** 누적의 종료시각은 출력시각 T가 아니라 cumulative_end이며, 그 뒤 열린 기여와 최근 창 사이의 공백 구간도 누적에 포함됐다고 말하지 않는다.

모든 출력은 `aggregation_started_at`, process_epoch, 집계 규칙 버전, window_start/end, 해당 창의 실제 첫/마지막 수락 finished_at, as_of, warming_up, coverage 상태를 동반한다. 누적에는 cumulative_end와 닫힘 정책·post_close 진단을 추가한다. 첫/마지막 결과가 없으면 null이며, 창 길이만으로 데이터가 완전하다고 보지 않는다.

**판정 예 3.** A=`10:00 valid`, B=`10:30 missing`은 각각 `[10:00,10:01)`, `[10:30,10:31)`에 속한다. close_at은 11:11, 11:41이다. 11:41 출력의 cumulative_end=10:31이고 두 기여는 `V=1,M=1`, 50%다. `[10:00,11:00)`과 `[10:01,11:01)` 최근 창을 각각 출력했어도 누적에 더하지 않아 B가 두 번 세어지지 않는다. 정확히 10:01 종료는 첫 버킷이 아닌 다음 버킷이다.

## 7. 식별 보존과 유한 메모리 — r3 결함 4

**선택: process_epoch 동안 최소 식별 기록을 유지한다.** 상세 만료 후에도 invocation_id·source·round_id(있으면)·최초 finished_at(없으면 null)·첫 digest(없으면 null)·연결 분류·현재 lifecycle 상태·귀속 버킷·닫힘 여부·필수 진단 비트를 남긴다. 따라서 처음 종료가 없던 호출과 기존 종료의 재전달을 구분할 수 있다. epoch는 실제 프로세스 생애이며, 용량을 확보하려고 epoch 이름만 바꾸거나 ID를 먼저 버리지 않는다. 재시작 후 다른 epoch 입력은 `foreign_epoch`로 제외하고 새 호출로 재등록하지 않는다. 재시작 사이 중복 제거·누적 복구는 보장하지 않는다.

접수 순서는 **epoch → 등록된 invocation 연결 → `(source,round_id)` 색인 → 최초 시각/digest → 버킷**이다. payload의 새로운 finished_at을 보고 먼저 새 버킷에 넣지 않는다. 등록 없는 오래된 ID나 임의 finish는 §1의 orphan으로 제외한다. 현 epoch의 기존 round_id를 다른 새 invocation에 붙이려는 시작도 식별 충돌이다. 식별 레코드 자체가 소실된 비정상 상태는 정확한 duplicate/conflict 판정을 중단하고 `post_close_unverified` 및 coverage 오류로 표시하며 기여를 추가하지 않는다. 정상 경로에서는 epoch 내 최소 레코드 만료가 없다.

| Tier 1 저장 제한 | 초기 수치와 도달 시 규칙 |
|---|---|
| 최소 식별/호출 레코드 | **최대 131,072개/epoch**. 초기화 실패·미종료 호출도 한 슬롯을 쓴다. payload 없는 호출을 무료로 누적시키지 않는다. |
| 열린 상세 기여 | **최대 2,048회차**, 정규화 저장분 **회차당 4 KiB 이하**. 원시 DOM·스냅샷 전체·오류 문자열/모든 충돌 변형을 저장하지 않는다. 제한 초과 보고는 `report_unavailable/aggregation_capacity`와 전체 통화 불확실성으로 남긴다. |
| 메모리 예산 | **집계기가 소유하는 상주 자료 전체 64 MiB 상한**(ID 색인·레코드·버킷·counters 포함), 직렬 처리하는 정규화 임시 자료 **256 KiB 상한**. Python 런타임/allocator의 RSS를 포함한 프로세스 전체 상한이라는 뜻은 아니다. 구현에서 객체·색인 비용 포함 계측과 상한 도달 시험이 필요하다. |
| 한도 도달 | 저장 전 예산 검사. 슬롯 또는 64 MiB를 넘기기 전에 `admission_stopped`를 고정하고 해당 epoch의 **새 호출 집계 접수를 중단**한다. 크롤러는 계속 실행한다. 기존 식별 기록은 유지한다. 고정 크기 용량 진단·`untracked_invocations` 계수·중단 시각을 남기고 전체 coverage/통화 파생값을 불확실로 표시한다. 메모리가 줄어도 이 epoch의 신규 접수를 자동 재개하지 않는다. |

기존 등록 호출의 상태 전이와 post-close 판정은 남겨 둔 슬롯으로 계속 처리한다. 상세 저장에 실패하면 통계를 부분 반영하지 않고 공백으로 남긴다. 새 접수 중단 뒤 식별 없는 종료는 모두 제외한다. 이미 중단된 구간을 정상 분모에서 빠진 성공처럼 표시하지 않는다. 원문 무한 reason/key 유입으로 한도를 우회하지 않도록 소스·계약·통화·경로·reason은 등록 enum과 고정 `other` 진단으로 제한한다. 미등록 계약은 자동 버킷 생성 없이 오류 처리하고, 등록표/규칙 변경은 새 검증 경계다. 프로세스 재시작을 이 설계가 자동 실행하지 않는다.

**회차 수 근거와 예산 산정.** 현재 코드의 IN/BREAK1/BREAK2에서 Investing은 분당 6회(`app/scheduler.py:781`, `app/scheduler.py:931`, `app/scheduler.py:1078`), bs·citi는 각 분당 1회(`app/scheduler.py:846-857`, `app/scheduler.py:1003-1014`, `app/scheduler.py:1125-1136`)다. 전일 최대 빈도가 유지된다고 보수적으로 잡으면 **8,640+1,440+1,440=11,520회/일**, 71분 약 **568회**다. OUT의 Investing 1분 주기(`app/scheduler.py:1243`) 등을 반영한 실제량은 더 작을 수 있다. 수동 호출·비정상 폭주·모드 전환 추가 호출은 이 추정에 포함하지 않으며 한도 검사가 담당한다. 131,072슬롯은 이 가정에서 약 11.38일분, 2,048상세 슬롯은 568회의 약 3.6배다. 최소 레코드와 색인을 합쳐 256 B/호출로 구현할 수 있다는 **예산 가정**은 32 MiB, 상세 최대는 8 MiB이며 나머지 24 MiB를 버킷·객체 오버헤드 등에 둔다. 256 B는 실측이 아니므로 확인 필요이며, 구현이 더 크면 64 MiB 한도에서 더 일찍 중단한다. 메모리 상한을 맞추려고 ID를 조기 퇴출하지 않는다.

**판정 예 4.** H1=`A valid,B missing,C valid`, H2=`A missing,B valid,C valid`의 누적은 둘 다 V=2,M=1이다. 상세 만료 뒤 A valid 재전달은 남은 A digest로 H1에서는 post_close_duplicate, H2에서는 post_close_conflict다. A의 finished_at을 현재 시각으로 바꿔도 먼저 같은 ID를 찾아 post_close_conflict로 판정하며 새 valid를 더하지 않는다. 131,072개 한도 뒤에는 신규 집계 접수가 중단되므로 오래된 A를 퇴출하고 새 회차로 받는 경로가 없다.

## 8. 첫 구현의 크기와 독립 검증

| 구분 | 범위 |
|---|---|
| **Tier 1 필수** | investing·bs·citi의 요청형 래퍼 진입/종료 연결, 보고 시작/초기화 실패/종료 연결, 단일 잠금 집계기, cohort 보존식, overdue·늦은 종료 전이, 통화별 세 축과 실행/폴백 분포, 값 검증·무결성·불확실성, 1분 버킷/60분 창/확정 누적, epoch 최소 ID 보존·용량 중단, log-only 주기 출력. |
| **Tier 1 검증 필수** | 보존된 원문 로그로 독립 재집계, 아래 반례·경계 시험, 메모리 상한/포화 격리 시험. 집계 로그 자체를 정답으로 다시 더하지 않는다. |
| **후속** | epoch 안 식별 기록 퇴출(§7 포화 전 — round 소유권 색인 보존·퇴출 당시 cohort 상태 동결·무종료 호출 만료와 늦은 종료 정책을 함께 정해야 한다; 그 전까지 약 11일 무재시작 epoch 에서 `admission_stopped` 가 운영 한계), 큐형 소스 연결·retry 계보와 실제 회복 집계, 하나/우리 부모 프레임 연결, IBK 통지 소유권(D8), DB 대조 기반 비율, 더 긴 epoch/영속 집계, 과거 파생 상태의 정확한 재계산, 스케줄 기대 실행 감지의 구체 구현, API·화면·알림. |

Tier 1의 retry 회복값은 현 대상에 큐 재시도가 없으므로 `not_applicable`로 둔다. 큐형 확대 시 §3의 명시 연결과 §5의 충돌 의존성 처리를 함께 구현해야 한다. 후속 표는 기존 운영 재시도나 IBK 통지를 바꾸는 허가가 아니다.

**독립 대조.** 객체 전달과 별개로 invocation/시작/초기화 실패/종료/래퍼 종료 lifecycle 원문에 같은 식별자·고정 시각을 남기고, 보관기의 원문에서 §1의 시작 cohort와 §6의 결과 버킷을 각각 재구성한다. 로그 유실 구간은 incomplete로 표시하며 in-process 값과의 차이를 성공률 차이라고만 해석하지 않는다. Investing의 기존 도구를 확장할 때 기존 일별 **시작 우선 귀속**(`scripts/investing_observe_aggregate.py:315-323`)을 새 finished_at 창과 그대로 비교하지 않는다. bs·citi도 동일 규칙의 오프라인 재집계가 필요하다. 보관 범위 검증은 기존 도구도 별도로 수행한다(`scripts/investing_observe_aggregate.py:249-288`).

필수 검증은 다음 입력과 기대값을 잠근다. 구현 테스트는 후속이며 아래는 r4 설계의 승인 기준이다.

| 사례 | 기대값 |
|---|---|
| wrapper 정상 1회 + 구 로거 예외; A 보고 연결 유실 뒤 B 실행 중 | §1 예 1의 정확한 등호. 구 통계로 호출 수를 대체하지 않음 |
| 15분 계속 실행 → 16분 종료; unavailable 뒤 늦은 종료 | §2 예 2. 현재 상태 1개, 과거 진단 유지, 종료 한 번 반영 |
| 서로 겹치는 최근 창 두 개 | §6 예 3. 확정 누적 V=1,M=1 |
| close_at 직전/정확히 close_at, 분 경계, 빈 epoch | 직전은 열린 처리, 같으면 post-close, 오른쪽 분 귀속, 빈 비율/end=null |
| 상세 만료 후 H1/H2, 같은 ID의 시각 변경, 용량 포화 | §7 예 4. digest 비교, 신규 기여 금지, 크롤러 동작 보존 |
| valid → init_failed → missing | §4 예 5. 세 통화 uncertain, 누락 판정 수 1 |
| A/B/C/D 뒤 C 충돌, 계약 혼합 | 열린 기여 V=1,M=2와 baseline unknown; 혼합은 두 계약에서 제외 |
| 수집 항목 누락/null/{}/status 없음/이상 enum | 해당 축 U 보충, 다른 축의 performed 유지 |
| 쿨다운/Investing USD만 writer 제출/writer 시작 계측 유실 | 각각 쓰기 N/N/N, U/N/N, 미확인 통화 U. 마지막 valid·누락 횟수를 쿨다운으로 초기화하지 않음 |
| 로그 실패·집계 실패·재시작·주간 모드 전환·역순 종료 | 관측 실패가 운영 제어로 전파되지 않음, coverage/epoch/불확실성 표시, 독립 대조 차이를 숨기지 않음 |

이번 작성에서 수행한 실제 코드 시험은 메모리 내 AST 격리 시험 두 종류다. 요청형 래퍼 정상 호출 뒤 debug 실패 주입에서 `calls=1, success=1, failure=1`을 확인했고, Investing의 실제 `_writing/cooldown` 메서드에서 미제출·USD만 제출·writer 계측 유실의 구분을 확인했다. 크롤러 네트워크·DB·서버를 실행한 시험이 아니다. 그 외 시간/상태 예는 설계 판정이며 구현 완료의 증거가 아니다.

shadow 결과 검토에는 주간 모드 전환·재시작·보고 유실·중복/충돌·계약 혼합·쿨다운·독립 대조 불일치·용량 초과를 포함한다. 7일 경과만으로 자동 승인하지 않는다. 큐 이관 시 재시도 연결 검증을 추가한다. API/화면 노출은 이후 빈 상태를 포함한 소비자 계약을 별도로 결정한다. §7.5의 지속장애/복구 알림은 이 집계 초안만으로 활성화하지 않는다(`SOURCE_HEALTH_PLAN.md:213-235`).

## 9. 결론 — r3 대비 변경과 닫힌 항목 대조

이번 r4는 r3의 남은 다섯 결함을 다음과 같이 바꾼다.

1. **§1:** 구 success+fail의 근사 대조를 없애고 독립 invocation_id·시각·초기화 실패 연결과 동일 시작 cohort의 등호 두 개를 고정했다.
2. **§2:** 15분을 잠정 overdue로 바꾸고 늦은 종료 수락, 이전 현재 상태 취소, 과거 진단 유지, 배타적 상태 분할을 정의했다.
3. **§6:** 이동 창 합산을 금지하고 겹치지 않는 1분 버킷·최초 종료시각 귀속·close_at·상세 만료·정확히 한 번 누적·cumulative_end를 고정했다.
4. **§5·7:** 상세 만료와 최소 ID 보존을 분리했다. epoch 내 ID/digest 유지, 시각 변경 재진입 차단, post-close 확정성 한계, 131,072호출/2,048상세/64 MiB 예산과 포화 정책을 정했다.
5. **§4:** report_init_failed를 포함한 보고 공백을 등록 통화 전체의 불확실성으로 전파하고 수집 missing으로 바꾸지 않도록 했다.

| r3 판정에서 닫힌 항목/보존해야 할 결정 | r4 위치와 유지 결과 |
|---|---|
| Investing 쓰기 제출/미제출/계측 유실 | §0·3·8. 쿨다운을 전부 unknown으로 되돌리지 않음 |
| 집계 시작시각·실제 관측 범위 | §1·6. aggregation_started_at·창·첫/마지막 종료·epoch·warm-up 유지 |
| 독립 로그 대조 | §8. 보관기 원문 재집계, 기존 도구와 창 귀속 차이 명시 |
| 선택 요약과 독립인 계측 오류 | §3.1. 어느 시도든 오류를 별도 회차 수로 보존 |
| 시작/종료 계약 연결·혼합 제외 | §1·5. 시작 계약 고정, 혼합을 두 계약에 중복 반영하지 않음 |
| 통화별 버킷 키·payload 독립 필수 통화 | §3. pair 포함 키와 등록표 유지 |
| 충돌 후 파생 상태 잔존 반례 | §4·5. baseline unknown과 retry 회복 의존성 무효화 유지 |
| report_incomplete 값 검증 | §3.1. 누락뿐 아니라 null·빈 객체·status·enum 검증, 다른 축 증거 보존 |
| 기간 밖 순서 의존의 한계 인정 | §5·7. 닫힌 수치 불변과 post_close 진단/불확실성 명시; 무제한 소급 정확성을 주장하지 않음 |
| 앞선 판정의 구 통계 이행 예외·분모·재시도·폴백·PRESERVED·API 보류 | §0·3·8. 유지 |

**검토 경과:** r1~r3 은 Claude 작성, 각 라운드 Codex REVISE(주요 원인: 쓰기 축·IBK 보존 조건의 과잉 일반화, 이동 창 합산, 시간 초과·늦은 종료 전이 부재, 식별 만료, 보고 공백 전파). r4 는 Codex 작성·Claude 검증. Claude 가 제안한 접두 퇴출은 Codex 반례 3건(round 재연결 이중 집계·cohort 상태 복원 불가·`report_unavailable` 의 늦은 종료)으로 후속으로 미뤘다.
