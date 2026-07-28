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

### §8.1 1C 구현 설계 (test-first 기준선, 2026-07-25 확정)

§8 wire contract 위에, "lease 15분"과 per-user 인가를 **실제로** 보장하기 위한 구현 경계.
codex 다라운드 감사 + 코드 실사로 수렴. **G의 테스트 매트릭스가 착수 단위**다.

#### A. 시간·인가 모델

- **A1 lease horizon** — lease 만료를 부여 시점이 아니라 **마지막 authoritative entitlement 확인 시각**에 고정한다.
  ```
  lease_expires_at = min(
      now                            + LEASE(15분),
      premium_verified_at            + LEASE,   # RevenueCat entitlement (authoritative)
      firebase_identity_verified_at  + LEASE,   # check_revoked=True 확인 (authoritative)
  )
  ```
  **authoritative한 것은 모두 같은 horizon 모델을 탄다** — entitlement든 identity(revocation)든,
  마지막 확인 시점 + 15분을 넘겨 lease를 줄 수 없다. 특례 없음.
  - ⚠️ **identity *verdict*는 토큰 단위다 — 단, *관측*은 UID 키가 맞다**(§A6-1 해법). UID 단위로 **verdict를** 캐시하면 **같은 UID의 새 토큰
    검증 결과를 revoked된 구 토큰이 공유**해 권한이 섞인다.
    (entitlement horizon은 UID 단위가 맞다 — 권한은 계정 속성이다.)
    - ✅ **해법 확정 (2026-07-27, SDK 소스 검증)**: UID 단위로 **관측(authority record)** 을 캐시하고
      **verdict는 요청별로 파생**한다 → UID 키를 쓰면서도 공유 사고가 구조적으로 불가능하다(§A6 2계층).
    - ⛔ **`auth_time`이 아니라 `iat`로 비교한다.** firebase-admin v6.9.0의
      `_check_jwt_revoked_or_disabled`가 쓰는 술어는
      `verified_claims['iat'] * 1000 < user.tokens_valid_after_timestamp` 이다(`_auth_client.py`, 소스 확인).
      `auth_time`은 세션 최초 로그인 시각으로 **고정**되고 `iat`는 갱신마다 전진하므로
      (`auth_time ≤ iat`), `auth_time`으로 대체하면 SDK보다 **엄격한 다른 술어**가 된다 —
      단위 테스트로는 드러나지 않는 의미 버그다. **구 서술("각 토큰의 `auth_time`과 비교")은 폐기.**
    - **단위**: `tokens_valid_after_timestamp`는 **밀리초** epoch(`_user_mgt.py`: `1000 * int(valid_since)`),
      `iat`는 **초** epoch. 축을 섞으면 **모든 토큰이 revoked로 오판**된다. 비교는 strict `<`
      (같으면 revoked 아님), **disabled를 revoked보다 먼저** 검사, `clock_skew_seconds`는 이 비교에 **미적용**.
  - 왜: 권한 상실 시각 L 이전의 마지막 authoritative 확인 V(≤L)로 부여된 lease는 최대 `V+15 ≤ L+15`에 만료
    → **총 revoke 상한이 정확히 15분**. lease 15분 정책도 그대로 유지된다.
  - 4분 전 확인한 캐시를 쓰면 이번 lease는 약 11분만 부여된다(짧아진 만큼 클라가 더 일찍 재인증).
  - **stale fallback 결과로는 lease를 연장하지 않는다.** horizon이 부족하면 authoritative 갱신을 시도한다.
  - **불변식**: `CACHE_TTL < LEASE`. 아니면 lease가 0으로 수렴해 재인증 storm이 된다.
    현재 `CACHE_TTL=5분`(`app/subscription.py`의 `CACHE_TTL`) < 15분.
    ⚠️ **"최소 lease 10분"은 `CACHE_TTL < LEASE`만으로 성립하지 않는다** — 그건 entitlement 항에만
    거는 상한이다. 3-way min이므로 **`firebase_identity_verified_at ≈ now`라는 E2 전제**가 함께 있어야
    한다(E2는 `check_revoked=True`를 horizon 갱신 시에 수행하므로 갱신 직후엔 성립).
    identity 확인이 오래됐다면 결과는 10분보다 짧거나 **이미 만료**일 수 있다 — 그 stale identity
    케이스도 테스트 대상이다.
    (⚠️ **이 문서의 코드 참조 규약**: `파일:라인`이 아니라 **심볼 앵커**를 쓴다 — 라인은 같은 커밋
    안에서도 밀린다. 실측 사례 2건: 2026-07-26 seam 커밋에서 `app/subscription.py` 4건이 5줄씩,
    §B5·§D7의 `main.py:928`은 실제 994행이라 66줄 어긋나 있었다. 다시 숫자로 바꾸지 말 것 —
    **다른 줄을 가리키는 각주 포인터도 같은 이유로 금지**한다.)
  - ⚠️ **`authoritative_verified_at`도 monotonic이어야 한다**(A2와 같은 축). 현행 캐시는 wall clock으로
    나이를 잰다(`EntitlementCache.get`의 `age = clock.wall() - cached_at` — 2026-07-26 seam 이후) — wall clock이
    역행하면 캐시가 실제보다 **젊게** 보여 horizon이 늘어나고 상한 증명이 깨진다.
    → WS strict 경로용으로 **`verified_at_monotonic`을 별도 저장**한다(REST 경로의 wall-clock 나이 계산은 불변, A4).
- **A2 시간 기준** — 서버 deadline은 `time.monotonic()`, 만료 판정은 **`now >= expires_at`**(경계 포함 = fail-closed).
  - 구현 노트: `Clock.mono`는 **기본값 없이 필수 주입**한다(wall과 동일). 기본값을 주면 호출부가
    빠뜨려도 실클럭으로 조용히 동작해 테스트에 실시간이 섞인다 — wall 축에서 같은 이유로 필수로 했다.
  lease deadline과 `authoritative_verified_at`은 **같은 monotonic 축**이어야 한다(A1 마지막 항목 — 축이 섞이면 상한 증명이 무의미).
  **그 축을 wall로 두면** wall clock 역행(NTP step / VM restore) 시 상한을 넘긴다(전진은 조기 만료 =
  안전한 방향). ⚠️ 이 위험은 **deadline 축에서는 해소됐고**(2026-07-27 monotonic land)
  **horizon 입력 축에는 아직 남아 있다** — WS 인가가 쓸 수 있는 유일한 관측 나이가 아직
  `EntitlementCache.get`의 wall 기반 계산뿐이기 때문이다.
  ⛔ **닫는 방법을 오해하지 말 것**: 그 wall 계산을 monotonic으로 **바꾸는 게 아니다**.
  A4대로 REST의 stale fallback과 wall-clock TTL은 **그대로 유지**하고, WS strict 경로가 그 REST 캐시를
  **인가 horizon 입력으로 쓰지 않도록** `verified_at_monotonic`을 **별도 저장·사용**해 분리한다
  (A1 마지막 항목). 즉 해법은 *교체*가 아니라 *경로 분리*다.
  registry가 in-memory라 프로세스 재시작 시 연결·구독이 함께 소멸 → 재시작 간 deadline 보존이 불필요하다.

  ✅ **A1/A2 산술 land (2026-07-27)** — `app/topic_lease.py`(`LEASE_MAX_SECONDS` /
  `compute_lease_expiry` / `is_expired`) + `app/clock.py`(`Clock.mono` 필수 주입 +
  `system_clock`). **순수 계산기만**이고 저장·배선은 후속이다(닫힌 G 행은 G 절 하단 참조).
  구현하며 확정한 세 계약:
  - **계산기는 `Clock`이 아니라 `now_mono: float`를 받는다.** D1("한 ack의 accepted들은 **같은
    순간** 갱신")·D2("lock 아래 **단일 snapshot**")를 지키려면 `now`를 **호출부가 요청당 1회**
    읽어 그 요청의 전 topic에 공유해야 한다. 계산기가 스스로 시각을 만들면 그 성질을 단위
    테스트로 잠글 수 없다(harness (5) `sweep_once(now)`, `app/atomic_retry.py`와 같은 형태).
    ⚠️ B1 전송 직전 재검증은 **새로** 읽는다 — 그 시점의 만료 여부를 봐야 하므로.
  - **비유한 입력 fail-closed** — `compute_lease_expiry`는 `ValueError`, `is_expired`는 만료로
    접는다. `min`이 NaN을 첫 인자일 때만 전파해(`min(1.0, nan, 2.0) == 1.0`) 가드가 없으면 NaN
    horizon이 조용히 무시되고 **상한 전량** lease가 나간다. `inf` sentinel은 영구 lease가 된다.
  - **gross future skew 거부**(신규 계약) — 관측 시각이 `now`보다
    `MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS`(= `LEASE_MAX_SECONDS`에서 파생) 넘게 미래면
    `ValueError`. 축 혼동(`time.time()` 값 혼입)이면 `min`이 `now`를 골라 **항상 상한 전량**
    lease가 나가 A1의 "총 revoke 상한 15분" 증명이 조용히 무효가 되는데, 그 실패가 정상 동작과
    구별되지 않기 때문이다. 미세한 미래는 `min`이 clamp하므로 통과시킨다.
    ⚠️ 이 `ValueError`는 **프로그래밍 오류**다 — 배선 슬라이스에서 광범위 `except Exception`이
    이것을 C4 `temporarily_unavailable`(retryable)로 접으면 재시도 storm이 된다.
  - ⚠️ **primitive가 막지 못하는 것**: 세 인자가 **모두** wall epoch면 서로의 관계가 정상이라
    가드를 통과한다(결과 = epoch+900 → 진짜 monotonic now와 비교 시 수십 년간 미만료). 축 보증은
    배선 슬라이스 몫 — 세 값이 같은 주입 `Clock.mono`에서 나올 것 / A6의 `active`만 계산기에
    도달할 것 / 계산 결과가 이미 만료면 등록·ack accepted를 하지 않을 것.
  - ⚠️ **"요청당 mono 1회"는 아직 강제되지 않는다.** float 주입이 그 강제를 *가능하게* 만들 뿐,
    계산기의 프로덕션 소비자가 0개라 잠글 대상이 없다. `tests/test_topic_lease.py`의
    `TestDocumentedCallerPattern`은 **실행 가능한 사용 예시 + 시그니처 잠금**이고, 실제 강제는
    ack/registry 호출부를 구동하는 **배선 슬라이스의 통합 테스트** 몫이다.
- **A3 "15분"의 기준점** — **우리가 권한 상실을 authoritative하게 관측한 시점**부터다.
  외부 스토어 → RevenueCat 전파 지연은 서버 lease가 보장할 수 없다. 문서·운영 커뮤니케이션에서 이 경계를 흐리지 말 것.
- **A4 REST와 분리** — REST의 stale fallback(최대 1시간, `app/subscription.py`의 `CACHE_STALE_TTL`)은 **그대로 유지**한다.
  strict 검증은 **WS 인가 경로에만** 적용한다(가용성 우선 표면과 권리 보호 표면의 정책을 분리).
  - ⚠️ **webhook 무효화 범위 확장 + epoch fence**: `invalidate_user_cache`(`app/subscription.py`)는 현재
    `_cache`와 `_pending`만 지운다. 신규 **strict cache(`verified_at_monotonic`)와 single-flight 결과도 함께 무효화**해야
    한다. **그것만으로는 부족하다** — `검증 시작 → webhook invalidate → 구 검증이 ACTIVE로 완료 → cache 재기록`
    경쟁이 남는다. **UID별 epoch를 증가**시키고 owner가 **캡처한 epoch와 일치할 때만 결과를 게시**한다
    (1B provider의 owner/epoch 패턴과 개념 동일).
    ⚠️ **게시만이 아니라 소비(lease 발급)까지 fence**해야 한다 — 폐기된 검증 결과로 새 15분 lease를 발급하면
    무효화가 무의미하다.
    - **소비 fence 방식 확정 (2026-07-28)** — `등록(비활성) → is_current 1회 재확인 → 활성화` 순서다.
      `snapshot()`의 epoch를 lease에 실어 두고 **활성화 직전에 한 번** 재확인한다.
      - **왜 충분한가**: epoch는 전진만 하므로 재확인 **이전**의 `bump`는 반드시 잡힌다. 재확인 **이후**의
        `bump`는 *정상 발급 직후 webhook이 온 것*과 구별되지 않는 통상적 중도 무효화이고,
        `compute_lease_expiry`가 `verified_at_mono`에 고정된 3-way min이라
        `expiry <= premium_verified_at + LEASE_MAX < bump 시각 + LEASE_MAX` — **A1/A3의 15분 상한 안**이다.
        따라서 공유 lock도, registry↔store 결합도 필요 없다.
      - ⛔ **`is_current()`만으로는 fence가 아니다** — `bump()`는 strict cache의 lock만 잡으므로
        **B4 lock을 쥔 채 호출해도 배제되지 않는다**. (구 문구 "critical section 안에서 호출"은 오류였다.)
      - ⛔ **매 전송마다 epoch를 검사하지 말 것.** 이 조항이 fence하라는 것은 *발급*이고 **B1의 검사 항목에
        epoch 항은 없다**(§B1). 확대하면 A4-2의 `unknown → invalidate`(denylist `TEST` 단독)와 곱해져
        `RENEWAL`·`PAYWALL_*` 같은 고빈도·비-entitlement 이벤트마다 — 권한이 오히려 **강해진** 사용자까지 —
        live lease를 중도에 끊는다. 지키는 것은 거의 없고 S5(15분)를 조용히 0으로 재가격한다.
      - ⚠️ 정정: 이 경쟁의 피해를 **"A1 상한이 깨진다"로 정당화하지 말 것** — 위 3-way min 때문에 깨지지 않는다.
        근거는 (1) 이 조항이 발급 fence를 명문으로 요구한다는 것, (2) epoch tagging이 있어야 후속
        bump-driven revoke가 완전해진다는 것, 둘뿐이다.
    - ⚠️ **lookup-time epoch 검사만으로는 게시 fence가 되지 않는다** (2026-07-27). 그건 *소비*만 막는다 —
      구 owner가 쓰기 자체를 하면 **이미 기록된 fresh 항목을 덮어써** 멀쩡한 상태를 파괴한다.
      → `put(result, expected_epoch=N)`이 **쓰기 시점에** 현재 epoch와 비교해 불일치면 **쓰기를 거부**한다(CAS).
      compare와 store 사이에 `await`가 없어야 하고(단일 event loop에서의 원자성), ⚠️ **그 전제는 소유권 계약**이다 —
      리포에 `asyncio.to_thread`가 다수 있고 동기 DB 조회(`app/entitlements.py`의 `has_entitlement`)가
      그 안으로 들어갈 자연스러운 후보라, **작은 `threading.Lock`으로 epoch+cache를 함께 보호**한다
      (`asyncio.Lock`은 스레드를 직렬화하지 못해 오답). fence 삭제 mutation이 red인지가 검증 기준.

- **A4-1 REST `PremiumStatus`를 strict가 재사용하지 않는다 (2026-07-27)**
  - `PremiumStatus.PENDING`은 **두 의미가 이미 섞인 REST 상태**다. `should_cache=True`(정상 응답) +
    `is_premium=False` + 캐시 없음이면 `_pending.mark()` 후 **PENDING**을 반환한다(TTL 12초) —
    즉 **구매 전파 유예**이지 "판정 불가"가 아니다. 판정 불가 PENDING은 `should_cache=False` 분기다.
    → `PENDING == temporarily_unavailable` 매핑은 **"구독 없음"과 "RevenueCat 장애"를 한 값으로 접는다**(C4 위반).
  - ⛔ **`_check_revenuecat_entitlement`도 관측 레벨 API가 아니다.** `(False, False)` 반환 지점이 5개이고
    성격이 전부 다르다 — API key 미설정(config) / JSON 파싱 실패(계약 위반) / `expires_date` 파싱 실패 /
    4xx·5xx 일괄(config ⊎ transient 혼합) / `except Exception`(transient ⊎ programming).
    strict가 이걸 그대로 소비하면 A6-1의 3-bucket 계약이 즉시 깨진다.
    → **typed provider**를 더 아래에 두고 REST·strict가 각자 정책을 얹는다:
    `Determined(is_premium)`(200 파싱 성공 / 404) · `ProviderUnavailable`(408·429·5xx·네트워크) ·
    `ProviderMisconfigured`(401·403) · `BadRequest`(400) · `ProtocolViolation`(그 외 4xx·파싱 실패).
    ⚠️ *"4xx 대 5xx"로만 나누면 **429를 영구 config 오류로 오분류**한다.*
    ⚠️ **REST는 behavior-change-0** — 위 4개 비-Determined를 전부 현행 `should_cache=False` 경로로 매핑해
    `PremiumStatus`·pending grace·stale fallback·clock 샘플링 지점을 그대로 보존한다.
  - **구매 전파 유예는 strict에 상속하지 않는다.** 정상 inactive는 **즉시** `Inactive(premium_inactive)`.
    유예를 채택하려면 별도 내부 상태가 필요한데(관측 저장소는 덮어쓰므로 "첫 inactive"를 알 수 없다),
    그 대신 아래 6′로 수렴시킨다.

- **A4-2 webhook 무효화 정책 — allowlist는 벤더 어휘라 조용히 낡는다 (2026-07-27, 공식 문서 검증)**
  - 현행 `invalidate_events`는 5종인데 문서의 이벤트는 그보다 많고 **닫힌 집합이 아니다**
    (*"We may add new fields or event types in the future without changing the API version."*).
    누락분은 대부분 **grant 방향**(`NON_RENEWING_PURCHASE` / `TEMPORARY_ENTITLEMENT_GRANT` /
    `REFUND_REVERSED` / `PURCHASE_REDEEMED` …)이라 구멍이 **가용성 쪽**에 난다.
    ⚠️ *"revoke 방향은 완전하다"고 단정하지 말 것* — `CANCELLATION`은 *"canceled **or refunded**… auto-renewal
    setting **may still be active**"*라 즉시 revoke가 아니다. **이벤트 이름에서 access 영향을 추론하지 않는다.**
  - ⛔ **"모든 이벤트 무효화"도 안전하지 않다.** 무효화는 재조회 지연이 아니라 **증거 폐기**다 —
    공급자 장애 중에 fresh REST 캐시를 지우면 stale fallback(A4)이 사라져 **503으로 떨어진다**.
    그리고 `PAYWALL_*` / `TEST` / `EXPERIMENT_ENROLLMENT` / `VIRTUAL_CURRENCY_TRANSACTION` 같은
    비-entitlement·고빈도 이벤트도 실재한다.
  - → **정책**: **strict epoch와 REST 무효화를 분리**한다. 무효화 공격성은 **그 경로의 실패 자세와 일치**해야 한다
    (strict=fail-closed → 공격적 OK / REST=가용성 우선 → 보수적).
    - **strict**: **unknown → invalidate**(vendor drift에 fail-safe). 초기 denylist는 **`TEST` 단독**.
      `SUBSCRIBER_ALIAS`는 deprecated여도 **invalidate 유지** — aliasing은 App User ID를 병합해
      *"어느 alias로 조회해도 같은 결과"*가 되므로 **같은 uid의 entitlement 조회 결과가 달라진다**.
    - **REST `invalidate_user_cache`**: **기존 동작 유지**(A4). 확장은 별도 결정.
  - ⚠️ **`PAYWALL_*` prefix로 전수 제외는 불가능**하다 — *"uses the default event names below **unless you
    configure custom Paywall event names**"*. 코드가 이름으로 완전성을 증명할 수 없으므로,
    대시보드 소스 필터(*"(Optional) Filter the kinds of events…"*, 다중 integration 지원)는 **볼륨 감소용**으로만 쓰고
    **runbook에 전제·점검 절차**를 남기되 **코드는 그것을 신뢰하지 않는다**.
  - **denylist 확장 절차**: 빈도는 **조사 트리거이지 정당화가 아니다** — 문서·payload로 **무관함을 입증한 뒤에만** 추가한다.
    denylist·대시보드 필터는 **부하 최적화 계층**이고, 정확성은 ① unknown→invalidate ② payload로 상태를 추론하지 않음,
    이 둘에서만 나온다. (`TEST`가 *"purchase-like sample payload"*로 `entitlement_ids`까지 실어 와도 안전한 이유다.)
  - **계측**: strict·REST **분리 카운터**(안 나누면 strict가 만든 부하를 귀속할 수 없다). event type은
    **bounded known + `other`** 로 집계하고(커스텀 이름 때문에 원문 label은 cardinality가 열린다),
    **unknown 원문은 rate-limited structured log** — 이게 denylist 확장의 **유일한 입력**이다
    (전부 `other`로 뭉치면 그 신호가 사라진다). ⚠️ *"strict 무효화는 잃는 게 없다"는 과장* —
    보안 방향이 안전할 뿐 **가용성과 호출량은 실제 손실**이다.

- **A5 single-flight (+ owner 격리)** — 같은 UID의 동시 authoritative 검증은 1회로 합친다(다중 연결·다중 topic).
  - **한 WebSocket이 끊겨도 공유 검증 owner를 취소하지 않는다**(다른 연결이 그 결과를 기다린다).
  - 실패·취소 후 슬롯은 **identity-safe하게 정리**돼 다음 요청이 다시 검증할 수 있어야 한다.
  - 1B `FirebaseAuthTokenProvider`와 **개념은 같다**(owner 격리 + epoch 정리). 단 **테스트를 그대로 옮기지 말 것** —
    Swift actor와 asyncio는 취소 전파 의미가 달라(unstructured Task vs `asyncio.Task` 취소) 수단이 다르다.
    asyncio에서는 detached task + `shield` 계열이 필요하다(구현 영역).
- **A6 strict verifier 반환 계약** — WS 인가용 검증기는 `bool`이 아니라 3-state를 반환한다.
  ```
  active(verified_at_monotonic) | inactive(verified_at_monotonic) | temporarily_unavailable(retry_after_seconds)
  ```
  - **`active`만 horizon을 연장**한다. `inactive`는 즉시 reject, `temporarily_unavailable`은 C4(registry 불변 + lease 미연장).
  - `bool`로는 "권한 없음"과 "확인 불가"를 구분할 수 없어 C4가 성립하지 않는다.

  **A6-1 저장·파생 2계층 (2026-07-27 확정 — 이걸 먼저 고정해야 나머지가 안 흔들린다)**

  **verdict는 저장하지 않는다. 저장하는 것은 관측(observation)이고 verdict는 요청별로 파생한다.**
  verdict를 저장하면 (a) UID 단위 `Inactive`를 같은 UID의 **새 토큰이 상속**하고, (b) 신선도 판단이
  **쓰기 시점에 박혀** read 시점 정책(freshness)을 적용할 자리가 없어진다.

  | 계층 | 내용 |
  |---|---|
  | **저장 (UID 키, concern별 분리)** | `PremiumObservation(active: bool, verified_at_mono, epoch)` / `IdentityAuthorityFound(disabled, tokens_valid_after_ms, verified_at_mono, epoch)` \| `IdentityAuthorityNotFound(verified_at_mono, epoch)` |
  | **파생 (요청별: 관측 + 토큰 claims)** | `Active(premium_verified_at_mono, identity_verified_at_mono)` \| `Inactive(reason)` \| `NeedsVerification(concern)` |

  - **identity는 합타입**(Found/NotFound) — `not_found=True`인데 watermark가 존재하는 불가능 상태를 만들 수 없다.
    타입 검사기가 없는 리포라도 이득이 있다: NotFound에서 `.tokens_valid_after_ms` 접근이 **오용 지점에서 즉시**
    `AttributeError`가 된다(평면+`Optional`은 `None`이 산술까지 흘러 한 단계 늦게 터진다).
  - **entitlement는 합타입 불필요** — RevenueCat 404는 *"신규 사용자 = 비구독"*으로 접히므로
    (`_check_revenuecat_entitlement`) not-found 변종이 없고 `active: bool` 두 값이 대칭이다.
  - **`Inactive.reason`** = `premium_inactive` \| `token_revoked` \| `account_disabled` \| `account_deleted`.
    `token_revoked`는 **저장 대상이 아니다** — `(uid, iat)` 단위 파생 판정이며, watermark 단조 증가 + `iat` 고정이라
    그 토큰에 대해서만 영구적이다. 새 토큰은 같은 record에서 `Active`를 파생한다.
  - ⚠️ **`TemporarilyUnavailable`은 파생 verdict가 아니라 verifier(I/O) 반환**이다 (2026-07-28 정정).
    파생 계층이 관측이 없거나 낡았을 때 돌리는 것은 **`NeedsVerification(concern)`** — 내부 제어
    신호이지 wire 상태가 아니다. 둘을 섞으면 "아직 안 물어봤다"가 retryable **장애**로 클라에 나가고
    재검증 자체를 건너뛴다(§8-C상 `temporarily_unavailable`은 전체-요청 + retry_after + registry 불변).
  - **`TemporarilyUnavailable.reason`** = 판정 불가 사유만. ⛔ **구매 전파(propagation)를 여기 넣지 말 것** —
    RevenueCat이 authoritative inactive를 반환했으므로 "판정 불가"가 아니다(§A4-1).
  - **programming·config 오류는 verdict가 아니다.** 검증기는 내부 예외로 전파하고 **캐시하지 않는다.**
    상위의 광범위 `except Exception`이 이를 `temporarily_unavailable`(retryable)로 바꾸면 재시도 storm이 되므로,
    **그 변환이 없음을 mutation 테스트로 잠근다**(docstring caveat만으로는 강제가 아니다).
    - **정정 (2026-07-28) — "wire 오류로도 접지 않는다"는 *배선 경계*에는 적용되지 않는다.**
      금지 대상은 **광범위 `except`에 의한 조용한 세탁**이지, 타입 기반의 의도된 변환이 아니다.
      그냥 전파시키면 **더 나쁘다** — 실측: `app/main.py:998`의 `except Exception`이 루프를 빠져나가고
      `finally`(:1003-1007)가 `registry.remove_websocket()`을 불러 **그 연결의 구독이 통째로 삭제**되며
      클라에는 오류 프레임도 가지 않는다(§8-A "조용한 실패 금지" 위반 + C4 "registry 불변"과 정반대).
      클라의 즉시 재연결이 `retry_after`보다 빨라 storm도 오히려 조인다.
    - **단일 정책**: 검증기 = 변환 **금지**(raise) / 배선 경계 = **타입 기반** catch로 변환 **필수**
      (`temporarily_unavailable` + `retry_after` 상한값, transient와 **분리된 카운터** + ERROR 로그,
      registry·기존 lease 불변). §8-C의 `temporarily_unavailable` 정의가 "인증·권한을 **판정할 수 없음**"이라
      죽은 API key도 그 정의에 들어간다 — 운영자 신호는 wire가 아니라 카운터·로그가 낸다.

  **A6-2 관측 신선도 (freshness는 verdict가 아니라 관측에 붙는다)**

  - **양성 관측**(`active=True`, `disabled=False`)은 **A1 horizon이 이미 상한**을 건다(보안 방향).
  - **부정 관측**은 horizon을 타지 않으므로 **별도 bound가 필요**하다(가용성 방향).
    `token_revoked`에는 **상수가 없다** — 저장된 관측이 아니라 요청마다 `iat`로 파생하는 판정이라
    "갱신" 개념 자체가 없기 때문이다. 반면 `disabled=True` / `not_found=True`는 **재활성화·uid 재생성**이
    가능해 monotone이 아니라서 상한이 필요하다.
    ⛔ 이걸 "stale record라도 revoke는 즉시 판정" 최적화로 읽지 말 것 — **구현은 record 신선도를 먼저 본다**
    (stale + revoke 증명 → `NeedsVerification`, 재검증 후 `Inactive`로 결과는 같고 RTT 1회를 더 쓴다).
    최적화하려면 (a) watermark 단조성상 *stale이 "revoked"면 건전 / "not revoked"면 불건전*이라
    **revoke 술어에만** 적용해야 하고, (b) 실측상 freshness 앞에 단락을 넣으면 SDK와 맞춘
    `disabled > revoked` 우선순위가 **깨진다**. 정확성 이득 0이라 채택하지 않았다
    (`tests/test_strict_authz.py::TestStaleRecordDoesNotShortCircuitRevoke`가 현 동작을 못 박는다).
  - 상수 2개는 **값이 같아도 독립**이다(파생 관계로 묶지 말 것 — 위험 프로파일이 다르다):

    ```
    PREMIUM_INACTIVE_RECHECK_SECONDS  = 300.0   # 결제한 사용자가 거부 상태로 남는 시간. 흔함 → 측정 후 짧아질 수 있음
    IDENTITY_NEGATIVE_RECHECK_SECONDS = 300.0   # disabled=True / NotFound 둘 다. 관리자 조치라 드묾
    ```

    둘 다 **측정 전 보수적 기본값**이다. ⚠️ 부하는 window가 아니라 **요청 간격 R과 window H의 관계**로 정해진다 —
    `R ≫ H`면 어느 값이든 매 요청이 miss라 등가다(D6 재인증 ~11~12분 기준). 실제 위험은 **빠른 재시도 클라**이고
    그건 상수가 아니라 rate limit으로 다룬다.
  - **`iat` 강제 miss**(더 새로운 토큰이 오면 부정 관측을 무효화)는 *"Firebase가 disabled·deleted 계정의 토큰
    재발급을 차단하는가"*가 미검증이라 **v1 correctness에서 제외**한다. 확인 후 **보조 최적화**로 추가하며,
    창을 좁히는 방향으로만 작동한다.

#### B. 강제 지점

- **B1 전송 직전 인가 (`send_if_authorized`)** — 최종 강제는 **각 send 직전**이다. live publish와 초기 snapshot 모두.
  - 왜: `get_subscribers`(app/topic_dispatcher.py:144) 결과를 루프에서 `await send_json`하므로, 느린 구독자가 있으면
    조회와 실제 전송 사이 간격이 벌어진다. 조회 시점 필터만으로는 경계를 넘긴 전송을 못 막는다.
  - 검사 항목: lease identity(ws, topic, uid, **`lease_id`**) + `now < expires_at`.
- **B2 조회 경계 필터는 1차 방어** — `get_subscribers`는 만료분을 제외하되 **read-only**(즉시 삭제 금지).
  삭제까지 하면 sweep이 `reauth_required`를 보낼 근거를 잃는다.
  **소유권**: 조회·전송 경계 = *강제* / sweep = *생명주기*(통지 후 제거).
- **B2a 전송 실패 시 registry 정책** — 현행 3곳(`topic_dispatcher.py:155`, `:223`, `topic_initial_snapshot.py:176`)이
  send 실패에서 `registry.remove_websocket(ws)` = **그 연결의 전 topic을 통지 없이 삭제 + 소켓은 유지**한다.
  1C에서 이대로 두면 **ack이 N개를 광고한 직후 snapshot 1건 실패로 N개가 사라지고**, 클라는 유효 lease를 믿고
  다음 재인증까지(최대 ~12분) 무데이터·무오류 상태가 된다 — "ack이 권위 있는 상태"(D2)가 무통지로 거짓이 된다.
  → **전송 실패는 연결이 죽은 것으로 간주하고 소켓을 닫는다**(C3의 sweep 실패 처리와 동일 정책).
  부분 삭제 후 소켓 유지 금지 — 클라가 재연결하면 ack으로 상태를 다시 확정한다.
- **B3 상속 경로** — `get_subscribers`와 이를 호출하는 `subscriber_count`(:109)만 필터를 상속한다.
  `subscribed_connection_count`(:99)는 `len(self._subscriptions)` 직접 반환이라 **미상속** —
  expiry 미반영 관찰 지표이며 **전송·인가 guard로 쓰지 말 것**.
- **B4 연결별 state-transition lock** — send만 잠그면 부족하다. sweep이 만료 상태를 **읽은 뒤** 재인증이
  registry를 갱신하는 read-then-update 경쟁이 남는다. 같은 lock이 **UID binding · lease CAS · ack/`reauth_required` 송신 ·
  registry 활성화**를 함께 직렬화해야 한다.
  - 순서 고정: **`ack` → registry 활성화(live 대상 편입) → `snapshot`**. 없으면 ack 이전 live 수신, stale 통지 역전.
  - **요청 단위 트랜잭션 (구현: `app/topic_lease_registry.py`의 `apply_subscribe`)** —
    한 subscribe 요청 = **lock 1회 · ack 1건 · 전부 아니면 전무**. topic 단위 API로는 성립할 수 없다:
    (i) topic마다 ack을 보내면 §8-B의 "요청당 1건"과 클라의 `request_id` 상관이 깨지고,
    (ii) 마지막 topic에서만 ack을 보내면 앞의 topic들이 **ack보다 먼저 활성화**되어 위 순서 고정이 무효가 된다.
    또 topic마다 lock을 잡았다 놓으면 그 사이 sweep·unsubscribe가 끼어들어 ack이
    **한 번도 존재한 적 없는 상태**를 광고할 수 있다(D2의 "단일 snapshot" 위반).
  - **ack 자료는 registry가 만들어 sender에 넘긴다** — sender가 registry 조회로 만들면 그 시점
    새 lease는 아직 미활성이라 **보이지 않는다**. ack이 방금 수락한 topic을 빠뜨리고, 클라는 구독이
    드롭됐다고 결론짓는데 서버는 곧바로 그 topic을 발행한다.
  - **`send_ack` 계약** — lock을 쥔 채 실행되므로: ① 성공 시 `True` 반환(⛔ `wait_for`의 제어 흐름은
    배달의 증거가 아니다 — sender가 `CancelledError`를 삼키고 정상 반환하면 `wait_for`도 **정상 반환**한다),
    ② `CancelledError`를 삼키거나 shield 금지, ③ **자체 timeout 금지**(예산은 registry 소유. sender가 던진
    `TimeoutError`는 우리 예산 만료와 구별 불가라 호출자 버그가 "죽은 클라"로 오분류된다),
    ④ registry 재진입 금지(`asyncio.Lock`은 재진입 불가 → hang. 구현은 **task 동일성**으로 감지해
    `ReentrantRegistryCall`로 시끄럽게 실패시킨다. ws만 보면 sweep·teardown의 정당한 대기까지 오탐한다).
  - **ack 실패 = 연결 사망**(B2a와 동일 정책) — "발급 안 함"으로 끝내면 안 된다.
    취소는 이미 transport 버퍼에 들어간 프레임을 **되돌리지 못하므로**(websockets legacy는 프레임 전체를
    버퍼에 넣은 뒤 drain을 await한다), 역압이 풀리면 그 ack이 **나중에 배달**된다 →
    클라만 lease를 가졌다고 믿고 서버엔 없는 상태(데이터 0·오류 0·통지 대상 lease도 없음).
    → registry가 직접 tombstone을 찍어 같은 소켓의 재발급을 fail-closed로 막고, 호출자는 소켓을 닫는다.
  - **snapshot build는 lock 밖**에서(무거움), **전송 직전 lock을 다시 잡아 lease를 재검증**한다(B1과 결합).
  - 인증·RevenueCat·DB 조회도 lock 밖에서 하고, lock 안에서 UID/`lease_id`를 재확인한 뒤 전이를 확정한다.

- **B5 연결당 처리 모델 (하나의 결정 — 개별 수정 시 서로 상충)**
  현행 `/ws`는 `while True: data = await receive_text(); await handle_client_message(...)`
  (`app/main.py`의 `websocket_endpoint` — 라인 대신 **심볼 앵커**)로
  **연결당 완전 직렬**이다. 1C의 subscribe는 Firebase RTT + RevenueCat(timeout 5.0s) + E2 `get_user()` RTT를 포함하므로
  그대로 두면 그 뒤의 `unsubscribe`·`ping`이 **큐잉된다**.
  - **(a) 직렬 유지 불가**: D8 fail-open이 **시간축에서 무력화**되고(제거가 인증 뒤에 줄 섬),
    iOS는 텍스트 ping + **pongTimeout 10초**(WebSocketService.swift)라 초과 시 **클라가 연결을 끊는다**.
  - **(b) 결정 = 메시지 dispatch는 연결별 동시(task spawn), 상태 전이는 B4 lock으로 직렬화.**
    무거운 I/O(토큰 검증·entitlement·snapshot build)는 lock 밖, 전이·송신만 lock 안.
  - **(c) lock 안 I/O에는 timeout 필수**(D-const) — 역압 걸린 클라가 자기 lock을 물고 unsubscribe를 봉쇄하면 안 된다.
  - **(d) identity 단조성**: 동시 처리가 되면 C1의 UID 재바인딩이 **도착 순서에만** 의존해선 안 된다.
    늦게 도착한 구 UID subscribe(또는 C4 retry)가 새 바인딩을 되돌리지 못하도록 구 요청을 폐기해야 한다.
    - ⛔ **`auth_time` 기준 단조 비교는 폐기 (2026-07-27)**. 두 가지로 성립하지 않는다:
      (1) refresh에서 `auth_time`이 **유지**되므로 같은 세션 안의 두 요청(= C4 retry)을 정렬하지 못한다.
      (2) 초 단위 타임스탬프라 **서로 다른 사용자가 같은 값을 가질 수 있어** 전순서 자체가 아니다.
    - ⛔ **서버 관측 도착 순서로도 복원 불가**. 단일 WebSocket은 TCP 순서 보장이라 도착 역전이 없고,
      실제 실패 모드는 **클라가 stale intent를 나중에 보내는 것**(구 UID 요청의 지연 재시도)이다.
      서버는 그것을 최신 요청으로 볼 수밖에 없고, 구 UID의 토큰이 아직 유효하면 **검증으로도 거를 수 없다**.
    - → **의도 순서는 클라만 안다**. 클라 소유의 identity generation(요청 구성 시점에 단조 증가)을 실어
      보내거나, 서버가 **live 소켓의 cross-UID subscribe를 거부**하고 재연결을 요구한다(둘 중 택일, C1에서 결정).
      ⚠️ 클라 generation은 **적용 여부만** 정하고 **어떤 신원을 부여할지는 여전히 토큰 검증이 정한다** —
      권한 위임이 아니다. ⚠️ D2의 서버 소유 `identity_generation`과 **이름이 충돌하지 않게** 할 것.
  - **(e) send 직전 재검증(B1)과 실제 `await send_json` 사이의 yield**에 UID 재바인딩이 끼면 in-flight 메시지 1건이
    새 UID로 갈 수 있다 → 전송도 lock 안에서 하거나, 전송 직후 결과를 폐기할 수 있어야 한다(B4와 일관).
    - ⛔ 단 **publish fanout이 연결 lock을 잡으면 안 된다**. `publish_topic`은 구독자를 **순차 루프**로
      돌므로(`app/topic_dispatcher.py`), 역압 연결 A가 ack으로 자기 lock을 최대 5초 쥔 사이 루프가 A에서
      막히면 **B·C의 tick까지 5초 밀린다** — 연결별 lock으로 얻으려던 격리가 통째로 사라진다.
      → B1 재검증은 **lock-free 조회**(lease identity + 만료 비교)로 두고, (e)의 좁은 경쟁(재바인딩
      직전 in-flight 1건)은 감수하거나 fanout을 연결별로 격리한 뒤에 lock을 도입한다.

#### C. 상태 전이

- **C1 연결당 UID 고정** — WebSocket 하나는 임의 시점에 **정확히 하나의 UID**에 바인딩된다.
  - 미바인딩 → 바인딩 / 바인딩 UID == 토큰 UID → C2 증분 규칙 / 바인딩 UID != 토큰 UID → **그 ws의 기존 구독 전부 제거** 후 재바인딩.
  - **처리 순서**: B 토큰 **검증 성공 즉시** A의 구독 제거 → 이후 B의 premium 확인이 실패해도 **A 구독을 복원하지 않는다**.
    반대로 **B 토큰 자체가 무효면 A의 유효 lease는 건드리지 않는다**.
  - 제거는 `reauth_required`를 발신하지 않는다(lease 만료가 아님). **대신 ack이 결과 전체를 실어 권위를 갖는다** —
    ack의 **`active_subscriptions`(그 연결의 최종 구독 전체 + topic별 `lease_id`·잔여 duration)** 로 클라가 서버 상태에 수렴한다(D2).
    ⚠️ ack이 **이번 요청 topic 결과만** 담으면, B가 일부 topic만 요청했을 때 클라는 언급되지 않은 A 시절 topic을
    여전히 accepted로 오인한다 — "ack이 권위 있는 응답"이 사실이 되려면 전체 상태를 실어야 한다.
  - **purge된 topic을 같은 요청이 다시 잡으면 `removed_topics`에 넣지 않는다** — 그건 제거가 아니라
    **교체**이고(새 `lease_id`), 한 topic을 `removed`와 `accepted`에 동시에 실으면 클라가 모순된 지시를 받는다.
    `active_subscriptions`가 새 `lease_id`로 이미 권위를 갖는다.
  - ⛔ **미결(배선 진입 전 필수) — 이 purge는 B5(d)의 stale 요청 방어와 결합되어야 한다.**
    purge는 *적용하기로 한* cross-UID 전이의 **의미**만 정하고, *적용할지*는 정하지 않는다.
    방어가 없으면 A→B 전환 뒤 지연 도착한 **구 UID(A) subscribe**(C4 retry, A 토큰은 아직 유효)가
    **B의 구독을 purge하고 A로 재바인딩**한다 — reject 정책이었다면 무해했을 요청이 purge에서는
    **파괴적**이 된다. B5(d)의 "둘 중 택일"은 C1에서 결정된다고 적혀 있으나 **아직 결정되지 않았다**
    (클라 소유 identity generation은 §8-B에 필드가 없다). registry는 이 전이를 그대로 구현했으므로,
    **배선 슬라이스는 B5(d)를 닫기 전에 진입하면 안 된다.**
  - **도달 가능성**: A→B 직접 전환은 `.signedOut`을 거치지 않는다(iOS FXiApp.swift:124-126은 signedOut에서만 WS stop /
    AuthService listener의 account-switch window). 소켓이 살아 있는 채 UID만 바뀐다.
    그리고 이건 **서버 측 인가 경계**라 클라 teardown 가정에 기대면 안 된다.
- **C2 재인증 replacement** — 인증 성공한 subscribe에서 **reject된 topic은 registry에서 제거**한다.
  `TopicRegistry.register`가 additive union(`existing.update(topics)`, :68)이라 "accepted만 추가"하면
  권한 잃은 이전 등록이 lease 만료까지 잔존한다.
  **이번 요청에 언급되지 않은 topic은 불변**(증분 subscribe 보존) — 단 **같은 UID 전제**이며 UID 변경 시 C1이 우선한다.
  - **구현**: `apply_subscribe`가 accepted와 **`rejected_topics`를 함께** 받는다. accepted만 받는 API로는
    이 조항을 표현할 수 없다 — 권한 잃은 기존 등록이 lease 만료까지(최대 15분) 살아남는다.
    제거는 **실제로 활성이던 것만** 대상이고, 그 결과가 `removed_topics`다(= **상태 델타**이지
    이번 요청의 거부 목록이 아니다. 거부됐지만 원래 없던 topic은 제거된 것이 아니다).
    한 topic이 accepted이면서 rejected이면 D5 단계상 불가능한 입력이므로 **호출부 계약 위반으로 크게 실패**시킨다.
- **C3 sweep ↔ 재인증 CAS (claim-then-notify)** — sweep이 만료 항목을 캡처한 뒤 `await send_json` 하는 동안
  재인증이 lease를 갱신하면 **구 sweep이 새 lease를 제거**할 수 있다.
  - **lock 안에서 만료 `lease_id`를 원자적으로 claim/mark** → 그 다음 통지 → 제거. "발신 후 제거"만으로는
    send hang 시 다음 sweep이 **같은 `lease_id`를 중복 통지**한다.
  - 제거·갱신은 `(ws, topic, lease_id)` **CAS** — 자기 lease일 때만. 갱신된 lease는 보존된다.
  - ⚠️ **sweeper의 lock 획득은 blocking이 아니다** — ack이 최대 `ACK_TIMEOUT_SECONDS` 동안 연결 lock을
    쥐므로(B4), 단일 sweeper가 연결을 순회하며 무한 대기하면 역압 연결 K개에 대해 한 사이클이
    최대 5s×K 늘어난다. 그러면 **무관한 다른 연결**의 만료 통지가 D-const의 "만료→통지 10초"
    관측 계약을 넘긴다(보안 상한은 B1이 지키므로 유출은 아니다).
    → 짧은 상한으로 시도하고 경합 시 **그 연결만 건너뛰어 다음 사이클로 미룬다**(지연 상한이 sweep 주기 1회로 유지).
  - `send_json` 실패·timeout 시 **소켓 전체를 정리**한다(부분 상태 잔존 금지).
  - **claim된 항목(`claimed_expired`)은 제거 전이라도 `get_subscribers`·`send_if_authorized`에서 즉시 제외**된다.
    아니면 통지를 보내는 동안 live publish가 다시 통과한다.
- **C4 일시적 실패는 registry를 바꾸지 않는다** — Firebase 인증서/네트워크 장애(main.py:2827 → 503)나
  RevenueCat PENDING(main.py:73 → 503)을 `invalid_token`/`premium_required`로 접으면 **기존 구독을 잘못 제거**한다.
  → 전체-요청 오류 `temporarily_unavailable`(retryable)을 추가하고, 이 경우 **registry 불변 + lease 미연장**.
  - ⚠️ **transient 소스는 Firebase·RevenueCat만이 아니다**: KRX `has_entitlement`(app/entitlements.py)는
    **캐시 없는 동기 DB 조회**라 DB 순단 시 False로 접히면 C2 replacement가 **정상 사용자의 krx 등록을 제거**한다.
    → entitlement 조회도 **실패와 "권한 없음"을 구분**해 실패는 `temporarily_unavailable`로 승격해야 한다.
  기존 lease는 원래 시각에 만료되므로 장애가 길어지면 자연히 fail-closed로 수렴한다.
  - **클라 재시도는 잔여 lease를 넘기지 않는다**:
    `retry_at = max(0, min(retry_after_seconds, lease_remaining − safety))`.
    `lease_remaining <= safety`면 대기 0 — 기다리지 말고 **즉시 재인증 또는 reconnect**한다
    (`retry_after`를 곧이곧대로 기다리다 lease가 만료되면 데이터가 끊긴 뒤에야 복구를 시작하게 된다).

#### D. wire 계약 추가 (§8 확장)

A1로 lease가 **가변**이 되고 증분 subscribe로 **topic마다 lease가 달라지므로**, scalar 필드로는 표현할 수 없다.

- **D1 lease는 topic별** — 한 ack 안의 accepted들은 같은 순간 갱신되니 duration이 같지만,
  **이번 요청에 없던 기존 topic은 자기 lease를 유지**한다. 따라서 **`lease_id`·duration은 topic 단위 필드다**(정수 generation 금지 — D2).
- **D2 `subscription_ack` 필드 의미** — **schema 정본은 §8-B 하나뿐이다**(중복 정의 금지 — 두 블록이 어긋나면
  구현이 갈린다). 여기서는 필드의 *의미*만 규정한다.
  - **`operation`**: `subscribe` | `unsubscribe`. 같은 schema를 쓰므로 구분자가 필요하다.
  - **`accepted_topics` / `rejected_topics` / `removed_topics` = 이번 요청의 결과.**
    `removed_topics`의 producer는 **unsubscribe(D8)** 뿐 아니라 **C1의 UID purge**와 **C2의 reject eviction**도
    포함한다(그래서 `operation: "subscribe"` 응답에도 값이 찰 수 있다).
  - **`active_subscriptions` = 그 연결의 최종 상태 전체.** 문자열 목록이 아니라 topic별 `lease_id` + 남은
    `lease_duration_seconds`를 싣는다 — 문자열만으로는 **이번 요청에 없던 기존 topic의 lease 상태를 복구할 수 없어**,
    클라가 상태를 잃었거나 UID reset 후 수렴할 때 부족하다(= ack이 "권위 있는 상태"가 되지 못한다).
    - **connection lock 아래 단일 snapshot**에서 생성한다(B4). topic마다 다른 시점에 읽으면 모순된 상태를 내보낸다.
    - 남은 duration은 **내림(floor)** 정수 초. **≤ 0이면 이미 만료**이므로 넣지 않는다(만료를 "활성"으로 광고 금지).
  - **`lease_id`(topic별, 재사용 불가 opaque)**: 늦게 도착한 구 `reauth_required`를 무시하는 유일한 수단(D3와 대조).
    ⚠️ **정수 generation을 topic별로 0부터 다시 세면 안 된다** — 제거 후 재구독 시 초기화돼 **과거
    `reauth_required`가 새 lease와 오인 일치**한다. 연결 수명 동안 **단조 증가하는 counter** 또는 opaque id를 쓴다.
  - **`identity_generation`(연결별)**: **같은 소켓에서의 UID 재바인딩만** 표현한다.
    ⛔ **strict cache의 UID epoch을 그대로 실으면 안 된다.** 그 epoch은 **UID별**이고 최초 관측
    순서로 할당되므로, 먼저 관측된 UID가 더 작은 값을 갖는다 → A→B 재바인딩에서 generation이
    **감소**하고, G의 "구 `identity_generation` ack 무시"를 지키는 클라가 **유효한 새 ack을 버린다**
    (실측: B를 먼저 관측하면 A=2 → B=1). registry가 **연결별로 소유**하고
    미바인딩 0 / 최초 바인딩 1 / 재바인딩마다 +1로 단조 증가시킨다. 같은 UID 재인증은 **불변**이다
    (재바인딩이 아니므로, 올리면 클라가 자기 상태를 불필요하게 버린다).
    ⚠️ **reconnect는 표현하지 못한다** — 새 WebSocket은 서버 상태가 처음부터 시작하므로 구/신 소켓 ack을
    전역 구분할 수 없다. **reconnect·구 receive task 격리는 클라 소유 `connectionGeneration`**이 담당한다(F).
- **D3 `reauth_required` schema** — ack과 대조 가능해야 하므로 **topic별 `lease_id`**를 싣는다.
  ```json
  {"type": "reauth_required", "topics": [{"topic": "krx:usd-krw-futures", "lease_id": "c7f1…"}], "reason": "lease_expired"}
  ```
  클라는 자기가 들고 있는 `lease_id`와 **다른** 통지를 무시한다(재사용 불가라 단순 비교로 충분).
- **D4 `request_id` 재시도 규칙** — ack timeout 재시도는 **새 `request_id`를 발급**하고 이전 ack은 무시한다
  (같은 ID 재사용은 payload 불일치 idempotency를 요구 → 서버 캐시 없이 새 ID가 단순).
  - ⚠️ **"첫 요청은 적용됐고 ack만 유실"된 경우가 정상 경로다.** 재시도의 idempotency는
    **"구독 topic 집합이 달라지지 않는다"**는 의미다 — **lease는 갱신된다**(재인증 = 다시 subscribe, §8-D).
    "재시도가 상태를 전혀 바꾸지 않는다"로 읽으면 재인증이 lease를 갱신하지 못해 전 topic이 만료된다.
    최종 ack의 `active_subscriptions`가 진실이다.
  - subscribe 재시도 시 **snapshot이 중복 전송될 수 있다**(허용). 다만 클라의 적용 규칙은 **"마지막 것"이 아니라
    기존 **timestamp-merge 계약**을 따른다(topic_initial_snapshot.py의 build-중 concurrent publish 회귀 방지 +
    REALTIME_V2_CLIENT_GUIDE §5). B4가 "snapshot build는 lock 밖"을 계약화하므로 구값 회귀는 구조적으로 가능하다.
    서버가 중복 억제를 위해 request 캐시를 두지는 않는다.
- **D5 오류 판정 = 단일 전순서가 아니라 처리 단계** — 단계마다 의미가 다르다
  (`temporarily_unavailable`이 토큰 검증 단계와 premium 조회 단계에서 다른 원인이고, `topics_disabled`면
  RevenueCat 호출 자체가 불필요).
  ```
  1) payload validation        → 형식 오류(D7 상한 위반 포함)
  2) token                     → invalid_token | temporarily_unavailable(Firebase 장애)
  3) capability / topic 존재   → topics_disabled | unknown_topic
  4) premium                   → temporarily_unavailable(조회 불가) | premium_required
  5) KRX entitlement           → krx_entitlement_required
  6) state transition          → UID 바인딩·lease CAS·registry 반영
  ```
  1~2는 **전체-요청** 오류, 3~5는 **per-topic** 결과다.
  - ⚠️ **`temporarily_unavailable`은 어느 단계에서 발생하든 전체-요청으로 승격**한다(§8-C). per-topic으로 두면
    (a) `rejected_topics` 엔트리에 `retry_after_seconds`를 실을 자리가 없어 C4 재시도 공식이 입력을 잃고,
    (b) **C2의 "reject된 topic 제거"가 발동해 C4의 "registry 불변"이 정면으로 깨진다**
    — RevenueCat 5분 장애가 **전 구독자 구독 삭제**로 증폭된다.
    `temporarily_unavailable`은 "권한 없음"이 아니라 **"판정 불가"**이므로 per-topic 판정 자체가 성립하지 않는다.
  ⚠️ 구 문서의 "존재 판정을 앞에 둬 정보 노출 차이를 없앤다"는 **근거가 틀렸다** — 지원 topic 목록은 이미
  공개 정보(REALTIME_V2_CLIENT_GUIDE)라 숨길 것이 없다. 존재 판정을 앞에 두는 실제 이유는 **더 싸고 오류가 명확**해서다.
- **D6 클라 재인증 공식(서버 wire 계약의 일부)** — 고정 12분은 가변 lease와 충돌한다(10분 lease를 받고 12분에
  갱신하면 이미 만료). ack의 `lease_duration_seconds`에서 유도한다.
  ```
  reauth_delay = max(0, lease_duration_seconds - 180s - U(0, 60s))
  ```
  - 15분(900s) lease → 11~12분 / 10분(600s) lease → 6~7분에 갱신.
  - 180s = ack RTT + 1회 재시도 여유. jitter는 **하향 전용**(상향은 deadline을 침범).
  - ack timeout **10초**, 실패 시 **새 request_id로 1회 즉시 재시도**(D4).
  - 클라 타이머는 **monotonic**이되 **suspension 경과를 반영**해야 한다(F 참조).
  - ⚠️ **lease는 topic별이라 `lease_duration_seconds`가 여러 개일 수 있다**(증분 subscribe로 갱신 시점이 다름).
    타이머는 **`active_subscriptions`의 최소 잔여**를 기준으로 건다 — 가장 짧은 topic이 만료 전에 갱신되게.
    한 번의 재인증이 **전 topic을 함께 갱신**하므로 per-topic 타이머는 불필요하다.
- **D7 입력 상한 (테스트 가능한 실제 값)** — "상한이 필요하다"만으로는 테스트할 수 없다. 초기값:
  | 항목 | 값 |
  |---|---|
  | **message** 크기 | 16 KiB (분할 frame **합산** — `max_size`는 message 상한이지 frame 상한이 아니다) |
  | `topics` 개수 | ≤ 8 (현재 사용 6: fx×3 · usdt:krw · krx · dxy 예약) |
  | topic 문자열 길이 | ≤ 64자 |
  | `request_id` | UUID 문자열, 36자 고정 |
  | 중복 topic | **first-occurrence 순서로 제거**(정규화) |
  값은 조정 가능하지만 **미정 상태로 test-first에 들어가지 않는다**.
  - ⚠️ **message 상한은 앱 레벨만으로 강제할 수 없다** — `handle_client_message(raw_text)`가 실행되는 시점엔
    ASGI 서버가 **분할 frame을 합쳐 message 전체를 이미 메모리에 받은 뒤**다
    (`app/main.py`의 `await websocket.receive_text()`).
    실제 자원 보호가 목적이라면 **Uvicorn/WebSocket `max_size` 또는 프록시 계층에서도 16 KiB를 강제**하고
    초과 시 **close code 1009**로 끊어야 한다. 앱 레벨 `len()` 검사는 프로토콜 검증(방어 심화)일 뿐이다.
    → 배포 설정도 계약의 일부이며 테스트 대상.
    **실사(2026-07-25)**: 당시 Dockerfile의 uvicorn 실행에 **`--ws-max-size`가 없고**,
    nginx의 `client_max_body_size`는 **HTTP body 전용이라 WebSocket 업그레이드 후 상한을 보장하지 않는다**.

    ✅ **land (2026-07-26)** — Dockerfile CMD에 **`--ws websockets --ws-max-size 16384`**,
    `tests/test_ws_message_limit.py` 8건. lock 정확 버전(uvicorn 0.44.0 / websockets 16.0) 실측 기반:

    - ⚠️ **수용 기준 정정**: 구 문구 "ASGI 통합 테스트에서 close 1009 확인"은 **원리적으로 불가능**하다.
      legacy impl의 `fail_connection`이 피어 close 프레임을 파싱하지 않아 **ASGI 앱은 1006**(=일반 네트워크
      단절과 같은 코드)을 본다. 계약은 **클라이언트가 관측한 1009**다.
      → **알려진 한계**: 서버 로그만으로 oversize 남용과 평범한 단절을 구분할 수 없다. "close 1009 로그로
      확인" 류 운영 기준은 채택 불가.
    - **`--ws websockets` 동반 고정이 필수**다. `auto`는 websockets 미설치 시 wsproto로 내려가는데
      **0.44.0의 wsproto는 `max_size`를 무시**하고(0.46.0부터 지원) wsproto는 lock에 이미 있어 기동
      실패조차 하지 않는다 = **조용한 fail-open**. 명시 고정하면 uvicorn이 기동 자체를 실패한다(fail-fast).
      ⚠️ 이 fail-open 경로는 **행동 테스트로 관측되지 않는다**(현 env에선 auto도 legacy로 귀결) —
      Dockerfile trip-wire가 유일한 방어다.
    - ⚠️ **업그레이드 게이트**: uvicorn **0.50.0(2026-07-04)부터 `auto` 기본이 `websockets-sansio`이고
      legacy는 deprecated**다. 올릴 때 sansio 전환을 검토할 것 — 실측상 sansio는 **앱이 1009+reason을 직접
      관측**해 위 관측 공백이 해소된다(0.44.0에서도 동작 확인). 전환 시 Dockerfile CMD와 테스트 기대값을 함께 변경.
    - **후속 위험(별건)**: `--ws-max-size`는 **다수의 작은 메시지** 공격을 막지 않는다. legacy 기본 큐는 32이고
      nginx `/ws`에는 API와 달리 rate/connection limit이 없다 → `--ws-max-queue` + 연결당 메시지 rate는 별도 항목.
    - **16 KiB 근거 주의**: "현행 정상 인바운드 최대 112 B" 같은 *현재* 트래픽 기준으로 축소 판단하지 말 것.
      1C canonical subscribe는 Firebase `id_token`(JWT) + UUID `request_id`를 싣고 topics ≤ 8 × 64자를 허용하므로
      **계약상 정상 요청**이 훨씬 크다. 축소하려면 실제 토큰 길이 측정이 선행돼야 한다.

- **D-const 상수 확정** (D7의 자기 원칙 "미정 상태로 test-first 금지"를 상수에도 적용)
  | 상수 | 값 | 근거 |
  |---|---|---|
  | `LEASE` 상한 | **900s** | S5 |
  | `safety` (D6·C4) | **30s** | ack RTT + 처리 여유. D6의 180s 마진과 별개 — C4 재시도 판정용 |
  | sweep 주기 | **10s** | = 만료 → `reauth_required` **최대 지연**(사용자 관측 계약). 전송 강제는 B1이므로 보안 상한과 무관 |
  | notify send timeout (C3) | **5s** | 초과 시 소켓 정리(B2a와 동일 정책) |
  | lock 안 I/O timeout (B5c) | **5s** | 역압 클라가 lock을 물지 못하게 |
  | `retry_after_seconds` | 정수 초, **1~30** | 서버가 산출해 전송. 클라는 C4 공식으로 clamp |
  값은 조정 가능하지만 **테스트는 이 값을 기대값으로 쓴다**.

- **D8 `unsubscribe` 계약 = ack 필수 + fail-open (§8 원문 공백)** — §8은 subscribe/ack/reauth만 정의했으나
  **iOS는 unsubscribe를 실제로 쓴다**(ExchangeRateViewModel.swift:333 — KRX 권한 해제 시). 현재
  `sendTopicCommand`는 request_id도 ack도 없는 **fire-and-forget**이라, 유실되면 서버·클라 상태가
  **lease 만료까지 어긋난다**. → **unsubscribe도 `request_id` + `subscription_ack`**(§8-B).
  - 불변식: **상태를 바꾸는 모든 클라 메시지는 `request_id`를 갖고, ack이 `active_subscriptions` 전체를 반환한다.**
  - ⚠️ **제거는 fail-open** — `unsubscribe`를 인증 성공에 종속시키면 **권한 축소가 실패**한다.
    토큰 만료·revocation·Firebase 장애 시 제거가 막혀 **사용자가 끄라고 한 데이터가 lease 만료까지 계속 흐른다**.
    제거는 접근을 **줄이는** 방향이라 인증 실패가 이유가 될 수 없다.
    → **`id_token` 불요.** `request_id`와 connection 유효성만 확인하고, **premium/entitlement 재검증 없이
    idempotent하게 제거한 뒤 ack**한다. 결과는 `removed_topics` + `active_subscriptions`로 전달.
  - (대안이던 `subscribe(replace_all=true)`는 증분 subscribe를 유지하는 현 설계와 덜 맞아 채택하지 않음.)
- **D9 오류 코드 완결** — 테스트 가능하려면 값이 확정돼야 한다.
  | 상황 | 응답 |
  |---|---|
  | malformed payload / 형식 위반 | `invalid_request` (전체-요청) |
  | D7 상한 위반 | `request_too_large` (전송 가능 시) 또는 **close 1009**(전송 계층, 클라이언트 관측) |
  | 일시 장애 | `temporarily_unavailable` + **`retry_after_seconds`** |
  | `request_id` UUID 형식 오류 | `invalid_request` |
  | `topics` 빈 목록 / 중복 정규화 후 빈 목록 | `invalid_request` (no-op 아님 — 조용한 실패 금지) |
  | 인증된 unsubscribe 성공·실패 | `subscription_ack` (D8, 동일 schema) |

#### E. 롤아웃·정책

- **E1 capability flag 분리 + 무토큰 subscribe 처리 (중간 상태 정의)**
  - ⚠️ **현행 코드 정정**: `TOPIC_DISPATCHER_ENABLED=true`면 무토큰 subscribe는 무시가 아니라
    **`registry.register()` + snapshot 전송**된다(topic_dispatcher.py:278-285 — `id_token`/`request_id` 검사 없음).
    현행 iOS도 무토큰이다(WebSocketService.swift, payload = `{type, topics}`). 구 문서의
    "구형 무인증 메시지만 silent-ignore로 남긴다"는 **오서술**이었다.
  - **인증 강제 ON**: `id_token` 없는 subscribe는 **등록하지 않고** `subscription_error`(`invalid_token`)를 보낸다.
    `request_id`가 없으면 **`request_id: null`로 응답**한다(§8-B error 메시지에서 nullable 허용) — 조용한 실패 금지.
    `unsubscribe`는 D8대로 `id_token` 없이 정상 처리(fail-open).
  - **중간 상태(§6 step 3, enforcement OFF)**: 인증 capability는 배포하되 **강제하지 않는다** —
    무토큰 subscribe는 **기존대로 등록**되고 lease·sweep·`reauth_required`가 **돌지 않는다**(전원 무기한).
    토큰이 실린 subscribe만 인증·lease 경로를 탄다. 이래야 dormant→flip이 all-or-nothing이 아니게 된다.
  - **강제 전환 시점**에 구 클라(무토큰)는 topic을 잃는다 → **Stage B 유예(S4)와 같은 시점**에 묶어야 한다.

- **E2 `check_revoked` = A1 horizon에 통합** (별도 정책 waiver 폐기).
  - `firebase_identity_verified_at`을 **entitlement와 동일한 authoritative horizon**으로 취급한다(A1의 3-way min).
    → `check_revoked=True`는 **매 메시지가 아니라 horizon 갱신 시에만** 수행하면서도,
    삭제·비활성 계정 접근이 **최대 15분**으로 제한된다.
  - ⚠️ 구 안(최초 바인딩만 `True`, 이후 최대 ~1시간 허용)은 **폐기**. 그 안은
    "삭제된 계정이 ID token 자연 만료까지 접근 가능"이라는 **별도 보안 정책 승인**을 요구했는데,
    horizon 통합이 **추가 비용 없이**(갱신 주기가 같음) 그 잔여를 없앤다 — 특례를 둘 이유가 없다.
  - 비용: horizon 갱신마다 Firebase `get_user()` RTT 1회가 **추가**된다(RevenueCat 조회와 같은 cadence이지
    "비용 없음"이 아니다 — 호출 비용과 **장애 표면이 하나 늘어난다**). ~11~12분/연결, **현 규모(<500명)에서
    수용 가능한 bounded cost**. 실패는 A6 `temporarily_unavailable` → C4(미연장 → 자연 만료).

- **E3 REST twin 게이트가 롤아웃 선행 조건 (blocker)** — ✅ **서버 land 2026-07-25**
  `GET /api/v2/topics/snapshot`은 **`verify_firebase_token`이 없고** `TOPIC_DISPATCHER_ENABLED`로만
  막혀 있었다(off → 404). 그런데 **1C는 이 flag를 켜야 동작한다** — 즉 1C 활성화가 현재 flag-off로 완화 중인
  **무인증 KRX REST 경로를 되연다**(운영 완화 기록: route auth 감사).
  → **REST twin 인증·entitlement 게이트가 `TOPIC_DISPATCHER_ENABLED=true` *이전에* land해야 한다.**
  ⚠️ 이 때문에 §8-D의 "KRX per-user 강제 동시 해소(ADR-038 Open 2 close)"는 **WS만으로 성립하지 않는다** —
  REST twin 포함이 1C 범위임을 명시하거나, Open 2 close 주장을 축소해야 한다. **여기서는 범위에 포함한다.**

  **구현된 계약** (`app/main.py` `get_v2_topic_snapshot` + `app/topic_initial_snapshot.visible_snapshot_topics_sync`):
  1. **순서가 계약** — dormant flag → 인증 → premium → topic 판정.
     flag는 인증보다 **앞**(dormant 계약 보존 + 불필요한 Firebase RTT 회피),
     인증은 topic 판정보다 **앞**(미인증자가 `supported_topics`를 열거하지 못하게).
  2. dormant 체크 다음이 **곧바로** `verify_firebase_token` — 인가 전에는 어떤 작업도 하지 않는다
     (401 시 premium 판정·DB 세션·entitlement 조회·빌드 **전부 미도달**).
  3. `require_premium(allow_empty=False)` → INACTIVE 403 / PENDING 503+Retry-After(공통 정책 재사용).
  4. **per-user KRX**: `visible_snapshot_topics_sync()`가 전역 집합 ∧ `compute_krx_visible`(G3∧G2∧G1∧premium).
     unknown_topic 판정과 `supported_topics` 에코가 **같은 함수**를 공유 — 갈리면 404 코드는 맞는데
     목록으로 존재가 새는 회귀가 난다(§3.2). 전역 게이트 off면 세션 자체를 열지 않는다(장애 표면 축소).
  5. **세션 수명이 계약**: `Depends(get_db)`를 쓰지 않는다. request-scoped 세션은 응답까지 커넥션을 쥐고,
     그 뒤 builder가 **두 번째** 세션을 연다 → 좁은 풀(`pool_size=3 + max_overflow=2`)에서 bootstrap
     동시 요청이 서로의 builder 커넥션을 기다린다. 가시성 조회는 `to_thread` 안에서 열고 **닫은 뒤**
     builder가 시작하므로 요청당 동시 checkout은 1개. 동기 SELECT를 이벤트 루프에서 돌리지 않는 효과도 겸한다.
  6. **핸들러가 직접 만드는 응답 전부**(200 + dormant/unknown_topic/topic_unavailable 404)에
     `Cache-Control: no-store`. 200만 붙이면 private 캐시가 비-entitled의 KRX 404를 저장해
     entitlement 부여 뒤에도 404가 남는다(404는 RFC 9110 §15.1 휴리스틱 캐시 대상, iOS는
     `.useProtocolCachePolicy`). **401/403/503엔 없다** — `HTTPException` → Starlette 기본 핸들러
     경로이고 이 앱엔 exception handler·middleware가 0건이라 주입 지점이 없다. 세 코드 모두
     휴리스틱 캐시 대상이 **아니라**(RFC 9110 §15.1 목록에 부재) 준수 캐시는 저장 자체를 못 하므로
     실질 위험 0. ⚠️ 단 이 응답들은 사용자별 개인화인데 `Vary: Authorization`이 없다 —
     **no-store 4개소가 유일한 방어선**이다. 앞단에 CDN을 두거나 max-age를 추가하면 즉시 교차사용자 누수.
  7. **23 테스트**(엔드포인트 12 + KRX 캐스케이드 통합 3 + 라우트 계약 1 + helper 7),
     전부 **개별 무력화로 실패 확인**(14 시나리오). 기존 REST bootstrap 테스트 **3건**도 auth/premium
     patch를 추가(dormant 케이스는 의도적 미패치 — 인증 이전에 반환되므로) — patch가 필요해진 것
     자체가 게이트 회귀 잠금이다.

  **의도적으로 안 한 것 (결정 + 재개 트리거)** — codex 후속 리뷰 + 6렌즈/3적대 감사 수렴:

  | 항목 | 결정 | 근거 / 재개 트리거 |
  |---|---|---|
  | ~~lazy entitlement shortcut 채택 안 함~~ → **채택** (2026-07-26 번복) | ✅ 구현 | **"최적화 vs 계약 명료성" 프레이밍이 틀렸다** — 이건 **견고성 속성**이다. FX/USDT builder는 Redis-first라 warm이면 DB 커넥션 0개인데, 무조건 조회하면 그 요청이 **DB 필수로 승격**된다 → entitlement DB 순단 하나로 entitlement와 무관한 FX bootstrap이 죽는다(콜드런치 FX 3 동시 + tether 1). §3.2는 등가성(`visible ⊆ supported` ∧ `supported \ visible ⊆ per_user_gated`)으로 보존되고, 위험했던 두 갈래는 **테스트로 봉인**: `per_user_gated_snapshot_topics()` registry + 집합대수 trip-wire(새 per-user 필터를 등록 없이 추가 → red) + "미지원 topic도 조회한다" 회귀(⛔ 조기 반환 금지). |
  | entitlement DB 장애 → 503 | ✅ **land 2026-07-26**(1C 이월에서 앞당김 — 형태 확정 + 컨텍스트 유효) | 구 동작은 plain **500**(exception handler 0건)이라 클라 재시도 분류에서 빠졌다. 최종 형태: **`TRANSIENT_DB_ERRORS` 4종**(`OperationalError`/`InterfaceError`/`TimeoutError`[풀 고갈]/`DisconnectionError`) → `{"error": "temporarily_unavailable"}`(WS §8-C와 같은 어휘) + no-store, **Retry-After 없음**(그 헤더는 PENDING 초 단위 신호. DB failover는 분 단위 → 클라 지수 backoff 계약으로 대체), **판정·빌드 양쪽** 감쌈. ⛔ 초안의 `except SQLAlchemyError`는 **너무 넓었다**(ProgrammingError·InvalidRequestError 등 영구 결함 포함) — codex Major, 음성 테스트를 `ValueError`로만 써서 못 잡았다. |
  | `topic` 입력 상한 | **D7 확정까지 보류** | WS는 D7이 64자를 두는데 REST twin엔 상한이 없다. twin 한쪽만 선결정하면 반대 방향 비대칭이 생긴다. XSS·로그인젝션은 nosniff + percent-encoded access log로 이미 차단. |
  | 거부 관측(로그·카운터) | **1C rollout 체크리스트** | 401/403/404-krx가 무성(無聲)이라 "iOS bootstrap 미이관" 잔여 리스크가 양쪽에서 조용히 실패한다. dormant 동안은 볼 소비자가 없다. |

  **해소됨**: `require_premium` fail-open은 별 슬라이스로 미루려 했으나 **behavior-change-0가
  증명되어**(enum 3값 × 모든 return 경로가 enum) 즉시 처리했다 — 커밋 `fa351ef`.

  **잔여(같은 E3 범위, flag ON 전 필수)**: iOS `APIService`의 bootstrap 3종
  (`fetchTetherSnapshot`/`fetchKrxSnapshot`/`fetchFxSnapshot`)이 **무인증 경로**라 flag ON 시 401을 받는다.
  전부 `try?` 격리라 크래시는 없고 cold-start bootstrap만 조용히 사라진다 → **F 슬라이스에서 1B
  인증 transport로 이관**. 운영은 현재 flag off라 이 시점의 사용자 영향은 0.

- **E4 구매 수렴 end-to-end 계약 — enforcement 활성화 blocker (2026-07-27, E3와 동급)**

  서버측 무효화·freshness만으로는 **구매가 수렴하지 않는다.** 둘 다 *"서버가 다시 물어볼 준비"*를 시킬 뿐
  아무 일도 일으키지 않기 때문이다 — 실제 복구는 **클라의 다음 subscribe**가 트리거인데,
  `premium_required`는 **terminal**이라 클라가 재시도하지 않는다. 그래서 5분이 지나도, webhook이 와도
  **그 연결은 거부 상태로 남는다.**

  → 활성화 전에 **"구매 완료 → 제한된 refresh/backoff → active ack"** 클라 경로가 필요하다.
  - ⚠️ **forced refresh도 즉시 복구가 아니다** — 우리 캐시만 우회할 뿐 RevenueCat 자신의 전파 지연은 못 넘는다.
    구매 직후면 다시 `Determined(is_premium=False)`를 받는다. 따라서 계약은 *"한 번 호출로 복구"*가 아니라
    **"제한된 재시도로 수렴"**이며 **single-flight + rate limit + bounded backoff가 필수**다
    (없으면 RevenueCat이 가장 느린 구간에 클라가 몰아친다).
  - 계약은 구매 전용이 아니라 일반형이다: **"거부됨 → 이제 허용됨" 전이는 전부 클라 재요청이 필요**하므로
    (KRX entitlement 부여도 동일) `premium_required` terminal성의 **예외 트리거**를 정의해야 한다.
  - 서버측 forced-refresh 진입점은 A4-1의 무효화 primitive를 **재사용**하는 또 다른 호출자일 뿐이라
    추가 설계 부담은 없다 — 남는 건 클라 계약과 rate limit이다.

#### F. iOS 슬라이스 입력 (여기서 잠그지 않음 — 슬라이스 2에서 코드와 함께 설계)

> ⚠️ 단 **ack/`reauth_required` schema(D2·D3) · 재인증 공식(D6) · unsubscribe ack(D8) ·
> UID reset 수렴 방식(C1·D2 `active_subscriptions`)은 서버 wire 설계**이므로 D/C에서 이미 확정했다.
> 아래는 그 계약을 소비하는 **클라 내부 구조**만이다.
>
> **`connectionGeneration`은 클라 소유**다 — 서버 `identity_generation`은 같은 소켓의 UID 재바인딩만 표현하므로
> reconnect 식별에 쓸 수 없다(D2). 구 receive task·구 request_id의 결과 폐기는 클라 `connectionGeneration`이 담당한다.


- `subscribedTopics` 단일 Set(WebSocketService.swift:27)을 **desired / pending request / accepted**로 분리.
  accepted가 아닌 topic의 snapshot은 무시(현재는 revoke·unsubscribe 후 늦게 온 snapshot이 그대로 적용된다).
- **batch subscribe** — `resendSubscriptions`(:185)가 topic마다 개별 전송한다. 연결 시 desired 전체를 **한 요청**으로
  보내고 이후 KRX 변화만 증분 처리(토큰 검증·premium 확인·ack timer·snapshot 작업의 4중 복제 제거, D1 startup 목표와 정합).
- **토큰 복구** — `subscription_error.invalid_token`에서 **1B provider의 forced refresh를 single-flight로 1회**만 수행하고
  **새 request_id**로 재전송. 두 번째 거부는 재시도하지 않는다. 계정전환·`connectionGeneration` 변경 이후 도착한 ack은 무시.
- **background/foreground** — 15분 이상 suspend되면 타이머 실행을 신뢰할 수 없다. foreground 복귀 시 lease가 불확실하면
  **데이터 적용 전에** 즉시 재인증 또는 reconnect. 타이머는 monotonic이되 suspension 경과도 반영.

#### G. 테스트 매트릭스 (착수 단위)

**소유 규약 (harness 선행 (6), 2026-07-26)** — **모든 행에 `[server]`/`[client]`/`[both]` 태그를 붙인다.
기본값은 없다.** 태그 없는 행은 미분류이며 착수 전에 분류한다.

이유: 기본값을 두면 태그를 잊었을 때 조용히 그 기본값으로 읽힌다. "무태그 = server"는 이 규약의 목적
(**서버 테스트가 클라 계약까지 덮는다는 착각 방지**)과 **정반대 방향으로 fail-open**한다. 날짜 기준
규칙("이전 행은 server")도 미래 독자가 어떤 행이 '기존'인지 판별할 수 없어 성립하지 않는다.

- `[server]` — 서버 테스트로 완결된다.
- `[client]` — 서버가 관측할 수 없다. 클라가 구현·검증해야 한다.
- `[both]` — **한쪽만 잠그면 계약이 성립하지 않는다.** 서버만 테스트하면 클라가 미구현이어도 green이다.

**계약 행 78개** (2026-07-26 기준: server 54 / client 12 / both 12).
아래 `G-ROWS-BEGIN`/`G-ROWS-END` 마커 **사이**의 태그만 센 값이다. 재산출:

```bash
# ⚠️ 패턴을 ^...$ 로 **줄 전체 고정**할 것. 앵커가 없으면 이 설명·명령 줄이 먼저 매치돼
#    awk가 불필요한 범위를 하나 더 처리한다(지금은 그 구간에 태그가 없어 우연히 맞을 뿐이다).
awk '/^<!-- G-ROWS-BEGIN -->$/,/^<!-- G-ROWS-END -->$/' FREE_TIER_ACCESS_MODEL_PLAN.md \
  | grep -o '`\[\(server\|client\|both\)\]`' | sort | uniq -c
```

⚠️ 마커를 쓰는 이유: "§8 기본:부터"처럼 **본문에도 등장하는 문자열**로 범위를 적으면 세는 사람마다
시작점이 달라진다(그 문구 자체가 매치돼 커버리지 문단까지 포함되면 54/13/13이 나온다). 범위 없는
개수는 재현이 안 된다 — 실제로 파일 전체를 세어 86으로 잘못 보고한 적이 있다. 행이 늘면 이 수도 갱신할 것.

⚠️ **커버리지 실측 (2026-07-26)**: `subscription_ack`·`reauth_required`·`lease_id`·`accepted_topics`·
`rejected_topics`는 서버(`app/`, `tests/`)·iOS(`FXi/`, `FXiTests/`) **모두 0건**이고, iOS 테스트는
`WebSocketService`를 의존성으로 생성만 해 `subscribedTopics` Set만 단언한다(연결·수신 상태기계 미구동).
→ **1C WebSocket 상태기계 커버리지는 양쪽 0**. 단 `[client]`/`[both]` 행 *전체*가 0인 건 아니다 —
`snapshot 중복 timestamp-merge`는 `TopicSnapshotMergerTests`가 순수 함수 수준으로 이미 덮는다.
없는 것은 그 merge가 **실제 WS 수신 경로를 거쳐** 적용되는 통합 검증이다.

<!-- G-ROWS-BEGIN -->
§8 기본: `[both]` request_id 상관관계 / `[server]` 중복 subscribe / `[both]` 부분 reject /
`[both]` token refresh / `[both]` reconnect / `[both]` lease 만료.
A: `[server]` horizon 계산(캐시 4분 → lease ~11분) / `[server]` stale fallback으로 연장 안 됨 /
`[server]` `CACHE_TTL < LEASE` 불변식 / `[server]` `now == expires_at` 경계 만료 / `[server]` single-flight.
B: `[server]` 만료 후 publish 0건(**sweep 미실행 상태에서도**) / `[server]` 조회~전송 지연 중 만료 시 전송 차단 /
`[server]` snapshot도 동일 차단 / `[server]` `get_subscribers`가 만료분 미삭제 /
`[server]` 소켓별 송신 순서(ack → live → snapshot).
C: `[server]` UID 변경 시 전량 제거 / `[server]` B premium 실패해도 A 미복원 / `[server]` B 토큰 무효면 A 불변 /
`[server]` 같은 UID면 언급 안 된 topic 불변 / `[server]` reject된 구 등록 제거 /
`[server]` 구 sweep이 갱신된 lease 미제거(CAS) / `[server]` transient 실패 시 registry 불변·lease 미연장.
D: `[both]` ack `lease_duration_seconds`(서버 산출 ⊥ 클라 **실제 wire decode** — 순수 계산 비교로는 부족) /
`[both]` `reauth_required` schema·`lease_id`(동일) / `[client]` 새 request_id 재시도 /
`[server]` **오류 단계 matrix**(D5 단계별 — 구 "우선순위" 폐기) / `[server]` 입력 상한.
E: `[server]` capability flag off에서 인증 요청에 ack 반환 / `[server]` sweeper lifecycle(lifespan 1 task,
예외 후 지속, 연결별 통지 1회, send 실패 시 소켓 정리, 일부 topic 만료 시 나머지 유지) /
`[server]` **identity horizon 갱신 시 `check_revoked=True`**(E2 — 구 "정기 갱신 False" 폐기).
F(클라 계약 — 종전 G에 행이 없어 통째로 누락돼 있었다):
`[client]` desired/pending/accepted 분리 + revoke·unsubscribe 후 늦은 snapshot 폐기 /
`[client]` batch subscribe(현행은 `resendSubscriptions`가 topic별 루프) /
`[client]` reconnect 후 `connectionGeneration`으로 구 receive task **데이터**까지 폐기(구 ack만이 아니다) /
`[client]` suspend 경과 반영 + foreground에서 lease 불확실 시 **데이터 적용 전** 재인증·reconnect.

보강(codex 감사):
`[client]` ack의 topic별 `lease_id` ↔ 늦게 온 구 `reauth_required` 무시 /
`[both]` UID 변경 ack의 `active_subscriptions`가 전체 상태로 수렴(서버 산출 ⊥ 클라가 로컬 accepted를 **교체**) /
`[server]` **wall clock 역행에도 strict horizon 불변**(A1 monotonic 축) /
`[server]` connection lock 안에서 sweep↔재인증 **양방향** 순서 /
`[server]` 만료 claim 후 통지 timeout·중복 sweep 없음 /
`[client]` **10분 lease → 6~7분 재인증** 계산(D6) / `[server]` `temporarily_unavailable`의 `retry_after_seconds` /
`[server]` D7 상한 **각각의 경계값**(message 16KiB / topics 8 / topic 64자 / request_id 36자 / 중복 first-occurrence) /
`[server]` **invalid token은 기존 UID·lease 불변** / `[client]` 구 `request_id` ack과 구 `identity_generation` ack 무시.

보강 2차(codex 감사):
`[both]` 인증된 unsubscribe의 ack + `active_subscriptions` 수렴(유실 시 상태 불일치 없음 — 서버만 잠그면
클라가 fire-and-forget으로 남아도 green. 실측: 현행 iOS `unsubscribe`는 `request_id`도 ack 처리도 없다) /
`[both]` `active_subscriptions`가 **미언급 기존 topic의 `lease_id`·잔여 duration까지** 실어 클라가 상태 복구 가능 /
`[server]` 서버 `identity_generation`은 **같은 소켓 UID 재바인딩만** 표현(reconnect 식별은 클라 `connectionGeneration`) /
`[server]` **identity horizon**(`check_revoked=True` 시점 + 15분)으로 삭제·비활성 계정 접근이 15분 내 종료 /
`[server]` **message** 상한이 **서버(uvicorn) 계층에서 강제**되고 초과 시 **클라이언트가** close 1009 관측 /
`[server]` `invalid_request`(malformed · UUID 오류 · 빈 topics · 중복 정규화 후 빈 목록) /
`[client]` `retry_at = min(retry_after, lease_remaining − safety)` — 재시도가 잔여 lease를 넘기지 않음 /
`[server]` strict verifier single-flight의 **owner 격리·정리**(한 소켓 종료가 공유 owner를 취소하지 않음, 1B 테스트 템플릿) /
`[server]` **claim된 항목이 `get_subscribers`·`send_if_authorized`에서 즉시 제외**(통지 중 live publish 통과 금지) /
`[server]` **webhook이 strict cache·single-flight 결과까지 무효화**(구 horizon 재사용 금지).

보강 3차(codex 감사):
`[server]` **unsubscribe는 토큰 없이도 성공**(만료·revoked·Firebase 장애에서도 제거됨 = fail-open) + `removed_topics`·`operation` /
`[server]` **`lease_id` 재사용 금지**(제거 후 재구독 / UID 변경 후 재등록에서 구 `reauth_required`가 새 lease와 불일치) /
`[server]` **identity verdict가 토큰 단위**(관측은 UID 키 — 같은 UID의 새 토큰 검증을 revoked 구 토큰이 공유하지 못함) /
`[server]` **webhook epoch fence**(검증 시작 → invalidate → 구 검증 완료가 cache를 되살리지 못함) /
`[client]` `retry_at`이 음수가 되지 않고 `lease_remaining <= safety`면 즉시 재인증 /
`[both]` **ack 유실 후 재시도 idempotency**(클라의 새 request_id 재시도 ⊥ 서버의 topic-set 불변·lease 갱신) /
`[server]` `active_subscriptions`가 **lock 아래 단일 snapshot**·duration은 floor·**≤0이면 미포함** /
`[server]` `--ws websockets --ws-max-size 16384` 적용 + **subprocess 서버에 붙은 클라이언트가 close 1009 관측** /
(⚠️ ASGI 앱은 legacy impl에서 **1006**을 본다 — §D7 참조. 구 "ASGI 통합 확인" 문구는 폐기) /
`[server]` §8 canonical schema ↔ §8.1 불변식 **상호 정합**(두 곳이 다른 구현을 유도하지 않음).

보강 4차(6렌즈 병렬 감사):
`[server]` **`temporarily_unavailable`이 어느 단계에서 나도 전체-요청 + registry 불변**(RevenueCat 장애가 구독을 지우지 않음) /
`[server]` **entitlement DB 순단이 krx 등록을 지우지 않음**(transient ≠ 권한 없음) /
`[server]` **무토큰 subscribe = 등록 거부 + `request_id: null` 오류**(조용한 실패 없음) /
`[server]` **enforcement-OFF 중간 상태에서 lease·sweep 미동작** /
`[server]` **REST twin 게이트가 flag ON보다 먼저**(무인증 KRX 재개방 방지) /
`[server]` **전송 실패는 소켓 종료**(부분 삭제 후 유지 금지 — ack이 거짓이 되지 않음) /
`[server]` **연결당 동시 dispatch에서 unsubscribe·ping이 subscribe 뒤에 막히지 않음**(pong timeout 내) /
`[server]` **구 UID subscribe가 새 바인딩을 되돌리지 못함**(⛔ `auth_time` 단조는 폐기 — 클라 소유 identity generation 또는 live 소켓 cross-UID 거부, C1에서 택일) / `[server]` **lock 안 I/O timeout** /
`[client]` **최소 잔여 기준 타이머**(혼합 lease에서 짧은 topic이 먼저 만료되지 않음) /
`[both]` **재인증 idempotency = topic 집합 불변이되 lease는 갱신됨** /
`[client]` snapshot 중복은 **timestamp-merge**로 적용 /
`[server]` **epoch 폐기 결과로 lease를 발급하지 않음** / `[server]` flag off → `topic_unavailable`(accepted 금지).
<!-- G-ROWS-END -->

**진행 상황 — A1/A2 산술 슬라이스 (2026-07-27)**
⚠️ 아래는 마커 **밖**에 둔다. 안에 쓰면 `[server]` 같은 태그가 커버리지 집계(54/12/12)에 섞인다.

- **닫힘**: `[server]` `now == expires_at` 경계 만료 / `[server]` `CACHE_TTL < LEASE` 불변식
  (`tests/test_topic_lease.py`, 11개 무력화 전부 red 확인 — survivor 0).
- **산술만 닫힘(절반 열림)**: `[server]` horizon 계산(캐시 4분 → lease ~11분) — 3-way min의
  **계산**은 잠겼으나 캐시가 `verified_at_monotonic`을 **저장**한다는 절반은 미검증.
- **열림(이 슬라이스로 닫히지 않음)** — 각각 필요한 선행이 다르다:
  `[server]` wall clock 역행에도 strict horizon 불변(strict cache **저장·재사용** 경로) /
  `[server]` stale fallback으로 연장 안 됨(A6 3-state verifier — 현행 `PremiumStatus`에는
  fresh/stale 구분이 없고 최대 1시간 stale로 ACTIVE가 나온다) /
  `[server]` single-flight(A5 owner·epoch) / `[server]` identity 관측이 **UID 키 저장 + 토큰별 `iat` 파생**을 실제 저장 경로에서도 유지(⛔ 구 "토큰 fingerprint/`auth_time` 저장 키" 전략은 폐기) /
  `[server]` webhook epoch fence.
- ⚠️ **"최소 lease 10분"을 이 슬라이스 근거로 주장하지 말 것** — `CACHE_TTL < LEASE`는 3-way min의
  *entitlement 항*에만 거는 상한이다. 10분 하한은 `firebase_identity_verified_at ≈ now`라는 E2
  전제가 함께 있어야 하며, 그 전제가 깨진 경우는 `TestStaleIdentity`가 **10분 미만·이미 만료**로
  실증한다.

**⚠️ 테스트 harness 선행 요건**(없으면 계약을 지워도 green인 가짜 통과가 기본형):
(1) **clock 주입 seam** — ✅ **wall 축 land (2026-07-26)**: `Clock(wall: Callable[[], datetime])` +
`system_clock()`을 `app/subscription.py`의 5개 판정 지점 전부에 배선, 커버리지 0건이던 모듈에
baseline 38건 추가(`tests/test_subscription_clock.py`). **값이 아니라 콜러블**을 주입한다 —
패스 시작 시각을 공유하면 `cached_at`이 RevenueCat HTTP 왕복 *이전* 시각으로 찍히고
(`timeout=5.0`은 **단계별** 값이라 왕복 총시간 상한이 아니다),
`get`(miss)·`state`(미등록)의 lazy 읽기도 깨진다(둘 다 무력화로 실증).
✅ **monotonic 축 land (2026-07-27)** — `mono: Callable[[], float]`가 **기본값 없는 필수 필드**로
추가되고 `system_clock`이 `time.monotonic`에 배선됐다. 소비자는 A1 계산기(`compute_lease_expiry`,
A2 참조)이며 호출부가 `clock.mono()`를 요청당 1회 읽어 `now_mono`로 넘긴다.

- ⚠️ **정본 모듈이 `app/subscription.py` → `app/clock.py`로 이동했다**(re-export 없음). 이유는
  §8.1 A4가 만드는 **prospective 순환**이다 — `invalidate_user_cache`가 strict cache·single-flight
  결과까지 무효화하게 되면 `subscription → WS 인가` 엣지가 생기고, `Clock`이 subscription에 남으면
  WS 인가 쪽의 import가 역엣지가 된다. `app/clock.py`는 **stdlib only**로 유지한다.
  `app/subscription.py`는 자기 판정 지점을 위해 가져다 쓸 뿐이고 `__all__`에 없다 —
  `app.subscription` 경유 import는 `tests/test_clock.py::TestCanonicalImportPath`가 AST로 막는다.
  (subscription의 기본 클럭 patch 대상은 **소비자 네임스페이스** `app.subscription.system_clock`.)
- **5개 TTL 판정 지점의 시그니처는 바뀌지 않았다.** 바뀐 것은 `Clock` 생성자와 조립부이고,
  이제 **3곳**이다: `system_clock` / `tests/test_subscription_clock.py`의 `_clock`(wall 전용, mono는
  호출 시 실패하는 **poison** — "subscription은 mono를 읽지 않는다"의 trip-wire) /
  `tests/test_topic_lease.py`의 `_clock`(두 축 독립 제어). 축이 또 늘면 세 곳 다 고칠 것.
  ⚠️ 호출부 **61곳**(`[0]` 51 + 언팩 10)은 2-tuple 반환을 유지해 무변경이다 — "조립부 2곳"과
  "호출부 61곳"은 다른 축의 수치이니 섞지 말 것.

⚠️ **그래도 G의 `wall clock 역행에도 strict horizon 불변` 행은 열려 있다.** monotonic 축 도입만으로는
부족하다 — 계산기가 wall을 무시한다는 것만 증명될 뿐이고, `verified_at_monotonic`의
**저장·재사용 경로**(strict cache + verifier 배선)까지 있어야 실제 불변식이 검증된다.
(⚠️ 이 문서 규약대로 **심볼 앵커**로 적는다. 초안은 `:726`이라 적었는데 이후 편집으로 실제 위치가
밀려 어긋났다 — 규약을 만든 문장이 규약을 어겼다.) (2) **Firebase 예외→verdict 매핑 seam** — ✅ **2a land (2026-07-26)**:
conftest가 `firebase_admin.auth`/`.exceptions`의 **예외 속성만 진짜 클래스**로 제공(나머지는 MagicMock 유지)
+ `google.auth`는 미설치 시에만 stub. 19건 추가(`tests/test_firebase_auth_mapping.py`), 기존 3693건 무영향.
  - **계층까지 재현한다**: firebase-admin v6.9.0에서 `Expired`·`Revoked`가 `InvalidIdToken`의 **하위**라
    `verify_firebase_token`의 except 순서(Revoked → Expired → Invalid)가 load-bearing이다. 형제로 stub하면
    순서 뒤바꿈 회귀가 **매핑 테스트를 통과한다**(합성 무력화로 실증) → `TestStubFidelity`가 그 전제를 잠근다.
  - **부모 속성 명시 배선 필수**: `from firebase_admin import auth`(main.py)는 부모 MagicMock의 자동 생성
    속성을 돌려줘 `sys.modules["firebase_admin.auth"]`와 **다른 객체**가 된다(실증). 둘 다 배선해야 한다.
  - **로컬/CI 격차 해소**: 종전 로컬은 `google.auth` 미설치로 `verify_firebase_token` 본문 진입 자체가
    ImportError, CI는 진입 가능해 `decoded_token["uid"]`가 MagicMock을 반환했다.
  ⚠️ **(2)는 아직 미완**: 2a가 잠근 것은 **HTTP 401/503 의미 경계**다. D5 2단계의 wire verdict
  (`invalid_token` / `temporarily_unavailable`)는 **여기서 검증되지 않는다** — `app/topic_dispatcher.py`의
  subscribe 경로가 아직 토큰을 읽지 않는다. **2b**에서 token-string 기반 verifier + WS verdict translator와
  함께 검증한다. 또 `check_revoked=True` 경로의 Firebase 계열 오류(`UserDisabledError` 등)를 generic 401로
  둘지 `temporarily_unavailable`로 승격할지는 **2b 전 별도 결정**이다(현행은 401 fail-closed). (3) **인터리빙 강제 hook** — 임계구역에 await가 없으면
단일 스레드에서 race가 물리적으로 안 나 **B4 lock/C3 CAS를 지워도 green**. (4) ✅ **land (2026-07-26)** — uvicorn subprocess harness(`tests/test_ws_message_limit.py`).
TestClient는 프레이밍 계층이 없어 검증 불가라는 판단은 맞았으나, **실서버로 바꿔도 서버측 단언은 불가능**하다
(앱은 1006을 본다 — §D7 참조). 계약은 클라 관측 1009. 하니스는 실앱이 아니라 최소 ASGI 앱을 띄우고
(conftest stub이 subprocess에 전달되지 않으므로), **Dockerfile CMD를 구조 파싱해 그 토큰을 재사용**한다
— 테스트가 플래그를 따로 하드코딩하면 프로덕션 설정과 조용히 갈라진다. (5) sweeper는 `sweep_once(now)` 순수 함수 + 등록 분리. (6) ✅ **land (2026-07-26)** — G 소유 규약
(**모든 행에 `[server]`/`[client]`/`[both]` 태그, 기본값 없음** — 기본값을 두면 태그를 잊었을 때
그 값으로 조용히 읽혀 규약의 목적과 반대로 fail-open한다). "iOS엔 WebSocketService 구동 테스트가 없다"는 실측 확인:
iOS는 `subscribedTopics` Set만 단언하고 연결·수신 상태기계를 구동하지 않는다. 덧붙여 **1C 프로토콜
심볼은 서버·iOS 양쪽 모두 0건**이라, 소유 태그는 "현재 커버리지"가 아니라 **테스트가 어디 있어야
하는지**를 정하는 것이다.

#### H. 동반 문서 갱신 (구현 커밋에서)

[REALTIME_V2_CLIENT_GUIDE.md](REALTIME_V2_CLIENT_GUIDE.md)가 1C와 **정면 충돌**한다 — :5 "prod LIVE" /
:56 "subscribe 성공/실패 ack 없음, 조용히 무시 · snapshot 수신 자체가 성공 신호" / :118 "per-user 노출은 클라
`krx_visible` gate 담당(WS 무인증)". 구현 커밋에서 반드시 함께 갱신할 것.

**Non-goal**: legacy 그래프 dead-code 삭제(별도 커밋) / 고급 rate-limiting(후속).

---

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
