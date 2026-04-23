# USDT Tab Proposal

> Status: design locked, ready for implementation
> Updated: 2026-04-23
> Phase 1 design: [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md)

## Goal

기존 모바일 탭 구조 `달러 / 엔화 / 유로 / 뉴스`를
`테더 / 달러 / 엔화 / 유로 / 뉴스`로 확장한다.

새 `테더` 탭은 국내 가상자산 거래소의 `USDT/KRW` 시세를 비교하고,
기존 달러 기준값과 함께 보여 주는 것을 목표로 한다.

## Proposed Tab Contents

테더 탭의 1차 표시 대상 (8개 소스):

거래소 USDT/KRW (5종):

- 업비트 `KRW-USDT`
- 빗썸 `KRW-USDT`
- 코인원 `KRW-USDT`
- 코빗 `USDT/KRW`
- 고팍스 `USDT-KRW`

달러 기준값 (3종):

- 인베스팅 `USD/KRW`
- 국민은행 `USD/KRW`
- 하나은행 `USD/KRW`

> KRX 미국달러선물은 시세 재배포 권리 확인 후 추가 예정 (Decision A 참조)

## Product Interpretation

이 탭은 단순 환율 탭이 아니라 `달러 대체 시장 비교 탭`에 가깝다.

사용자는 아래를 한 화면에서 보게 된다.

- 거래소별 USDT 실제 매매 가격
- 기존 현물환 기준 달러 가격 (인베스팅, KB, 하나)
- 최저/최고 소스
- 소스 간 가격 차이

> 향후 KRX 미국달러선물은 optional source로 추가 가능

## Confirmed Public Crypto Endpoints

2026-04-12 기준 퍼블릭 호출로 실제 응답을 확인한 엔드포인트:

- Upbit: `GET https://api.upbit.com/v1/ticker?markets=KRW-USDT`
- Bithumb: `GET https://api.bithumb.com/v1/ticker?markets=KRW-USDT`
- Coinone: `GET https://api.coinone.co.kr/public/v2/ticker_utc_new/KRW/USDT`
- Korbit: `GET https://api.korbit.co.kr/v2/tickers?symbol=usdt_krw`
- GOPAX: `GET https://api.gopax.co.kr/trading-pairs/USDT-KRW/ticker`

Observed response price fields:

- Upbit: `trade_price`
- Bithumb: `trade_price`
- Coinone: `tickers[0].last`
- Korbit: `data[0].close`
- GOPAX: `price`

## Source Policy Recommendation

### 1. Initial transport

외부 거래소는 1차 구현에서 각 거래소 Public REST polling으로 수집한다.

이유:

- 현재 서비스 자체 WebSocket은 이미 존재한다.
- 외부 거래소 WebSocket 5종을 동시에 붙이면 운영 복잡도가 크게 오른다.
- 5~10초 polling만으로도 모바일 비교 UX는 충분하다.
- 장애 대응과 재시도 정책을 기존 크롤러 구조에 맞추기 쉽다.

### 2. Internal model

`USDT 탭`은 기존 `currency pair` 모델에 억지로 끼우기보다
`source-normalized asset feed`로 확장하는 쪽이 맞다.

다만 1차 구현에서는 기존 API 호환성을 위해
표면적으로는 `currency = usdt-krw`처럼 보이게 유지할 수 있다.

### 3. KRX futures

KRX 미국달러선물은 기술보다 권리와 제공 범위가 더 큰 문제다.

현재 검토 원칙:

- 퍼블릭 배포 전제라면 `재배포 가능 여부`를 먼저 확인
- 명확히 허용되지 않으면 1차 범위에서 제외
- 대체 기준값은 인베스팅/은행 3종으로도 충분히 시작 가능

## UI Layout (Confirmed: Single Unified List)

테더 탭은 기존 달러/엔화/유로 탭과 동일한 막대 비교 UI 패턴을 사용한다.
하이브리드 UI(요약 카드 + 섹션 분리)는 채택하지 않는다.

### 기본 표시 순서

```text
인베스팅
국민은행
하나은행
(미국달러F)        ← Phase 2 추가 예정
업비트
빗썸
코인원
고팍스
코빗
```

### UI 기능

- 기존 환율 탭과 동일한 막대(bar) 비교 컴포넌트 재사용
- 사용자가 정렬 기준 변경 가능 (가격 오름차순/내림차순, 기본 순서 등)
- 사용자가 기준 소스 변경 가능 (예: Investing 대신 KB 기준 diff 계산)
- 최고/최저/스프레드 등 파생값은 **클라이언트에서 계산** (서버 API 미제공)
- 그래프는 Phase 2에서 추가 (Phase 1은 실시간 값 + 알림 중심)

### 표시 컬럼

- 현재가
- 기준값 대비 차이 (원, 클라이언트 계산)
- 기준값 대비 퍼센트 (%, 클라이언트 계산)
- 업데이트 시각

## Alert Design

### A. Single-source alert

현재 구조를 확장해 아래를 지원:

- 특정 소스의 `usdt-krw` 값이 얼마 이상/이하

예:

- 업비트 테더가 1480원 이하
- 코인원 테더가 1495원 이상

### B. Cross-source comparison alert

환율 알림 섹션 아래에 `비교 알림` 섹션을 별도로 두는 안이 적절하다.

비교 알림이 필요한 이유:

- 이번 USDT 탭 요구사항의 핵심이 단일 가격이 아니라 `가격 차이`이기 때문
- 이후 `달러/엔화/유로` 탭에도 재사용 가능하기 때문

예:

- 빗썸 USDT − Investing USD/KRW, signed, gte, 8.0 → "김프 8원 이상"
- 업비트 USDT − Investing USD/KRW, absolute, lte, 1.0 → "차이 1원 이내"
- 업비트 USDT − KB USD/KRW, signed, lte, -3.0 → "역프 3원 이상"

비교식: `left_source_rate − right_source_rate`

조건 체계 (Decision D):

- `diff_type`: `signed` | `absolute`
- `operator`: `gte` | `lte`

## Architecture Impact

현재 백엔드는 아래 제약이 있다.

- 지원 통화쌍이 `usd-krw`, `jpy-krw`, `eur-krw`로 고정
- 알림 모델이 `bank + currency + threshold` 단일 소스 기준
- 그래프 API가 `usd/jpy/eur` 고정

따라서 이번 작업은 단순 크롤러 추가가 아니라 아래 4개 축의 확장이다.

1. 소스 목록 확장
2. `usdt-krw` 자산 추가
3. 그래프/탭 API 확장
4. 비교 알림 모델 추가

## Recommended Delivery Phases

### Phase 0. Design lock (대부분 완료)

잠긴 결정: Decision A~F 참조

남은 결정 (Open Decisions 참조):

- 비교 기준 기본값 (인베스팅 고정 vs 사용자 선택)
- 알림 반복형 허용 여부
- 크립토 그래프 범위
- 백엔드 데이터 모델 세부 설계

### Phase 1. USDT tab data feed

백엔드 1차 목표:

- 거래소 5곳 `USDT/KRW` 수집
- `usdt-krw` 조회 API
- 기존 WebSocket에 `usdt-krw` 포함
- 단일 소스 알림 지원

이 단계에서는 비교 알림은 넣지 않아도 된다.

### Phase 2. Graph and advanced metrics

- 테더 탭 전용 일간 그래프
- 장기 그래프 필요 여부 검토
- 기준값 대비 차이/퍼센트 계산

### Phase 3. Cross-source comparison alerts

- 비교 알림 DB 모델 추가
- 생성/조회/수정/삭제 API 추가
- FCM payload 타입 추가
- 모바일 UI 섹션 추가

## Decisions Locked So Far

### Decision A. KRX futures in phase 1

- `KRX 미국달러선물`은 1차 출시 범위에서 제외
- 이유: 기술보다 시세 재배포 권리/계약 확인이 우선
- 문서/설계에서는 `optional future source`로만 남겨 둔다

### Decision B. Comparison alert model scope

- 비교 알림 데이터 모델은 처음부터 `전 탭 공용`으로 설계
- 다만 실제 UI 노출과 적용은 `테더 탭`부터 시작
- 이후 `달러 / 엔화 / 유로` 탭으로 확장

이 결정의 의미:

- DB 모델은 `bank + currency + threshold` 전용 구조로 굳히지 않는다
- `left source`, `right source`, `operator`, `threshold` 중심의 공용 비교 규칙으로 간다
- 모바일 UI는 1차에서 테더 탭에만 붙여 복잡도를 제한한다

### Decision C. USDT tab layout — Single Unified List (revised 2026-04-23)

- 테더 탭은 **기존 환율 탭과 동일한 단일 리스트 UI**로 확정
- 하이브리드 UI(요약 카드 + 섹션 분리)는 **채택하지 않음**
- 이유:
  - UI 일관성: 사용자가 기존 탭 UX를 그대로 재사용 가능
  - 백엔드 단순화: 서버에서 summary 계산 불필요 (클라이언트에서 계산)
  - 중복 API 제거: investing/kb/hana는 기존 `/api/rates`로 이미 제공 중
- 기본 표시 순서: `인베스팅 → 국민은행 → 하나은행 → (미국달러F) → 업비트 → 빗썸 → 코인원 → 고팍스 → 코빗`
- 사용자가 정렬 기준과 기준 소스를 앱에서 변경 가능

### Decision D. Comparison alert conditions — 2-axis

- 비교 조건은 `signed_diff`와 `absolute_diff` 2축 지원
- `diff_type`: `signed` (방향성, left − right) | `absolute` (방향 무관, |left − right|)
- `operator`: `gte` (이상) | `lte` (이하)
- 4가지 조합으로 모든 케이스 커버:
  - signed + gte: 김프 X원 이상 (빗썸 > Investing by 8)
  - signed + lte: 역프 X원 이상 (빗썸 < Investing by -3)
  - absolute + lte: 수렴 X원 이내 (차이 ≤ 1)
  - absolute + gte: 괴리 X원 이상 (차이 ≥ 10)

### Decision E. Naming convention — bank vs source (revised 2026-04-23)

3층 구조:

| 레이어 | 용어 |
| --- | --- |
| **기존 은행 DB** (`bank_exchange_rates`, `investing_exchange_rates`) | `bank` + `currency` |
| **새 source DB** (`source_rates`) | `source` + `asset` |
| **레거시 호환 API** (`/api/rates`, WebSocket `rates` 배열) | `bank` + `currency` 형태로 응답 (어댑터 변환) |
| **새 API** (비교 알림 등) | `source` + `asset` 구조 사용 |

- 기존 클라이언트 호환성 보존
- 새 기능은 깔끔한 source 기반 설계
- API 응답 시에만 `source → bank`, `asset → currency` 어댑터 변환
- canonical key (`{source}:{asset}`)는 **서버 내부 helper로만** 사용, 외부 API에 노출하지 않음 (source + asset으로 충분)

### Decision F. Transport — REST polling

- 1차 구현은 거래소 WebSocket이 아닌 REST polling (5~10초)
- 기존 APScheduler + 서비스 자체 WebSocket 재전송 구조 활용
- 이유: 운영 복잡도 최소화, 10초 주기면 정보 제공 서비스로 충분

## Open Decisions

없음. 모든 설계 결정이 잠겼다.

이전에 열려 있던 항목들의 처리 결과:

1. ~~비교 기준 기본값~~ → **앱에서 사용자 선택 토글로 처리** (서버는 원시 rate만 제공, summary/diff 계산은 클라이언트 책임)
2. ~~알림 반복형 여부~~ → **Phase 1은 1회성 통일** (USDT_PHASE1_DESIGN.md Alert Design 참조)
3. ~~크립토 그래프 범위~~ → **Phase 2로 연기** (Phase 1은 실시간 값 + 알림 중심)
4. ~~백엔드 데이터 모델 세부 설계~~ → **USDT_PHASE1_DESIGN.md에서 확정 완료** (source_rates / source_notification_settings / source_notification_logs / comparison_alerts draft)
