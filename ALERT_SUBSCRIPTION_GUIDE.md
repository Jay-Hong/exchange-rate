# 환율 알림 & 구독 관리 구현 가이드

> **목적**: 환율 알림 서비스와 크로스 플랫폼 구독 관리 구현을 위한 기술 선택지 비교
> **작성일**: 2025-12-03
> **상태**: ✅ **결정됨** (2025-12-05)

---

## 📋 목차

1. [결정된 아키텍처](#-결정된-아키텍처)
2. [구현 로드맵](#️-구현-로드맵)
3. [개인화 알림 동작 흐름](#-개인화-알림-동작-흐름)
4. [환율 알림 서비스 구현 방법](#1-환율-알림-서비스-구현-방법)
5. [크로스 플랫폼 구독 관리 방법](#2-크로스-플랫폼-구독-관리-방법)
6. [비용 비교 시나리오](#3-비용-비교-시나리오)
7. [구현 난이도 및 기간](#4-구현-난이도-및-기간)
8. [다음 단계](#5-다음-단계)

---

## 🎯 결정된 아키텍처

### 최종 선택: AWS RDS PostgreSQL + Firebase Auth + FCM

```text
┌─────────────────────────────────────────────────────┐
│ AWS Cloud (서울 리전)                                │
│                                                      │
│  ┌──────────────────────────────────────────────┐  │
│  │ VPC (같은 네트워크, 지연 1-3ms)                │  │
│  │                                               │  │
│  │  ┌─────────────────┐    ┌─────────────────┐ │  │
│  │  │ EC2 t3.small    │←→ │ RDS PostgreSQL  │ │  │
│  │  │                  │    │ db.t4g.micro    │ │  │
│  │  │ ├── FastAPI     │    │                  │ │  │
│  │  │ ├── 크롤러 (10개)│    │ ├── 환율 데이터  │ │  │
│  │  │ ├── WebSocket   │    │ ├── user_devices │ │  │
│  │  │ └── Redis       │    │ └── notifications│ │  │
│  │  └─────────────────┘    └─────────────────┘ │  │
│  └──────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
              ↓↑ (외부 서비스)
┌─────────────────────────────────────────────────────┐
│ Firebase (Google Cloud)                              │
│ ├── Firebase Auth (로그인) - 무료 50k MAU           │
│ └── FCM (푸시 알림) - 무료 무제한                    │
└─────────────────────────────────────────────────────┘
```

### 선택 이유

| 항목 | 선택 | 이유 |
|------|------|------|
| **DB** | AWS RDS PostgreSQL | 프리티어 12개월, 같은 VPC 지연 1-3ms |
| **Auth** | Firebase Auth | FCM과 자연스러운 통합, 무료 |
| **Push** | Firebase FCM | 무료 무제한, iOS/Android 통합 |
| **인스턴스** | db.t4g.micro | ARM64, 20% 저렴 (~$15/월) |

### 비용 요약

| Phase | 기간 | EC2 | RDS | Auth/FCM | 합계 |
|-------|------|-----|-----|----------|------|
| **1** | 서비스 시작 ~ 12개월 | $15/월 | $0 (프리티어) | $0 | **$15/월** |
| **2** | 12개월 이후 | $15/월 | ~$15/월 | $0 | **$30/월** |
| **3** | 1,000명+ | $30/월 (t3.medium) | ~$25/월 | $0 | **$55/월** |

---

## 🗺️ 구현 로드맵

### Phase 1: iOS MVP 개발

**목표**: 기본 앱 완성 (알림 기능 제외)

| 작업 | 설명 |
|------|------|
| UI/UX 구현 | 환율 비교 화면, 그래프, 은행 목록 |
| API 연동 | 현재 SQLite 기반 REST API 사용 |
| WebSocket | 실시간 환율 업데이트 |

**인프라**: 기존 t2.micro + SQLite (변경 없음)

---

### Phase 2: Firebase Auth + FCM 통합

**목표**: 사용자 인증 + 푸시 알림 기반 구축

| 작업 | 설명 |
|------|------|
| Firebase 프로젝트 생성 | FXi 전용 새 프로젝트 |
| APNs 설정 | 기존 .p8 키 재사용 (Team-wide) |
| iOS Firebase Auth SDK | 로그인/회원가입 구현 |
| iOS FCM SDK | Device Token 등록 |
| 서버 API 구현 | `/api/register-device`, `/api/notification-settings` |
| Alembic 설정 | DB 마이그레이션 스크립트 준비 |

**인프라**: t2.micro + SQLite (개발 계속)

---

### Phase 3: Revenue Cat 통합

**목표**: 구독 결제 시스템 구축

| 작업 | 설명 |
|------|------|
| Revenue Cat 계정 생성 | 앱 등록, 상품 설정 |
| iOS SDK 통합 | 구독 구매 플로우 |
| 유료 기능 설계 | 프리미엄 알림 (다중 조건, 빈도 등) |
| Firebase ↔ Revenue Cat 연동 | uid 기반 사용자 식별 |

**비용**: $2,500 MTR까지 무료

---

### Phase 4: RDS PostgreSQL 마이그레이션

**시점**: 출시 2주 전

| 작업 | 설명 |
|------|------|
| RDS 인스턴스 생성 | db.t4g.micro (프리티어, ARM64) |
| VPC 설정 | EC2와 같은 네트워크 |
| Alembic 마이그레이션 실행 | 스키마 생성 |
| 데이터 이전 | SQLite → PostgreSQL |
| EC2 업그레이드 | t2.micro → t3.small |
| 통합 테스트 | 전체 플로우 검증 |

**비용**: EC2 $15/월 + RDS $0 (프리티어 12개월)

---

### Phase 5: Android 개발

**목표**: 크로스 플랫폼 완성

| 작업 | 설명 |
|------|------|
| Kotlin + Jetpack Compose | UI 구현 |
| Firebase Auth/FCM SDK | iOS와 동일 플로우 |
| Revenue Cat SDK | 구독 동기화 자동 처리 |
| Google Play Billing | Revenue Cat이 처리 |

---

### 타임라인 요약

```text
[현재]
   └── t2.micro + SQLite (API 운영 중)

[Phase 1] iOS MVP
   └── 기존 인프라 유지

[Phase 2] Firebase Auth + FCM
   └── 서버 API 추가, Alembic 준비

[Phase 3] Revenue Cat
   └── 구독 시스템 완성

[Phase 4] RDS 마이그레이션 (출시 2주 전)
   └── t3.small + RDS PostgreSQL

[Phase 5] Android
   └── 크로스 플랫폼 완성
```

---

## 🔔 개인화 알림 동작 흐름

### 요구사항

```text
사용자: "하나은행 USD 환율이 1475원 이상이 되면 알려줘"
→ 환율 1478원 도달 시 푸시 알림 수신
```

### Step 1: 사용자 로그인 (앱 최초 실행)

```text
[사용자가 앱 설치]
    ↓
[Firebase Auth 로그인 (이메일/소셜)]
    ↓
[Firebase가 ID Token 발급] ← JWT 형태, user_id 포함
    ↓
[FCM Device Token 획득: "xyz789"]
    ↓
[서버에 등록: POST /api/register-device]
    Headers: { "Authorization": "Bearer <Firebase ID Token>" }  ← 필수!
    Body: { "device_token": "xyz789", "platform": "ios" }
    ↓
[서버에서 Firebase Admin SDK로 ID Token 검증]
    → 검증 성공 시 토큰에서 user_id 추출: "abc123"
    → 검증 실패 시 401 Unauthorized
    ↓
[RDS PostgreSQL에 저장]
    Table: user_devices
    | user_id | device_token | platform | created_at |
    |---------|--------------|----------|------------|
    | abc123  | xyz789       | ios      | 2025-12-05 |
```

**보안 핵심:**

- 클라이언트가 보낸 user_id를 **절대 신뢰하지 않음**
- 서버가 Firebase ID Token을 검증 후 **직접 user_id 추출**
- 이렇게 해야 스푸핑 공격 방지 가능

### Step 2: 알림 설정 저장

```text
[사용자가 앱에서 알림 설정]
    - 은행: 하나은행
    - 통화: USD
    - 조건: 1475원 이상
    ↓
[서버에 저장: POST /api/notification-settings]
    Headers: { "Authorization": "Bearer <Firebase ID Token>" }  ← 필수!
    Body: {
        "bank": "hana",
        "currency": "usd-krw",
        "condition": "greater_than",
        "threshold": 1475.0
    }
    ↓
[서버에서 ID Token 검증 → user_id 추출: "abc123"]
    ↓
[RDS PostgreSQL에 저장]
    Table: notification_settings
    | user_id | bank | currency | condition     | threshold |
    |---------|------|----------|---------------|-----------|
    | abc123  | hana | usd-krw  | greater_than  | 1475.0    |
```

**보안 핵심:** 클라이언트는 user_id를 보내지 않음. 서버가 ID Token에서 직접 추출.

### Step 3: 크롤러 → 조건 체크 → FCM 전송

```text
[크롤러가 하나은행 USD 환율 크롤링]
    Before: 1470원
    After:  1478원 ← 변경 감지!
    ↓
[CRUD에서 조건 체크]
    SELECT * FROM notification_settings
    WHERE bank='hana' AND currency='usd-krw'
    ↓
[조건 충족 사용자 찾기]
    user_id: abc123, threshold: 1475.0
    1478 >= 1475? → YES!
    ↓
[FCM API 호출]
    device_token: "xyz789"
    title: "환율 알림"
    body: "하나은행 USD 환율이 1478원이 되었습니다"
    ↓
[사용자 기기에 푸시 알림 도착]
    🔔 "환율 알림: 하나은행 USD 1478원"
```

### 필요한 DB 테이블 (RDS PostgreSQL)

```sql
-- 1. 사용자 기기 정보
CREATE TABLE user_devices (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,           -- Firebase Auth user_id
    device_token TEXT NOT NULL,       -- FCM Device Token
    platform TEXT NOT NULL,           -- 'ios' or 'android'
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, device_token)
);

-- 2. 알림 설정
CREATE TABLE notification_settings (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    bank TEXT NOT NULL,               -- 'hana', 'kb', etc.
    currency TEXT NOT NULL,           -- 'usd-krw', etc.
    condition TEXT NOT NULL,          -- 'greater_than', 'less_than'
    threshold REAL NOT NULL,          -- 1475.0
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 3. 알림 히스토리 (중복 방지)
CREATE TABLE notification_logs (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    bank TEXT NOT NULL,
    currency TEXT NOT NULL,
    rate REAL NOT NULL,
    sent_at TIMESTAMP DEFAULT NOW()
);

-- 인덱스
CREATE INDEX idx_user_devices_user ON user_devices(user_id);
CREATE INDEX idx_notification_settings_bank_currency
    ON notification_settings(bank, currency);
```

### 핵심 포인트

1. **로그인 필수**: 개인화 알림은 user_id로 설정을 저장해야 함
2. **Device Token**: FCM이 "어느 기기로 보낼지" 식별
3. **우리 DB에 저장**: user_devices, notification_settings는 RDS PostgreSQL에 저장
4. **Firebase Auth는 외부**: 로그인만 담당, user_id만 제공

---

## 1. 환율 알림 서비스 구현 방법

### 요구사항
```
사용자가 특정 은행의 환율이 N원 이상 변동하면 푸시 알림 수신
예: "하나은행 USD 환율이 1469원 → 1470원으로 변경되었습니다"
```

---

### Option A: Firebase + FCM

**아키텍처:**
```
크롤러 → SQLite/PostgreSQL → CRUD (변화 감지) → FCM → iOS/Android
```

**구현:**
```python
# CRUD 레이어에서 변화 감지 시
if last_record.rate != current_rate:
    await send_fcm_notification(bank, currency, old_rate, new_rate)
```

**장단점:**
| 항목 | 평가 |
|------|------|
| 푸시 알림 | ✅ FCM 완전 무료 (무제한) |
| 구현 난이도 | ✅ 간단 (Python SDK 제공) |
| iOS/Android | ✅ 통합 지원 (APNs 래핑) |
| 배터리 효율 | ✅ OS 레벨 최적화 |
| 인증 연동 | ✅ Firebase Auth 통합 쉬움 |
| 벤더 락인 | ⚠️ Google 의존 |
| DB | ⚠️ Firestore(NoSQL) 또는 별도 PostgreSQL 필요 |

---

### Option B: Supabase + FCM

**아키텍처 B-1: FastAPI 서버 유지**
```
크롤러 → PostgreSQL(Supabase) → CRUD → FCM → iOS/Android
```

**아키텍처 B-2: Edge Functions (서버리스)**
```
크롤러 → PostgreSQL(Supabase) → DB Webhook → Edge Function → FCM
```

**장단점:**
| 항목 | 평가 |
|------|------|
| 푸시 알림 | ✅ FCM 무료 (Firebase와 동일) |
| DB | ✅ PostgreSQL 기본 제공 (현재 프로젝트 Phase 2 계획과 일치) |
| 오픈소스 | ✅ 자체 호스팅 가능 (벤더 락인 없음) |
| Edge Functions | ✅ 무료 500k 요청/월 (서버리스 알림 가능) |
| 구현 난이도 | ⚠️ Edge Functions는 Deno/TypeScript (Python 아님) |
| 생태계 | ⚠️ Firebase보다 작음 (성장 중) |

---

### Option C: 자체 구현 (FCM만 사용)

**아키텍처:**
```
크롤러 → PostgreSQL → CRUD → 자체 알림 로직 → FCM
```

**특징:**
- Firebase/Supabase 없이 FCM만 직접 사용
- 모든 로직을 FastAPI에서 처리

**장단점:**
| 항목 | 평가 |
|------|------|
| 완전 제어 | ✅ 모든 로직 커스터마이징 가능 |
| 비용 | ✅ FCM 무료 + PostgreSQL만 |
| 구현 난이도 | ⚠️ 중간 (사용자 관리, 토큰 관리 직접 구현) |
| 인증 | ⚠️ 별도 인증 시스템 필요 |

---

### 환율 알림 방법 비교표

| 항목 | Firebase + FCM | Supabase + FCM | 자체 구현 |
|------|---------------|---------------|----------|
| **푸시 알림** | FCM (무료) | FCM (무료) | FCM (무료) |
| **DB** | Firestore 또는 별도 | PostgreSQL 포함 | PostgreSQL (별도) |
| **인증** | Firebase Auth | Supabase Auth | 직접 구현 |
| **서버리스 옵션** | Cloud Functions | Edge Functions | ❌ |
| **오픈소스** | ❌ | ✅ | ✅ |
| **벤더 락인** | Google | Supabase (탈출 가능) | 없음 |
| **구현 시간** | 1주 | 1-2주 | 2-3주 |
| **비용 (500명)** | $0 | $0 | $0 |
| **비용 (1000명)** | $15/월 | $25/월 | $25/월 |

---

## 2. 크로스 플랫폼 구독 관리 방법

### 요구사항
```
iOS에서 구독 → Android에서 로그인 → 자동 동기화 (다시 결제 안 함)
```

### 핵심 개념
```
구독 관리 = 사용자 인증 + 영수증 검증 + 크로스 플랫폼 동기화

1. 사용자 인증: Firebase Auth 또는 Supabase Auth
2. 영수증 검증: Revenue Cat 또는 자체 구현
3. 동기화: 서버 기준으로 user_id로 구독 상태 관리
```

---

### Option A: Firebase Auth + Revenue Cat ⭐

**아키텍처:**
```
[iOS/Android 앱]
    ↓ Firebase Auth 로그인 → user_id 획득
    ↓ Revenue Cat에 user_id 연결
[Revenue Cat]
    ↓ 영수증 검증 (iOS/Android)
    ↓ 크로스 플랫폼 자동 동기화 ✅
[서버] (선택적) Webhook으로 구독 상태 저장
```

**구현 복잡도:**
```swift
// iOS
FirebaseAuth.auth().signIn(...) { result in
    let userId = result?.user.uid
    Purchases.shared.logIn(userId) { ... }  // Revenue Cat 연결
}

// Android (같은 user_id로 자동 동기화!)
FirebaseAuth.getInstance().signIn(...) { result ->
    val userId = result.user?.uid
    Purchases.sharedInstance.logIn(userId) { ... }
}
```

**장단점:**
| 항목 | 평가 |
|------|------|
| 구현 난이도 | ✅ 매우 쉬움 (3-5줄 코드) |
| 크로스 플랫폼 | ✅ 자동 동기화 |
| 영수증 검증 | ✅ 자동 (iOS + Android) |
| 환불/취소 | ✅ Webhook 자동 처리 |
| 분석 | ✅ MRR, Churn, LTV 대시보드 |
| 비용 | ⚠️ 매출의 1% (월 $2,500 이하 무료) |
| 생태계 | ✅ Notion, Duolingo 등 사용 |

---

### Option B: Supabase Auth + Revenue Cat ⭐

**아키텍처:**
```
[iOS/Android 앱]
    ↓ Supabase Auth 로그인 → user_id 획득
    ↓ Revenue Cat에 user_id 연결
[Revenue Cat]
    ↓ 영수증 검증 + 크로스 플랫폼 동기화 ✅
```

**Firebase와의 차이점:**
- Auth만 Supabase로 변경
- Revenue Cat 사용법은 동일

**장단점:**
| 항목 | 평가 |
|------|------|
| 구현 난이도 | ✅ Firebase와 동일 |
| 크로스 플랫폼 | ✅ 자동 동기화 |
| 비용 | ✅ Firebase와 동일 |
| DB | ✅ PostgreSQL 포함 |
| 오픈소스 | ✅ 자체 호스팅 가능 |

---

### Option C: 자체 구현 (영수증 검증)

**아키텍처:**
```
[iOS/Android 앱]
    ↓ 구독 완료 → 영수증 전송
[서버]
    ↓ App Store API 검증 (iOS)
    ↓ Google Play API 검증 (Android)
    ↓ DB 저장 (user_id 기준)
[Cron Job]
    ↓ 매일 구독 갱신 상태 확인
```

**구현 복잡도:**
- iOS 영수증 검증: App Store API (복잡)
- Android 영수증 검증: Google Play API (매우 복잡, OAuth 필요)
- 자동 갱신 확인: Cron Job 필요
- 환불/취소 처리: 수동 또는 Webhook 구현
- 엣지 케이스: Offer Code, 가족 공유, Grace Period 등 (10-20개)

**장단점:**
| 항목 | 평가 |
|------|------|
| 비용 | ✅ Revenue Cat 매출 1% 절약 |
| 완전 제어 | ✅ 모든 로직 커스터마이징 |
| 구현 난이도 | ❌ 매우 높음 (3-4주) |
| 유지보수 | ❌ Apple/Google API 변경 대응 필요 |
| 실질 비용 | ⚠️ 개발 + 유지보수 고려 시 Revenue Cat보다 비쌈 |

---

### 구독 관리 방법 비교표

| 항목 | Firebase + Revenue Cat | Supabase + Revenue Cat | 자체 구현 |
|------|----------------------|----------------------|----------|
| **인증** | Firebase Auth | Supabase Auth | 직접 구현 |
| **영수증 검증** | Revenue Cat | Revenue Cat | 직접 구현 |
| **크로스 플랫폼** | ✅ 자동 | ✅ 자동 | ⚠️ 수동 |
| **환불/취소** | ✅ 자동 | ✅ 자동 | ❌ 수동 |
| **구현 시간** | 1주 | 1주 | 3-4주 |
| **비용 (월 매출 $1k)** | $0 | $0 | $0 |
| **비용 (월 매출 $10k)** | $100 | $100 | $500 (유지보수) |
| **Apple 정책** | ✅ | ✅ | ✅ |
| **추천도** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⚠️ |

---

## 3. 비용 비교 시나리오

> **참고**: 아래는 검토 당시 비교 자료입니다. 최종 선택은 **AWS RDS PostgreSQL + Firebase Auth + FCM**입니다.
> 선택 이유: RDS 프리티어 12개월 무료, 같은 VPC 네트워크 지연 최소화 (1-3ms), 기존 AWS 인프라 활용, FCM과 자연스러운 통합

### 시나리오 1: 초기 (사용자 500명, 월 매출 $1,000)

| 항목 | Firebase + Revenue Cat | Supabase + Revenue Cat | 자체 구현 |
|------|----------------------|----------------------|----------|
| 인증 | $0 (무료) | $0 (무료) | $0 |
| DB | $0 (Firestore 무료) | $0 (PostgreSQL 500MB) | $0 (SQLite) |
| 푸시 알림 | $0 (FCM) | $0 (FCM) | $0 (FCM) |
| 구독 관리 | $0 (매출 $2.5k 이하) | $0 (매출 $2.5k 이하) | $0 |
| **월 합계** | **$0** | **$0** | **$0** |
| **개발 시간** | 2주 | 2주 | 4주 |

**결론:** 초기에는 비용 동일, **Supabase가 PostgreSQL 제공으로 약간 우세**

---

### 시나리오 2: 성장 (사용자 1,000명, 월 매출 $10,000)

| 항목 | Firebase + Revenue Cat | Supabase + Revenue Cat | 자체 구현 |
|------|----------------------|----------------------|----------|
| 인증 | $0 | $0 | $0 |
| DB | $15 (Firestore) | $25 (PostgreSQL Pro 8GB) | $25 (RDS t3.micro) |
| 푸시 알림 | $0 (FCM) | $0 (FCM) | $0 (FCM) |
| 구독 관리 | $100 (매출의 1%) | $100 (매출의 1%) | $0 |
| Edge Functions | $10 (Cloud Functions) | $0 (무료 500k) | - |
| **월 합계** | **$125** | **$125** | **$25** |
| **유지보수** | $0 | $0 | $500 (버그 수정, API 대응) |
| **실질 비용** | **$125** | **$125** | **$525** |

**결론:** Firebase와 Supabase 비용 동일, **자체 구현은 유지보수 고려 시 더 비쌈**

---

### 시나리오 3: 대규모 (사용자 5,000명, 월 매출 $100,000)

| 항목 | Firebase + Revenue Cat | Supabase + Revenue Cat | Supabase 자체 호스팅 |
|------|----------------------|----------------------|-------------------|
| 인증 | $55 (50k MAU 초과) | $25 | $0 (자체) |
| DB | $100 (Firestore) | $100 | $30 (RDS t3.small) |
| 푸시 알림 | $0 (FCM) | $0 (FCM) | $0 (FCM) |
| 구독 관리 | $1,000 (매출의 1%) | $1,000 | $1,000 |
| 서버 | - | - | $35 (EC2 t3.medium) |
| **월 합계** | **$1,155** | **$1,125** | **$1,065** |

**결론:** **Supabase + Revenue Cat이 가장 저렴** ($1,125/월)

---

## 4. 구현 난이도 및 기간

### 환율 알림 서비스

| 방법 | 난이도 | 개발 기간 | 유지보수 |
|------|-------|----------|---------|
| Firebase + FCM | ⭐⭐ (쉬움) | 1주 | 낮음 |
| Supabase + FCM (FastAPI) | ⭐⭐ (쉬움) | 1-2주 | 낮음 |
| Supabase + Edge Functions | ⭐⭐⭐ (중간) | 2주 | 낮음 |
| 자체 구현 | ⭐⭐ (쉬움) | 2-3주 | 중간 |

---

### 크로스 플랫폼 구독 관리

| 방법 | 난이도 | 개발 기간 | 유지보수 |
|------|-------|----------|---------|
| Firebase + Revenue Cat | ⭐ (매우 쉬움) | 1주 | 매우 낮음 |
| Supabase + Revenue Cat | ⭐ (매우 쉬움) | 1주 | 매우 낮음 |
| 자체 구현 | ⭐⭐⭐⭐⭐ (매우 어려움) | 3-4주 | 매우 높음 |

---

## 5. 다음 단계

### ✅ 결정된 항목

#### A. 인증 & DB 플랫폼

- [x] **AWS RDS PostgreSQL + Firebase Auth** 선택
  - DB: AWS RDS PostgreSQL 프리티어 (db.t4g.micro)
  - Auth: Firebase Auth (무료 50k MAU)
  - 이유: 같은 VPC 지연 1-3ms, 프리티어 12개월, FCM 통합 용이

#### B. 구독 관리 방식

- [x] **Firebase Auth + Revenue Cat** 선택
  - 이유: Firebase uid와 자연스러운 통합, 크로스 플랫폼 자동 동기화
  - 비용: 월 매출 $2,500 이하 무료, 이후 매출의 1%

#### C. 환율 알림 아키텍처

- [x] **FastAPI 서버 유지** 선택
  - Python 코드베이스 유지
  - CRUD 레이어에서 변화 감지 → FCM 전송

---

### 📋 구현 체크리스트

#### Phase 1: DB 마이그레이션 (서비스 시작 시)

- [ ] AWS RDS PostgreSQL 인스턴스 생성 (db.t4g.micro)
- [ ] SQLite → PostgreSQL 데이터 마이그레이션
- [ ] DATABASE_URL 환경변수 변경
- [ ] SQLAlchemy 연결 테스트

#### Phase 2: 인증 & 알림 시스템

- [ ] Firebase 프로젝트 생성
- [ ] Firebase Auth 설정 (이메일/소셜 로그인)
- [ ] FCM 설정 및 서버 키 발급
- [ ] user_devices, notification_settings 테이블 생성
- [ ] FastAPI 알림 API 구현 (/api/register-device, /api/notification-settings)
- [ ] CRUD 변화 감지 → FCM 전송 로직 구현

#### Phase 3: 모바일 앱 통합

- [ ] iOS 앱에 Firebase Auth SDK 통합
- [ ] iOS 앱에 FCM SDK 통합
- [ ] Android 앱에 Firebase Auth SDK 통합
- [ ] Android 앱에 FCM SDK 통합

#### Phase 4: 구독 관리 (Revenue Cat)

- [x] Revenue Cat 도입 결정
- [ ] Revenue Cat 계정 생성 및 앱 등록
- [ ] iOS/Android 앱에 Revenue Cat SDK 통합
- [ ] 유료 기능 설계 (프리미엄 알림 등)
- [ ] 크로스 플랫폼 구독 동기화 테스트

---

### 💡 비용 전략 (확정)

```text
Phase 1 (서비스 시작 ~ 12개월):
├── EC2 t3.small: $15/월
├── RDS db.t4g.micro: $0 (프리티어)
├── Firebase Auth/FCM: $0
└── 합계: $15/월

Phase 2 (12개월 후):
├── EC2 t3.small: $15/월
├── RDS db.t4g.micro: ~$15/월
├── Firebase Auth/FCM: $0
└── 합계: $30/월

Phase 3 (1,000명+):
├── EC2 t3.medium: $30/월
├── RDS db.t4g.small: ~$25/월
├── Firebase Auth/FCM: $0
└── 합계: $55/월
```

---

## 🔧 기기 토큰 정리 전략 (구현 예정)

> **목적**: FCM device_token 관리 및 정리 전략 (구현 시점에 최종 결정)
> **상태**: 📋 검토 완료, 구현 대기

### 문제 상황

FCM device_token은 다음 상황에서 무효화됨:

1. **앱 삭제/재설치**: 새 토큰 발급, 기존 토큰 무효
2. **기기 변경**: 새 기기에서 새 토큰 발급
3. **토큰 만료**: FCM이 주기적으로 토큰 갱신 (드물게 발생)
4. **OS 업데이트**: 일부 메이저 업데이트 시 토큰 변경

무효 토큰으로 FCM 전송 시 `InvalidRegistration` 또는 `NotRegistered` 에러 발생.

### 전략 옵션

#### Option A: 전송 시점 정리 (Lazy Cleanup) ⭐ 권장

```python
# FCM 전송 후 응답 확인
response = messaging.send(message)
# 또는 send_multicast 사용 시
response = messaging.send_multicast(message)

# 실패한 토큰 처리
for idx, result in enumerate(response.responses):
    if not result.success:
        error_code = result.exception.code
        if error_code in ['UNREGISTERED', 'INVALID_ARGUMENT']:
            # 무효 토큰 삭제
            delete_device_token(tokens[idx])
```

**장점:**

- 실제 문제 발생 시에만 정리 (불필요한 API 호출 없음)
- 구현 간단
- FCM 응답으로 정확한 상태 파악

**단점:**

- 첫 번째 실패 시까지 무효 토큰 유지
- 알림 전송 실패 1회 발생

#### Option B: 주기적 검증 (Scheduled Cleanup)

```python
# 매일 새벽 3시 실행
@scheduler.scheduled_job('cron', hour=3)
async def cleanup_stale_tokens():
    # 30일 이상 미사용 토큰 삭제
    cutoff = datetime.now() - timedelta(days=30)
    await db.execute(
        "DELETE FROM user_devices WHERE last_used < ?",
        (cutoff,)
    )
```

**장점:**

- 미사용 토큰 자동 정리
- DB 크기 관리

**단점:**

- `last_used` 컬럼 추가 필요
- 토큰 갱신 시점 추적 필요

#### Option C: 하이브리드 (권장 조합)

```text
1. FCM 전송 실패 시 즉시 삭제 (Option A)
2. 30일 이상 미사용 토큰 주기적 삭제 (Option B)
3. 앱 시작 시 토큰 갱신 확인 (클라이언트)
```

### 멀티 디바이스 정책

한 사용자가 여러 기기 사용 시 정책:

| 정책 | 설명 | 구현 |
|------|------|------|
| **모든 기기** | 모든 등록된 기기에 알림 전송 | `SELECT device_token FROM user_devices WHERE user_id = ?` |
| **최신 기기만** | 마지막 활성 기기에만 전송 | `ORDER BY last_used DESC LIMIT 1` |
| **기기 수 제한** | 최대 N개 기기 허용 | 초과 시 가장 오래된 토큰 삭제 |

**권장**: 모든 기기에 전송 (사용자 경험 우선)

### 중복 토큰 처리

```sql
-- user_devices 테이블의 UNIQUE 제약
UNIQUE(user_id, device_token)

-- 같은 토큰이 다른 user_id로 등록 시도 시
-- (기기 소유자 변경, 로그아웃 후 다른 계정 로그인)
ON CONFLICT (device_token) DO UPDATE SET
    user_id = EXCLUDED.user_id,
    updated_at = NOW()
```

### 구현 체크리스트

- [ ] FCM 전송 응답 핸들링 (무효 토큰 삭제)
- [ ] `last_used` 컬럼 추가 (선택)
- [ ] 주기적 정리 Job 추가 (선택)
- [ ] 멀티 디바이스 정책 결정
- [ ] 중복 토큰 처리 로직 구현

### 관련 문서

- [ADR-016](DECISIONS.md#adr-016-인증알림-인프라---aws-rds--firebase-auth--fcm): 인증/알림 인프라 결정
- [FCM Error Codes](https://firebase.google.com/docs/cloud-messaging/send-message#admin_sdk_error_reference): 에러 코드 참고

---

## 참고 자료

### Firebase 관련
- Firebase Auth: https://firebase.google.com/docs/auth
- FCM: https://firebase.google.com/docs/cloud-messaging
- Pricing: https://firebase.google.com/pricing

### Supabase 관련
- Supabase Auth: https://supabase.com/docs/guides/auth
- PostgreSQL: https://supabase.com/docs/guides/database
- Edge Functions: https://supabase.com/docs/guides/functions
- Pricing: https://supabase.com/pricing

### Revenue Cat 관련
- 공식 문서: https://www.revenuecat.com/docs
- Pricing: https://www.revenuecat.com/pricing
- Firebase 통합: https://www.revenuecat.com/docs/firebase-integration
- Supabase 통합: Custom User ID 사용

### 기타
- Apple In-App Purchase: https://developer.apple.com/in-app-purchase/
- Google Play Billing: https://developer.android.com/google/play/billing

---

**마지막 업데이트**: 2025-12-05
**결정 완료**: AWS RDS PostgreSQL + Firebase Auth + FCM
