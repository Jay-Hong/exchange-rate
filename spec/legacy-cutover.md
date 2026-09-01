# legacy cutover — 삭제 범위 · 문서 정정 · 테스트 · 순서

- 책임: 삭제 범위 · 문서 정정 · 테스트 · 순서
- 상태: Draft — 구현 착수 전 합의 대상
- 코드 근거 기준일: 2026-08-09
- server 기준 commit: `ba54a8077f739369f58ac5d23fdb720cdb57f9b3`
- iOS 기준 commit: `8f6afff299621d50c3431dbea739ed07c378c59a`
- archive SHA: `cde1d2ca3e714733776e1b0d7e821a542e1f8d183cb2951bef8c93fb444d9814`
- manifest SHA: `d9584f3f829639783a78822001630275519f08960697ab356747babf58213442`
- baseline SHA: `6b1205f2a04fe884178fbd3e9aaa75800b89e15c4e093da86a40a741d44fa946`
- 검증: `python3 scripts/topic_migration_manifest.py preflight`

이 문서는 신규 앱이 legacy 소비를 걷어낼 때 **무엇을 지우고 · 무엇을 먼저 고쳐 쓰고 ·
무엇으로 검증하는가**를 소유한다. 불변식과 arming 게이트는 `DECISIONS.md` ADR-041 이,
서버 build·ack·close 계약은 `spec/topic-snapshot-handoff.md` 가,
클라 상태기계는 `spec/ios-topic-state-machine.md` 가 소유한다.

---

## 1. 삭제 범위 — 전수 inventory

<!-- rid: R-CUT-1 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-1"></a>
### R-CUT-1

초안은 `usdtDisplayState`의 legacy 분기와 `baseRates(for:)` 두 곳만 적어 범위를 크게 과소평가했다.
`0f2a3f8`은 premium runtime의 legacy REST/WS/cache/graph 소비와 관련 dead code를 함께 제거했다.
`testPremiumRuntimeHasNoLegacyRateOrGraphConsumer`가 제거된 파일과 금지 심볼의 복원을 잠근다
(`ios/FXiTests/TopicMessageTests.swift:386-428`).

⚠️ Release 기본값은 여전히 topic **OFF**다(baseline F14). 이제 OFF는 legacy fallback이 아니라 빈
topic 표면이므로 unarmed artifact를 출시할 수 없다. 따라서 코드 cutover 완료와 Release arming GO는
서로 다른 상태이며, 후자는 여전히 미완료다.

→ 따라서 이 문서의 삭제 작업은 Release arming 게이트와 **묶여 있다**. arming 은 별도 GO 로만
열리고 자동화가 커밋하지 않는다.

<!-- relation: references target=R-GATE-2 -->
- references: [R-GATE-2](../DECISIONS.md#r-gate-2)
<!-- relation: references target=R-GATE-4 -->
- references: [R-GATE-4](../DECISIONS.md#r-gate-4)
<!-- /rid: R-CUT-1 -->

<!-- rid: R-CUT-2 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-2"></a>
### R-CUT-2

**(A) 단순 삭제로 끝나지 않았던 것 — `0f2a3f8`에서 완료**

| # | 지점 | 왜 어려운가 |
|---|---|---|
| A1 | **`AppState` lifecycle 재설계** | `.connected/.offline/.refreshingCached`의 legacy 배열 payload와 `AppState.rates`를 제거했다(`ios/FXi/Models/AppState.swift:11-18`). topic last-known 존재 여부와 transport 상태를 `applyConnectionState`가 결합하고(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:258-287`), 최상위 라우팅은 payload 없는 lifecycle만 본다(`ios/FXi/ContentView.swift:148-160`). tether-only last-known과 재연결 어포던스 회귀도 행동 시험으로 잠겼다(`ios/FXiTests/TetherDataPathTests.swift:3186-3307`) |

<!-- /rid: R-CUT-2 -->

<!-- rid: R-CUT-3 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-3"></a>
### R-CUT-3

| # | 지점 | 왜 어려운가 |
|---|---|---|
| A2 | **은행 가격알림 시트** | `AlertAddSheet.rate(for:)`는 `rateViewModel.rates(for:)`를 쓰고(`ios/FXi/Views/Components/AlertAddSheet.swift:90-103`), 그 조회는 탭과 같은 `baseRates(for:)`의 FX topic live/last-known으로 수렴한다(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:580-625`). cold-start 캐시 기반 현재가도 행동 시험이 잠근다(`ios/FXiTests/TetherDataPathTests.swift:687-708`) |

<!-- /rid: R-CUT-3 -->

<!-- rid: R-CUT-4 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-4"></a>
### R-CUT-4

| # | 지점 | 왜 어려운가 |
|---|---|---|
| A3 | **WS 3-in-1 legacy envelope** | `handleLegacyRatesMessage`와 rates/indices/graph callback, `GraphViewModel` 생성·환경 주입·stop 배선을 함께 제거했다. 서버는 구버전용 legacy envelope를 유지하지만 신규 앱은 이를 소비하지 않는다. 금지 심볼과 삭제 파일은 architecture test가 잠근다(`ios/FXiTests/TopicMessageTests.swift:386-428`) |

<!-- /rid: R-CUT-4 -->

<!-- rid: R-CUT-5 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-5"></a>
### R-CUT-5

| # | 지점 | 왜 어려운가 |
|---|---|---|
| A4 | `usdtDisplayState` fallback 단순화 | topic gate가 꺼지면 빈 상태, 켜지면 live `tetherTopicRates` 또는 disk `cachedTopicRates`만 쓴다(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:691-714`). 빈 snapshot과 legacy cache를 다시 노출하지 않는 행동 시험이 있다(`ios/FXiTests/TetherDataPathTests.swift:469-487` · `:606-617`) |

<!-- /rid: R-CUT-5 -->

<!-- rid: R-CUT-6 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-6"></a>
### R-CUT-6

**(B) 선행 정리 완료**

`RateGraphView`·`PeriodTabBar`·`GraphViewModel`·`WebSocketMessage` 파일과 legacy 사본/helper를
`0f2a3f8`에서 제거했다. `SampleGraphView`가 소유하던 정책 설명은 지역화해 삭제된 파일을 문서
정본으로 가리키지 않는다. Debug suite 776개와 Release OFF/임시 ON 빌드가 통과했고,
architecture test가 callback 소유권과 파일 복원을 함께 감시한다
(`ios/FXiTests/TopicMessageTests.swift:386-428`).

<!-- /rid: R-CUT-6 -->

<!-- rid: R-CUT-7 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-7"></a>
### R-CUT-7

**(C) 사용자 행위 커버리지 완료**

삭제된 envelope 자체가 아니라 cold-start/last-known, legacy frame 무시, DXY live/cache/purge,
은행 알림 현재가, 연결 lifecycle을 테스트한다. 구체적인 행동 시험은 [R-CUT-13](#r-cut-13)에
연결했고, architecture test는 legacy 소비 경로가 다시 생기는 것을 별도로 막는다.

<!-- /rid: R-CUT-7 -->

<!-- rid: R-CUT-8 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-8"></a>
### R-CUT-8

**(D) 타입은 남긴다**

`ExchangeRate` **struct 자체는 무료 티어 스냅샷이 verbatim 디코드**해 재사용하므로 남겼다
(`ios/FXi/Models/FreeSnapshotModels.swift:27-31` ·
`ios/FXiTests/TopicMessageTests.swift:494-520`). legacy wrapper인 `ExchangeRateResponse`와
`IndicesPayload`는 제거됐다.

<!-- /rid: R-CUT-8 -->

---

## 2. 반증된 서술 정정 목록 (같은 슬라이스에서 처리)

<!-- rid: R-CUT-9 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-9"></a>
### R-CUT-9

문서-코드 드리프트가 **실제 분석 오류를 만들었다**. 아래를 고치지 않으면 같은 오판이 재발한다.

| 위치 | 현 서술 | 실제 |
|---|---|---|
| `WebSocketService.swift` ~1582 | "`performTopicCommand` 의 `catch` 는 `forgetPending` 만 하고 재시도하지 않는다 … 재연결 전까지 조용히 미구독" | catch 가 **bounded retry 한다**(`shouldRetryCommandFailure` denylist + 최대 3회 — baseline F5). 그 주석이 열어 둔 항목이 **이미 구현됐는데 주석만 안 고쳐졌다**. 실제 공백은 **재시도 소진 이후** |
| `TOPIC_V2_RELEASE_RUNBOOK.md` MODE 2 | ✅ **해소** — 신 pin 런북에 "45s 초과 시 자동으로 legacy 표시" 문구 0건 | 이 표의 '실제'대로 이미 재작성돼 있다 — "MODE 2 (tether 45초 무수신) → 조용한 재검증: 화면과 last-known은 그대로이고 배너도 없다"(`ios/TOPIC_V2_RELEASE_RUNBOOK.md:313`) · "45초 legacy fallback을 기다리는 절차가 아니다"(`ios/TOPIC_V2_RELEASE_RUNBOOK.md:350`) · "FX/KRX에는 이 시간 threshold를 적용하지 않는다"(`ios/TOPIC_V2_RELEASE_RUNBOOK.md:369`). 불변식 [R-INV-1](../DECISIONS.md#r-inv-1) 과 일치 |
| 같은 문서 rollback 절 | ✅ **해소** — 신 pin 런북에 "graceful degrade"·"서비스 중단 아님" 문구 0건 | Rollback 절(`ios/TOPIC_V2_RELEASE_RUNBOOK.md:341-362`)은 degrade 를 주장하지 않고 두 실패를 분리해 적는다 — topic 데이터 장애는 last-known 유지 + 조용한 재검증(`ios/TOPIC_V2_RELEASE_RUNBOOK.md:356`), safety-stop 은 클라가 topic 값을 purge 하고 **명시적 unavailable 화면**을 표시(`ios/TOPIC_V2_RELEASE_RUNBOOK.md:346-348`) |
| 같은 문서 (그래프) | ✅ **해소** — 해당 문구가 신 pin 런북에서 삭제됨(0건) | 남은 서술은 graph live-tail 을 topic surface 로 다룬다(purge 범위에 포함 — `ios/TOPIC_V2_RELEASE_RUNBOOK.md:148`). 코드도 같다 — `GraphV2LiveBridge`(`ios/FXi/Views/Components/GraphV2Section.swift:1422`) 의 `tabTopicRates`(`ios/FXi/Views/Components/GraphV2Section.swift:1431-1443`) 가 `fxTopicRates` 를 읽는다 |
| `DECISIONS.md` ADR-039 요약 | "신규 앱 legacy fallback 금지" | 원문은 "legacy **anon** fallback 금지" — 금지 **근거**가 인가라는 사실이 지워졌다 |
| `REALTIME_V2_CLIENT_GUIDE.md` | "화면을 건드리지 않으면 **영원히** stale 표시가 안 됐다" | 리허설 관측은 **90초**다. 기전상 그럴듯해도 관측보다 강한 단정 |

<!-- relation: references target=R-INV-1 -->
- references: [R-INV-1](../DECISIONS.md#r-inv-1)
<!-- /rid: R-CUT-9 -->

<!-- rid: R-CUT-18 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-18"></a>
### R-CUT-18

| 위치 | 현 서술 | 실제 |
|---|---|---|
| `DECISIONS.md` ADR-038 D2 | MODE 2 revert 전제 | **제약은 유지**("KRX 수신은 tether 전달 생존의 증거가 아니다"는 여전히 참이고 재검증 오판 방지에 필요). **목적만** 갱신 |

<!-- /rid: R-CUT-18 -->

---

## 3. 검증

<!-- rid: R-CUT-12 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-12"></a>
### R-CUT-12

**검증 (실기기).** 리허설과 **같은 조작**으로 판정한다 — 조작이 다르면 결함이 숨는다.

1. **dispatcher 는 ON 인 채 tether publisher/trigger 만 침묵** + **무조작 90초**
   → 거래소 5행 **값 유지** / 재구독 정확히 1회 / 배너는 실패 확정 후에만
   (조용한 재구독과 배너 분리는 [R-CLI-11](ios-topic-state-machine.md#r-cli-11)).
   ⛔ 이 항목을 `TOPIC_DISPATCHER_ENABLED=false` 로 재현하면 **2번과 동시에 만족 불가**다
      (그건 `topics_disabled` → purge 경로다). 두 시나리오는 **다른 조작**이다.
2. **global dispatcher off + 재기동** → `topics_disabled` 수신 → 명시적 비활성
   ([R-CLI-12](ios-topic-state-machine.md#r-cli-12)) + purge
   ([R-CLI-13](ios-topic-state-machine.md#r-cli-13)) + **재시도 0** + 서버 재활성화 후 복구
3. 백그라운드 45초 초과 후 foreground → 즉시 재평가
4. **lease 15분 초과** 연결 유지 → 재인증 성공 시 지속 / 실패 시 클라 hard-expiry
   ([R-CLI-10](ios-topic-state-machine.md#r-cli-10)) 발화
5. 평일 장중 FX live merge + 주말 무발행이 **장애로 표시되지 않음**
6. 구버전 앱 legacy 무영향
7. dev 서버 flag **on/off 양쪽**

⚠️ 판정은 rc + `Executed N tests` + `TEST SUCCEEDED` **셋을 함께** 본다. Release 빌드로
`#if DEBUG` seam 누수도 확인한다.

<!-- relation: references target=R-CLI-10 -->
- references: [R-CLI-10](ios-topic-state-machine.md#r-cli-10)
<!-- relation: references target=R-CLI-11 -->
- references: [R-CLI-11](ios-topic-state-machine.md#r-cli-11)
<!-- relation: references target=R-CLI-12 -->
- references: [R-CLI-12](ios-topic-state-machine.md#r-cli-12)
<!-- relation: references target=R-CLI-13 -->
- references: [R-CLI-13](ios-topic-state-machine.md#r-cli-13)
<!-- /rid: R-CUT-12 -->

<!-- rid: R-CUT-13 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-13"></a>
### R-CUT-13

**테스트 매트릭스 — 보존할 사용자 행위 기준**

⛔ **삭제 예정 경로에 테스트를 달지 않는다.** `ExchangeRateResponse`·`onRatesReceived`·legacy cache
동작을 지금 고정하면 **곧 버릴 테스트**를 만드는 것이다. 고정할 것은 **cutover 후에도 참이어야 하는
사용자 행위**다 — 그 테스트는 replacement 를 검증하는 데 그대로 쓰인다.

| # | 보존할 행위 | 지금 쓸 수 있나 |
|---|---|---|
| B1 | topic cold-start 에서 **불필요한 blank/reflow 가 없다** | ✅ legacy cache를 seed해도 FX 미수신 표면이 비어 있고(`ios/FXiTests/TetherDataPathTests.swift:469-487`), 빈 tether snapshot도 legacy로 채우지 않는다(`:606-617`) |
| B2 | 오프라인/재기동에서 **topic last-known 으로 복원**된다 | ✅ USDT(`ios/FXiTests/TetherDataPathTests.swift:899-917`) · FX와 은행 알림(`:687-708`) · DXY(`:396-411`) cold-start 복원을 잠근다 |
| B3 | **legacy 프레임을 무시해도 앱 lifecycle 이 정상**이다 | ✅ legacy `rates/indices`를 넣어도 DXY topic owner가 유지되고 transport lifecycle은 connected로 수렴한다(`ios/FXiTests/TetherDataPathTests.swift:396-411`). architecture test도 legacy handler 복원을 거부한다(`ios/FXiTests/TopicMessageTests.swift:386-428`) |
| B4 | 무료 티어가 `ExchangeRate` **타입**으로 계속 디코드된다 | ✅ 무료 스냅샷의 `rate.entries`를 `ExchangeRate`로 디코드한다(`ios/FXiTests/TopicMessageTests.swift:494-520`) |
| B5 | 45초 무수신 → **조용한 재구독** → 실패 확정 시에만 배너 | ✅ **구현·잠금 완료** — 실패 확정이 degraded 로 수렴하는 것을 재검증 경로(`ios/FXiTests/TopicMessageTests.swift:4001` · `ios/FXiTests/TopicMessageTests.swift:6323`)와 최초 인도 경로(`ios/FXiTests/TopicMessageTests.swift:6363`)에서 각각 잠근다 |
| B6 | **DXY topic 이 live tip 을 공급**한다 | ✅ wire decode(`ios/FXiTests/TopicMessageTests.swift:342-353`) · REST bootstrap(`ios/FXiTests/TetherDataPathTests.swift:357-370`) · WS routing/거절 후 늦은 frame 차단(`ios/FXiTests/TopicMessageTests.swift:3910`)을 잠근다 |
| B7 | **은행 알림 현재가가 FX topic 을 쓴다** | ✅ FX topic disk last-known을 복원한 뒤 `rates(for:)`의 은행 현재가를 직접 단언한다(`ios/FXiTests/TetherDataPathTests.swift:687-708`) |
| B8 | `topics_disabled` → purge → **재활성화 시 복구** | ✅ **양쪽 잠김** — purge 는 `ios/FXiTests/TetherDataPathTests.swift:650`(fail-close) · `ios/FXiTests/TetherDataPathTests.swift:621`(메모리+디스크+파생 상태 동시 제거)가, **재활성화 복구**는 `ios/FXiTests/TopicMessageTests.swift:3933` 이 실 transport 로 잠근다 — 서버 OFF 중 desired 보존 + **자동 재구독 반복 없음**, 다음 연결 세대에서 재전송 → ACK + snapshot → healthy 수렴. ⚠️ 자동 회귀는 코드 복구만 덮는다 — 운영 flag 와 인증·UI 통합은 실기기 smoke 몫이다 |
| B9 | lease 만료 → **hard-expiry 재연결** | ✅ **구현 완료** — 같은 lease id 는 절대 만료를 연장하지 못하고 새 id 만 연장한다(`ios/FXiTests/TopicMessageTests.swift:6108`). 세대당 1회 강제 reconnect 불변식도 **같은 lease 세대의 두 topic 동시 만료**로 잠겼다(`ios/FXiTests/TopicMessageTests.swift:6389`) — 단일 topic 반복 검사로는 세대 가드를 제거해도 통과하므로 이 형태여야 판별된다 |

<!-- relation: references target=R-CLI-10 -->
- references: [R-CLI-10](ios-topic-state-machine.md#r-cli-10)
<!-- relation: references target=R-CLI-11 -->
- references: [R-CLI-11](ios-topic-state-machine.md#r-cli-11)
<!-- relation: references target=R-CLI-12 -->
- references: [R-CLI-12](ios-topic-state-machine.md#r-cli-12)
<!-- relation: references target=R-CLI-13 -->
- references: [R-CLI-13](ios-topic-state-machine.md#r-cli-13)
<!-- relation: references target=R-CUT-2 -->
- references: [R-CUT-2](#r-cut-2)
<!-- relation: references target=R-CUT-3 -->
- references: [R-CUT-3](#r-cut-3)
<!-- relation: references target=R-INV-4 -->
- references: [R-INV-4](../DECISIONS.md#r-inv-4)
<!-- /rid: R-CUT-13 -->

<!-- rid: R-CUT-20 -->
<!-- requirement-meta: disposition=proposed owner=None -->
<a id="r-cut-20"></a>
### R-CUT-20

[제안·결정 대기]

매트릭스의 B10 행. 파생 숫자 숨김 정책이 채택될 때에만 성립하므로 조건부다.

| # | 보존할 행위 | 지금 쓸 수 있나 |
|---|---|---|
| B10 | 김프는 **확정된 전달 실패에서만** 숨는다(정상 sparse 에선 보인다) | 🔶 파생 숫자 숨김 정책 구현과 함께 |

<!-- relation: conditional_references target=R-DEC-2 -->
- conditional_references: [R-DEC-2](../DECISIONS.md#r-dec-2)
<!-- /rid: R-CUT-20 -->

<!-- rid: R-CUT-21 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-21"></a>
### R-CUT-21

당시 결론대로 B2·B3는 A1([R-CUT-2](#r-cut-2), `AppState` 대체)와 같은 `0f2a3f8`에
들어갔다. replacement 행동을 먼저 정의한 뒤 구현과 함께 green으로 만들었고, 현재 매트릭스
B1~B9는 자동 회귀로 잠겼다. 이 완료는 실기기 smoke([R-CUT-12](#r-cut-12))나 Release arming을
대체하지 않는다.

<!-- relation: references target=R-CUT-2 -->
- references: [R-CUT-2](#r-cut-2)
<!-- /rid: R-CUT-21 -->

---

## 4. arming 차단 게이트 중 이 문서가 소유하는 항목

<!-- rid: R-CUT-14 -->
<!-- requirement-meta: disposition=proposed owner=None -->
<a id="r-cut-14"></a>
### R-CUT-14

[제안·결정 대기]

**arming 차단 게이트 2 — safety-stop 리허설 실측.**
`topics_disabled` → purge → 재활성화 복구를 **실기기로 돌려 본다**.

- `topics_disabled` 수신 시의 **명시적 비활성 전환**은 [R-CLI-12](ios-topic-state-machine.md#r-cli-12) 가,
  지워야 할 **purge 범위**(메모리 + `cached_topic_rates` 디스크 + 파생 상태)는
  [R-CLI-13](ios-topic-state-machine.md#r-cli-13) 이 정의한다. 리허설은 그 둘을 실기기에서 확인한다.
- 이 게이트는 파생 숫자(김프/비교 spread) 숨김 정책 제안([R-DEC-2](../DECISIONS.md#r-dec-2))의
  **조건부 수용**을 성립시키는 세 게이트 중 하나이며, 그중 이 문서가 소유하는 항목이다.

⛔ 셋 중 하나라도 미루면 수용으로 보지 않는다 — 따라서 이 리허설 실측은 arming 전에 끝나야 한다.

<!-- relation: references target=R-CLI-12 -->
- references: [R-CLI-12](ios-topic-state-machine.md#r-cli-12)
<!-- relation: references target=R-CLI-13 -->
- references: [R-CLI-13](ios-topic-state-machine.md#r-cli-13)
<!-- /rid: R-CUT-14 -->
