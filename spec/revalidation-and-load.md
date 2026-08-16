# 재검증과 부하 — jitter · single-flight · bounded wait

- 책임: jitter · single-flight · bounded wait
- 상태: Draft — 구현 착수 전 합의 대상
- 코드 근거 기준일: 2026-08-09
- server 기준 commit: `5c9b932edfaa4b2fb2a6e4fe1db04cb13ba57910`
- iOS 기준 commit: `8aadc2fb66be926a809d6e1bc5dff42951f15a7a`
- archive SHA: `cde1d2ca3e714733776e1b0d7e821a542e1f8d183cb2951bef8c93fb444d9814`
- manifest SHA: `70cf15b17e3ecabe7e25658587047443277a87894f68936fb0fb7c349190b7de`
- baseline SHA: `52501e595e0e9c453a1d1080fd412a98266cfb12568aca4add308015f83cce3d`
- 검증: `python3 scripts/topic_migration_manifest.py preflight`

> 이 문서가 소유하는 것은 **재검증(재구독)이 만드는 동시 부하** 하나다.
> 언제 조용히 재구독하고 언제 배너를 띄우는지는 클라 상태기계 문서가, ack·build·close 의
> 종결 계약은 handoff 문서가 소유한다. 여기서는 그 재구독이 **동시에 몰릴 때** 클라와 서버가
> 각각 무엇을 하는지만 정한다.
>
> ⚠️ 원문의 `§` 번호는 분할과 함께 사라졌다. 다른 문서의 내용은 **RID 와 링크**로만 가리킨다.

---

## 45초 동시 재구독 폭주 완화

이 완화는 **두 쪽이 함께** 있어야 성립한다. 클라는 재구독을 **흩뜨리고**(jitter · 재시도 상한 ·
클라 재시도 cooldown), 서버는 남은 동시 도착을 **흡수한다**(single-flight/cache · bounded wait ·
I/O 상한 · 서버 실패 cooldown).

⚠️ 클라 절반은 이 문서가 아니라 [R-CLI-24](ios-topic-state-machine.md#r-cli-24) 가 소유한다 —
한쪽만 구현하면 완화가 성립하지 않는다는 것이 아래 [R-LOAD-3](#r-load-3) 의 마지막 항목이다.

<!-- rid: R-LOAD-4 -->
<!-- requirement-meta: disposition=active owner=LOAD -->
<a id="r-load-4"></a>
### R-LOAD-4 — 폭주 완화는 확정 계약이다 · 즉시거절 bulkhead 금지
<!-- relation: references target=R-CLI-11 -->
- references: [R-CLI-11](ios-topic-state-machine.md#r-cli-11)

[R-CLI-11](ios-topic-state-machine.md#r-cli-11) 의 조용한 재구독은 **확정**이다. 그것이 만드는
동시 폭주의 완화도 같은 등급이어야 한다 — 이 절은 ADR-041 의 `[제안·결정 대기]`
**파생 숫자 숨김 정책** 제안의 채택 여부와 무관한 **확정 계약**이다.

⛔ **즉시거절 형태의 bulkhead 를 쓰지 않는다.** 이 리포는 그걸 **이미 기각**했다 —
`auth_executor` docstring: *"즉시거절 semaphore 는 별도로 기각됐다: 배포 재연결은 평균 유입이
낮아도 **동시 도착** 이라 1초면 빠질 큐를 대량 거절한다."*
⚠️ 그리고 그 executor 는 **큐 상한이 아니다**(`SimpleQueue` 무제한) — 같은 docstring 이
*"자원 상한 완료 라고 쓰지 말 것"* 이라 못 박는다.

근거(baseline): **E3 [결정]** `app/auth_executor.py:14-16` docstring(즉시거절 기각) +
`app/auth_executor.py:29-31`(**`SimpleQueue` 무제한** — "자원 상한 완료" 라고 쓰지 말 것).
⚠️ S1a 로 모듈 docstring 이 늘어 두 근거가 갈렸다 — 한 범위로 합쳐 인용하면 낡는다.
⚠️ 즉 기존 `auth_executor` 를 "이미 상한이 있다"는 근거로 인용해서는 안 된다. 그것은 직렬화 장치이지
자원 상한이 아니며, 이 문서가 요구하는 bounded wait 를 대신하지 못한다.
<!-- /rid: R-LOAD-4 -->

<!-- rid: R-LOAD-3 -->
<!-- requirement-meta: disposition=active owner=LOAD -->
<a id="r-load-3"></a>
### R-LOAD-3 — 서버는 동시 도착을 흡수한다
<!-- relation: references target=R-CLI-24 -->
- references: [R-CLI-24](ios-topic-state-machine.md#r-cli-24)
<!-- relation: references target=R-HAND-14 -->
- references: [R-HAND-14](topic-snapshot-handoff.md#r-hand-14)
<!-- relation: references target=R-HAND-16 -->
- references: [R-HAND-16](topic-snapshot-handoff.md#r-hand-16)
<!-- relation: references target=R-HAND-2 -->
- references: [R-HAND-2](topic-snapshot-handoff.md#r-hand-2)
<!-- relation: references target=R-HAND-4 -->
- references: [R-HAND-4](topic-snapshot-handoff.md#r-hand-4)

**서버 — 동시 도착을 흡수하는 쪽이다.**

- topic 별 **single-flight / cache**.
- **bounded wait** — 큐에서 기다리게 하되 무한이 아니다.
- **DB/Redis I/O 자체 상한** (서버 요청 전체 absolute deadline —
  [R-HAND-16](topic-snapshot-handoff.md#r-hand-16) — 만으로는 부족하다.
  그 부족의 사유는 [R-HAND-4](topic-snapshot-handoff.md#r-hand-4) 가 소유한다).
- **bounded wait/queue 후 예산이 소진됐을 때만 close 1013**.
  ⚠️ 여기서 쓰는 close 1013 은 새 신호가 아니라
  [R-HAND-14](topic-snapshot-handoff.md#r-hand-14) 의 서버 snapshot 총 예산 초과 종결과
  [R-HAND-2](topic-snapshot-handoff.md#r-hand-2) 의 출시용 최소 계약이 이미 정한 그 종결이다 —
  대기 없이 곧바로 닫는 용도로 쓰면 위의 "즉시거절 금지"를 우회하는 것이 된다.
- **서버 실패 cooldown** — ⚠️ **실패 fan-out 이 폭주를 재동기화한다**: single-flight 하나가 실패해
  대기 소켓을 동시에 닫으면 재연결이 다시 정렬된다. 서버 실패 cooldown 과
  클라 jitter([R-CLI-24](ios-topic-state-machine.md#r-cli-24)) 가 **함께 있어야** 완화가 성립한다.

근거(baseline): **E1 [코드]** `app/database.py:30` — PostgreSQL `pool_size=3`, `max_overflow=2`
(**최대 5**), 주석은 *"RDS db.t4g.micro 메모리 절약"*. **E2 [코드]** `app/topic_initial_snapshot.py:342-354`
— `for topic in topics: ... await asyncio.to_thread(subscribe_load.timed_call, …, _build_snapshot_sync, topic)` /
**E2-inf [추론]** ⇒ 연결당 **순차**이므로 순간 동시 job ≈ 연결 수 N, 총작업량 N×M.
⇒ 흡수 장치가 없으면 동시 도착 N 이 최대 5 커넥션 앞에 그대로 쌓인다.
<!-- /rid: R-LOAD-3 -->

---

## arming 게이트 — 이 문서가 지는 몫

<!-- rid: R-LOAD-1 -->
<!-- requirement-meta: disposition=proposed owner=None -->
<a id="r-load-1"></a>
### R-LOAD-1 — 게이트 3: 45초 동시 재구독 폭주 완화
[제안·결정 대기]
<!-- relation: references target=R-CLI-24 -->
- references: [R-CLI-24](ios-topic-state-machine.md#r-cli-24)
<!-- relation: references target=R-LOAD-3 -->
- references: [R-LOAD-3](#r-load-3)
<!-- relation: references target=R-LOAD-4 -->
- references: [R-LOAD-4](#r-load-4)

ADR-041 의 파생 숫자 숨김 정책 제안은 **조건부**로만 수용된다. 그 조건인 **arming 차단 게이트 3개**
중 세 번째가 이 문서의 몫이다 — 조건 자체는 ADR-041 의
[R-DEC-5](../DECISIONS.md#r-dec-5) 가 소유한다.

> 3. **45초 동시 재구독 폭주 완화** — [R-LOAD-4](#r-load-4) · [R-LOAD-3](#r-load-3) ·
>    [R-CLI-24](ios-topic-state-machine.md#r-cli-24) 계약이 구현·검증됐는가.

⚠️ 이 게이트가 여기서 `[제안·결정 대기]` 로 표시되는 것은 **완화 계약이 미결정이라는 뜻이 아니다.**
[R-LOAD-4](#r-load-4) 와 [R-LOAD-3](#r-load-3) 은 그 자체로 **확정**이며, 미결정인 것은
"이 셋이 서면 파생 숫자 숨김 정책을 arming 한다"는 **조건부 수용** 쪽이다.
<!-- /rid: R-LOAD-1 -->

---

## nginx 상한

<!-- rid: R-LOAD-2 -->
<!-- requirement-meta: disposition=active owner=LOAD -->
<a id="r-load-2"></a>
### R-LOAD-2 — WS handshake `limit_req` 는 추가, IP별 `limit_conn` 은 계측 후

**nginx — WS handshake `limit_req` 는 추가, IP별 `limit_conn` 은 계측 후.**
이 항목은 **확정**이다(ADR-041 [R-DEC-4](../DECISIONS.md#r-dec-4)).

실측: zone 은 이미 정의돼 있고(`limit_req_zone` / `limit_conn_zone` — `nginx/conf.d/default.conf:19-20`),
**`location /api/` 에만** `limit_req` + `limit_conn` 이 걸려 있다(`nginx/conf.d/default.conf:105-106`).
**`location /ws` 에는 둘 다 없다**(`nginx/conf.d/default.conf:80-99`).

→ handshake 폭주는 `limit_req` 로 막는다. 다만 **IP별 `limit_conn` 은 모바일 캐리어 NAT 위험이 크다**
(한 IP 뒤에 다수 사용자) → **계측 후 결정**. 이 항목은 "신설 vs 위험 수용" 이분법이 아니었다.

근거(baseline): **E4 [코드·부정]** `nginx/conf.d/default.conf` 의 `location /ws` 는 **80~99행** 블록이고
그 안에 `limit_req`·`limit_conn` 이 **없다**(블록 전체를 훑어 확인). `location /api/` 에는 둘 다 있고
zone 정의는 파일 상단에 존재한다.
<!-- /rid: R-LOAD-2 -->
