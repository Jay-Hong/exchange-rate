# topic snapshot handoff — 서버 build · ack · close 계약

- 책임: 서버 build · ack · close 계약
- 상태: Draft — 구현 착수 전 합의 대상
- 코드 근거 기준일: 2026-08-09
- server 기준 commit: `0cfe4748defdfcad1ef9b55dcab1f5fbc2a0df01`
- iOS 기준 commit: `8aadc2fb66be926a809d6e1bc5dff42951f15a7a`
- archive SHA: `cde1d2ca3e714733776e1b0d7e821a542e1f8d183cb2951bef8c93fb444d9814`
- manifest SHA: `ddee7b90355c9df97bd7dc8add1a0b8c3c82b2a18325f671de10bd7cc43031ca`
- baseline SHA: `a0f569c48ad2d2c06afccd6d5513b388db22a02196424718f09d4d1692f13ea7`
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

**(a) WS 인가 격차 — Release arming 전에 닫는다.**

→ **Release arming 전에 WS FX/USDT 에도 인증 + premium 판정과 premium lease 를 강제해야 한다**
(ADR-039 Stage A). 이게 닫히기 전까지 [R-INV-1](../DECISIONS.md#r-inv-1) 의 불변식은 **문서상 목표일
뿐이다**.

격차의 현재 상태는 [R-INV-2](../DECISIONS.md#r-inv-2) 가 기록한다 — **강제 경로는 `0cfe474`
에서 구현됐고 운영은 아직 켜지 않았다**. `enforce_authenticated_premium` 에서 식별된 FX/USDT 는
premium-only 로 분류되고(`app/topic_policy.py:288-330`) coordinator 가 premium 을 관측한다
(`app/topic_authorization.py:311-341`). 기본값 `compatibility` 에서는 종전대로 identity-only 다.
REST twin 은 stage 와 무관하게 premium 을 강제한다(`app/main.py:3058-3061`, ADR-039 §8.1 E3).
⛔ **arming 조건은 "구현"이 아니라 "활성화 + 실측"이다** — 최종 stage 는 미실측이다.

⚠️ **부분 갱신(2026-08-12)**: 익명(미식별) 축만 `WS_TOPIC_AUTH_STAGE` 로 갈린다 —
`compatibility`(기본) 는 구 동작 보존, `reject_anonymous_fx` 는 무료 집합에서 canonical FX 만
조용히 제외한다. 근거는 stage 정의·코드 기본값 `app/config.py:645-688` · 필터 정책
`app/topic_auth_rollout.py:173-188` · 필터 호출과 등록 `app/topic_dispatcher.py:547-569` ·
production 주입 `app/main.py:302-308` 다. **이 슬라이스는 차단 경로를 구현했을 뿐** 운영 stage 는
미실측이며, premium 축과 USDT 는 잔존한다.

<!-- relation: references target=R-INV-1 -->
- references: [R-INV-1](../DECISIONS.md#r-inv-1)
<!-- relation: references target=R-INV-2 -->
- references: [R-INV-2](../DECISIONS.md#r-inv-2)
<!-- /rid: R-HAND-11 -->

<!-- rid: R-HAND-19 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-19"></a>
### R-HAND-19

**(b) DXY 는 현재 legacy envelope 로만 온다 → `dxy` topic 신설이 출시 선행조건이다.**
(근거: baseline F12 · 서버 지원 topic 집합 `app/topic_initial_snapshot.py:64-69`)

이번 출시의 범위는 [R-INV-4](../DECISIONS.md#r-inv-4) 가 `dxy:spot` **하나로 확정**했다 — futures
topic 은 legacy 이탈에 불필요하므로 phased 로 미룬다. 서버 몫은 그 topic 을 신설해 legacy `rates`
프레임의 `indices` 경유 없이 DXY 가 전달되게 만드는 것이다.

<!-- relation: references target=R-INV-4 -->
- references: [R-INV-4](../DECISIONS.md#r-inv-4)
<!-- /rid: R-HAND-19 -->

<!-- rid: R-HAND-20 -->
<!-- requirement-meta: disposition=active owner=HAND -->
<a id="r-hand-20"></a>
### R-HAND-20

**인가 판정은 명시적 정책표로 구현한다.**

[R-OPEN-1](../DECISIONS.md#r-open-1) 이 확정한 Stage A 범위(**비-KRX 최신 topic = Firebase 인증 +
premium / KRX = premium + entitlement**)는, [R-INV-4](../DECISIONS.md#r-inv-4) 가 이번 출시 범위로
확정한 **`dxy:spot` 까지 포함한 명시적 fail-closed 정책표**로 구현하면 된다
(**미지정 topic 은 통과 불가**).

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
(`app/topic_dispatcher.py:737-818`; baseline D5).
그리고 snapshot build 가 실패하거나 `None` 이면 **연결을 유지한 채 조용히 skip** 한다
(`app/topic_initial_snapshot.py:296-326`; baseline D3). FX publisher 도 build/publish **전** 예외를
격리하고 `False` 만 반환한다(`app/fx_topic_publisher.py:291-323`).

→ **연결·pong·ack·lease 가 전부 정상인데 snapshot 이 한 번도 오지 않는 상태**가 실제로 표현된다.
이때 [R-HAND-6](#r-hand-6) 의 close 는 발동할 기회조차 없다(send 자체가 없으므로).

**결정 — 출시 범위**:

**1. 서버 — 정적 `unavailable` 만 ack 전에 판정하고, 나머지는 등록→ack→전송 순서를 유지한다.**

⛔ 초안은 *"ack 뒤에 종결 신호를 보내거나 연결을 닫는다"* 라고 썼다. **철회한다 — 종결 계약 위반이다.**
`topic_dispatcher` §8-B-term 이 *"식별된 요청은 반드시 종결된다 … **종결 프레임 하나 또는 연결 종료**"*
라고 정한다(baseline D4). ack 이 이미 그 하나이므로, 그 뒤에 또 종결 신호를 보내면
**종결 프레임이 둘**이 된다.

⛔ **"ack 전에 전부 build 한다"(초안 (a))도 철회한다.** 그러면 두 가지가 깨진다 —
① build↔register 사이에 발생한 publish 를 놓치고(조용한 FX 는 낡은 prebuilt 로 오래 남는다),
② **"지원되지만 아직 데이터가 없음"을 거부로 오분류**한다. 실제로 KRX 는 데이터가 없어도 구독을
유지하도록 설계돼 있고(`app/topic_initial_snapshot.py:274-275`), iOS 모델은
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
- ⛔ **경계는 넷이고(클라 2 · 서버 2), 제약은 둘이다.** 초안은 *"build 총예산 < iOS 20초"* 라고 썼는데 **틀렸다** —
  iOS 는 ACK 수신 시 `takePending` 이 timeout task 를 **즉시 취소**한다(baseline F3 · F3-inf). build 는 ACK **뒤**라
  그 20초는 이미 사라졌고, 클라 쪽에 build 를 묶는 상한이 **없다**. 서버의 실제 순서도 ack 전송 뒤
  `send_initial_snapshots` 호출이다(`app/topic_dispatcher.py:801-818`).

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

- ⚠️ **`asyncio.to_thread` 취소는 내부 작업을 멈추지 않는다** — 스레드는 계속 돈다.
  현재 snapshot builder 가 이 경계를 사용한다(`app/topic_initial_snapshot.py:296-316`). 따라서 서버
  절대 deadline 만으로는 부족하고 **DB/Redis I/O 자체에 상한**이 필요하다.
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
**앱 레벨에서 명시적으로 닫는 곳은 `close(1008)`(인증) 하나뿐이다**(`app/topic_dispatcher.py:655`)
— 16KB 초과는 transport 가 1009 로 닫지만 그건 앱 정책이 아니다.
이 부정 사실은 고정 server commit 의 `app/main.py`·`app/topic_dispatcher.py`에서 WebSocket
`close(` 호출을 전수 검색해 확인했다(collector DB의 `close()`는 범위 밖).

→ 결과: **연결은 살아 있고 ping/pong 도 정상인데 그 연결의 모든 구독이 사라진다.** 영구 침묵.

**결정**: 서버가 send 실패 시 **소켓을 닫는다**. 탐지 불가능한 조건을 **이미 처리되는 재연결
경로**로 바꾼다. (전송 재시도 1회 후 close 도 허용 — 다만 "조용히 구독만 제거"는 금지.)
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
