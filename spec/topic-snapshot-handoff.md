# topic snapshot handoff — 서버 build · ack · close 계약

- 책임: 서버 build · ack · close 계약
- 상태: Draft — 구현 착수 전 합의 대상
- 코드 근거 기준일: 2026-08-09
- server 기준 commit: `4e009ff1777a6d2d4b60a861eb411d4be1c9a288`
- iOS 기준 commit: `8f6afff299621d50c3431dbea739ed07c378c59a`
- archive SHA: `cde1d2ca3e714733776e1b0d7e821a542e1f8d183cb2951bef8c93fb444d9814`
- manifest SHA: `5bac4fe37004b8aa42ea30e9b83fa999d50c3f57512aa37d6143328941a8eb8a`
- baseline SHA: `4cc944e333789fb4a2ff08217d2dbd3469f29c9f09826946bb1776d43c27d708`
- 검증: `python3 scripts/topic_migration_manifest.py preflight`

> 이 문서는 **서버가 subscribe 요청을 어떻게 종결하는가**만 소유한다 — initial snapshot build 결과의
> 분류, ack 과 전송의 순서, 실패 시 close code, 서버가 지는 deadline, 그리고 서버만 고칠 수 있는
> 조용한 실패. 클라이언트 상태기계·재검증은 [ios-topic-state-machine.md](ios-topic-state-machine.md),
> 불변식·결정·arming 게이트는 `DECISIONS.md` ADR-041 이 소유한다.

**요구사항 색인** — 출시 선행조건: [R-HAND-11](#r-hand-11) · [R-HAND-19](#r-hand-19) ·
[R-HAND-20](#r-hand-20). snapshot handoff: [R-HAND-1](#r-hand-1) · [R-HAND-2](#r-hand-2) ·
[R-HAND-9](#r-hand-9). deadline: [R-HAND-3](#r-hand-3) · [R-HAND-13](#r-hand-13) ·
[R-HAND-14](#r-hand-14) · [R-HAND-17](#r-hand-17) · [R-HAND-15](#r-hand-15) ·
[R-HAND-18](#r-hand-18) · [R-HAND-16](#r-hand-16) · [R-HAND-4](#r-hand-4).
조용한 실패: [R-HAND-8](#r-hand-8) · [R-HAND-6](#r-hand-6) · [R-HAND-10](#r-hand-10).

---

## 1. 출시 선행조건 — 서버가 먼저 닫아야 하는 두 구멍

<!-- rid: R-HAND-11 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-11"></a>
### R-HAND-11

**(a) WS 인가 경로 — 구현됨, Release arming 전에 활성화·실측한다.**

`0cfe474` 는 WS FX/USDT 인증 + premium 판정 + premium lease 경로를 구현했다
(`app/topic_policy.py:278-323` · `app/topic_dispatcher.py:792-948`, ADR-039 Stage A).
Release arming 은 이 경로의 존재가 아니라 **최종 stage 활성화와 운영 실측**을 요구한다.

격차의 현재 상태는 [R-INV-2](../DECISIONS.md#r-inv-2) 가 기록한다. 최종 stage
`enforce_authenticated_premium` 에서 식별된 FX/USDT 는
premium-only 로 분류되고(`app/topic_policy.py:278-323`) coordinator 가 premium 을 관측한다
(`app/topic_authorization.py:372-402`). 기본값 `compatibility` 에서는 종전대로 identity-only 다.
REST twin 은 stage 와 무관하게 premium 을 강제한다(`app/main.py:3251-3255`, ADR-039 §8.1 E3).
익명 요청은 같은 최종 stage 에서 전부 조용히 제외된다(`app/topic_policy.py:237-275`).
⛔ production 의 현재 stage 와 활성화 이력은 이 코드 근거로 확정하지 않는다. arming 직전에 실행
중인 컨테이너와 env 를 직접 측정하고, cache-free RevenueCat 결합을 수용한 별도 GO가 필요하다.

<!-- relation: references target=R-INV-1 -->
- references: [R-INV-1](../DECISIONS.md#r-inv-1)
<!-- relation: references target=R-INV-2 -->
- references: [R-INV-2](../DECISIONS.md#r-inv-2)
<!-- /rid: R-HAND-11 -->

<!-- rid: R-HAND-19 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-19"></a>
### R-HAND-19

**(b) DXY `dxy:spot` 수직 슬라이스는 구현됐다.**
(근거: baseline F12 · 서버 지원 topic 집합 `app/topic_initial_snapshot.py:312-324`)

이번 출시의 범위는 [R-INV-4](../DECISIONS.md#r-inv-4) 가 `dxy:spot` **하나로 확정**했다 — futures
topic 은 legacy 이탈에 불필요하므로 phased 로 미룬다. 서버 `8755510`은 publisher + initial
snapshot을, iOS `0f2a3f8`은 인증 bootstrap + WS merge + topic cache/purge를 land했다.
운영 배포·canary는 별도 게이트다.

<!-- relation: references target=R-INV-4 -->
- references: [R-INV-4](../DECISIONS.md#r-inv-4)
<!-- /rid: R-HAND-19 -->

<!-- rid: R-HAND-20 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-20"></a>
### R-HAND-20

**인가 판정은 명시적 정책표로 구현한다.**

[R-OPEN-1](../DECISIONS.md#r-open-1) 이 확정한 Stage A 범위(**비-KRX 최신 topic = Firebase 인증 +
premium / KRX = premium + entitlement**) 중 현재 구현 topic(FX 3 + USDT + KRX)은 `0cfe474`의
명시적 정책표에 들어갔고, 미지정 topic 은 fail-closed 다(`app/topic_policy.py:78-85` ·
`app/topic_policy.py:224-234`). [R-INV-4](../DECISIONS.md#r-inv-4) 가 출시 범위로 확정한
`dxy:spot`도 명시적 `PREMIUM_ONLY` 정책행과 fail-closed universe 검사를 갖고
(`app/topic_policy.py:78-100` · `app/topic_policy.py:224-234`), 지원 집합과 snapshot builder에
포함된다(`app/topic_initial_snapshot.py:312-344` · `app/topic_initial_snapshot.py:972-995`).
publisher·snapshot·정책행을 함께 추가한다는 요구는 `8755510`에서 완료됐다.

<!-- relation: references target=R-INV-4 -->
- references: [R-INV-4](../DECISIONS.md#r-inv-4)
<!-- relation: references target=R-OPEN-1 -->
- references: [R-OPEN-1](../DECISIONS.md#r-open-1)
<!-- /rid: R-HAND-20 -->

---

## 2. initial snapshot handoff — build 결과 분류와 종결

<!-- rid: R-HAND-1 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-1"></a>
### R-HAND-1

**연결·pong·ack·lease 가 전부 정상인데 snapshot 이 한 번도 오지 않는 구멍 (출시 차단)**

서버는 **registry 등록과 ack 를 먼저 끝낸 뒤** initial snapshot 을 만든다
(`app/topic_dispatcher.py:858-948`; baseline D5).
LOAD-S3/S7 구현 뒤 snapshot deadline·transient build 실패와 active cooldown은 **1013**, fatal 실패는
**1011**로 닫힌다(`app/topic_initial_snapshot.py:1072-1098`; baseline D3). 다만 builder의 `None`은
여전히 연결을 유지한 채 조용히 skip한다(`app/topic_initial_snapshot.py:1100-1101`). FX publisher도 build/publish **전** 예외를
격리하고 `False`만 반환한다(`app/fx_topic_publisher.py:291-323`).

→ build **실패**의 조용한 구멍은 닫혔지만, `None` 경로에는 여전히 **연결·pong·ack·lease가 전부
정상인데 snapshot이 한 번도 오지 않는 상태**가 표현된다. 이때 [R-HAND-6](#r-hand-6)의 close는
발동할 기회조차 없다(send 자체가 없으므로; baseline D3).

**결정 — 출시 범위**:

**1. 서버 — 정적 `unavailable` 만 ack 전에 판정하고, 나머지는 등록→ack→전송 순서를 유지한다.**

⛔ 초안은 *"ack 뒤에 종결 신호를 보내거나 연결을 닫는다"* 라고 썼다. **철회한다 — 종결 계약 위반이다.**
`topic_dispatcher` §8-B-term 이 *"식별된 요청은 반드시 종결된다 … **종결 프레임 하나 또는 연결 종료**"*
라고 정한다(baseline D4). ack 이 이미 그 하나이므로, 그 뒤에 또 종결 신호를 보내면
**종결 프레임이 둘**이 된다.

⛔ **"ack 전에 전부 build 한다"(초안 (a))도 철회한다.** 그러면 두 가지가 깨진다 —
① build↔register 사이에 발생한 publish 를 놓치고(조용한 FX 는 낡은 prebuilt 로 오래 남는다),
② **"지원되지만 아직 데이터가 없음"을 거부로 오분류**한다. 실제로 KRX 는 데이터가 없어도 구독을
유지하도록 설계돼 있고(`app/topic_initial_snapshot.py:1014-1015`), iOS 모델은
`usd_krw_futures: null` 인 **빈 snapshot 을 이미 표현**한다(`ios/FXi/Models/TopicMessage.swift:100-101`).
이를 `topic_unavailable` 로 거부하면 첫 데이터가 생겨도 **그 연결에서는 영영 못 받는다.**

→ **채택: build 결과를 분류하고, 분류마다 다른 경로로 보낸다.** 이분법(a/b)이 아니라 분류 문제다.

| build 결과 | 판정 시점 | 응답 |
|---|---|---|
| **unavailable** — flag off 등 의도적 미지원(현 코드의 `None`) | **ack 전**(플래그 조회, build 불필요) | 같은 ack 의 `topic_unavailable` 거부 |
| **ready(payload)** | ack 후 | 정상 snapshot |
| **supportedEmpty(payload)** — 지원되나 아직 데이터 없음 | ack 후 | **schema-valid 빈 snapshot**. 거부 아님 |
| **transientFailure** — DB/Redis 일시 실패 | **항상 ack 후** | **close 1013**. ⛔ `topic_unavailable` 로 위장 금지 |
| **fatalFailure** — 직렬화/프로그래밍 오류 | **항상 ack 후** | **close 1011** |

⚠️ **transient/fatal 에 "ack 전" 분기가 없는 이유**: 최소 계약에서 ack 전에 하는 일은 **정적 flag 조회**뿐이고
그건 일시 실패하지 않는다. 실제 build 는 전부 ack 후이므로 두 실패도 전부 ack 후다.
<!-- /rid: R-HAND-1 -->

<!-- rid: R-HAND-2 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-2"></a>
### R-HAND-2

**출시용 최소 계약 (이 순서를 그대로 구현한다)**

```text
1. 정적 unavailable 판정 (flag 조회, build 불필요) → 같은 ack 의 topic_unavailable 거부
2. register
3. ack 송신
4. ready / supportedEmpty 전송
5. ack 후 transient build 실패 → close 1013 (Try Again Later)
6. ack 후 fatal 실패        → close 1011 (Internal Error)
   ⛔ 5·6 은 종결 프레임을 더 보내지 않는다 — §8-B-term 은 "프레임 하나 **또는 연결 종료**"다.
      close 로 넘기면 기존 재연결 경로가 그대로 복구를 맡는다.
```

같은 계약의 7번 항목(*"클라는 요청 송신 시점의 수신 세대로 ack 전후 live frame 을 모두 인정한다"*)은
클라 소유이며 [R-CLI-21](ios-topic-state-machine.md#r-cli-21) 에 있다.
<!-- /rid: R-HAND-2 -->

<!-- rid: R-HAND-9 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-9"></a>
### R-HAND-9

✅ **재구독 중 기존 active topic 의 build 실패도 같은 규칙**(post-ACK → close)으로 닫힌다.
연결이 닫히므로 `register` 의 합집합 의미론과 `rejected_topics`/`active_subscriptions` 모순 문제가
**발생할 여지 자체가 없다**. 초안이 이걸 미결로 남긴 것은 최소 계약을 채택하기 전 서술이 남은 것이다.
<!-- /rid: R-HAND-9 -->

---

## 3. deadline — 넷의 경계와 둘의 제약

<!-- rid: R-HAND-3 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-3"></a>
### R-HAND-3

⚠️ 함께 확정할 것:

- *"ack 이 데이터보다 먼저"* 는 **initial snapshot 에만** 적용된다. **모든 live frame 보다 먼저**라는
  보장은 현 구조(outbound 직렬화 없음)에서 성립하지 않는다 — 그래서 클라 쪽 기준선을 요청 송신
  시점으로 옮기는 [R-CLI-3](ios-topic-state-machine.md#r-cli-3) 가 필요하다
- ⛔ **경계는 넷이고(클라 2 · 서버 2), 제약은 둘이다.** 서버는 ACK 뒤에
  `send_initial_snapshots`를 호출한다(`app/topic_dispatcher.py:922-948`). iOS는 송신 시점부터 control과
  delivery deadline을 한 arbiter에 등록하고 ACK 뒤 delivery phase로 전환한다(baseline F3). 따라서
  ACK watchdog을 snapshot 상한으로 오독하지 않으면서도 build 무기한 대기는 허용하지 않는다.

넷 중 서버가 소유하는 둘은 [R-HAND-13](#r-hand-13)·[R-HAND-14](#r-hand-14) 이고, 클라가 소유하는
둘은 [R-CLI-17](ios-topic-state-machine.md#r-cli-17)·[R-CLI-20](ios-topic-state-machine.md#r-cli-20) 이다.

<!-- relation: references target=R-CLI-3 -->
- references: [R-CLI-3](ios-topic-state-machine.md#r-cli-3)
<!-- /rid: R-HAND-3 -->

<!-- rid: R-HAND-13 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-13"></a>
### R-HAND-13

서버가 소유하는 첫 번째 경계 — 짝은 제약 ①.

| deadline | 대상 | 소유 | 짝 |
|---|---|---|---|
| **서버 ACK 예산** (신규) | 요청 수신 → ack 송신 | 서버 | ① |
<!-- /rid: R-HAND-13 -->

<!-- rid: R-HAND-14 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-14"></a>
### R-HAND-14

서버가 소유하는 두 번째 경계 — 짝은 제약 ②.

| deadline | 대상 | 소유 | 짝 |
|---|---|---|---|
| **서버 snapshot 총 예산** (신규) | ack 이후 전체 build → 초과 시 **close 1013** | 서버 | ② |
<!-- /rid: R-HAND-14 -->

<!-- rid: R-HAND-17 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-17"></a>
### R-HAND-17

⛔ **제약 ②는 단순 대소 비교가 아니다 — 두 시계의 _원점이 다르다_.**
클라 initial-delivery([R-CLI-20](ios-topic-state-machine.md#r-cli-20))는 **요청 송신** 부터,
서버 snapshot 예산은 **ack 이후** 부터 잰다.

<!-- relation: references target=R-CLI-20 -->
- references: [R-CLI-20](ios-topic-state-machine.md#r-cli-20)
<!-- /rid: R-HAND-17 -->

<!-- rid: R-HAND-15 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-15"></a>
### R-HAND-15

제약 ① — 짝은 클라 ACK deadline([R-CLI-17](ios-topic-state-machine.md#r-cli-17)).

```text
① 서버 ACK 예산 < 클라 ACK deadline
```

<!-- relation: references target=R-CLI-17 -->
- references: [R-CLI-17](ios-topic-state-machine.md#r-cli-17)
<!-- /rid: R-HAND-15 -->

<!-- rid: R-HAND-18 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-18"></a>
### R-HAND-18

제약 ② — 원점이 다르므로 경과분과 전송 여유를 모두 더한 뒤 클라
initial-delivery deadline([R-CLI-20](ios-topic-state-machine.md#r-cli-20))과 비교한다.

```text
② (서버 ACK 경과) + (서버 snapshot 예산) + (전송 여유) < 클라 initial-delivery deadline
```

<!-- relation: references target=R-CLI-20 -->
- references: [R-CLI-20](ios-topic-state-machine.md#r-cli-20)
<!-- /rid: R-HAND-18 -->

<!-- rid: R-HAND-16 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-16"></a>
### R-HAND-16

그리고 **서버는 요청 전체 absolute deadline 도 함께 유지**한다 — 부분 예산만 두면 합이 새어 나간다.
<!-- /rid: R-HAND-16 -->

<!-- rid: R-HAND-4 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-4"></a>
### R-HAND-4

- ⚠️ **`asyncio.to_thread` 취소는 실행 중 I/O를 즉시 멈추지 않는다.** 현재 LOAD-S5/S6/S7 wrapper는
  waiter마다 요청 예산을 따로 적용하고 shared build에는 독립된 예산을 준다. leader 하나의 취소는
  build나 그 admission permit을 반환하지 않지만, 마지막 waiter가 사라지면 shared task를 취소한다
  (`app/topic_initial_snapshot.py:733-838`). worker는 각 phase 앞 checkpoint에서 다음 Redis/DB I/O
  진입을 막는다(`app/topic_initial_snapshot.py:143-239` · `app/topic_initial_snapshot.py:242-266` ·
  `app/topic_initial_snapshot.py:841-895`). transient build 실패만 exact key cooldown을 arm하며,
  admission 대기 deadline과 caller 취소는 arm하지 않는다(`app/topic_initial_snapshot.py:733-752`).
  그러나 이미 시작한 I/O의 종료는 협력 신호가 아니라
  **DB/Redis 자체 timeout**이 맡는다. 따라서 permit 반환 뒤에도 취소된 worker가 다음 checkpoint까지
  잠시 executor를 점유할 수 있다.
<!-- /rid: R-HAND-4 -->

---

## 4. 조용한 실패 — 클라가 탐지할 수 없으므로 서버가 고친다

<!-- rid: R-HAND-8 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-8"></a>
### R-HAND-8

**조용한 실패 두 가지 — 서버가 고친다.**

클라가 **구조적으로 탐지할 수 없는** 실패다. 클라에 탐지 기계를 짓는 대신 원인 지점에서 고친다.
<!-- /rid: R-HAND-8 -->

<!-- rid: R-HAND-6 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-6"></a>
### R-HAND-6

**publish send 실패가 구독을 지운다.**

`topic_dispatcher.remove_websocket` docstring 이 스스로 적어 뒀다(baseline D1) —
*"main.py 의 disconnect 경로와 **publish 송신 실패 격리**에서 호출된다 … 이 메서드는
'연결이 죽었다'의 동의어가 아니다."*
앱 레벨 close는 인증 충돌의 `1008`(`app/topic_dispatcher.py:769`)뿐 아니라 snapshot 실패의
`1013`/`1011`도 있다(`app/topic_initial_snapshot.py:75-110` ·
`app/topic_initial_snapshot.py:1072-1098`). 16KB 초과의 transport `1009`는 별도 축이다.

→ 방치했을 때의 결과: **연결은 살아 있고 ping/pong 도 정상인데 그 연결의 모든 구독이 사라진다.**
영구 침묵.

**결정**: 서버가 send 실패 시 **소켓을 닫는다**. 탐지 불가능한 조건을 **이미 처리되는 재연결
경로**로 바꾼다. (전송 재시도 1회 후 close 도 허용 — 다만 "조용히 구독만 제거"는 금지.)

✅ **배선됐다.** `publish_topic` 과 `publish_topic_detailed` 가 send 실패 소켓을 모아
(`app/topic_dispatcher.py:334` · `app/topic_dispatcher.py:423`) 전송을 모두 끝낸 뒤
`_close_failed_topic_subscribers` 로 넘기고(`app/topic_dispatcher.py:341` ·
`app/topic_dispatcher.py:429`), 그 helper 가 `_close_after_topic_send_failure` 를 `gather` 로
동시에 돌려 각각 `close(code=1011)` 한다(`app/topic_dispatcher.py:284-296`).
⚠️ close 를 send 루프 **안**에서 기다리면 N 개 실패가 정상 fan-out 을 N 초까지 막으므로
전송 뒤로 미뤘고, 각 close 에 1초 timeout 을 걸어 총 지연을 ~1초로 묶는다.
<!-- /rid: R-HAND-6 -->

<!-- rid: R-HAND-10 -->
<!-- requirement-meta: disposition=proposed owner=None -->
<a id="r-hand-10"></a>
### R-HAND-10

[제안·결정 대기]

lease 만료가 조용한 문제에 대한 서버 측 대안이다. 클라 hard-expiry 가 (A)로 채택된 반면 이것은 (B)다.

- **(B) 서버 expiry sweeper** — 주기적으로 만료 lease 제거 + `reauth_required` 통지 또는 close.
  클라 버그까지 방어. **후속 권장.**
<!-- /rid: R-HAND-10 -->
