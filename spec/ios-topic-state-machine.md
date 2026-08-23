# iOS topic 상태기계 — 클라이언트 계약

- 책임: 클라이언트 상태기계 · 재시도 · 재검증
- 상태: Draft — 구현 착수 전 합의 대상
- 코드 근거 기준일: 2026-08-09
- server 기준 commit: `a09e435fc797297cef812a8e51432e39d21dca92`
- iOS 기준 commit: `029d77428a2d5a49ba49bd8cbf114ba445d10c70`
- archive SHA: `cde1d2ca3e714733776e1b0d7e821a542e1f8d183cb2951bef8c93fb444d9814`
- manifest SHA: `0aca0354d225b76a57d7d2f68cc10ced0e2115194a1c38c342e000dd130eeff1`
- baseline SHA: `4cc944e333789fb4a2ff08217d2dbd3469f29c9f09826946bb1776d43c27d708`
- 검증: `python3 scripts/topic_migration_manifest.py preflight`

> 이 문서는 `TOPIC_ONLY_DELIVERY_CONTRACT.archive.md` 에서 **클라이언트 상태기계 · 재시도 · 재검증**
> 책임만 떼어낸 것이다. 나머지 책임의 소유 문서는 아래와 같다 —
> 불변식 · 결정 · arming 게이트는 `DECISIONS.md` ADR-041,
> 서버 build · ack · close 계약은 [topic-snapshot-handoff.md](topic-snapshot-handoff.md),
> 삭제 범위 · 문서 정정 · 테스트 · 순서는 [legacy-cutover.md](legacy-cutover.md),
> jitter · single-flight · bounded wait 는 [revalidation-and-load.md](revalidation-and-load.md),
> publisher health · SLO 는 [publisher-health-slo.md](publisher-health-slo.md).
> 검증된 코드 사실의 정본은 [topic-only-baseline-facts.md](topic-only-baseline-facts.md) 다.
>
> ⚠️ 분할 후에는 원문의 `§` 번호가 존재하지 않는다. 다른 요구사항을 가리킬 때는 **RID 와 링크**로만 가리킨다.

---

## 1. 시간 deadline — tether 하나만

<!-- rid: R-CLI-1 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-1"></a>
### R-CLI-1 — 시간 deadline 은 tether 하나만 유지한다

FX/KRX 에 임의의 시간 임계를 만들지 않는다. 대신 **연결 상태 + ack + lease** 로 판단한다.

⚠️ **이 선택에는 전제가 붙는다.** FX 침묵 감지가 아래 셋에 의존하게 된다 —
(a) 연결 상태, (b) lease 갱신([R-CLI-10](#r-cli-10) · [R-CLI-16](#r-cli-16)),
(c) 서버 close-on-send-failure([R-HAND-6](topic-snapshot-handoff.md#r-hand-6)).
**셋 중 하나라도 빠지면 FX 는 그 축의 감지 수단이 0 이 된다.** 계약으로 함께 잠근다.

⚠️ **이 셋이 덮는 것은 transport / auth / subscription 이상까지다.** *"FX 침묵을 감지한다"* 로
넓게 읽으면 안 된다 — [R-HLT-1](publisher-health-slo.md#r-hlt-1) 대로 **정상 snapshot 이후의
publisher 사망은 클라가 판별할 수 없다**.

**관계**

<!-- relation: references target=R-CLI-10 -->
- references: [R-CLI-10](#r-cli-10)
<!-- relation: references target=R-CLI-16 -->
- references: [R-CLI-16](#r-cli-16)
<!-- relation: references target=R-HAND-6 -->
- references: [R-HAND-6](topic-snapshot-handoff.md#r-hand-6)
<!-- relation: references target=R-HLT-1 -->
- references: [R-HLT-1](publisher-health-slo.md#r-hlt-1)
<!-- /rid: R-CLI-1 -->

---

## 2. 최소 계약에서 클라가 지는 몫

서버 build · ack · close 의 최소 계약은 [R-HAND-2](topic-snapshot-handoff.md#r-hand-2) 가 소유한다.
그 순서의 마지막 항목과, 네 deadline 중 둘이 클라 몫이다.

<!-- rid: R-CLI-21 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-21"></a>
### R-CLI-21 — 클라는 요청 송신 시점의 수신 세대로 ack 전후 프레임을 모두 인정한다

서버 최소 계약([R-HAND-2](topic-snapshot-handoff.md#r-hand-2)) 순서의 **7번 항목이 클라 몫**이다.

```text
7. 클라는 **요청 송신 시점의 수신 세대**로 ack 전후 live frame 을 모두 인정한다
```

**관계**

<!-- relation: references target=R-HAND-2 -->
- references: [R-HAND-2](topic-snapshot-handoff.md#r-hand-2)
<!-- /rid: R-CLI-21 -->

### 2.1 deadline 넷 중 클라가 소유하는 둘

<!-- rid: R-CLI-17 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-17"></a>
### R-CLI-17 — 클라 ACK deadline (현행 20초)

| deadline | 대상 | 소유 | 짝 |
|---|---|---|---|
| **클라 ACK deadline** (현행 20초) | 요청 → ack | 클라(`topicRequestTimeoutTasks`) | ① |

짝 ①의 서버 쪽 상대는 [R-HAND-15](topic-snapshot-handoff.md#r-hand-15) —
**서버 ACK 예산 < 클라 ACK deadline** 이어야 한다.

**관계**

<!-- relation: references target=R-HAND-15 -->
- references: [R-HAND-15](topic-snapshot-handoff.md#r-hand-15)
<!-- /rid: R-CLI-17 -->

<!-- rid: R-CLI-20 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-20"></a>
### R-CLI-20 — 클라 initial-delivery deadline (신규)

| deadline | 대상 | 소유 | 짝 |
|---|---|---|---|
| **클라 initial-delivery deadline** (신규) | 요청 송신 시점 수신 세대 → 첫 프레임 | 클라([R-CLI-3](#r-cli-3)) | ② |

짝 ②는 서버 snapshot 총 예산과 맞물린다. ⛔ **단순 대소 비교가 아니다 — 두 시계의 _원점이 다르다_**
([R-HAND-17](topic-snapshot-handoff.md#r-hand-17)). 성립해야 하는 부등식은
[R-HAND-18](topic-snapshot-handoff.md#r-hand-18) 의
`(서버 ACK 경과) + (서버 snapshot 예산) + (전송 여유) < 클라 initial-delivery deadline` 이다.

**관계**

<!-- relation: references target=R-HAND-17 -->
- references: [R-HAND-17](topic-snapshot-handoff.md#r-hand-17)
<!-- relation: references target=R-HAND-18 -->
- references: [R-HAND-18](topic-snapshot-handoff.md#r-hand-18)
<!-- /rid: R-CLI-20 -->

### 2.2 두 deadline 의 단일 소유자 — 공용 arbiter

<!-- rid: R-CLI-2 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-2"></a>
### R-CLI-2 — watchdog 둘이 아니라 공용 arbiter 하나

⛔ **초안의 근거는 틀렸다.** *"ACK 가 흐름상 먼저"* 라고 썼는데,
**register 가 ack send 보다 먼저라 live frame 이 ack 보다 먼저 도착할 수 있다**([R-CLI-3](#r-cli-3)).
ACK deadline 을 먼저 두는 진짜 이유는 프레임 도착 순서가 아니라 **control-plane 완료를 먼저
판정하기 위해서**다.

✅ **세 경우의 단일 소유자는 공용 arbiter 하나로 구현됐다.** 한 요청이 control/delivery deadline을
함께 저장하고, ACK 전에는 control phase 하나만, ACK 뒤에는 delivery phase 하나만 무장한다
(`ios/FXi/Services/WebSocketService.swift:535-653` ·
`ios/FXi/Services/WebSocketService.swift:806-823`). 따라서 독립 watchdog `Task` 둘의 재개 순서에
의존하지 않고 아래 전이표를 한 소유자가 집행한다.

⛔ **arbiter 는 두 가지를 _분리해서_ 든다** — 섞으면 ACK 유실이 **이미 받은 데이터 증거를 지운다**:

| | 범위 | 의미 |
|---|---|---|
| `controlState` | **배치 요청 단위** | 요청이 서버에 확인됐는가(ack) |
| `deliveryState[topic]` | **topic 단위** | 그 topic 이 요청 송신 세대 이후 유효 frame 을 받았는가 |

⛔ **subscribe 는 배치다**(baseline F4) — 한 요청에 여러 topic 이 실린다. 따라서 구조는
`request.controlState + [Topic: DeliveryState]` 이고, **한 topic 의 frame 이 배치 전체의 수신 성공으로
처리되면 안 된다**. 재검증([R-CLI-11](#r-cli-11))도 **미수신 topic 만** 범위로 삼는다.

| 조건 | arbiter 전이 |
|---|---|
| ACK 미수신 & ACK deadline 경과 | **먼저 요청 소유권을 회수**(`takePending` 패턴) → 늦게 온 ACK 는 무시 → 재구독. ⛔ **이미 병합한 값과 `receiveGeneration` 은 보존**한다 |
| ACK 수신 & delivery deadline 경과 & 세대 증가 없음 | 재구독([R-CLI-11](#r-cli-11)) |
| **둘 다 경과**(예: background 복귀) | **ACK 를 먼저 처리** — control-plane 판정이 앞선다 |

⚠️ **반례가 실재한다**: ack 이 유실됐지만 **유효 frame 은 먼저 도착**한 경우(register→ack race,
[R-CLI-3](#r-cli-3)). 이때 delivery 판정을 무효화하면 **진짜 데이터를 받고도 못 받은 것으로 처리**한다.

⚠️ 그리고 **`ACK deadline < delivery deadline` 을 상수 수준에서 강제**한다(assert). 값이 뒤집히면
위 전이표가 무의미해진다.

**관계**

<!-- relation: references target=R-CLI-3 -->
- references: [R-CLI-3](#r-cli-3)
<!-- relation: references target=R-CUT-18 -->
- references: [R-CUT-18](legacy-cutover.md#r-cut-18)
<!-- /rid: R-CLI-2 -->

### 2.3 deadline 의 기준선

<!-- rid: R-CLI-3 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-3"></a>
### R-CLI-3 — 기준선은 ack 이 아니라 요청 송신 시점이다

**클라 — deadline 의 기준선은 ack 이 아니라 _요청 송신 시점_ 이다.**

⚠️ `registry.register(...)` 가 **ack send 보다 먼저** 실행되고 outbound 직렬화가 없다(baseline D5) →
그 사이 publish 가 발화하면 **live frame 이 ack 보다 먼저 도착**할 수 있다.
"ack 이후 프레임만 수신 성공"으로 세면 **조용한 FX 에서 정상 데이터를 받고도 재연결**한다.

→ 요청을 보내는 시점에 topic 별 **수신 세대(카운터)를 캡처**하고, 그 이후 도착한 프레임은
ack 전후 무관하게 **전부 인정**한다. 기한 내 0건이면 재구독 → 실패 시 재연결
(사다리는 [R-CLI-11](#r-cli-11)).
<!-- /rid: R-CLI-3 -->

### 2.4 monitor 재무장 — 지금 어기면 2026-08-08 결함의 재생산이다

<!-- rid: R-CLI-4 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-4"></a>
### R-CLI-4 — 시간 deadline 이 둘 이상이 되면 monitor 재무장이 필수 동반이다

⚠️ **만약 나중에 시간 deadline 을 둘 이상 두게 되면**, `startFreshnessMonitorIfNeeded` 는
`guard freshnessMonitor == nil` 이라 **이미 도는 monitor 를 재무장하지 않는다**
(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:334-335`). 균일 임계에서는
새 deadline 이 항상 기존보다 뒤라 안전하지만, 임계가 갈리면 더 이른 deadline 이 생겨
**monitor 가 자면서 지나친다** = 2026-08-08 결함의 재생산. 그때는 *"새 최근접 deadline 이 현재
수면 목표보다 이르면 재무장"* 이 **필수 동반**이다.
<!-- /rid: R-CLI-4 -->

---

## 3. 거부 사유별 전이표

<!-- rid: R-CLI-5 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-5"></a>
### R-CLI-5 — last-known 은 전송 장애에만 맞는 답이다

**last-known 은 전송 장애에만 맞는 답이다.** 인가 거부에 last-known 을 보여주면 그게 곧 페이월
우회다. 사유를 구분하지 않으면 legacy 를 걷어내도 인가 정책이 다시 섞인다.

아래 사유는 **실제 코드에서 확인한 것만** 적는다(지어낸 코드 없음).
<!-- /rid: R-CLI-5 -->

### 3.1 전이표

<!-- rid: R-CLI-6 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-6"></a>
### R-CLI-6 — 사유별 화면 · 데이터 · 구독 의도 · 재시도

**프레임 아닌 조건**

| 사유 | 출처 | 화면 | 데이터 | 구독 의도 | 재시도 |
|---|---|---|---|---|---|
| **무수신(45s)** — tether | 클라 타이머 | 변화 없음(조용히 재검증, [R-CLI-11](#r-cli-11)) | last-known 유지 | 보존 | 즉시 재구독 1회 |
| **연결 끊김** | `connectionState` | "연결 끊김" + 재시도 | last-known 유지 | 보존 | 기존 재연결 |
| **transport close 1009** | 16KB 초과 | "연결 끊김" 경로와 동일 | last-known 유지 | 보존 | 기존 재연결 |

**전체-요청 (모든 요청 topic 에 적용)**

| 사유 | 화면 | 데이터 | 구독 의도 | 재시도 |
|---|---|---|---|---|
| `temporarily_unavailable` | 변화 없음 | last-known 유지 | 보존 | **재시도**(클라 allowlist 유일) |
| `invalid_token` — **동일 UID 토큰 refresh 진행 중** | 변화 없음 | last-known 유지 | 보존 | refresh 후 재요청 |
| `invalid_token` — **refresh 실패 / revoked / disabled 확정** | 재인증 요구 | **premium·KRX 값 clear** (인가 격리) | 보존 | ❌ 재인증 전까지 |
| `invalid_request` | 진단 로그만 | 변화 없음 | — | ❌ terminal(프로그래밍 오류) |
| `request_too_large` | 위 1009 행 참조 | — | — | 실제 운영 경로는 **frame 이 아니라 close** |

⚠️ **`invalid_token` 을 한 줄로 두면 [R-CLI-5](#r-cli-5) 의 원칙 자체를 위반한다** — 서버의
`invalid_token` 에는 만료뿐 아니라 **revoked / disabled user** 가 포함되고(`app/main.py:3344-3352`)
클라는 셋을 구별할 수 없다.
확정 실패에 last-known 을 유지하면 **인증이 끝난 뒤에도 과거 premium/KRX 값을 무기한 표시**하게 된다.

**per-topic (그 topic 만)**

| 사유 | 화면 | 데이터 | 구독 의도 | 재시도 |
|---|---|---|---|---|
| `topics_disabled` | **명시적 비활성 안내** | 전 topic surface purge([R-CLI-12](#r-cli-12) · [R-CLI-13](#r-cli-13)) | **보존** | 폭풍만 중단, 재연결·foreground·수동에서 복구 |
| `topic_unavailable` | 그 topic 만 unavailable | 그 topic 값 제거 | **보존** | lifecycle 기반 복구 |
| `unknown_topic` | 그 topic 만 숨김 | 그 topic 값 제거 | **보존** | `nextConnection` |
| `premium_required` | 무료 화면 | **인증된 v2 hourly 로 전환** | 복구 집합에 보존 | 자격 변화 시 재요청 |
| `krx_entitlement_required` | KRX **즉시 숨김** | KRX 행 제거 | **`confirmed` 만 제거, `desired` 보존** | `entitlementChange` |

**클라에 이미 있는 것**: `SubscriptionError.isTerminal`(invalid_token/invalid_request/request_too_large),
`isRetryable`(temporarily_unavailable), `premium_required` 복구 집합.
✅ **다섯 거부 코드가 모두 배선됐다.** `handleSubscriptionAck` 가 `topicRejection(from:)` 으로
`topics_disabled`·`topic_unavailable`·`unknown_topic`·`premium_required`·`krx_entitlement_required`
를 각각 `TopicRejectionReason` 으로 옮기고
(`ios/FXi/Services/WebSocketService.swift:1102-1111`), **이번 배치로 보낸 topic 에 한해**
`topicStateStore.applyAck(rejections:)` 로 per-topic 기록한다
(`ios/FXi/Services/WebSocketService.swift:1053-1066` ·
`ios/FXi/Models/TopicSubscriptionState.swift:165-186`). 기록된 사유는 접근 상태
(`accessState(authResolution:)` — `ios/FXi/Models/TopicSubscriptionState.swift:72-86`)와
재시도 trigger(`rejectionRetryTriggers(for:)` — 같은 파일 `117-137`)에서 위 표대로 서로 다르게
갈라지므로, `topic_unavailable`·`unknown_topic`·`topics_disabled` 도 더는 로그만 남기지 않는다.
그 위에 **추가로** 전용 콜백을 갖는 것은 `premium_required`(`onPremiumAccessRejected`)와
`krx_entitlement_required`(`onKrxAccessRejected`) 둘뿐이다
(`ios/FXi/Services/WebSocketService.swift:1083-1088`).
`SubscriptionError.isTerminal`/`isRetryable` 은 그대로다
(`ios/FXi/Models/TopicMessage.swift:237-259`).

⚠️ **`topics_disabled` 를 "영구 중단"으로 처리하지 않는다.** 재시도 폭풍만 멈추고 **구독 의도는
보존**해야 서버 재활성화 후 복구된다.

⚠️ **`premium_required` 가 FX/USDT 에서 발화하는 것은 `WS_TOPIC_AUTH_STAGE` 가
`enforce_authenticated_premium` 일 때뿐이다.** 코드 기본값 `compatibility` 와
`reject_anonymous_fx` 에서는 식별된 FX/USDT 가 identity-only 라 그 행이 나오지 않는다. 클라는
**두 경우를 모두** 다뤄야 한다
(stage 는 서버 env 이고 클라는 그것을 모른다). 근거: `app/topic_policy.py:278-323`
(stage 별 partition) · `app/topic_dispatcher.py:874-915`(per-topic 거부 코드).
상태 서술은 `DECISIONS.md` ADR-041
[R-INV-2](../DECISIONS.md#r-inv-2), 선행조건은 [R-HAND-11](topic-snapshot-handoff.md#r-hand-11).
전이표는 Stage A 완료를 전제로 한다.

**관계**

<!-- relation: references target=R-HAND-11 -->
- references: [R-HAND-11](topic-snapshot-handoff.md#r-hand-11)
<!-- /rid: R-CLI-6 -->

### 3.2 구독 상태 — canonical 저장 필드 + 파생 필드

<!-- rid: R-CLI-7 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-7"></a>
### R-CLI-7 — canonical topic 상태는 두 compatibility projection보다 넓다

`subscribedTopics`/`confirmedTopics`는 기존 호출부를 위한 compatibility projection으로 남아 있다.
canonical source는 `TopicSubscriptionSnapshot`/`TopicSubscriptionState`이며, 의도·서버 확인·수신
세대·delivery·거부 사유를 저장하고 접근 상태와 재시도 trigger를 파생한다
(`ios/FXi/Models/TopicSubscriptionState.swift:64-137`).

| 축 | 의미 |
|---|---|
| `desired` | 앱이 원하는 topic — **거부 사유가 무엇이든 함부로 비우지 않는다** |
| `confirmed` | 서버 ack 의 `active_subscriptions` — 연결의 최종 상태 |
| `rejectionReason` | 마지막 거부 사유 — 화면 분기의 입력 |
| `retryPolicy` | **무엇이 재시도 자격을 다시 여는가** (아래) |
| **`deliveryState`** | **구독이 아니라 _데이터가 오고 있는가_** (아래) |
| `receiveGeneration` | 요청 송신 시점에 캡처하는 topic 별 수신 카운터([R-CLI-3](#r-cli-3)) |

⛔ **`retryEligible` 을 boolean 으로 두면 안 된다** — 표의 사유마다 복구 트리거가 다르다
(`topics_disabled`=재연결·foreground·수동 / `topic_unavailable`=lifecycle / `unknown_topic`=다음 연결 세대).
foreground 는 **같은 연결 세대 안에서** 일어날 수 있으므로 boolean 으로는 모호하다.
→ **단일 enum 도 부족하다** — `topics_disabled` 는 reconnect·foreground·manual 을 **모두** 열어야 하고
`serverDelay` 는 **지연값**을 함께 지녀야 한다.
→ `Set<RetryTrigger>` 또는 associated value 를 가진 enum:
`RetryTrigger ∈ { serverDelay(seconds), authChange, entitlementChange, nextConnection, foreground, manual }`.
빈 집합 = 재시도 없음.

✅ 위 저장/파생 구조와 복수 trigger 집합은 현재 구현에 반영됐다. `desired`·`confirmed`·
`receiveGeneration`·`deliveryState`·`rejection`은 canonical 저장이고, `accessState`와
`rejectionRetryTriggers`는 그 값에서 계산된다
(`ios/FXi/Models/TopicSubscriptionState.swift:64-137`).

**관계**

<!-- relation: references target=R-CLI-3 -->
- references: [R-CLI-3](#r-cli-3)
<!-- /rid: R-CLI-7 -->

<!-- rid: R-CLI-8 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-8"></a>
### R-CLI-8 — `deliveryState` 를 별도 축으로 두고, 저장과 파생을 나눈다

⛔ **`desired`/`confirmed`/`rejectionReason`/`retryPolicy` 만으로는 재검증([R-CLI-11](#r-cli-11))과
김프 정책([R-DEC-2](../DECISIONS.md#r-dec-2))을 표현할 수 없다.** 아래 둘이 **같은 튜플**이 된다 —
`desired=true, confirmed=true, rejectionReason=nil` 인데
① snapshot 을 정상 수신한 상태 ② ack 은 받았지만 snapshot 을 **한 번도 못 받은** 상태.
tether 45초 후의 `suspect → revalidating → degraded` 도 거부가 없으므로 나타나지 않는다.
→ **per-topic `deliveryState`** 를 별도 축으로 둔다:

```text
deliveryState = neverReceived | healthy | suspect | revalidating | degraded
accessState   = allowed | disabled | authorizationDenied | unknownTopic
```

⛔ **두 축을 섞지 말 것.** 초안은 `disabled`/`authorizationDenied` 를 `deliveryState` 에 넣었는데,
그러면 `rejectionReason`·auth resolution 과 **중복**돼 판정이 갈린다. delivery 는 *데이터가 오는가*,
access 는 *받을 자격/가능성이 있는가* 다.

⛔ **그리고 축 개수를 세지 말고 _저장/파생_ 을 나눠라.** `rejectionReason`·`accessState`·`retryPolicy`
를 **셋 다 저장하면 서로 불일치**할 수 있다(같은 사실의 세 사본).

| 범위 | canonical 저장 |
|---|---|
| **배치 요청** | `controlState` · **`WholeRequestFailure?`** (4종) |
| **topic** | `desired` · `confirmed` · `receiveGeneration` · `deliveryState` · **`TopicRejection?`** (5종) |
| **연결/계정** | `authResolution` (topic 별로 복제하지 않는다) |

✅ 이 canonical 저장 구조는 `TopicSubscriptionState`와 `TopicSubscriptionSnapshot`에 구현됐고,
접근 상태·수동 재시도 표면은 저장 사본을 추가하지 않고 파생된다
(`ios/FXi/Models/TopicSubscriptionState.swift:64-137`).

| 파생(계산) | 입력 |
|---|---|
| `accessState` | (`TopicRejection`, `authResolution`) |
| `rejectionRetry` | (`WholeRequestFailure`, `TopicRejection`, `authResolution`) — **거부에 대한 재시도** |
| `revalidation` | 아래 **6입력** — **무수신에 대한 재검증** |

⛔ **재시도와 재검증은 _다른 정책_ 이다. 하나로 합치면 45초 무수신을 표현할 수 없다.**
45초 무수신에는 `WholeRequestFailure` 도 `TopicRejection` 도 **없다** — 서버가 거부한 게 아니라
아무 말이 없는 것이다. 그래서 거부 3입력만으로는 `suspect → revalidating` 재구독을 도출할 수 없다.

- **`rejectionRetry`** — 서버가 **거부했을 때** 무엇이 자격을 다시 여는가
  ([R-CLI-6](#r-cli-6) 표의 retryPolicy 열).
- **`revalidation`** — 서버가 **아무 말이 없을 때** 언제 다시 물어보는가
  (조용한 재구독, [R-CLI-11](#r-cli-11)).

⛔ **`deliveryState` + `receiveGeneration` 둘로는 부족하다** — "45초가 지났나 / 이미 한 번 물었나 /
연결은 살아 있나"를 판단할 수 없다. 필요한 입력:

| 입력 | 무엇을 정하나 |
|---|---|
| `deliveryState` | 지금 의심 상태인가 |
| `receiveGeneration` | 요청 이후 프레임이 왔나 |
| `deadline` vs `now` | 45초가 실제로 지났나 |
| `desired` · `confirmed` | 애초에 구독 의도가 있고 확인됐나 |
| `revalidationAttempt`(또는 generation) | **1회 상한**을 넘었나 |
| connection / lifecycle 상태 | 연결이 살아 있나, foreground 인가 |

초안이 per-topic 단일 `rejection` 하나만 입력으로 둔 것은 사유의 두 층 구분([R-CLI-9](#r-cli-9))과도,
위 이분과도 어긋났다.
<!-- /rid: R-CLI-8 -->

<!-- rid: R-CLI-9 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-9"></a>
### R-CLI-9 — 사유는 tagged enum 두 개로 저장한다

⛔ **사유를 _문자열만_ 저장하면 정보가 사라진다.** `temporarily_unavailable` 은 `retry_after_seconds`
를 함께 싣는데([R-CLI-6](#r-cli-6) · `app/topic_wire.py:51` · `app/topic_wire.py:205-227`),
문자열만 남기면 `serverDelay(seconds)` 를 **재구성할 수 없다**.

⛔ **그렇다고 optional 필드 구조체(`rejection(reason, retryAfter)`)도 안 된다** — `invalid_token` +
`retryAfter` 같은 **불가능한 조합을 표현할 수 있다**. **tagged enum** 으로 타입이 조합을 강제한다:

⛔ **그리고 사유의 _두 층_ 을 enum 에서도 지킨다.** 초안은 한 enum 에 전체-요청 오류
(`temporarilyUnavailable`·`invalidToken`)를 섞고 `invalidRequest`·`requestTooLarge` 는 빠뜨렸다.
서버(`topic_wire.py`)처럼 **분리**한다:

```swift
// 전체-요청 (4종) — 요청 전체에 적용. 배치의 모든 topic 이 영향받는다.
enum WholeRequestFailure {
    case temporarilyUnavailable(retryAfter: TimeInterval)
    case invalidToken
    case invalidRequest
    case requestTooLarge          // 실제 운영 경로는 frame 이 아니라 close 1009 (R-CLI-6)
}

// per-topic (5종) — 그 topic 만.
enum TopicRejection {
    case topicsDisabled
    case topicUnavailable
    case unknownTopic
    case premiumRequired
    case krxEntitlementRequired
}
```

`WholeRequestFailure` 는 **배치 요청 단위**, `TopicRejection` 은 **topic 단위**로 저장한다
(arbiter 의 범위 구분과 같은 축이다 — [R-CLI-2](#r-cli-2)).

⚠️ **`authResolution` 은 per-topic canonical 표 _밖_ 이다** — **연결/계정 범위**(토큰 refresh 진행
중인가, 확정 실패인가)다. topic 마다 복제하면 같은 사실의 N 사본이 되어 [R-CLI-8](#r-cli-8) 이
막으려던 불일치가 되살아난다.

파생 필드는 저장하지 않는다 — 저장하는 순간 동기화 책임이 생기고, 그게 불일치의 원천이다.

배너([R-CLI-11](#r-cli-11))와 파생 숫자 차단([R-DEC-2](../DECISIONS.md#r-dec-2))은
**이 축 하나를 공유**해 판정이 갈리지 않게 한다.

⚠️ **`invalid_token` 의 두 갈래는 wire reason 이 같다**([R-CLI-6](#r-cli-6) ·
`app/main.py:3344-3352`) — `rejectionReason` 만으로 구분되지 않는다. **auth resolution 상태**(refresh 진행 중 / 확정 실패)를 별도로 둬야
표의 두 행이 구현된다.

⛔ **`unknown_topic` 에서 `desired` 를 지우면 안 된다.** 버전 스큐나 순차 배포가 끝나도 **자동 복구할
근거가 사라진다**. 대신 `retryPolicy = {nextConnection}` 으로 이 연결 세대에서 억제하고, 재연결 시 한 번 더
시도한다 — 이러면 재시도 폭풍 없이 자연 복구된다.
같은 원칙이 `topics_disabled`·`topic_unavailable` 에도 적용된다(서버 재활성화 시 복구되어야 한다).

<!-- evidence: E-WIRE-1 supports=R-CLI-9 -->
**근거 — 사유는 두 층이다. 이걸 섞으면 안 된다.**

`app/topic_wire.py` 가 어휘를 **실행 가능한 집합**으로 강제한다(총 9종; baseline B4).

- **전체-요청** `WHOLE_REQUEST_ERRORS` (4) — 요청 전체가 실패. **모든 요청 topic 에 영향.**
  `invalid_token` / `temporarily_unavailable` / `invalid_request` / `request_too_large`
- **per-topic** `PER_TOPIC_ERRORS` (5) — ack 의 `rejected_topics` 에만 실림. **그 topic 만 영향.**
  `topics_disabled` / `unknown_topic` / `premium_required` / `krx_entitlement_required` / `topic_unavailable`

⚠️ 초안은 7종만 적고 `unknown_topic`·`topic_unavailable` 을 빠뜨렸으며 **두 층 구분 자체가 없었다**.
<!-- /evidence: E-WIRE-1 -->
<!-- /rid: R-CLI-9 -->

---

## 4. lease 만료가 조용하다 — 클라 hard-expiry

<!-- rid: R-CLI-10 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-10"></a>
### R-CLI-10 — (A) 클라 hard-expiry 채택, (B) 서버 sweeper 는 후속

`leased_subscribers()` 는 **전송 직전에만** 만료를 거른다 — registry 에서 지우지도, 클라에
알리지도 않는다. iOS 는 `applyLeaseSchedule` 로 만료 전 재구독을 예약하고 실패 시
bounded retry(최대 3회)가 돌지만, **소진되면 그걸로 끝**이다.

⚠️ **조용한 topic 에서는 서버의 close-on-send-failure([R-HAND-6](topic-snapshot-handoff.md#r-hand-6))도
구제하지 못한다** — publish 자체가 없어 send 실패도 없기 때문이다.

**결정: (A) 채택** ([R-CLI-16](#r-cli-16) 에서 확정). (B) 는 후속.
- **(A) 클라 hard-expiry** — lease 실제 만료 시각에 갱신이 확인되지 않았으면 **강제 재연결**.
  클라 전용, 결정적 테스트 가능, 기존 재연결 경로 재사용. **출시 범위 권장.**

**관계**

<!-- relation: references target=R-HAND-6 -->
- references: [R-HAND-6](topic-snapshot-handoff.md#r-hand-6)
<!-- relation: deferred_references target=R-HAND-10 -->
- deferred_references: [R-HAND-10](topic-snapshot-handoff.md#r-hand-10)
<!-- /rid: R-CLI-10 -->

---

## 5. 45초 내부 복구와 사용자 경고를 분리한다

<!-- rid: R-CLI-11 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-11"></a>
### R-CLI-11 — 조용한 재구독 → 실패 확정 시에만 배너

45초에 도달했다고 **즉시 배너를 띄우지 않는다**. 정상적인 짧은 침묵을 장애로 노출하게 된다.

```
45s 도달 → (조용히) 재구독 1회
        → 성공/값 수신 → 아무 일도 없었던 것처럼 복귀, 배너 없음
        → 실패 또는 불일치 확정 → degraded 배너 + 수동 재시도 노출
```

✅ **게이트가 전달 상태까지 넓어졌다.** `ConnectionStatusView` 는 여전히 `connectionState` 만
보지만(`ios/FXi/Views/ConnectionStatusView.swift:11-48`), 전달 상태는 별도 `StatusBanner` 가
든다 — 탭 위에 붙고(`ios/FXi/ContentView.swift:87`) `topicStatusMessage(for:)` 문구와
`canRetryTopicDelivery(for:)` 수동 재시도를 탭 범위로 렌더한다
(`ios/FXi/Views/Components/OfflineBanner.swift:150-155`).

⚠️ 단, ADR-038 D2 의 **제약은 유지**된다 — "KRX 수신은 tether 전달 생존의 증거가 아니다"는 여전히
참이고 재검증 오판 방지에 필요하다. 갱신되는 것은 **목적뿐**이다
([R-CUT-18](legacy-cutover.md#r-cut-18)).

**관계**

<!-- relation: references target=R-CUT-18 -->
- references: [R-CUT-18](legacy-cutover.md#r-cut-18)
<!-- /rid: R-CLI-11 -->

<!-- rid: R-CLI-24 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-24"></a>
### R-CLI-24 — 클라는 재구독을 흩뜨리는 쪽이다

**클라 — 재구독을 흩뜨리는 쪽이다.**
- 재연결·재구독에 **jitter** 를 건다.
- **재시도 상한** 을 둔다.
- 실패 후 **클라 재시도 cooldown** 을 둔다.

**현재 구현 상태 (아래 좌표의 기준 트리 = iOS `99316a9`. LOAD-S4 리허설 자체는 iOS `45a8a12` 에서
수행했고, 그 뒤 아래 튜닝 값 자체는 그대로이며 좌표만 재도출됐다):**
- 현재 reconnect는 `2초 × attempt ±20%` jitter와 최대 5회 상한을 쓰며, 첫 frame에서 attempt를
  초기화하지 않고 같은 channel이 30초 안정 구간을 버틴 뒤에만 초기화한다
  (`ios/FXi/Utils/Constants.swift:270-277` · `ios/FXi/Services/WebSocketService.swift:2057-2115`).
- 현재 자동 reconnect 뒤 복구 subscribe batch만 별도 `U(0, 2초)` jitter를 거친다. 최초 연결의
  subscribe는 지연하지 않고, 연결 확인 시점에 있던 topic만 캡처해 그 뒤의 신규 subscribe와
  중복되지 않게 한다(`ios/FXi/Services/WebSocketService.swift:1622-1643` ·
  `ios/FXi/Services/WebSocketService.swift:1881-1900`).
- 현재 topic 실패 재시도는 서버 최소 cooldown 뒤 `U(0, base)`를 더하고, exact
  `(verb, sorted topics)` 실패는 저장된 cooldown task 하나를 공유한다. topic command는 최초
  시도를 포함해 최대 3회이며 cleanup은 저장 cooldown을 취소·제거한다
  (`ios/FXi/Utils/Constants.swift:248` · `ios/FXi/Services/WebSocketService.swift:1430-1582` ·
  `ios/FXi/Services/WebSocketService.swift:1684-1693`).

⚠️ 위 값은 다수 client의 45초 동시 도착에서 서버 queue wait·1013·cooldown suppression과
클라이언트 retry 시간축을 함께 보는 LOAD-S4 리허설을 통과했다. 다만 이 결과를 waiter 개수 hard
cap이나 영구 activation 완료로 쓰지 않는다.

서버 쪽 대응(single-flight · bounded wait · I/O 상한 · 서버 실패 cooldown)은
[R-LOAD-3](revalidation-and-load.md#r-load-3) 가 소유한다. ⚠️ **서버 실패 cooldown 과 클라 jitter 는
함께 있어야** 완화가 성립한다.
<!-- /rid: R-CLI-24 -->

---

## 6. 롤백 레버를 대체한다

<!-- rid: R-CLI-12 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-12"></a>
### R-CLI-12 — `topics_disabled` = 명시적 비활성 상태(safety stop)

`TOPIC_V2_RELEASE_RUNBOOK.md` 의 2차 롤백은 `TOPIC_DISPATCHER_ENABLED=false` + 재기동이다.
⚠️ **45초 legacy revert 는 코드에서 제거됐다.** 45초 무수신이 하는 일은 조용한 재구독 한 번과
상태 안내뿐이고 **화면 값은 유지된다** — 상수 주석이 그 계약을 명시하고
(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:218-221`), stale 전이는
`revalidateSilencedTopic("usdt:krw")` 만 부르며
(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:315-324`), 표시 경로 `usdtDisplayState` 에는
`tetherIsFresh` 를 보고 legacy 로 되돌리는 분기가 더 이상 없다
(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:691-714`, 특히 671 행 주석).
런북도 재작성돼 2차 롤백 bullet 이 "45초 legacy fallback 을 기다리는 절차가 아니다"라고 못박고,
클라가 topic 값을 purge 하고 명시적 unavailable 화면을 띄운다고 적는다
(`ios/TOPIC_V2_RELEASE_RUNBOOK.md:346-350`).
그래서 kill 이 화면에 도달하는 경로는 legacy 되돌림이 아니라 아래 **결정**대로의 `topics_disabled`
명시 비활성 상태 하나이고, 그 경로는 배선돼 있다 — canonical state 적용이 `topics_disabled` 최초
진입에서 전 topic 을 purge 하고(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:1051-1074`)
탭 상태 문구가 "실시간 시세를 일시적으로 제공할 수 없습니다"를 띄운다
(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:503-526`).
legacy 소비를 걷어내는 일([R-CUT-1](legacy-cutover.md#r-cut-1))은 이제 이 레버를 무력화하지 않는다.

⚠️ 이 판단의 근거였던 "현 레버"(45초 legacy revert)는 **이제 코드에 없다**. 당시 문제는 화면이
**바뀌기는 하되** USDT 탭에 은행 USD 시세라는 **무관한 데이터**로 바뀐다는 것이었고, 롤백의
목적("새 경로가 오도하는 것을 멈춘다")을 달성하지 못했다. 그래서 되돌리는 게 아니라 **대체**하기로
했고, 아래 결정이 그 대체다. 지금은 `topics_disabled` purge 후 `tetherReceived=false` + store 가
비어 legacy 로 떨어지지 않고 빈 상태가 된다
(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:691-714`).

**결정**: `topics_disabled` 를 받으면 클라는 **명시적 비활성 상태**로 전환한다 — 마지막 값을
지우고 "실시간 시세를 일시적으로 제공할 수 없습니다"를 표시한다. 구독 의도는
[R-CLI-7](#r-cli-7) 대로 보존한다. 이로써 서버 flag off 가 다시 화면에 도달한다.

⚠️ **이건 "rollback" 이 아니라 전 사용자 실시간 기능을 정직하게 멈추는 safety stop 이다.**
개별 FX/KRX 레버가 필요하면 `topic_unavailable` 전이([R-CLI-6](#r-cli-6))를 함께 써야 한다.

**관계**

<!-- relation: references target=R-CUT-1 -->
- references: [R-CUT-1](legacy-cutover.md#r-cut-1)
<!-- /rid: R-CLI-12 -->

### 6.1 purge 범위

<!-- rid: R-CLI-13 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-13"></a>
### R-CLI-13 — 메모리만 지우면 부족하다

topic 값은 메모리뿐 아니라 **`cached_topic_rates` 로 디스크에 영속화되고 앱 시작 시 복원**된다
(`ios/FXi/Services/CacheService.swift:37-42` · `ios/FXi/ViewModels/ExchangeRateViewModel.swift:446`).
지우지 않으면 재실행 시 **비활성이어야 할 값이 되살아난다** — ✅ 이 요구는 충족됐다.
`stop()` 이 마지막에 `purgeTopicData(.all)` 을 부르고
(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:550-568`), `.all` 분기가 in-memory store 를
비우는 데 그치지 않고 `cachedTopicRates` 를 nil 로 만든 뒤
`cacheService.removeCachedTopicRates()` 로 **디스크 키까지** 지운다
(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:1078-1090` ·
`ios/FXi/Services/CacheService.swift:51-54`).

| 범위 | `topics_disabled` | 인가 거부(per-topic, [R-CLI-6](#r-cli-6)) |
|---|---|---|
| tether/fx/KRX in-memory store + `received` 상태 | 전부 | 해당 topic 만 |
| `cached_topic_rates` 디스크 + 메모리 복원본 | 전부 | 해당 topic 만 |
| 파생 상태(live-tail / 김프 / 알림 현재가) | 전부 | 해당 topic 파생만 |
<!-- /rid: R-CLI-13 -->

---

## 7. 데이터 나이 — 지금은 잴 수 없다

<!-- rid: R-CLI-14 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-14"></a>
### R-CLI-14 — "N분 전" 라벨을 새로 만들지 않는다

**"N분 전" 라벨을 새로 만들지 않는다.** 클라에 일관된 per-source 관측 나이가 저장되지 않는다.

- 서버는 rate 가 같으면 **`rate_changed_at` 을 보존**한다 — `seen_at` 만 전진
  (`app/latest_rates_cache.py:496-516`).
- 클라 `TopicSnapshotMerger` 는 `mergeAt = rateChangedAt ?? timestamp` 로 비교해
  `mergeAt <= existing.mergeAt` 이면 **entry 를 통째로 버린다**
  (`ios/FXi/Services/TopicSnapshotMerger.swift:31-46`).
- → 가격이 평평하면 5초마다 오는 새 `seen_at` 이 전부 폐기되고, store 의 timestamp 는
  **마지막 가격 변동 시각에 동결**된다.
- 게다가 DB fallback 서빙에서는 `rate_changed_at` 이 없고 `timestamp` 자체가 변경 시각이라
  **같은 필드의 의미가 뒤집힌다**.
<!-- /rid: R-CLI-14 -->

### 7.1 이미 살아 있는 age gate

<!-- rid: R-CLI-15 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-15"></a>
### R-CLI-15 — 이번 출시에서는 gate 를 유지한다(제거하지 않는다)

`GraphV2Section.liveFreshnessThreshold` 가 `SourceRate.timestamp` 기준 600초(hana 1200초)로
live-tail 을 **버린다**. 그 함수 주석이 스스로 실토한다 —
*"timestamp=last-change(merge unchanged skip + 서버 SET-only)라 임계가 너무 짧으면 calm flat 을 과도 skip."*

즉 나이가 아닌 값을 나이로 쓰고, 임계를 길게 잡아 무마했다.

**결정 — 이번 출시에서는 gate 를 유지한다(제거하지 않는다).** 초안은 "같은 슬라이스에서 제거 또는
교체"라고 썼으나 **그건 위험하다**:

- 그냥 제거하면 **주말 FX 종가나 장마감 KRX 값을 현재 시점의 live tail 로 ingest** 하게 된다.
  bridge 는 source timestamp 를 검사한 뒤 rate 만 `ingestLive` 에 넘기므로
  (`ios/FXi/Views/Components/GraphV2Section.swift:1446-1468`), `ingestLive` 는 그 rate 를
  `Date()` 시각으로 누적한다(`ios/FXi/ViewModels/GraphV2ViewModel.swift:200-206`). 검사를 빼면
  오래된 값이 지금 값으로 그려진다.
- "전달 상태 기준으로 교체"도 안 된다 — 정상 휴장 중에는 연결·lease 가 **healthy** 라 오래된 값을
  현재 tail 로 연장하는 문제가 그대로 남는다.

→ **의미 재설계는 후속**(관측 나이가 생긴 뒤 — [R-OPEN-2](../DECISIONS.md#r-open-2)).
이번엔 gate 를 남기되 **"이건 데이터 나이가 아니라 last-change 경과다"** 를 주석과 이 문서에 명시해
다음 사람이 나이로 오해하지 않게 한다.

**관계**

<!-- relation: deferred_references target=R-OPEN-2 -->
- deferred_references: [R-OPEN-2](../DECISIONS.md#r-open-2)
<!-- /rid: R-CLI-15 -->

---

## 8. 부수로 고칠 것

<!-- rid: R-CLI-18 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-18"></a>
### R-CLI-18 — 알림 시트 범위 검증 우회(cold-start fail-close)

- **알림 시트 범위 검증** — ✅ **fail-closed 로 배선됐다.** 판정은 `SourceAlertValidation` 로
  분리됐고, `canSaveThreshold` 는 threshold 가 nil 이면 거절하며 `validRange` 가 nil 이면
  `isThresholdInRange` 가 `false` 를 돌려준다
  (`ios/FXi/Views/Components/SourceAlertAddSheet.swift:20-40`).
  `validRange` 는 현재가가 없거나 비유한·0 이하면 nil 이 되므로
  (`ios/FXi/Views/Components/SourceAlertAddSheet.swift:150-158`),
  **한 번도 수신 못 한 cold-start** 에서는 ±50% 가드가 꺼지는 게 아니라 **저장이 막힌다**.
  `canSave` 가 그 판정을 그대로 쓴다(같은 파일 `196-206`).
  예외는 하나 — **가격 조건을 그대로 둔 편집**이다. source·asset·condition·threshold 가 모두
  그대로면(`priceFieldsUnchanged`, 같은 파일 `164-170`) live rate 없이도 활성/반복 토글만 저장할
  수 있다. 요구했던 **사유 표시 + 탈출구**도 함께 있다 — 현재가를 못 읽으면 "현재 시세를 확인할 수
  없어 가격 조건을 저장할 수 없습니다" 와 `다시 연결` 버튼을 띄운다(같은 파일 `422-433`).
  불변식([R-INV-1](../DECISIONS.md#r-inv-1))대로 last-known 이 공급되면 애초에 이 상태에 거의
  들어가지 않는다. 서버는 여전히 `threshold > 0` 만 검증하므로(`app/schemas.py:211`) 범위 가드는
  클라가 유일한 방어선이다.

**관계**

<!-- relation: references target=R-INV-1 -->
- references: [R-INV-1](../DECISIONS.md#r-inv-1)
<!-- /rid: R-CLI-18 -->

<!-- rid: R-CLI-19 -->
<!-- requirement-meta: disposition=proposed owner=None -->
<a id="r-cli-19"></a>
### R-CLI-19 — usd 탭 그래프의 KRX tail 결합

[제안·결정 대기]

- **usd 탭 그래프의 KRX 결합** — fx stale 시 guard 가 뒤의 KRX tail 까지 버린다.
  KRX 는 "freshness 비결합"이 명시 정책인데 그래프만 어긋난다.
<!-- /rid: R-CLI-19 -->

---

## 9. 확정 — lease 계약에 함께 잠글 것

<!-- rid: R-CLI-16 -->
<!-- requirement-meta: disposition=active owner=CLIENT -->
<a id="r-cli-16"></a>
### R-CLI-16 — (2) lease: 출시는 (A) 클라 hard-expiry, (B) 서버 sweeper 는 후속

**(2) lease — 출시는 (A) 클라 hard-expiry, (B) 서버 sweeper 는 후속.**

계약에 함께 잠글 것:
- topic 별 **절대 expiry** 를 추적한다
- **새 `lease_id` 를 포함한 유효 ack 만** 만료를 갱신한다(낡은/미지 request 의 ack 은 해제 불가)
- 만료 topic 은 `confirmed` 에서 제거하되 **`desired` 는 보존**([R-CLI-7](#r-cli-7))
- **연결/lease 세대당 강제 reconnect 1회**, 그 뒤엔 기존 backoff
- **foreground 복귀 시 즉시 expiry 재평가**

**관계**

<!-- relation: references target=R-CLI-10 -->
- references: [R-CLI-10](#r-cli-10)
<!-- relation: deferred_references target=R-HAND-10 -->
- deferred_references: [R-HAND-10](topic-snapshot-handoff.md#r-hand-10)
<!-- /rid: R-CLI-16 -->
