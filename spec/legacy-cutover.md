# legacy cutover — 삭제 범위 · 문서 정정 · 테스트 · 순서

- 책임: 삭제 범위 · 문서 정정 · 테스트 · 순서
- 상태: Draft — 구현 착수 전 합의 대상
- 코드 근거 기준일: 2026-08-09
- server 기준 commit: `49fae18c82f7a92bda3d27938c1dc8566b479931`
- iOS 기준 commit: `8aadc2fb66be926a809d6e1bc5dff42951f15a7a`
- archive SHA: `cde1d2ca3e714733776e1b0d7e821a542e1f8d183cb2951bef8c93fb444d9814`
- manifest SHA: `3a56ce03c3dbb68d7489c9fedbaad898d5ad2a0fd752bfd2aebcffc68a8ccf87`
- baseline SHA: `fb84a670ef5d7eaab51a2091919b00d9b3ff5da08c04366d07d21968ef8ad4bc`
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

**삭제 범위는 두 함수가 아니다** (2026-08-09 전수 inventory).

초안은 `usdtDisplayState` 의 legacy 분기와 `baseRates(for:)` 두 곳만 적었다. **크게 과소평가였다.**

⚠️ **`usdtDisplayState` 의 최외곽 게이트가 `if RealtimeV2Config.isTetherTopicEnabled` 이고 그 else 가
`sourceRates()` 다**(baseline F13). Release 기본값은 topic **OFF** 다(baseline F14) — 그러므로 arming
없이 출시하면 테더 탭은 간헐이 아니라 **상시 legacy reference subset** 으로 간다. 그 subset 은
SourceRegistry 와 legacy 가 겹치는 investing/kb/hana 후보뿐이며 사용자 visibility 에 따라 더 줄 수
있다(`ios/FXi/Models/RateSource.swift:32-91` ·
`ios/FXi/Services/SourcePreferenceManager.swift:133-161`). 즉 거래소 5 + KRX 는 표시되지 않는다.
legacy 분기 제거는 곧 **arming 이 출시 전제**임을 뜻한다.

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

**(A) 단순 삭제로 끝나지 않는 것 — 각각 별도 작업이다**

| # | 지점 | 왜 어려운가 |
|---|---|---|
| A1 | **`AppState` enum 자체** — `.connected(rates:)` / `.offline(cachedRates:)` / `.refreshingCached(cachedRates:)` | legacy 배열이 **enum payload** 이고 `AppState.rates` 가 모든 소비의 단일 관문이다(`ios/FXi/Models/AppState.swift:11-35`). 지우면 앱 **최상위 화면 라우팅**(loading/error/tab — `ios/FXi/ContentView.swift:116-129`)과 배너 3종(`ios/FXi/Views/Components/OfflineBanner.swift:75-118`)의 판정 근거가 통째로 사라진다. topic 쪽엔 전역 lifecycle 상태 개념이 없고 `tetherReceived`/`fxReceivedAssets`(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:27-47`)/`freshFxAssets`(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:224`) 로 흩어져 있다 → **앱 수명주기 상태머신 재설계**다 |

<!-- /rid: R-CUT-2 -->

<!-- rid: R-CUT-3 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-3"></a>
### R-CUT-3

| # | 지점 | 왜 어려운가 |
|---|---|---|
| A2 | **은행 가격알림 시트** — `AlertAddSheet.rate(for:)`(`ios/FXi/Views/Components/AlertAddSheet.swift:96-103`) → `rateViewModel.rates(for:)` → `appState.rates` **직결**(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:504-509`) | `baseRates`(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:548-564`) 를 **안 거친다**. 즉 fx topic 이 켜져 있어도 이 시트는 **topic 값을 못 본다**(오늘도 그렇다 — 탭은 topic, 알림 시트는 legacy). 배선 교체가 아니라 **topic 경로 신설**이다 |

<!-- /rid: R-CUT-3 -->

<!-- rid: R-CUT-4 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-4"></a>
### R-CUT-4

| # | 지점 | 왜 어려운가 |
|---|---|---|
| A3 | **WS `handleLegacyRatesMessage` 3-in-1 envelope** — `rates` + `indices`(DXY) + `graph_buckets` 동거(`ios/FXi/Services/WebSocketService.swift:1066-1084`) | `rates` 만 지워도 `graph_buckets` callback 소유자(`GraphViewModel` — `ios/FXi/ViewModels/GraphViewModel.swift:553-560`)가 남아 디코드를 못 지운다. ⚠️ **단 서버 계약 변경은 불필요** — 그 VM 은 런타임에 생성·주입되고 callback/observer 를 설치하지만(`ios/FXi/FXiApp.swift:24-28` · `ios/FXi/ViewModels/GraphViewModel.swift:86-101`), premium `ContentView` 는 legacy graph 를 시작하거나 mount 하지 않는다(`ios/FXi/ContentView.swift:89-96`). `RootView` 에 남은 실제 소비는 로그아웃/구독 전환의 `stop()`뿐이다(`ios/FXi/FXiApp.swift:134-140` · `ios/FXi/FXiApp.swift:178-184`). **클라에서 VM·callback·환경 주입과 stop 배선을 함께 제거**하면 끝이고, 서버는 구버전용으로 `graph_buckets` 를 계속 보내면 된다 |

<!-- /rid: R-CUT-4 -->

<!-- rid: R-CUT-5 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-5"></a>
### R-CUT-5

| # | 지점 | 왜 어려운가 |
|---|---|---|
| A4 | `usdtDisplayState` 5분기 사다리(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:623-665`) | 각 분기가 서로 다른 실측 버그(행 reflow / blank flash / MODE 2 frozen)의 대응이고 근거가 주석에 박혀 있다. 마지막 두 분기만 떼면 `isOffline`/`isRefreshingCached` 조건이 함께 무의미해진다 |

<!-- /rid: R-CUT-5 -->

<!-- rid: R-CUT-6 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-6"></a>
### R-CUT-6

**(B) 선행 정리 — cutover 표면을 먼저 줄인다 (저위험, 순이익)**

사용처가 **0** 인 데드 코드: `RateGraphView` 전체(사용처=자기 `#Preview` —
`ios/FXi/Views/RateGraphView.swift:1446-1458`) / `PeriodTabBar`(`ios/FXi/Views/Components/PeriodTabBar.swift:11-58`) /
`WebSocketService` 의 `latestRates`·`lastUpdated` 사본(`ios/FXi/Services/WebSocketService.swift:37`) /
`WebSocketMessage.updateGraphCache`(`ios/FXi/Models/WebSocketMessage.swift:33`) /
`ExchangeRateViewModel.referenceRate(for:)`·`rateRange(for:)`(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:511-519`).
여기서 0은 고정 iOS commit 의 production tree 전수 검색 결과다. 각 심볼의 정의/Preview 를 제외한
호출을 `git grep` 으로 확인했고 `RateGraphView`·`PeriodTabBar` 는 자기 Preview 외 0건,
`updateGraphCache`·두 ViewModel helper 는 호출 0건이었다.
→ **cutover 전에 지운다.** 지우고 나면 남는 소비처가 줄어 나머지 작업이 작아진다.
⚠️ **"위험 0"은 과장이다** — `RateGraphView`/`PeriodTabBar` 는 Preview 외 사용처가 없지만,
`GraphViewModel` 은 **런타임에 생성되어**(`ios/FXi/FXiApp.swift:24-28`) **legacy graph callback 과
observer 를 설치**한다(`ios/FXi/ViewModels/GraphViewModel.swift:86-101`).
저위험이되 **동작 변화 0 은 아니다** → Debug/Release 양쪽 빌드 + callback 소유권 확인이 필요하다.

<!-- /rid: R-CUT-6 -->

<!-- rid: R-CUT-7 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-7"></a>
### R-CUT-7

**(C) ⛔ 테스트 커버리지 공백 — 지금 상태로 제거하면 _맹목_ 이다**

정확히는 **envelope 1차 디코드 테스트는 있다**(`TopicMessageTests.testEnvelopeDecodes_legacyRates_topicAbsent`
— `ios/FXiTests/TopicMessageTests.swift:18-24`).
없는 것은 **사용자 행위** 커버리지다 — `ExchangeRateResponse`/`IndicesPayload`/`DxyLiveTick` 를
참조하는 테스트가 **0건**(부정 사실이라 행 번호가 없다 — 명령·범위·결과로 단다:
`git grep -n "ExchangeRateResponse\|IndicesPayload\|DxyLiveTick" <pinned ios> -- FXiTests` → 0건)이라
DXY 적용·callback·legacy cache 동작은 제거해도 신호가 없다.

⛔ **그렇다고 "제거 대상 경로"에 테스트를 다는 것은 틀렸다** — 곧 버릴 테스트를 만드는 셈이다.
→ **보존해야 할 사용자 행위**를 먼저 테스트한다(테스트 매트릭스는 [R-CUT-13](#r-cut-13)).
그 테스트는 cutover 후에도 산다.

<!-- /rid: R-CUT-7 -->

<!-- rid: R-CUT-8 -->
<!-- requirement-meta: disposition=active owner=CUT -->
<a id="r-cut-8"></a>
### R-CUT-8

**(D) 타입은 남긴다**

`ExchangeRate` **struct 자체는 무료 티어 스냅샷이 verbatim 디코드**해 재사용한다
(`ios/FXi/Models/FreeSnapshotModels.swift:27-31`).
제거 대상은 `ExchangeRateResponse` / `Metadata` / `IndicesPayload` 이지 `ExchangeRate` 가 아니다.

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
| `WebSocketService.swift` ~822 | "`performTopicCommand` 의 `catch` 는 `forgetPending` 만 하고 재시도하지 않는다 … 재연결 전까지 조용히 미구독" | catch 가 **bounded retry 한다**(`shouldRetryCommandFailure` denylist + 최대 3회 — baseline F5). 그 주석이 열어 둔 항목이 **이미 구현됐는데 주석만 안 고쳐졌다**. 실제 공백은 **재시도 소진 이후** |
| `TOPIC_V2_RELEASE_RUNBOOK.md` MODE 2 | "45s 초과 시 자동으로 legacy 표시"(합격 기준) | 불변식 [R-INV-1](../DECISIONS.md#r-inv-1) 로 폐기 |
| 같은 문서 rollback 절 | "legacy 가 graceful degrade 하므로 서비스 중단 아님" | 테더는 거래소 5 + KRX 가 **전부 사라진다**. degrade 가 아니라 핵심 기능 상실 |
| 같은 문서 (그래프) | "FX 그래프 live-tail 은 legacy 읽고 테이블은 topic" | 반대다 — `GraphV2LiveBridge` 가 `fxTopicRates` 를 읽는다 |
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
| B1 | topic cold-start 에서 **불필요한 blank/reflow 가 없다** | ✅ **이미 있다** — `testUsdtDisplay_coldStart_returnsEmpty_noCachedNoPartialLegacy`(`ios/FXiTests/TetherDataPathTests.swift:592-611`). 감사만 |
| B2 | 오프라인/재기동에서 **topic last-known 으로 복원**된다 | 🔶 **A1([R-CUT-2](#r-cut-2)) 과 함께** — 현재는 cache roundtrip·저장만 있고(`ios/FXiTests/TetherDataPathTests.swift:554-580`) *재기동 후 표시*는 미검증 |
| B3 | **legacy 프레임을 무시해도 앱 lifecycle 이 정상**이다 | 🔶 **A1([R-CUT-2](#r-cut-2)) 과 함께** — 현행 코드에선 **통과 불가**(`onRatesReceived` 가 `appState = .connected` 를 직접 세팅 — `ios/FXi/ViewModels/ExchangeRateViewModel.swift:942-945`) |
| B4 | 무료 티어가 `ExchangeRate` **타입**으로 계속 디코드된다 | ✅ **이미 있다** — 무료 스냅샷 디코드 테스트(`ios/FXiTests/TopicMessageTests.swift:513-522`)가 잠근다. 감사만 |
| B5 | 45초 무수신 → **조용한 재구독** → 실패 확정 시에만 배너 | 🔶 [R-CLI-11](ios-topic-state-machine.md#r-cli-11) 구현과 함께 |
| B6 | **DXY topic 이 live tip 을 공급**한다 | 🔶 `dxy:spot`([R-INV-4](../DECISIONS.md#r-inv-4)) 구현과 함께 |
| B7 | **은행 알림 현재가가 FX topic 을 쓴다** | 🔶 A2([R-CUT-3](#r-cut-3)) 신설과 함께 (지금은 legacy 만 본다 = 현행 버그; `ios/FXi/Views/Components/AlertAddSheet.swift:96-103` · `ios/FXi/ViewModels/ExchangeRateViewModel.swift:504-509`) |
| B8 | `topics_disabled` → purge → **재활성화 시 복구** | 🔶 [R-CLI-12](ios-topic-state-machine.md#r-cli-12)·[R-CLI-13](ios-topic-state-machine.md#r-cli-13) 구현과 함께 |
| B9 | lease 만료 → **hard-expiry 재연결** | 🔶 [R-CLI-10](ios-topic-state-machine.md#r-cli-10) 구현과 함께 |

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

⛔ **정정: 지금 새로 쓸 테스트는 사실상 없다.** B1·B4 는 **이미 잠겨 있어 감사만** 하면 되고,
B1 은 `ios/FXiTests/TetherDataPathTests.swift:592-611`, B4 는
`ios/FXiTests/TopicMessageTests.swift:513-522` 가 잠근다.
B2·B3 는 **A1([R-CUT-2](#r-cut-2), `AppState` 대체) 구현과 같은 커밋**에 들어가야 한다 —
특히 B3 는 legacy 가 `AppState` 를 직접 갱신하는 현행 구조
(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:942-945`)에서 **원리적으로 통과할 수 없다**.
→ 즉 **"테스트부터 시작"이라는 착수 경로는 없다.** 계약을 닫는 것이 실제 다음 단계다.

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
