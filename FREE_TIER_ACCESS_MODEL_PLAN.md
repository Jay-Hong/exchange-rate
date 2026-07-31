# 무료/구독 차등 접근 모델 — ADR 초안 (Draft, rev5 · Proposed 후보)

> **Status**: **Proposed ADR-039** (2026-07-17, [DECISIONS.md](DECISIONS.md) 등재) — 설계 수렴 rev5, codex 5-round. Final 승격엔 제품 결정 **S4만** 남음 (S5=bounded-lease v1 15분 확정 2026-07-25 → 1C 착수 가능 / S6=24h hard cutoff 확정 2026-07-21, 서버+iOS 구현 완료 dev-side). **배포 완료(2026-07-21): 효율화 slice(refresh_not_before) + step 3 무료 hourly endpoint를 FX 3탭(usd/jpy/eur)으로 확장 — 서버 프로덕션 live(acea749), iOS dev-side 활성(3f5801e/e740ed2/57f6648). 출시앱 v1.2.2 legacy엔 무료 탭 미포함 → 실사용자 노출·트래픽 실측은 App Store 배포(별도 GO) 이후. **jpy/eur dev 기능 E2E PASS(2026-07-21).**
> **Scope**: 비구독자 hourly 무료 스냅샷 + 최신-데이터 endpoint 접근 강제 + KRX entitlement 게이트 + iOS·Android 양 플랫폼 legacy 종료.
> **닫는 것**: [ADR-038](DECISIONS.md#L5977) 잔여 Open 2(WS per-user 인증). `krx_visible = G3 ∧ G2 ∧ G1 ∧ premium` 전 표면 준수.
> **supersede/연동**: [REALTIME_ARCHITECTURE_PLAN.md:479](REALTIME_ARCHITECTURE_PLAN.md#L479) 레거시 제거 계약(양 플랫폼 <1% + 6개월)을 §4.4/S4에서 명시적 조정. topic 계약 = [REALTIME_V2_CLIENT_GUIDE.md](REALTIME_V2_CLIENT_GUIDE.md). 전략 = iOS reference → Android 이식.
> **rev5 변경**: codex 리뷰 1 High + 2 Medium. **무료 hourly에서 KRX 항상 제외**(High) / WS 전체-요청 실패 frame + bounded-lease 확정(Medium 1) / Stage B 측정 = store console primary + Android 토큰 신호(Medium 2).

---

## 1. Context

제품 방향(2026-07-10): **비구독자 = 매시간 고정 갱신 스냅샷 / 구독자 = 실시간 WS**. 최신 rate/graph endpoint 대부분 무인증 → 매시간 제한이 UI에만 존재 → 서버 강제 필요.

### 1.1 무인증 최신-데이터 endpoint 전수 (2026-07-17)

작성 시점(2026-07-17) main.py 890~2620 `verify_firebase_token`/`require_premium` 0건.
"호출 확인"과 "공개=차단 대상"은 별개:

| endpoint | 신규앱 caller | 차단 | 상태 |
|---|---|---|---|
| `/api/rates` | iOS ✅ / Android ✅([FXiApiService.kt:25]) | B | 무인증 |
| `/api/rates/{currency}` · `/api/banks/{pair}` · `/api/investing/{pair}` | ❌ (공개=차단대상) | B | 무인증 |
| `/ws` legacy `type:rates` | iOS ✅ / Android ✅([WebSocketService.kt:177]) | B | 무인증 |
| `/api/graph/{currency}` | iOS ❌(dead-runtime) / Android ✅([FXiApiService.kt:28]) | B | 무인증 |
| `/api/v2/topics/snapshot` | iOS ✅ / **Android ❌** | **A** | ✅ **인증+premium+per-user KRX (E3, 2026-07-25)** |
| `/api/v2/graph/tab` · `/api/v2/graph/catalog` | iOS ✅ / **Android ❌** | **A** | 🔴 **무인증 잔여** — §3.1 아래 경고 |
| topic WS (`/ws` subscribe) | iOS ✅ / **Android ❌** | **A** | 🔴 무인증 잔여 — 1C |
| `/api/news` | iOS·Android | §7 S2 | 무인증 |

> ⚠️ **Android = 완전 legacy**(v2 grep 0건). 운영 v1.2.2 실사용은 §5 측정.
> ⚠️ 라인 번호는 자주 어긋나 **제거**했다(구 `:2615`는 실제 `:2745`였다) — 심볼명으로 찾을 것.
> ⚠️ **무인증 표면은 endpoint 목록이 전부가 아니다**: `/openapi.json` · `/docs` · `/redoc`이
> prod에서 무인증 200이다(2026-07-25 실측 — 49 paths, `/admin/api/*` 라우트명 전부 + 핸들러
> docstring + `EntitlementsResponse.krx_visible` 필드명 포함). §3.2 각주 참조.

---

## 2. 확정 결정

- **D1** hourly 스냅샷 = Firebase 인증만(익명·premium 불요). **단 KRX 항상 제외**(§3.1, codex High).
- **D2** strict-신규표면: 최신 realtime 표면(topic WS·v2 snapshot·GraphV2·catalog) = 출시 시 **premium**. hourly는 인증만.
- **D3** 배포본 = iOS·Android 모두 v1.2.2 계열.
- **D4** 신규 앱 legacy anon fallback 금지(양 플랫폼).
- **D5** 무료 그래프 = 실제 hourly-clamped(모의 제외, KRX 없이). 숨김은 비상 fallback만.

---

## 3. 접근 계약

| 주체 | 허용 |
|---|---|
| **무료** (Firebase, 비구독) | hourly 스냅샷만 (rate + real hourly 그래프, **KRX 제외**) |
| **구독** (premium) | topic WS + v2 snapshot + GraphV2 + catalog + hourly. **KRX는 entitlement 추가** |
| **v1.2.2 (iOS·Android)** | legacy REST/WS만. 유예 후 종료 |
| **신규 앱** | legacy anon fallback 금지 |

### 3.1 Stage A 권한 매트릭스 (ADR-038 준수)

| 표면 | 요구 권한 |
|---|---|
| hourly 무료 스냅샷 | **Firebase 인증만 + KRX 항상 제외**(codex High — 무료엔 KRX 불포함, premium+entitled는 최신 경로 사용) |
| 최신 Graph/topic/catalog (비-KRX) | Firebase + premium |
| KRX WS·REST snapshot | Firebase + premium + KRX entitlement |
| Graph catalog/tab의 KRX series | ⚠️ **미구현** — 아래 경고 참조 |

> 🔴 **graph v2 행은 "제외"까지만 참이고 per-user 판정은 아직 없다 (2026-07-26 현재).**
> 구 상태: `_effective_tab_series`가 전역 게이트(G2∧G3)만 봐서 `KRX_CLIENT_DISTRIBUTION_ENABLED=true`
> 한 줄로 **무인증 caller가 KRX 그래프를 받았다**(ADR-038 D3가 수용한 절충이나 §3.1과 모순).
> 현 상태: 무인증 경로는 **어떤 flag로도 krx를 싣지 않는다**(`krx_visible: bool = False` 파라미터
> + serve-time strip, §6.1). 남은 것은 **entitled 사용자에게 다시 보여주는 per-user 게이트** —
> 그게 land해야 이 행이 완전히 참이 된다.

- 판정 = 서버 단일 `krx_visible = G3 ∧ G2 ∧ G1 ∧ premium`. 클라 조합 금지.
- **캐시 = 전역 게이트(G2∧G3) 후 저장 → serve-time per-user(G1∧premium) 필터**. 개인화 결과 공용 키 재캐시 금지.
- **무료 hourly precompute는 KRX 없는 별도 산출물**(무료 endpoint에 KRX가 아예 안 들어가므로 serve-time 필터 불요).

### 3.2 KRX 존재 완전 비노출 계약 (사용자 지시 2026-07-17, authoritative)

**비구독자는 KRX(미국달러선물)의 존재 자체를 알 수 없어야 한다.** KRX는 구독자 중에서도 서버 등록된
일부(entitlement)만 보는 표면이므로, 무료 티어의 어떤 표면에도 다음을 포함하지 않는다:

| 표면 | 계약 |
|---|---|
| 무료 rate payload | `bank=="krx"` / `currency=="usd-krw-futures"` (FX shape) **및** `source=="krx"` / `asset=="usd-krw-futures"` (topic-native shape, 테더 N4) 전부 제외. `_assert_krx_free`는 양 shape 검사 |
| 무료 graph series | `krx.*` id 제외 (서버 `exclude_krx=True` + 클라 어댑터 strip 이중) |
| 무료 UI | 잠금 티저/행/범례/아이콘/명칭 일절 금지 (잠금 카드로도 노출 금지 — codex 티저 제안 기각됨) |
| Paywall 문구·기능 목록 | KRX 미언급 (구독 유도 문구에 "달러선물" 포함 금지) |
| 접근성(VoiceOver) 라벨 | KRX 미언급 |
| 소스/은행 설정 화면 | 무료 경로에서 KRX 항목 미노출 |

> ⚠️ **이 계약의 범위 = 런타임 응답 표면** (2026-07-25 명시). topic 이름 `krx:usd-krw-futures`
> 자체는 (a) `REALTIME_V2_CLIENT_GUIDE.md` §0/§2.5에 공개 문서화돼 있고 (b) prod `/openapi.json`·
> `/docs`가 무인증이라 핸들러 docstring·`EntitlementsResponse.krx_visible` 필드명으로 노출된다
> (2026-07-25 curl 실측). 즉 §3.2가 실제로 보장하는 것은 **"제품 표면(응답 payload·UI·문구·알림)에
> KRX가 나타나지 않는다"**이지 "이름을 절대 알 수 없다"가 아니다.
> 이 한정을 적어두지 않으면 후속 검토가 OpenAPI 노출을 §3.2 위반 blocker로 과대평가한다
> (실제로 한 번 그렇게 보고됐다). **더 엄격히 가려면** prod에서 `docs_url`/`openapi_url`/`redoc_url`을
> 끄거나 `verify_admin` 뒤로 옮기는 별 슬라이스가 필요하다 — 무인증 API 맵이 `/admin/api/*`
> 라우트명까지 담고 있어 KRX와 무관하게도 권장된다(미착수).

- 구현 상태: 서버 3중(legacy allowlist + `exclude_krx` + `_assert_krx_free`) + iOS 3중(어댑터 series strip +
  buildRatesState 명시 제외 + prepared 기반 토글). **테더 N4 land 전 `_assert_krx_free` source/asset shape 확장 필수**
  (topic-native 우회 차단) — KRX가 잠깐이라도 무료 payload/UI에 들어가는 중간 상태 금지.

---

## 4. ADR 4축

### 4.1 인증된 무료 hourly endpoint
- 신규 self-describing endpoint(예: `GET /api/v2/free/snapshot?tab=`), day-1 `verify_firebase_token`, premium 불요, **KRX series 미포함**.

### 4.2 매시간 고정 갱신 계약 — HH:30 basis (2026-07-18 개정)
- **시간 계약(사용자 + codex 2026-07-18)**:
  ```text
  as_of:        매시 HH:30:00 (마지막 :30 경계. 09:00 개장 → 09:30 첫 무료 정보로 30분 만에 장 파악)
  rates/graph:  timestamp <= as_of 인 마지막 데이터만 포함 (쿼리 불변식 — 라벨과 데이터 정합)
  generated_at: 실제 스냅샷 조립 완료 시각
  UI:           "매시 30분 갱신 · HH:30 기준"
  ```
- cutoff은 라벨이 아니라 **쿼리 불변식**: rate = `fetch_rate_entries_until`(`timestamp <= as_of` 각 소스 최신 1건),
  1d graph = `build_tab_1d_payload(now_kst=as_of)`(잘라낼 경계=as_of 고정 — cron 지연/재빌드에도 초과 봉 유입 불가),
  1w/3m/1y = hourly/daily canonical이라 as_of(HH:30) 초과 bucket 자체가 없음. 배경: :00 floor + 최신값 조회 시
  "21:00 기준" 라벨에 21:19 값이 보이는 구조적 mismatch(사용자 실측 2026-07-18).
- cron 매시 :30 발화. keep-last-good(성공+non-empty일 때만 SET) + 원자적 Redis SETEX + long-TTL + 무료용 별도 키(`free:snapshot:{tab}:{period}`).
- **serve 모델(구현 확정, codex 리뷰 반영)**: cron이 **Redis 단독 canonical writer**. serve는 **cron canonical만 반환하고 DB로 재생성하지 않는다** — 이게 무료=1시간 고정의 핵심(serve가 DB 최신값으로 rebuild하면 같은 시간대에도 값이 바뀌어 유료 실시간 차등이 깨짐). serve = Redis canonical read(timeout 2s + serve-time 재검증[KRX/empty fail-closed] + circuit) → 성공값 process-local last-good 보존 → Redis 장애 시 마지막 canonical → **canonical 전무 시 503**(최신값 fabricate 금지). serve는 Redis/DB에 쓰지 않음.

### 4.3 최신 ↔ 무료 분리
- 무료 그래프는 hourly `as_of` 별도 snapshot(최신 GraphV2 재사용 금지).

### 4.4 강제선 / 종료 (2단, 양 플랫폼)
- **soft 전환기** → **Stage A(출시 시)**: §3.1 매트릭스. 선행 = 클라·서버 토큰 전달 + §6 legacy 이탈. behavior-change-0 아님.
- **Stage B(유예 → hard)**: legacy rate/graph 종료 + 웹 디버그 페이지([templates/index.html](templates/index.html):1437/:1862) 처리.

**종료 조건 (양 플랫폼, [REALTIME:479](REALTIME_ARCHITECTURE_PLAN.md#L479) 승계):**
1. **iOS·Android 양쪽 모두** legacy 활성 사용자 < 1%(한쪽만 <1%면 보류).
2. 최소 유예 경과 — **기간은 S4 제품 결정**(기존 6개월 유지 vs 단축).
3. unknown·bot 잔여는 종료 기준 아님.

---

## 5. Legacy 잔여 측정 (Stage B 근거)

**측정 난점(codex Medium 2)**: 구버전은 X-Client-*를 안 보냄. 서버 로그 단독으론 버전 판별 한계.

- **Primary = App Store Connect · Play Console 버전별 활성 기기 비율**(우리 계측 독립, 로그아웃 사용자 포함, 봇 무관). 종료 조건 <1%의 authoritative source.
- **Secondary(서버 보조)**:
  - **iOS legacy**: `FXi/1` UA(약한 신호, REST 토큰 없음 — [APIService] Authorization 전무).
  - **Android legacy**: **Firebase Bearer 토큰 첨부**([NetworkModule.kt:37] auth interceptor → 모든 요청에 `Authorization: Bearer`) → "유효 토큰 + X-Client-* 없음 + legacy endpoint" 조합으로 **실사용자↔봇 구분 + UID 카운트 가능**(봇은 유효 토큰 없음). 서버가 legacy endpoint에서 토큰 presence/UID를 로깅해야 활용.
  - 신규 앱: X-Client-* 헤더.
- **⚠️ client metadata는 출시+채택 후에야 데이터 생성**(§6 step 2는 capability 배포일 뿐 — 관측은 신규 앱 release 이후). 필요 시 **telemetry-only 중간 버전** 선출시 옵션.

---

## 6. 롤아웃 (dormant→flip, 양 플랫폼)

1. 무료 그래프 = real hourly 확정(D5).
2. **iOS·Android 공통 client metadata(`X-Client-Platform/Version/Build`) + nginx log_format/미들웨어**([nginx.conf:23]에 `$http_x_client_*` 추가). **← 최저위험 첫 슬라이스**. ⚠️ 데이터는 신규 앱 release 후 생성.
3. **서버 hourly endpoint(인증 즉시 ON) + 최신 표면 인증 capability 추가하되 최신 표면 enforcement = OFF**(hourly 인증은 dormant 아님).
4. **iOS legacy 이탈**: 4a REST/WS 토큰 전달 → 4b `dxy:spot` topic 신설 → 4c 부팅·offline·stale를 topic snapshot/cache 기준 전환 → 4d /api/rates+legacy WS 제거.
5. **Android 이식**(iOS reference 경화 후, 완전 legacy라 topic client 신규 구현). ℹ️ **REST Authorization interceptor는 이미 보유**([NetworkModule:37]) → REST 인증 거의 free, WS subscribe payload 토큰만 추가.
6. **TestFlight/내부 테스트**(양 플랫폼): 로그인·무료·구독·재연결·토큰 만료·KRX entitlement grant/revoke.
7. **Stage A enforcement ON** + 재검증 후 출시.

### 6.1 KRX 관련 flag는 **둘**이고, 무인증 graph의 KRX는 **flag가 아니라 파라미터**다 (2026-07-26 확정)

| flag | 여는 표면 | 선행 조건 | 상태 |
|---|---|---|---|
| `TOPIC_DISPATCHER_ENABLED` | topic WS subscribe + `/api/v2/topics/snapshot` | ① **E3 REST twin 게이트** ✅ land ② **1C WS 인증** ③ **iOS bootstrap 3종 인증 이관** ④ **entitlement 조회 실패 503**(아래) | 🔴 false |
| `KRX_CLIENT_DISTRIBUTION_ENABLED`<br>(= G2, `KRX_FUTURES_ENABLED`와 AND) | KRX topic 발행/snapshot, KRX 알림 게이트 | 없음(topic 쪽은 E3+1C가 담당) | 🔴 false |

**무인증 graph v2(`/api/v2/graph/tab`·`/catalog`)의 krx.\* series는 어떤 flag로도 열리지 않는다.**
`_effective_tab_series` / `tab_1d_specs` / `build_catalog` / `build_tab` 등이 **호출자가 넘기는
`krx_visible: bool = False`**로 결정한다. 이 endpoint들엔 인증이 없어 넘길 사용자가 없으므로
실제 호출자는 항상 default(False)를 쓴다.

- **왜 env flag가 아닌가** (2026-07-26, codex 2R 수렴): 초안은 `KRX_GRAPH_ALLOW_UNAUTHENTICATED_EXPOSURE`
  라는 default-false 승인 flag였다. 그러나 그 flag의 **유일한 용도가 §3.2 위반 상태를 켜는 것**이라,
  per-user 게이트를 만들지 않은 채 `.env` 한 줄로 열 수 있는 통로가 남는다. 파라미터는 그 통로를
  없애면서도 계약 테스트가 `krx_visible=True`로 **entitled 구성을 그대로 검증**하게 해준다 —
  "계약 보존 vs 우회 통로 제거"가 trade-off가 아니었다.
- **왜 노출 자체를 삭제하지 않았나**: krx-in-graph는 실수가 아니라 **명세된 계약**이다(ADR-038 D3
  Open 2 + D4, GRAPH_API_V2_CONTRACT §3/§4). 파라미터 default를 False로 두면 노출은 닫히고
  계약은 코드·테스트에 남는다.

  ⚠️ **"판정만 넘기면 끝"은 아니다** (2026-07-26 정정, codex): graph 응답은 **사용자 공통 Redis 키**
  (`graph_v2:tab:{tab}:{period}`)를 쓰고 1d precompute도 `krx_visible` 기본값으로 굽는다. 판정만
  endpoint에 흘리면 **캐시 내용이 "누가 먼저 요청했는가"에 좌우된다** — 비인가 요청이 먼저 캐시를
  만들면 entitled 사용자도 KRX를 못 받고(1d precompute는 아예 KRX 없이 구워져 필터를 풀어도 복원 안 됨),
  반대 순서면 비인가 응답은 serve-time strip으로 안전하지만 캐시가 최초 요청에 의존한다.

  **per-user 게이트 slice가 실제로 해야 할 일**:
  ① `/api/v2/graph/tab`·`/catalog`에 **인증 추가**(현재 무인증이라 넘길 사용자 자체가 없다)
  ② **캐시 전략** — §3.1이 이미 처방한 대로 **superset 캐시(krx 포함) + serve-time per-user 필터**
     (개인화 결과를 공용 키에 재캐시 금지). 대안인 visibility별 캐시 키 분리는 키·precompute가 2배.
  ③ 1d **precompute도 superset**으로 굽기(현재 default False)
  ④ WARNING(`graph_v2_krx_stripped`) → debug 격하 (superset에선 제거가 정상 동작)
  ⑤ catalog는 서버 Redis 캐시가 없어 ①만 하면 되지만, 무인증인 한 per-user가 불가능한 건 동일

  **현재 subset 캐시는 §3.1 처방과 의도적으로 다르다**(더 엄격한 쪽): 오늘은 graph KRX를 볼
  legitimate 소비자가 0이라 **캐시에 KRX를 아예 넣지 않는 쪽**이 fail-closed다. superset은
  serve-time 필터를 load-bearing으로 만들므로, 그 전환은 위 slice에서 필터가 실증된 뒤에 한다.
- **guard는 2중이다**: build 경로(series accessor)만 막으면 **캐시 hit이 우회한다**
  (`/api/v2/graph/tab`은 Redis read-through라 hit 시 build를 안 거치고, 과거에 krx가 포함된 채
  구워진 payload가 TTL[최대 30분] 살아 있으며 startup DEL은 예외를 비치명으로 흡수한다 —
  codex Major, 실제 probe로 재현). → 3경로(장기 hit / 1d closed / 1d in_progress) **공통 exit**에
  serve-time `strip_krx_if_not_allowed(payload, krx_visible=False)`: `series` 리스트 +
  `in_progress` seed 두 shape를 copy-on-write로 훑고, 제거가 실제로 일어나면
  WARNING(`graph_v2_krx_stripped`)으로 캐시 잔존을 관측 가능하게 한다.
- **startup fail-fast는 기각**: CLAUDE.md "KRX optional source — baseline은 KRX 없이 항상 정상 동작"
  위반이고, 단일 인스턴스라 KRX **설정** 하나로 전면 장애가 된다. 노출되는 건 선물 가격 시계열
  (개인정보 아님)이라 서비스 중단과 균형이 맞지 않는다. → 해당 표면만 serve-time fail-closed.
- **catalog version**: 기본 응답에서 krx가 영구 제외되므로 `CATALOG_VERSION`을 `2026-07-26`으로
  올렸다(계약 식별자. iOS는 `version`을 decode만 하고 기능적으로 쓰지 않아 무해).
- **pre-flip 필수 항목** (`TOPIC_DISPATCHER_ENABLED=true` 전, 단순 후속 아님):
  ① iOS bootstrap 3종 인증 이관 ② 1C WS 인증
  ③ ~~entitlement 조회 실패의 HTTP 계약~~ → ✅ **land 2026-07-26**: 경계 = `TRANSIENT_DB_ERRORS`
  (`OperationalError`/`InterfaceError`/`TimeoutError`[풀 고갈]/`DisconnectionError` **4종**)
  **∧ `is_transient_db_error`**(영구 SQLSTATE deny-list: 28000·28P01·3D000·42501) →
  503 `{"error": "temporarily_unavailable"}`(WS `subscription_error`와 같은 어휘) + no-store,
  **Retry-After 없음**(PENDING 초 단위 신호와 충돌 + DB failover는 분 단위라 storm. 대신 가이드에
  **유한 재시도**를 계약으로 고정 — 3회 상한 + 조기종료 + 취소 4조건), 판정·빌드 **양쪽** 감쌈
  (한쪽만 감싸면 상태코드가 topic 종류에 따라 갈린다).
  ⛔ `except Exception`은 물론 **`except SQLAlchemyError`도 너무 넓다**(ProgrammingError·
  InvalidRequestError 등 영구 결함 포함). 클래스만으로도 부족해(PEP 249 `OperationalError`는
  인증 실패·DB 부재 같은 영구 케이스 포함) SQLSTATE deny-list를 덧댔다 —
  **allow-list가 아닌 이유**: connect 실패/failover가 SQLSTATE 없이 도착해(psycopg 3.3 실측:
  도달 불가 호스트 → `OperationalError`, `sqlstate is None`) allow-list면 **가장 중요한 케이스가
  500**이 된다. 비용 비대칭도 같은 방향(미지→transient는 재시도 낭비 / transient→permanent는
  회복 가능한 상태 포기).
  ⚠️ **단 그 비대칭은 재시도가 유한할 때만 성립한다** — codex 지적. 초안은 "bounded 재시도"라고
  적었지만 가이드엔 backoff 모양만 있고 **종료 조건이 없어** 계약상 무기한이었다. 이 endpoint는
  best-effort 가속기(정본은 WS snapshot)이므로 **3회 상한 + snapshot 도착 시 조기종료 + lifecycle·
  generation·게이트 변경 시 취소**를 가이드 §3에 명시해 전제를 실제로 만들었다.
  (현 iOS는 이미 만족 — KRX는 `krxBootstrapMaxAttempts=3`, tether/fx는 재시도 자체가 없다.
  위험은 "다음 구현자가 backoff만 보고 무기한 루프를 만드는 것"이었다.)
- 두 flag 모두 2026-07-22 route auth 감사 완화 이후 false다.

---

## 7. 미결 sub-decision

- **S1** 무료 그래프 → RESOLVED = real hourly-clamped(KRX 없이).
- **S2** `/api/news` → 권고 무료 공개 유지.
- **S3** 익명 무료 → RESOLVED = 인증 필수.
- **S4 (제품 결정) legacy 유예 기간**: 기존 6개월([REALTIME:483]) 유지 vs 단축. 권고 = **양 플랫폼 <1% + 최소 30일**(codex, <500명+개편 감안) — 단 **미업데이트 사용자 지원 종료 수용** 결정 필요. **ADR-039에서 명시적 supersede**.
- **S5 (제품 결정) KRX revoke 반영 지연 — RESOLVED = bounded-lease v1, 최대 15분** (2026-07-25, 사용자+codex+Claude).
  즉시 제거(UID registry + Redis 제어 이벤트)는 **별도 후속** — 운영·라이선스 요구가 확인될 때 재검토.
  - **왜 15분**: leak window(권한 상실 후 KRX 수신 지속)를 15분으로 상한. 토큰 수명(~1h)에 정렬하면 지연이 1시간이라 과다.
  - **구현 계약(정본 = §8.1)**: 서버 lease **상한** 15분 — 실제 값은 **가변**이다(§8.1 A1 3-way min).
    클라 재인증은 고정 주기가 아니라 **§8.1 D6 공식**(`max(0, lease_duration_seconds − 180 − U(0,60))`, jitter 하향 전용).
    제거·통지 시점은 topic별 `lease_expires_at`.
    ⚠️ 구 표기("서버 15분 / 클라 ~12분 + jitter / 15분 경계 제거")는 가변 lease 도입으로 **폐기** —
    10분 lease를 받고 12분에 갱신하면 이미 만료다.
  - **핵심**: leak window를 가르는 건 토큰 신선도가 아니라 **서버의 entitlement 재조회 주기**다. 클라가 캐시 ID token을
    보내도(Firebase는 만료 전까지 캐시 반환) 서버가 UID 기준으로 premium/KRX entitlement를 다시 조회하므로 권한 변화는 정확히 반영된다.
  - ⚠️ **구현 규모 실사(코드 확인 2026-07-25)**: "lease timer만 추가"가 아니다. `TopicRegistry`(app/topic_dispatcher.py:46)는
    `Dict[WebSocket, Set[str]]`라 **per-subscription 메타데이터가 없고**, `/ws`(app/main.py:889)는 **현재 무인증**이다.
    → 필요한 것 = (a) subscribe 경로 인증 (b) (ws, topic)별 uid·만료 메타 (c) 만료 sweep (d) `reauth_required` 발신.
- **S6 (제품 결정, codex Medium) last-good 최대 stale 정책**: serve의 process-local last-good은 현재 **무기한**(Redis/cron 장기 장애 시 며칠 stale canonical도 반환 — 실시간 우회는 아님, `as_of`가 정직하게 old 표시). (a) 최대 stale N시간 초과 시 503 전환 vs (b) 무기한 유지 + 클라 UI에 stale 명시. **결정 = (a) 24h hard cutoff** (2026-07-21, 코덱스+Claude+사용자). 구 비결정 혼합(process-local 생존 중 무기한 + Redis ~25h TTL → 재시작 여부로 stale-or-503)을 (now-as_of)>=24h serve 거부로 일관화. **서버+iOS 구현 완료**(서버 d9e60ab: `is_snapshot_too_stale` per-candidate age + stale Redis가 fresh local 안 덮음 / iOS 7050bd3: `.unavailable` + isTooStale age>=24h[future clock-skew 허용] + gated current·displayData[sticky 포함] + **fetch-독립 expiryTask**로 SwiftUI 시간-미관찰 in-flight 만료 공백까지 폐쇄 + 뷰 전파). codex Blocker/High/Medium 0. source-level staleness는 별도 source-health.

> S4·S5·S6는 서버 MVP(step 2·3)와 **독립**. S5=WS(1C) 전 확정 필요했고 **2026-07-25 확정 완료** /
> S4=**Stage B**(legacy 종료) 전 / S6=App Store 출시(실사용자 client) 전.
> ⚠️ 구 표기는 S4·S5를 "Stage B/WS 전"으로 묶었으나, **S4는 WS 인증(1C) 블로커가 아니다**(legacy 종료 시점 결정).

---

## 8. WS 인증 계약 (출시 임계 경로, testable)

> **역할 분리**: **§8 = canonical wire schema**(메시지 형태의 단일 진실 소스) /
> **§8.1 = 구현 불변식**(시간·lock·상태 전이·롤아웃). 구현은 §8의 schema를 그대로 따른다.

### 8-A 클라 → 서버

상태를 바꾸는 모든 메시지는 **`request_id`를 갖고 ack을 받는다**(조용한 실패 금지).

```json
{"type":"subscribe",   "request_id":"<uuid>", "id_token":"<firebase>", "topics":["fx:usd-krw", …]}
{"type":"unsubscribe", "request_id":"<uuid>", "topics":["krx:usd-krw-futures", …]}
```
- `unsubscribe`에는 **`id_token`이 필요 없다** — 권한을 **축소**하는 작업이라 fail-open이다(§8.1 D8).

### 8-B 서버 → 클라

**전체-요청 실패** (토큰 자체 무효 / 형식 위반 / 일시 장애):
```json
{"type":"subscription_error","request_id":"<uuid>","error":"invalid_token"}
{"type":"subscription_error","request_id":"<uuid>","error":"temporarily_unavailable","retry_after_seconds":5}
```

**성공 ack** (subscribe/unsubscribe **공통**):
```json
{
  "type": "subscription_ack",
  "request_id": "<uuid>",
  "operation": "subscribe",
  "identity_generation": 3,
  "accepted_topics": [
    {"topic": "fx:usd-krw", "lease_id": "c7f1…", "lease_duration_seconds": 660}
  ],
  "rejected_topics": [{"topic": "krx:usd-krw-futures", "error": "krx_entitlement_required"}],
  "removed_topics": ["usdt:krw"],
  "active_subscriptions": [
    {"topic": "fx:usd-krw", "lease_id": "c7f1…", "lease_duration_seconds": 660}
  ]
}
```
#### 8-B-stage — **단계형 wire (2026-07-30 개정)**

⚠️ 위 ack은 **lease 기계를 전제**한다(`lease_id` / `lease_duration_seconds` / `identity_generation`).
lease가 없는 단계에서 그 필드를 채우면 **없는 사실을 만들어 내는 것**이고, 빼면 코드가 정본과 다른
**제3의 계약**을 만든다. 그래서 단계를 명시한다 — 구현은 자기 단계의 행을 그대로 따른다.

| 필드 | Stage 1 (**인증된 subscribe 전용**, 현재) | Stage 2 (lease 도입) |
| --- | --- | --- |
| `type` / `request_id` / `operation` | 그대로 | 그대로 |
| `accepted_topics` | `[{"topic": …}]` | `+ lease_id`, `+ lease_duration_seconds` |
| `rejected_topics` | `[{"topic": …, "error": …}]` | 그대로 |
| `removed_topics` | `[]` (제거 전이 미구현) | §C2 eviction 결과 |
| `active_subscriptions` | `[{"topic": …}]` (연결 최종 상태) | `+ lease_id`, `+ lease_duration_seconds` |
| `identity_generation` | **보류(미포함)** | 재검토 후 결정 |

⛔ **컨테이너 형태는 Stage 1부터 최종형(객체 배열)이다.** 문자열 배열로 시작하면 lease가 들어올 때
클라가 shape를 바꿔야 한다 — 그 비용을 피하는 것이 이 표의 요점이다.

⚠️ `identity_generation` 보류 근거: 폐기된 트랙에서 "소비자 0 + 새 소켓은 서버 상태가 처음부터라
reconnect를 전역 구분할 수 없다"는 이유로 제거 결정이 있었다. 그 판단 자체는 archive와 함께
보류 상태이고, **필수 필드로 모델링한 뒤 빼면 breaking**이므로 소비자가 생길 때 결정한다.

⛔ **Stage 1은 `subscribe`만이다.** 위 ack은 정본에서 subscribe/unsubscribe **공통**이지만,
현행 `unsubscribe`는 registry에서 제거만 하고 **프레임을 0개 보낸다**(실측). 표를 "Stage 1 현재"로
읽어 unsubscribe ack이 있다고 오해하지 말 것 — 그건 별도 red 테스트로 구현한다.
(한때 이 표가 그 범위를 적지 않아 과대표현이었다 — codex 지적.)

⚠️ **Stage 1의 알려진 공백 (의도적)**: `TOPIC_DISPATCHER_ENABLED` off일 때 정본은 전 topic
`topics_disabled`를 요구하지만, 현행 구현은 그 지점에서 **조용히 무시**한다(기존 동작). 그 경로를
바꾸면 프로덕션의 현재 상태(flag off)를 건드리고, 오늘 그 조합을 보내는 클라가 없다 —
별도 red 테스트와 함께 닫는다. **누락이 아니라 기록된 유예다.**

⚠️ **판정기 부재의 처리**: per-user 판정이 필요한 topic이 지원 집합에 들어 있는데(배포 flag on)
WS 판정기가 없으면, 그건 transient가 아니라 **설정 결함**이다 → ERROR 로그 + 전체-요청
`temporarily_unavailable`. `topic_unavailable`을 쓰면 안 된다 — 그 코드는 "개별 flag off"를 뜻하고,
flag가 켜진 상태에 쓰면 운영자가 flag를 보고 코드와 모순을 겪는다.

⚠️ **timeout 은 다음 슬라이스 필수 — 그리고 축이 두 개다** (2026-07-30 정정):

1. **wire deadline** — 호출자가 기다리는 시간의 상한. **dispatcher 에** 걸어야 한다: E2E harness 가
   verifier 를 통째로 fake 하므로 verifier 안에 두면 `/ws` 경계에서 영원히 관측되지 않는다.
2. **SDK transport 상한** — 1번만으론 부족하다. `asyncio.to_thread` 작업은 **취소되지 않으므로**
   호출자가 포기해도 스레드는 계속 돌며 **공유 executor** 슬롯을 점유한다(직접 재현). 그 pool 은
   `min(32, cpu+4)` 이고 리포의 `to_thread` 호출부가 함께 쓴다 — 인증 대기가 snapshot·DB 작업까지
   밀어낸다.

⛔ **정정**: 한때 "`httpTimeout` 은 프로세스 전역이라 FCM 까지 조인다"고 적었는데 **틀렸다**(codex).
firebase-admin 6.9.0 은 `app.options.get("httpTimeout", …)` 로 **app 별**로 읽고
(`_auth_client.py:42` 와 `messaging.py:470` 이 각각 자기 app 을 본다), `initialize_app(..., name=…)`
로 named app 을 만들 수 있으며 `verify_id_token(id_token, app=…)` 이 그 app 을 받는다.
즉 **인증 전용 named app 으로 transport 상한을 FCM 과 분리**할 수 있다 — "전역이라 못 한다"는
근거는 성립하지 않는다.

#### 자원 상한 — 열린 항목 2건 (flag ON 전 결정 필요)

timeout 두 축을 넣으면서 **자원 상한**을 판정했는데, 그때 두 개념을 뭉쳐 기각했다. 분리한다.

- ⛔ **admission control(semaphore 즉시거절) = 기각 유지.** 측정: 유입 0.2×용량에서 성공 120→**18**,
  거절 0→**102**. 배포 재연결은 평균 유입이 낮아도 **동시 도착**이라 `sem.locked()` 가 즉시 참이
  되고, 1초면 빠질 큐를 대량 거절한다. 이 워크로드의 모양과 정반대다.
- 🔲 **격리(인증 전용 executor) = 미결.** 이건 admission control 이 **아니다** — 거절하지 않고
  자원을 나눌 뿐이라 새 wire 결과가 없다. 측정: 동거 작업 p99 **3967ms → 31ms**.
  ⚠️ 대가: 워커 수 W 가 곧 인증 처리 용량(W/T)이라 잘못 잡으면 스스로 문턱을 낮춘다. 그리고
  `/ws` 프레임으로 관측되지 않아 구조 트립와이어로만 잠긴다.
- 🔲 **ingress 상한(nginx `/ws`) = 미결.** 실측: `/ws` location 블록에 `limit_req`·`limit_conn` 이
  **없다**(주석도 "Rate Limit 없음"). `/api/` 만 3r/s + conn 10 이 걸려 있다. 즉 인증 이전 단계에서
  토큰 flood 를 막는 것이 없다. **호스트 config 변경이라 별도 승인이 필요하다.**

⚠️ 큐 자체는 caller deadline 이 드레인하지만(취소가 concurrent future 로 전파돼 dequeue 시 skip),
큐 **크기**는 유입률 × deadline 이고 고정 상한이 없다 — 위 두 항목이 그 상한을 정하는 자리다.

⚠️ 그래도 SDK 재시도가 남는다: `accounts:lookup` 은 per-attempt 120초 기본 + status 재시도 4회
(`_http_client.py:41-52`)라, transport 상한을 낮추지 않으면 한 subscribe 가 분 단위로 스레드를
점유할 수 있다.

- `operation`: `"subscribe"` | `"unsubscribe"` — 같은 schema를 쓰므로 구분자가 필요하다.
- `accepted_topics`/`rejected_topics`/`removed_topics` = **이번 요청의 결과**.
- **`active_subscriptions` = 그 연결의 최종 상태 전체**(topic별 `lease_id` + 남은 duration). 클라는 이걸로 수렴한다.
- **`lease_id`는 재사용 불가한 opaque 값**(§8.1 D2) — 정수 generation을 쓰면 제거 후 재구독 시 초기화돼
  과거 `reauth_required`가 새 lease와 오인 일치할 수 있다.

**lease 만료 통지**:
```json
{"type":"reauth_required","topics":[{"topic":"krx:usd-krw-futures","lease_id":"c7f1…"}],"reason":"lease_expired"}
```
- 클라는 **자기가 들고 있는 `lease_id`와 다르면 무시**한다(늦게 도착한 구 통지 방어).

### 8-C 오류 코드

| 코드 | 범위 | 의미 |
|---|---|---|
| `invalid_token` | 전체 | 토큰 무효·만료·revoked |
| `temporarily_unavailable` | **전체(항상)** | 인증·권한을 **판정할 수 없음**. **`retry_after_seconds` 동반**, registry 불변 |
| `invalid_request` | 전체 | 형식 위반 / UUID 오류 / 빈 topics / 중복 정규화 후 빈 목록 |
| `request_too_large` | 전체 | 상한 위반(전송 계층은 **close 1009** — 클라이언트 관측) |
| `topics_disabled` | per-topic | 서버 topic 기능 off |
| `unknown_topic` | per-topic | 미지원 topic |
| `premium_required` | per-topic | 구독 필요 |
| `krx_entitlement_required` | per-topic | KRX entitlement 필요 |
| `topic_unavailable` | per-topic | topic 자체가 서버에서 비활성(개별 flag off) — REST twin이 이미 사용(main.py:2771) |

**flag → 코드 매핑** (없으면 "accepted + lease인데 데이터가 영원히 안 오는" 상태가 생긴다):

| flag | off일 때 |
|---|---|
| `TOPIC_DISPATCHER_ENABLED` | 전 topic `topics_disabled` |
| `FX_TOPIC_ENABLED` 등 topic별 flag | 해당 topic `topic_unavailable` (accepted 금지) |
| `KRX_CLIENT_DISTRIBUTION_EFFECTIVE` | krx topic `topic_unavailable` |

> §3.2(KRX 존재 비노출)와의 관계: 무료 사용자는 D5 **4단계에서 `premium_required`로 먼저 거부**되어
> 5단계(`krx_entitlement_required`)에 도달하지 않는다. premium이면서 entitlement가 없는 조합은 §3.1이
> 명시적으로 허용하는 상태이므로 `krx_entitlement_required` 노출은 §3.2 위반이 아니다.

- 판정은 단일 전순서가 아니라 **처리 단계**를 따른다(§8.1 D5).
- 검증 실패 topic은 registry에 **등록하지 않는다**.

### 8-D 재인증·만료

- **재인증**: 클라는 ack의 **`lease_duration_seconds`에서 유도한 시점**에 `id_token`을 재첨부해 다시 subscribe한다
  (고정 주기 아님 — §8.1 D6 공식).
- **만료**: 서버가 registry에서 해당 topic을 제거하고 `reauth_required`를 보낸다.
  **LEASE 상한 = 15분**(S5). 실제 부여 lease는 authoritative 확인 시점에 묶여 **가변**이다(§8.1 A1).
- 구현 시 **KRX per-user 강제 동시 해소**(ADR-038 Open 2 close).

### §8.1 1C 구현 설계 — **폐기 (2026-07-30)**

> 이 자리에 1C 구현 설계 약 765줄(A 시간·인가 / B 강제 / C 전이 / D wire / E 롤아웃 / F iOS 입력 /
> G 테스트 매트릭스 / H 문서 갱신)이 있었다. **삭제했다.**
>
> **왜**: 그 설계로 만든 구현은 소비자를 한 번도 갖지 못한 채 81커밋 동안 자랐고, 그 과정에서
> 이 절이 전제한 것 여럿이 반증됐다(노출 상한 산술 · 관측 시각의 출처 · 실패를 접는 방향과 위치 ·
> 요청 단위). 폐기 시점에 이 본문에는 **부정확한 것으로 확인된 서술**이 남아 있었고(어긋난
> 파일:라인 포인터, 과소평가된 유한 상한, 이후 제거된 wire 필드), 머리에 경고만 달아 두면
> `rg` 결과나 부분 인용은 **경고를 지나쳐 본문에 도달한다**. 그래서 본문을 남기지 않는다.
>
> **어디에 있나**: 태그 `archive/adr039-dormant-2026-07-30` (설계 문서·코드·테스트 전량).
> **무엇을 배웠나 / 어떻게 다시 시작하나**: `DECISIONS.md`의 **ADR-040** — 착수 전 그것부터 읽을 것.
>
> ⛔ archive에서 이 절을 되살려 조항 목록으로 쓰지 말 것. 폐기 이유가 "코드가 나빠서"가 아니라
> **"소비자 없이 자라 검증의 종료 조건이 없어서"**이므로, 되살리면 그 조건이 그대로 돌아온다.

#### §8.1이 남긴 것 — 리셋 후에도 유효한 롤아웃 제약

구현 설계와 달리 아래는 **현행 코드·운영에 대한 사실**이라 그대로 살아 있다.

- **무토큰 subscribe는 현재 무시되지 않고 실제로 등록된다.** topic dispatch flag가 켜지면
  `id_token`/`request_id` 검사 없이 registry 등록 + snapshot 전송이 일어난다(`app/topic_dispatcher.py`의
  subscribe 분기). 현행 iOS도 무토큰 payload(`{type, topics}`)를 보낸다. 구 문서의 "구형 무인증
  메시지는 silent-ignore" 서술은 **오서술이었다**.
- **enforcement는 capability와 분리해야 한다.** 인증 capability를 배포하되 강제하지 않는 중간 상태
  (무토큰은 기존대로 등록되고 만료·재인증이 돌지 않음)가 있어야 dormant→flip이 all-or-nothing이 아니다.
  ⚠️ **강제 전환 시점에 구 클라(무토큰)는 topic을 잃는다** → legacy 유예(S4)와 **같은 시점**에 묶어야 한다.
- **REST twin 인증 게이트는 land됐다**(2026-07-25). `GET /api/v2/topics/snapshot`이 인증+premium+
  per-user KRX를 강제한다. 이것이 선행 조건이었던 이유: **1C는 topic dispatch flag를 켜야 동작**하는데,
  그 flag가 현재 무인증 KRX REST를 막고 있던 유일한 장치였다.
  ⚠️ **잔여(flag ON 전 필수)**: iOS `APIService`의 bootstrap 3종이 아직 무인증 경로라 flag ON 시
  401을 받는다. 전부 `try?` 격리라 크래시는 없고 cold-start bootstrap만 조용히 사라진다 →
  인증 transport로 이관 필요. 운영은 현재 flag off라 사용자 영향 0.
- **구매 수렴 계약이 enforcement 활성화의 blocker다.** 서버측 무효화·freshness만으로는 구매가
  수렴하지 않는다 — 둘 다 "서버가 다시 물어볼 준비"만 시키고, 실제 복구 트리거는 **클라의 다음
  subscribe**인데 `premium_required`가 **terminal**이라 클라가 재시도하지 않는다. 그래서 시간이
  지나도, webhook이 와도 그 연결은 거부 상태로 남는다. 활성화 전에 "구매 완료 → 제한된
  refresh/backoff → 정상 ack" 클라 경로가 필요하다.
  ⚠️ forced refresh도 즉시 복구가 아니다 — 우리 캐시만 우회할 뿐 RevenueCat 자신의 전파 지연은
  못 넘는다(구매 직후면 다시 "구독 없음"을 받는다).
- **`check_revoked`는 별도 정책이 아니라 인가 상한에 통합**한다 — 계정 삭제·비활성도 같은 상한 안에서 종료.

## 9. 구현 체크리스트

- [ ] 전 라우트 auth 감사 — 누수 0.
- [x] iOS·Android 공통 client-version metadata + nginx 로깅 (step 2 land 2026-07-17: server `55ab1d8` / iOS `1b736f2` / Android `5f93409`. 데이터는 신규 앱 release 후 생성 — nginx deploy/reload + 실 로그 cp/cv/cb 확인 별도).
- [x] hourly endpoint(인증만, **KRX 제외**, self-describing) + 매시간 계약(§4.2) — **step 3 land + 배포 2026-07-17** (app/free_snapshot.py + GET /api/v2/free/snapshot, MVP=usd·1d/1w/3m/1y). workflow 설계+adversarial + **codex MCP 다라운드**: B1~B4/N5/N6/NB → **freshness 불변식(serve canonical-only, DB 재생성 제거)** → validator Pydantic 스키마. **2026-07-18 HH:30 basis 개정(§4.2)**: as_of=마지막 HH:30 + `timestamp <= as_of` cutoff 쿼리 불변식(fetch_rate_entries_until + build_tab_1d_payload now_kst 주입) + cron :30 — 구 :00 floor의 라벨↔데이터 mismatch(사용자 실측) 해소. **S6 결정=(a) 24h hard cutoff(2026-07-21) — 서버+iOS 구현 완료(dev-side)**: 구 비결정 혼합(process-local 무기한 + Redis 25h TTL)을 (now-as_of)>=24h 렌더 거부로 일관화. **서버**(d9e60ab: `is_snapshot_too_stale` per-candidate age, stale Redis가 fresh local 안 덮음). **iOS**(7050bd3: `LoadState.unavailable` + isTooStale age>=24h[future asOf는 clock-skew 허용] + current/displayData gated[sticky 포함] + **fetch-독립 expiryTask**로 SwiftUI 시간-미관찰 in-flight 만료 공백까지 폐쇄 + 뷰 전파. codex Blocker/High/Medium 0). 실사용자 노출은 App Store 배포 후. iOS USD reference slice land(987d161). **FX 3탭 확장(2026-07-21)**: `FREE_SNAPSHOT_TABS=("usd","jpy","eur")` — jpy/eur는 usd와 동일 코드 경로(bank/investing rate reader + source_daily/hourly canonical + 1d intraday) 재사용, `TAB_ASSET`만 차이(별도 reader 0). tether는 free reader가 source_rates(거래소 데이터) 미조회로 rate가 비어 N4(별도 reader) 전까지 제외. 프로덕션 read-only 실증(jpy/eur 4기간 non-empty·asset 정확·KRX-free) + 계약 테스트 +3 + **프로덕션 배포(acea749, 2026-07-21)**. **iOS 활성화 완료(step 4, 57f6648) — freeConfig switch(usd/jpy/eur)로 FX 3탭 실렌더 dev-side 활성(합성 시뮬 게이트 포함)**. **jpy/eur dev 기능 E2E PASS(사용자 dev 빌드 2026-07-21)**: 그래프 4기간/기본 토글 investing+hana/은행목록/알림 통화(jpy-krw·eur-krw)/KRX·DXY 미노출 + VM-hoist(News 왕복·인접 왕복 재요청 0, 3탭×4기간=12 GET 초기 warm만) 전부 확인. 실사용자 노출·트래픽 실측은 출시앱 배포(별도 GO) 이후.
- [x] **테더 N4 (무료 테더 탭 백엔드 + iOS N4-4 + graph 하드닝)** — 무료 테더 rate는 grouped shape(`kind="source_grouped"`: `usdt_krw`[거래소 5] + `usd_krw_banks`[kb·hana] + `usd_krw_reference`[investing singleton], `primary_asset="usdt-krw"`)로 도입(FX는 flat `{asset,entries}` 그대로). **N4-1 land**(58b9d9c): `_assert_krx_free`가 FX shape(bank/currency)뿐 아니라 topic-native shape(source/asset)도 검사 → 테더 KRX(달러선물) 우회 차단. **N4-2a land**(grouped-aware validation 리팩터, codex Blocker/Medium 2라운드 반영): `_iter_rate_items`(shape 무관 순회 단일 진실소스)로 nonempty·within-as_of·precompute 카운트 통일 + rate.asset(grouped=primary_asset) grouped-aware + **3중 hybrid/KRX 방어** — (1) `_FreeRate`/`_FreeRateGrouped` `extra="forbid"`(반대 shape 컨테이너 키를 extra로 얹은 hybrid를 schema서 거부, 양방향) (2) `_assert_krx_free`가 `_iter_all_rate_dicts`로 entries+3그룹을 **kind 무관 동시 스캔**(hybrid가 반대 컨테이너에 KRX를 숨기는 우회 폐쇄, belt-and-suspenders) (3) `_GROUPED_RATE_TABS` **tab↔shape 결합**(grouped-USD/flat-tether 오염 canonical을 fail-closed 거부 → 구 FX client 깨진 200 회귀 차단). graph 절반은 코드 완료(tether series 정의 + `exclude_krx` 필터). **N4-2b land**(cutoff-aware grouped tether reader): 유료 토픽 grouper `build_tether_tab_payload`(정규화·정렬·investing singleton 무결성) 재사용 + cutoff fetcher만 신규 — `crud.get_source_rates_until`(source_rates cutoff 변형, `timestamp<=as_of`, rate_changed_at 미포함[정적 스냅샷]) + `free_snapshot.fetch_tether_grouped_rate_until`(거래소 5=source_rates / kb·hana·investing=usd-krw는 기존 FX cutoff reader 재사용 후 선별) → `build_free_snapshot_payload` tab 분기(tether=grouped / FX=flat). free↔paid shape 일치(iOS 단일 어댑터). dup-source/wrong-asset은 각 source explicit query라 자연 차단(entitlement 우회 아님, KRX는 조회 경로에 아예 없음 + `_assert_krx_free` belt-and-suspenders). codex 2라운드(N4-2a Blocker/Medium + N4-2b) 통과. **N4-3 land + 배포 + 프로덕션 verify**(08db271, 2026-07-22): `FREE_SNAPSHOT_TABS`에 tether 추가(precompute 순회 + endpoint 게이트 단일 진실소스 → 자동 활성) + build 분기 + serve tab↔shape 결합. codex 3라운드 통과. EC2 배포(build+force-recreate) 후 precompute 트리거 + Redis canonical 직접 검증 — **4기간(1d/1w/3m/1y) 전부 validate=True / kind=source_grouped / primary_asset=usdt-krw / as_of=HH:30 / stale=False / rate=거래소5(upbit·bithumb·coinone·korbit·gopax)+kb·hana+investing / KRX series·rate 0 / graph 실데이터 non-empty(1d 10/10 각 144pt · 장기 4/4)**. 서버 활성은 dormant(엔드포인트 auth-gated, 소비 client는 App Store 배포 후). 테스트 free_snapshot 72 passed / 전체 3604. **N4-4 land + dev E2E PASS(2026-07-22)**: iOS grouped adapter(FreeRate flat/grouped, kind-peek)/group-aware allowlist(KRX fail-closed)/코어 탭(source 바)/그래프(거래소 5토글·DXY↔선물 상호배타·구역 프리미엄 정합)/3 알림 preview(거래소 가격·김프·비교, no-persist + malformed-rate 크래시 가드)/라우팅(테더-first, 인증 게이트). **+ graph fail-closed 하드닝(codex 6라운드 Blocker 0)**: per-tab ID allowlist(4탭 카탈로그 1:1) + envelope 결합(assertEnvelope/decodeAndValidate behavioral) + content-level KRX guard(서버 `_assert_krx_free` graph + 클라 `hasKrxContaminatedPoint`, 4마커 대칭) — 서버 257a05d 배포·검증 / iOS 1cfa978·778d294·9c255eb. 실사용자 노출은 App Store 배포(TOPIC_V2 arming, 별도 GO) 후.
- [ ] §3.1 매트릭스 + 캐시 G2∧G3 전역 → serve-time G1∧premium.
- [ ] iOS 4a~4d → Android 이식(REST interceptor 재사용).
- [x] **E3 REST twin 게이트** — `GET /api/v2/topics/snapshot`에 인증+premium+per-user KRX 강제 (2026-07-25 서버 land,
      §8.1 E3). `TOPIC_DISPATCHER_ENABLED=true` 선행 조건. 잔여 = iOS bootstrap 3종 인증 이관(F 슬라이스).
- [ ] WS 계약(§8): subscription_error + ack accepted/rejected + bounded-lease(15분) + reauth_required. **← 1C 진행 중**
      — A1/A2 **산술** land(2026-07-27, `app/clock.py` + `app/topic_lease.py`, 배포 없음).
      다음은 strict cache 저장(`verified_at_monotonic`) → A6 3-state verifier → A5 single-flight → 배선.
- [ ] 웹 디버그 페이지 Stage B.
- [ ] Stage B 측정: store console primary + 서버 보조(iOS UA / Android 토큰+UID).
- [x] **제품 결정 S5(revoke latency) = bounded-lease v1 15분 확정(2026-07-25)** — §7 S5 / §8 만료 항목 참조.
- [ ] **제품 결정 S4(유예 기간·supersede) — Stage B 전 확정**. ⚠️ S4는 *legacy를 언제 끄느냐*라 **Stage B 게이트**이고
      WS 인증(1C) 구현의 블로커는 아니다(구 표기는 S4·S5를 "Stage B/WS 전"으로 함께 묶어 오해 소지).
- [x] **제품 결정 S6(last-good 최대 stale) = 24h hard cutoff 확정(2026-07-21)** — **서버(d9e60ab, 배포)+iOS(7050bd3, dev-side) 구현 완료**((now-as_of)>=24h 렌더 거부; 서버 503 + 클라 `.unavailable`+fetch-독립 expiryTask). 실사용자 노출은 App Store 배포 후. source-level staleness는 별도 source-health. + [ ] validator value-level 완결(entry.rate 타입/point 내부).
