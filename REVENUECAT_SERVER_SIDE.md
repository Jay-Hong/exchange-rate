# FXi 서버 - RevenueCat 서버 사이드 구현 문서

이 문서는 FXi 서버에서 RevenueCat 구독 상태를 검증하고, 알림 API를 Premium으로 게이팅하기 위한
서버 사이드 구현 기준을 정리한다. 구현 전/중/후에 이 문서를 기준으로 논의하고 확정한다.

## 1. 목적 및 범위

- 목적: 서버에서 Premium 구독을 검증하여 알림 설정 API를 보호한다.
- 범위:
  - 요청 시 RevenueCat REST API 검증
  - LRU + TTL + Stale 캐시
  - Webhook Authorization 헤더 검증 및 캐시 무효화
  - 알림 API 게이팅
- 범위 제외:
  - 구독 상태 DB 영속 저장 (스키마/마이그레이션 없음)
  - 앱 Paywall/클라이언트 UX 로직

## 2. 참고 문서

- `ALERT_SUBSCRIPTION_GUIDE.md` 5.5 섹션
- RevenueCat REST API v1 `/subscribers/{app_user_id}`

## 3. Premium 게이팅 정책

Premium-Gated 엔드포인트 (서버 기준):

- `POST /api/notification-settings` -> 비구독자 403
- `PUT /api/notification-settings/{id}` -> 비구독자 403
- `DELETE /api/notification-settings/{id}` -> 비구독자 403
- `GET /api/notification-settings` -> 비구독자 빈 목록 반환
- `GET /api/rates`, `WS /ws` -> 항상 허용

## 4. 환경 변수 (필수)

- `REVENUECAT_API_KEY` (Secret API Key, `sk_...`, API version **V1** 권장)
- `REVENUECAT_WEBHOOK_AUTH_KEY` (Webhook Authorization 헤더 비교용 고정 값)

`app.config`를 통해 환경 변수를 접근하여 `.env` 로딩 순서를 보장한다.

### 4.1 Secret API Key 생성 방법 (RevenueCat Dashboard)

Secret API Key는 자동 생성되지 않으며, 아래 절차로 직접 생성해야 한다.

1. RevenueCat Dashboard → **API keys** → **Secret API keys**
2. `+New secret API key` 클릭
3. **Generate a new secret API key**에서 다음을 설정
   - Name 필드 입력
   - API version: **V1** 선택 (현재 서버는 `/v1` 엔드포인트 사용)
4. 생성된 `sk_...` 값을 `REVENUECAT_API_KEY`에 입력

### 4.2 Webhook Authorization 값 주의사항

- Authorization 헤더 값은 **문자열 전체 일치 비교**로 검증한다.
- 값에 공백이 포함되는 경우(예: `Bearer <token>`), `.env`에서 반드시 따옴표로 감싼다.
  - 예: `REVENUECAT_WEBHOOK_AUTH_KEY="Bearer <token>"`
- Dashboard Webhook 설정의 Authorization 값도 **동일한 문자열**로 맞춘다.

## 5. 캐시 정책 (LRU + TTL + Stale)

- 캐시 크기: 1000명
- TTL: 5분
- Stale TTL: 1시간
- 캐시 가능한 응답: 200, 404만 캐시
- 캐시 불가 응답: 4xx/5xx (401, 429 포함)
- API 오류 + 캐시 존재: stale 값 반환 (유료 사용자 보호)
- API 오류 + 캐시 없음: PENDING 반환 → 503 + `Retry-After: 5초` (API 정상화 후 pending TTL 12초 만료 시 INACTIVE)

주의:
- 캐시는 프로세스 로컬이다. 현재 Docker는 `--workers 1` 고정이지만,
  워커가 2개 이상이거나 스케일 아웃 시 Redis 캐시로 전환한다.

## 6. 구독 상태 판정 로직

RevenueCat 응답에서 `subscriber.entitlements.premium`을 조회한다.

권장 판정 순서:

1. entitlement가 존재하고 `expires_date`가 null이면 lifetime으로 간주 (활성)
2. `expires_date`가 현재 시각보다 미래면 활성
3. 위 조건을 만족하지 않으면 비활성

※ `grace_period_expires_date`는 접근 제어가 아닌 상태 표시(UI)용으로만 사용
※ `is_active` 필드는 REST API v1 응답에서 확인되지 않는다.

## 7. Webhook 처리 정책

### 7.1 Authorization 헤더 검증

- 헤더: `Authorization`
- 방식: Dashboard에서 설정한 고정 값과 문자열 비교
- Secret: `REVENUECAT_WEBHOOK_AUTH_KEY`
- 헤더 누락/불일치 시 401 반환

### 7.2 이벤트 처리

처리 이벤트:
- `INITIAL_PURCHASE`
- `RENEWAL`
- `EXPIRATION`
- `CANCELLATION`
- `TRANSFER`

동작:
- `invalidate_user_cache()` 호출
- 대상 user_id:
  - `app_user_id`
  - `original_app_user_id`
  - `aliases[]`
  - `transferred_from` / `transferred_to` (TRANSFER 이벤트)
- 반복 호출은 안전 (idempotent)
- DB 업데이트는 현재 범위에서 제외

## 8. 구현 상세 (파일별)

### 8.1 `app/subscription.py`

- `Clock` (frozen dataclass, `wall: Callable[[], datetime]`) + `system_clock()`
  - 시간 소스 주입점 (ADR-039 §8.1 harness 선행 (1)). **값이 아니라 콜러블**을 주입한다 —
    판정 지점들이 RevenueCat HTTP 왕복(timeout 5s)을 사이에 두고 흩어져 있어 패스 시작 시각
    하나를 공유하면 `cached_at`이 호출 *이전* 시각으로 찍힌다. `get`(miss)·`state`(미등록)의
    lazy 읽기도 호출부 선평가로는 보존되지 않는다.
  - monotonic 축은 lease 도입(§8.1 A1) 때 이 클래스에 추가 — 판정 지점에서 `time.monotonic()` 직접 호출 금지
- `EntitlementCache` (OrderedDict 기반 LRU)
  - `get(user_id, *, clock) -> (value|None, is_fresh)`
  - `set(user_id, is_premium, *, clock)`
  - ⚠️ `clock`은 **필수**다(기본값 없음). 기본값을 주면 호출부가 빠뜨려도 실클럭으로 조용히
    동작해 seam이 무의미해진다. `EntitlementCache`가 `__all__`에 있어 이 둘은 **source-breaking**
    시그니처 변경이다 — 리포 내부 호출자는 전부 갱신됐고 런타임 동작 변화는 0이다
  - `invalidate(user_id)` / `clear()` (test 용도)
- `verify_premium_status(user_id, *, clock=None) -> PremiumStatus`
  - 캐시 조회
  - 없거나 stale이면 RevenueCat API 호출
  - 200/404만 캐시, 4xx/5xx는 캐시하지 않음
  - API 실패 + stale 캐시 → stale 값 반환 (유료 사용자 보호)
  - API 실패 + 캐시 없음 → PENDING 반환 (API 정상화 후 TTL 만료 시 INACTIVE)
- `verify_premium(user_id, *, clock=None) -> bool`
  - `verify_premium_status` 래퍼 (ACTIVE면 True). clock을 그대로 전달 — export된 표면이라
    여기서 막으면 wrapper 경유 결정적 테스트가 불가능해진다
- `invalidate_user_cache(user_id)`
  - Webhook에서 호출

### 8.2 `app/webhooks.py`

- `verify_webhook_auth(auth_header: str) -> bool`
- `POST /webhooks/revenuecat`
  - Authorization 헤더 검증 실패 시 401
  - 이벤트 타입 + 사용자 식별자(`app_user_id`, `original_app_user_id`, `aliases`, `transferred_*`) 추출
  - 이벤트에 따라 캐시 무효화
  - 정상 시 `{ "status": "ok" }`

### 8.3 `app/main.py`

- Webhook 라우터 등록
- `verify_premium()` 적용 범위:
  - POST/PUT/DELETE -> 403
  - GET -> 비구독자는 빈 목록

### 8.4 `requirements.txt`

- `httpx>=0.27.0,<1.0.0` 추가

## 9. 테스트 체크리스트

- ✅ `verify_premium()` 단위 테스트 — `tests/test_subscription_clock.py` (38건, 2026-07-26)
  - 200/404 캐시 동작 / 4xx/5xx 미캐시 / stale fallback / lifetime entitlement
  - + TTL 경계 3종(`age == CACHE_TTL`은 stale / `age == CACHE_STALE_TTL`은 삭제 /
    pending `now == expires_at`은 active) · RevenueCat `expires_dt == now`는 만료
  - + 상태머신 **주요 분기** baseline (fresh short-circuit / stale+성공 / stale+실패 fallback /
    miss+실패 / miss+inactive의 pending none·active·expired). ⚠️ 완전한 곱 매트릭스는 아니다
  - ⚠️ 이 파일 이전엔 `app/subscription.py` 커버리지가 **0건**이었다
- Webhook Authorization 헤더 검증 테스트
  - 정상/누락/불일치
  - `aliases` 포함 payload 캐시 무효화
  - `TRANSFER` 이벤트 처리 (transferred_from/to)
- Stale 캐시 동작 테스트
  - TTL 만료 + API 실패 -> stale 반환

## 10. Open Questions (확정 후 업데이트)

- REST API v1에서 lifetime entitlement 처리 방식 재확인
- `/v1` 엔드포인트 사용 시 Secret API Key(V1) 동작 재확인
- 향후 Redis 캐시 전환 시점 (워커 수, 트래픽 기준)
