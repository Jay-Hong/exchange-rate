# Topic-only 계약 분할 — migration matrix

> **입력(정본 — ⚠️ 동결 아님. 승인된 commit/tag 전까지 가변)**
> - archive `TOPIC_ONLY_DELIVERY_CONTRACT.archive.md` — **829줄**(§3.4·§6.1 신설 반영), SHA — **`spec/topic-only.lock.json` 참조**
> - 사실 baseline `spec/topic-only-baseline-facts.md` — SHA — **`spec/topic-only.lock.json` 이 정본**(여기 값을 복제하지 않는다)
>   ⚠️ 이전 사본은 세션 scratchpad 에 있었다(세션 종료 시 소멸) — **리포로 옮긴 이 파일이 정본**이다.
> - 참조 revision: **`spec/topic-only.lock.json` 의 `pinned_commit` 참조**
>   (값을 여기 복제하지 않는다 — 6회 drift 실측. iOS `project.pbxproj` arming 은 범위 제외)
>
> ⚠️ **정본 입력 변경 이력** — lock 은 *우발적* 불일치만 잡는다(입력과 lock 을 함께 바꾸면 통과).
>   그러니 의도적 변경은 여기 남긴다. 안 남기면 다음 세션이 SHA 차이를 설명하지 못한다.
> - 2026-08-10 `spec/topic-only-baseline-facts.md` — B3-op 행의 `CLAUDE.md` 를 backtick 표기로
>   교정(서식만, 사실 변경 0). lock·manifest 의 baseline SHA 동시 갱신.
>
> ⛔ **규칙**: 소유 단위는 **절이 아니라 원자 요구사항**이다. 한 절이 서버·클라 계약을 함께 담으면
> 쪼개서 각각 다른 문서가 소유한다(예: §3.3 = 서버 handoff + 클라 arbiter).
> ⛔ **요구사항 ID** 를 붙여 새 문서에서 역추적 가능하게 한다.
> ⛔ 분할 완료 전에는 **워크플로를 재실행하지 않는다** (입력이 움직이면 같은 문제가 반복된다).

## 산출물 — 기존 1(baseline) + 신규 6(ADR 1 + spec 5)

| 코드 | 문서 | 책임 |
|---|---|---|
| **ADR** | `DECISIONS.md` ADR-041 (짧게) | 불변식 · 결정 · arming 게이트만 |
| **BASE** | `spec/topic-only-baseline-facts.md` (lock 고정) | 검증된 사실. 이미 존재 |
| **HAND** | `spec/topic-snapshot-handoff.md` | 서버 build/ack/close 계약 |
| **CLIENT** | `spec/ios-topic-state-machine.md` | arbiter · 상태 범위 · 재시도⊥재검증 |
| **LOAD** | `spec/revalidation-and-load.md` | jitter · single-flight · bounded wait |
| **HEALTH** | `spec/publisher-health-slo.md` | 인과 3축 · 탐지·대응 시간 |
| **CUT** | `spec/legacy-cutover.md` | 삭제 범위 · 문서 정정 · 테스트 · 순서 |

## ⛔ "남은 일" 은 manifest 가 답하지 못한다 — 정본이 셋으로 나뉜다

| 무엇의 정본인가 | 파일 |
|---|---|
| 구속력(`disposition`) · 소유(`normative_owner`) · 관계(`references` 등) | `spec/topic-only-migration-manifest.json` (동결) |
| **작업 종류 · 완료 상태 · 검증 근거** | `spec/topic-only-implementation-ledger.json` |
| — | 파생 부분집합은 **탐색용 힌트일 뿐 게이트가 아니다** |

manifest 의 requirement 스키마에는 **완료·상태 필드가 없다**(`rid`/`source`/`destination`/`normative_owner`/
`references`/`conditional_references`/`deferred_references`/`supports`). `disposition` 은 구속력이지 이행 여부가 아니다.

파생으로 뽑으려던 시도는 실제로 실패했다(2026-08-11 실측) — 블록 제목 필터 **34건** ∪ 순서·게이트 9건에서
`references` 를 전이 추적한 폐쇄 **40건** = 합집합 **56건**이고, **active 20건이 양쪽 모두의 사각지대**였다.
그 20건에 `R-LOAD-3`·`R-LOAD-4`·`R-CLI-24`(전부 "6. 내부 복구와 사용자 경고 분리" 블록)가 들어 있다.
두 집합은 포함 관계도 아니다(제목만 잡은 것 16 / 폐쇄만 잡은 것 22). 그래서 대장은 부분집합이 아니라
**active 전량 76건**을 덮는다.

### 대장이 실제로 잠그는 것

`tests/test_topic_only_ledger.py` 는 반례와 양성 대조군을 함께 둔다. 테스트 개수는 계약이 아니므로
여기에 복제하지 않고 `pytest --collect-only` 결과를 따른다.

| 규칙 | 왜 |
|---|---|
| active 전량 커버 · manifest 와 `owner`/`block_title` 일치 · `manifest_sha256` 고정 | 누락과 표류를 동시에 막는다 |
| 근거 객체는 종류별 정확한 필드만 허용 — `commit`(full SHA + 실제 변경 path, 부모 이력 필수) / `test`(server는 실제 수집 node, iOS는 `파일::XCTestCase/testMethod` source locator) / `doc`(Markdown의 RID anchor 또는 `evidence … supports=<RID>`) | 초안은 `evidence=["trust me"]`·`[True]`·`[0]`, 없는 test node와 anchor도 통과했다 |
| `deploy` 는 환경 enum + repo full SHA + HTTPS URL 또는 `sha256:` digest를 기록하되, **외부 배포 성공과 URL 불변성을 이 검사에서 확인하지 않는 attestation** 으로 취급 | 문자열 두 개를 배포 검증으로 과장하지 않는다 |
| `done`·`verified` 는 해석된 근거 ≥1. `verified` 는 reviewer와 작업 종류별 근거를 요구(구현=commit+test, ops=deploy, doc/decision=doc) | 근거 객체의 실재만으로 RID 주장과의 의미 적합성이 증명되지는 않는다 |
| **닫힘은 `verified` 하나뿐** — `closed_statuses` 를 데이터로 두고 값까지 잠금 | `done` 을 닫힘으로 슬쩍 넓히지 못하게 |
| `not_required` **폐지** → `non_actionable`(사유 enum + evidence + note + **reviewer**, work kind 금지). `covered_by_other_rid` 대상은 `todo` 이상 실제 작업 상태여야 한다 | 순환 위임으로 실제 이행자 0인 상태를 막는다 |
| 최상위/근거 객체의 정확한 스키마 · enum 설명값 검사 | 초안은 필드를 지우거나 임의 필드·객체를 넣어도 통과했다 |
| doc 근거는 `.md`의 명시적 RID scope만 허용한다. RID anchor는 canonicalize하고, `evidence` marker는 `supports=`를 파싱한다. `covered_by_other_rid`는 note·근거·실제 작업 대상을 모두 대조한다 | DOM/job id와 Python 문자열을 doc anchor로 오인하거나 `E-WIRE-1 supports=R-CLI-9`를 다른 RID에 재사용하지 못하게 한다 |

⚠️ `non_actionable` 은 manifest 의 `active` **구속력을 취소하지 않는다** — 사유·근거·검토자가 남는 분류일 뿐이다.
⚠️ `work_kind`와 `non_actionable` 사유가 자연어 요구에 맞는지는 기계가 판정하지 않는다. 임의의 `doc` 격하나
`context_statement` 남용을 막는 최종 경계는 reviewer다. 대장은 이 분류를 감사 가능하게 만들 뿐 진실을 증명하지 않는다.
⚠️ **`test` 근거는 실행 성공을 확인하지 않는다.** 서버는 `--collect-only` 로 node 수집만 확인하므로
skip 표시된 node도 근거가 된다. iOS는 Xcode를 실행하지 않고 source locator만 확인하므로 target membership·
실제 수집·통과를 증명하지 않는다(워크플로는 Ubuntu에서 pytest만 실행). 실행 성공은 `verified` reviewer가 책임진다.
iOS 근거는 source locator 규약(`XCTestCase/testMethod` · 클래스 본문 소속 · `test` 로 시작 · 인자 없음 · instance visibility)까지 본다 —
그러지 않으면 파일 안 아무 helper 나 근거가 된다(실측: `private func t(_:)`).

⚠️ 구조 검사와 reviewer 기록은 자연어 의미가 참임을 기계적으로 증명하지 않는다. 특히 deploy reference는
외부 시스템에서 dereference하지 않는다. `verified` reviewer가 근거와 RID 주장 사이의 의미 적합성을 책임진다.

### ⚠️ pin 된 인용 경로 11개는 사실상 읽기 전용이다 (2026-08-11 실측)

`preflight` 의 `E_CODECHANGED` 는 **경로 단위**로 검사한다 — 인용된 파일이 pin 이후 한 줄이라도
바뀌면 빨강이다. 그래서 아래 경로들은 **재-baseline 없이는 수정할 수 없다**:

```
  CLAUDE.md
  app/auth_executor.py
  app/database.py
  app/fx_topic_publisher.py
  app/latest_rates_cache.py
  app/legacy_policy.py
  app/main.py
  app/topic_dispatcher.py
  app/topic_initial_snapshot.py
  app/topic_wire.py
  nginx/conf.d/default.conf
```

pin 은 `spec/topic-only.lock.json` **과** 동결 manifest **양쪽**에 박혀 있어(`E_PINNED`),
옮기려면 manifest → lock → 이행 대장 `manifest_sha256` 까지 연쇄 갱신 = 마이그레이션 전체
재-baseline 이다. **작은 문서 수정 때문에 할 일이 아니다.**

✅ **CI 서술 drift 해소**: `CLAUDE.md` 의 2026-06-11 항목대로 전체 pytest workflow
(`.github/workflows/tests.yml`)는 Markdown-only 변경을 다시 건너뛴다. 대신
`.github/workflows/topic-only-docs.yml` 이 모든 Markdown 변경에서 provenance와 topic-only 문서·원장
게이트만 실행한다. 따라서 pin 된 `CLAUDE.md`를 재-baseline 하지 않고도 기존 서술과 현행 동작을
일치시켰다. 두 workflow의 iOS provenance 검사는 앱 runtime secret과 별개로 read-only deploy key
`TOPIC_MIGRATION_IOS_DEPLOY_KEY`를 요구한다.

⛔ **B3-op 인용의 성격 주의**: baseline 은 `CLAUDE.md` 를 *기록 근거*로 인용한다(코드 아님).
그런데 경로 단위 검사는 "인용된 사실이 바뀌었다" 와 "무관한 줄이 바뀌었다" 를 구분하지 못한다.
과잉 차단이지만 **fail-closed 방향**이라 그대로 둔다.

초기값 `unreviewed` 는 의도다 — 76건 분류가 끝날 때까지 구현을 막지 않는다.

## ⛔ 절 단위 배정 표는 **삭제했다** — routing 정본은 `spec/topic-only-migration-manifest.json` 하나다

구 표는 `R-CUT-10`·`R-CUT-11`·`R-HLT-3` 처럼 **manifest 에 존재하지 않는 RID** 를 담고 있었고,
§8.2 를 CLIENT 로 적어 manifest(ADR)와 어긋났다. 두 곳에 배정을 적으면 반드시 갈라지고,
갈라진 쪽을 사람이 읽으면 **없는 RID 를 사실로 믿는다**(2026-08-10 실측: 검증 입력으로 고정될 뻔했다).

- 어느 절이 어느 문서로 가는지 = `manifest` 의 `destination`
- 누가 규범을 소유하는지 = `manifest` 의 `normative_owner`
  (`active` 요구사항당 정확히 하나, `proposed`·`evidence` 는 반드시 `null`)
- 확인 = `python3 scripts/topic_migration_manifest.py verify` (구조) + `preflight` (인용 근거)

이 문서에는 **규칙과 판단 근거만** 남긴다 — 배정 값은 남기지 않는다.

⛔ **초안의 "소유 없는 절 0 / 둘인 절 0" 주장은 철회한다** — 개수 대조로 얻은 결론이라 근거가 없다.
실제로 §13.1 은 6행에 걸쳐 있고, §3.3 은 서버 handoff 와 클라 arbiter 를 함께 담는데 HAND 단독으로
배정돼 클라 절반이 소유자를 잃는다. 원자 요구사항 분해 결과로 교체한다.

## 김프 — ⛔ **[제안·결정 대기]** (ADR R-DEC-2)

> ⛔ **사용자가 명시적으로 수용하기 전까지 [제안] 이다.** 초안이 이걸 확정 문구로 적었는데
> **무단 상태 변경**이었다(같은 세션 두 번째). 외부 리뷰어의 권고는 사용자 승인이 아니다.

아래는 **제안 문안**이다 — 수용 시 그대로 ADR 에 기록한다.

> ✅ **정본 결정(2026-08-10, 사용자)** — 김프 게이트는 **archive 3게이트 문안**이 정본이고,
> 제안 상태는 **[제안·결정 대기] 유지**다. manifest 는 이미 그대로 추적한다
> (R-HLT-2 / R-CUT-14 / R-LOAD-1, 전부 `proposed` · `normative_owner: null`).
>
> ⛔ **철회 2건** — 이 자리에 있던 두 서술이 모두 틀렸다:
> 1. *"3게이트 판은 폐기했다(대화에서 2게이트로 수렴)"* — **근거를 댈 수 없다**.
> 2. *"내용은 사실상 같다(묶음만 다르다)"* — **틀렸다**. 3게이트 ②는 실기기 `topics_disabled`
>    → purge → **재활성화 복구** 리허설을 **독립 의무**로 명시하는데(archive 755),
>    2게이트 A 는 safety-stop 을 대응 시간 경로에 넣을 뿐 실기기·재활성화를 명시하지 않는다.
>    재그룹화가 아니라 **계약 약화**였다. 아래 2게이트 문안은 **역사 기록으로만** 남긴다.


> **김프 잔존 위험을 조건부 수용한다.** 이는 **조건부 제품 결정 기록**이며
> **구현 승인도 arming 승인도 아니다.** 아래 두 게이트의 **수치 기준과 통과 증거가 모두 확보된
> 뒤에만** arming 을 검토한다. 하나라도 미달이면 arming 하지 않고 **김프 표시 정책을 재결정**한다.
>
> **게이트 A — end-to-end 대응 시간(수치화)**
> `실패 → 알람 → safety-stop → 영향받은 클라 purge` 까지의 **총 대응 시간**을 수치로 정하고 실측한다.
> - **시작점 = 아래 셋 중 _최초_ 사건**: ① eligible flush 실패 ② 외부 expected-run 누락
>   ③ 외부 heartbeat/telemetry 상실
> - ⛔ **감시자는 publisher 와 다른 failure domain** 에 있어야 한다 — 같은 프로세스·호스트면
>   함께 죽어서 타이머가 시작조차 하지 않는다.
> - ⛔ 판정은 **인과** 로 한다(시간 침묵 금지). ⚠️ 단 **모집단이 둘**이다 — 하나로 묶으면
>   flush 자체가 없는 liveness 실패가 다시 빠진다(초안이 그랬다):
>
>   | 모집단 | 시작 사건 | 탐지 주체 |
>   |---|---|---|
>   | **데이터 경로 SLO** | `flushDisposition == eligible` 인데 전달 실패 | publisher 내부 결과(3축) |
>   | **liveness 의무** | expected-run 누락 / heartbeat·telemetry 상실 | **publisher 밖** 감시자 |
>
>   게이트 A 의 대응 시간은 **둘 중 먼저 발생한 사건**부터 잰다.
>
> **게이트 B — 45초 동시 재구독 완화(수치화)**
> jitter · topic 별 single-flight/cache · **bounded wait** · I/O timeout · 실패 cooldown · 재시도 상한.
> - ⛔ **즉시거절 금지** — 이 리포가 이미 기각했다(`auth_executor`: 동시 도착이라 1초면 빠질 큐를 대량 거절).
> - 수치 합격 기준: **목표 동시성 · queue wait · DB 포화 · 오류율 · 재연결 fan-out** + 부하 증거.

## manifest 4렌즈 리뷰 — **종료 (2026-08-10)**

launcher 생성물(archive·baseline·manifest SHA 고정 + 전 블록 열거 강제)로 **4렌즈**
(disposition · ownership · **references** · omission) 리뷰를 돌린다. 매회 `complete: true` ·
실패 렌즈 0 · 네 렌즈 모두 그 시점 SHA 와 전 블록을 보고했다.

> ⛔ **아래 라운드 표는 역사 기록이다** — 그때의 블록 수·SHA 이고 현행 상태가 아니다.
>    현행 배정은 `manifest` 하나에만 있다.

| 라운드 | manifest | 지적 | 성격 |
|---|---|---|---|
| 1 | 33블록 | 8 | disposition 오배정 · 절 미분할 · 문단 통째 누락 |
| 2 | 34블록 | 6 | 소유 오배정 · 범위 선언 누락 |
| 3 | 35블록 | 5 | 서버/클라 계약 혼입 · HAND 가 굶고 있었음 |
| 4 | 36블록 | 5 | 오배정 1건 정정 + 실제 누락 3건 |

⛔ **"진동이니 종료"라는 내 판정은 철회한다.** 4라운드는 잡음이 아니라 **판별력이 작동한 결과**였고
(오배정 정정 + 실제 누락 3건), 내가 스스로 세운 종료 조건("2건 이하 또는 재분리뿐")도 충족하지 않았다.
조건을 못 채운 채 종료를 선언한 것은 **골대를 옮긴 것**이다.
→ 계속했고, **전면 재실행**으로 종료했다(구 `--delta` 축소 실행안은 폐기 — 이름도 `--focus` 로 바뀌었고
   초점일 뿐 범위 축소가 아니다. 실제 종료 라운드는 전 블록을 다시 봤다).
⛔ **블록 수만으로 drift 를 판단하지 않는다** — 4라운드 검토본과 그 다음 판이 **둘 다 36블록인데 SHA 가
달랐다**(998e3ef5 vs 9d9bf0df). 표에 블록 수만 적으면 drift 가 숨는다.

## 🔒 manifest 동결 — 게이트 전량 통과 (2026-08-11)

⚠️ **내용 동결이지 불변 선언이 아니다** — 3행의 "승인된 commit/tag 전까지 가변" 은 그대로다.
새 사실이 나오면 다시 열고, 열면 아래 게이트를 **같은 SHA 에서 전부 다시** 돌린다.
(실제로 한 번 그렇게 했다 — `db597696` 동결 후 §3.4·§6.1 문장이 **계약을 넓힌 것**을 발견해
 되좁히고 재검증했다. 참조 제거인 줄 알았으나 **의미 변경**이었다.)

| 입력 | SHA |
|---|---|
| `archive` | `cde1d2ca3e71` (829줄) |
| `manifest` | `8ec93340a71f` (94 요구 / 40 블록) |
| `baseline` | `c7538755f04f` (**미변경**) |
| `lock` | `742c623b4185` |
| `validator` | `0f66aa7e2ec9` |
| `launcher` | `1121d03399eb` |

| 게이트 | 결과 |
|---|---|
| `verify` / `preflight` | exit 0 |
| 반례 스위트 | **141 / 0** (허용 대조군 2 포함) |
| 변이 코퍼스 | **73 / 73** · 구멍 0 · stale 0 · 원본 무결 (bytecode-cache 방어 #73 포함 전량 재실행) |
| pytest | manifest·번들 도구 테스트 통과 · 산출물/ADR 구조 게이트 **46 passed** |
| 4렌즈 리뷰 | `complete: true` · 실패 렌즈 0 · **findings 0** · 4렌즈 × 40블록 · 동일 SHA 에코 |

### 수렴 (라운드별 findings)

| 회차 | manifest | findings | 성격 |
|---|---|---|---|
| r1 | `c3ece680` | 11 | 참조 9 · 소유 1 |
| r2 | `65fdb670` | 8 | 참조·누락 혼재 |
| r3 | `a05b593e` | 4 | 참조 3 · 소유 1 |
| r4 | `dccd761c` | 4 | 참조 2 · 누락 2 |
| r5 | `f6464c3e` | 6 | **전부 참조**(구조 축 0) |
| r6 | `2acdaa70` | **0** | — |
| r7 | `2a5cdc89` | 1 | 참조(HAND→CLIENT 부등식 우변) |
| r8 | `8ec93340` | **0** | — |

r5 에서 구조 축(disposition·ownership·omission)이 0이 된 뒤 남은 참조 부류를
**전수 판정**(17건)으로 닫아 r6 이 비었고, archive 의미 정정 후 r7 의 1건까지 닫아 r8 이 비었다.

### 문서 작성 후 남은 조건

- ✅ **§2.2 해소** — ADR-041 의 `R-INV-1` 구간에 `E-INV-1`·`E-INV-2` 두 근거가 모두 렌더됐고
  `tests/test_adr041_grounds.py` 4건이 통과했다.
- ✅ **산출물 6종 작성·구조 검증** — `DECISIONS.md` ADR-041 + spec 5종이 존재하며
  `check-docs` 와 `tests/test_topic_only_documents.py` 가 통과한다. ADR 은 기존 본문 삭제 없이 append 했다.
- ✅ **의미 리뷰 완료** — 문서 작성·초기 분류 주체와 다른 Codex reviewer 가 같은 동결 입력에서
  ADR-041/spec 5종을 역방향으로 검토했다. manifest 출력 요구 86개, claim 후보 59개
  (`code_fact` 47 · `normative` 12), 후보 밖 단정, archive 규범 보존, 인용의 **존재가 아니라
  주장 뒷받침 여부**까지 확인했다.
  - journal: `spec/topic-only-semantic-review.json` — 검토한 문서 6종 SHA, 원장 SHA, 입력 SHA,
    finding 6건과 처분을 고정한다.
  - 처분: high 3건 + medium 3건 모두 **실제 수정 + 검증**으로 닫았다. 특히 고정 3행 오기,
    동결 밖 미커밋 arming 단정, baseline B1 범위 확대를 교정했다.
  - `tests/test_topic_only_semantic_review.py` 가 문서/원장 drift, 열린 finding, 불완전 review scope 를
    fail-closed 로 거부한다. 자연어 판정 자체를 기계가 증명한다는 뜻은 아니다.
- 번들은 리포의 `scripts/topic_migration_doc_bundle.py` 로 **가장 마지막에** 재생성한다.
  `generate --out /tmp/topic-only-doc-bundle.md --force` 후 같은 스크립트의
  `check --bundle /tmp/topic-only-doc-bundle.md` 가 통과해야 문서 작성 입력으로 쓴다. 번들은
  `source_metadata`·pinned commit 과 baseline·lock·matrix 본문도 포함한다. 문서 작성 후에는
  `check-docs` 로 manifest 대비 산출물 완전성을 확인한다.

## ⚠️ 게이트 밖 관찰 — 강제되지 않는다

⛔ **이전 판의 "문서 작성 시 **반드시** 반영할 것" 이라는 제목을 철회한다.**
`launcher` 는 archive·baseline·manifest 만 고정하고 **이 문서를 읽지도, SHA 로 잠그지도 않는다**
(참조 0건, 실측). 따라서 여기 적은 것은 어떤 게이트도 강제하지 않는다 —
**수동적 산출물을 능동적 기전으로 부르지 않는다.**

~~기존 HEALTH 항목은 삭제했다.~~ 당시 `R-HLT-2` 일부를 그대로 확정하면 proposed 를 우회해
승격하는 결함이어서, **블록 분할 + 별도 사용자 결정 전에는** 열어 뒀다. 이후 그 조건을 충족해
`R-HLT-3`(active)을 별도 소유자로 신설했으므로 현재는 아래 해소 기록이 정본이다.

manifest 로 옮긴 것(= 게이트 안):
- CUT inventory 보강 → `R-CUT-15`(**750행**, §12 게이트 사다리라 소유는 **ADR**, 계약은 `R-CUT-1..8` 참조)
- 재검증이 의존하는 KRX 제약 → `R-CUT-18`(**679행**) (구 기록의 `R-CLI-22` 는 **현행 manifest 에 없는 구 RID** 다 — 이전 판에 있었는지는 확인하지 않았다. 내용 자체는 덮여 있다)
- 교차 소유 금지 → 검증기 `E_OWNOVERLAP` (반례 + 코퍼스 등록)

게이트 밖에 남는 **관찰**(강제 없음, spec 작성 시 참고):
- `dxy:spot` — **최소 산출물**은 `R-HAND-19`(54행, HAND/active)가 소유한다. 그러나 archive 에
  **상세 build 계약(payload·cache·auth)은 여전히 없다** — spec 에서 발명하지 말 것.
- ~~`R-CLI-11`(확정) 폭주를 `R-LOAD-1`(제안)이 완화하는 **비대칭**~~ → **해소**(§6.1 active 화).
  같은 형태였던 HEALTH 도 `R-HLT-3`(§3.4, 304-329, **active**)으로 분리했다 — 이제
  ⛔ *"N분 무발행"* 알람을 구현하면 **확정 요구 위반**이다.
- ~~archive 757행 `(§6.1)` dangling~~ → **해소**. §6.1(**567-592**, 절 헤딩 568)을 **active 계약으로 신설**하고
  §13.1 게이트 3은 한 줄 acceptance 로 축소했다(`R-LOAD-4`/`R-CLI-24`/`R-LOAD-3` 소유).

## 분할 검증기 (완료 판정)

1. ⛔ **개수 비교 금지** — `절 수 == 행 수` 는 전단사를 증명하지 않는다(30행이 전부 §1 을 가리켜도 통과).
   대신 **원자 요구사항 ID** 를 열거하고 `active` ID 의 normative owner 가 **정확히 하나**인지,
   `proposed`·`evidence` ID 의 owner 는 **`null`** 인지 검증한다.
2. archive 의 모든 normative 문장이 **어떤 ID 에도 안 잡히는지** 역방향으로 훑는다(누락 검출)
3. 새 문서마다 헤더에 **책임 · 입력 baseline SHA · 검증기**
4. 새 문서의 모든 코드 단정이 baseline 항목 id 또는 새 file:line 을 인용.
   코드 경로는 **backtick** 으로 표기한다. code span 밖의 `_path_` / `__path__` underscore 강조는
   두 추출기가 함께 놓칠 수 있으므로 preflight 가 `E_CITEFORMAT` 으로 거부한다.
5. ⛔ 워크플로 재실행 전: `python3 scripts/topic_migration_manifest.py preflight` **통과**.
   ⚠️ `verify` 는 **구조만** 본다. CI 는 pinned iOS 리포를 read-only checkout 한 뒤
   `TOPIC_MIGRATION_IOS_ROOT` 를 지정해 `preflight` 를 별도 실행한다.
   인용 경로 대조가 필요한 검증은 반드시 `preflight` 명령이어야 한다.
   ⛔ **HEAD 일치는 검증기가 아니다**(문서를 커밋하면 당연히 달라지는데 코드 근거는 그대로일 수 있다)
   — lock SHA + **인용 경로의 pinned commit 대비 diff**로 판정한다.
6. ✅ **워크플로 SHA 강제 (Launch Blocker) — 구현됨.**
   `scripts/topic_migration_workflow_launcher.py` 가 lock 을 읽어 게이트를 통과할 때만
   **SHA 를 schema `enum` 으로 박은 스크립트**를 만든다. 워크플로는 **그 생성물로만** 실행한다.

       python3 scripts/topic_migration_workflow_launcher.py --check        # 게이트만
       python3 scripts/topic_migration_workflow_launcher.py --out run.js   # 통과 시 생성

   게이트: lock 유효(`L_LOCK`) · 파일 SHA 재계산 일치(`L_SHA`) · manifest↔lock 전파(`L_PROPAGATE`)
   · preflight 통과(`L_PREFLIGHT`). 하나라도 어긋나면 **아무것도 만들지 않는다**.
   ⛔ 손으로 스크립트를 쓰거나 생성물의 SHA 를 고치면 이 게이트가 통째로 무의미해진다.
   ⚠️ **과장 금지** — enum 은 *정직한* 에이전트의 오독을 잡는 트립와이어이지 **날조 방지가 아니다**.
   기계가 보장하는 것은 launch 시점 입력 동일성과 경로 고정까지다.
   각 게이트는 `tests/test_topic_migration_launcher.py` 에서 **지웠을 때 실제로 새는지**까지 잠갔다.
7. ⛔ **검증기를 고쳤으면 반례 코퍼스를 돌린다.**
   `python3 scripts/test_topic_migration_manifest.py --mutations` — `scripts/vacuity_mutations.json`
   의 퇴행 전부를 **격리 사본**에 주입해 스위트가 모두 잡는지 본다(건수는 여기 적지 않는다 —
   숫자는 반드시 어긋난다. 실제로 19 라 적어 둔 사이 23 이 됐다).
   ⚠️ 적중 판정은 `exit!=0` 이 **아니다** — `rc==1` ∧ `stderr` 비어 있음 ∧ 빨강 행 존재.
   traceback·구문오류·환경실패로 죽은 것은 "잡았다"가 아니다(그렇게 세면 검증기가 죽어도 초록이 된다).
   러너 자체도 합성 코퍼스 meta-test 로 복원·대조군·판정식을 시험한다.
   ⚠️ **"스위트 전 행 초록" 은 공허하지 않음의 증거가 아니다** — 실제로 퇴행 8건이 초록인 채로
   통과했다(실측). 행 수·건수를 여기 적지 않는다 — 적으면 반드시 어긋난다(이 문서에서 두 번 어긋났다).
   느려서 CI 에는 없다. 검증기 diff 를 승인받기 전에 손으로 돌릴 것.
8. ⛔ **구조 검증 ≠ 의미 완전성.** ~~전부 prose 로 둬도 통과~~ 는 **거짓**이었다(E_NOACTIVE 가 막는다).
   실제 한계는 **배정이 옳은지**를 못 본다는 것이다 — 모든 요구사항을 한 문서로 몰아넣어도 통과한다(실측 rc=0) —
   분류가 옳은지는 **독립 리뷰 게이트**가 따로 판정한다.
