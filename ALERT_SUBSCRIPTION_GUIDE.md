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
| APNs 키 생성 | `.p8` 키 생성 (Sandbox & Production, Team Scoped) |
| Firebase에 iOS 앱 등록 | Bundle ID 등록, `GoogleService-Info.plist` 다운로드 |
| APNs 키 Firebase 등록 | 개발/프로덕션 APNs 인증 키 업로드 |
| Google 로그인 설정 | 프로젝트 공개용 이름, 지원 이메일 설정 |
| Apple 로그인 설정 | App ID, 서비스 ID, Sign in with Apple 키 생성 |
| Firebase OAuth 설정 | 서비스 ID, 팀 ID, 비공개 키 등록 (Android용) |
| iOS Firebase Auth SDK | 로그인/회원가입 구현 |
| iOS FCM SDK | Device Token 등록 |
| 서버 API 구현 | `/api/register-device`, `/api/notification-settings` |
| Alembic 설정 | DB 마이그레이션 스크립트 준비 |

**인프라**: t2.micro + SQLite (개발 계속)

#### 🔑 생성되는 키/파일 목록

| 파일 | 용도 | 비고 |
|------|------|------|
| `AuthKey_XXXXXXXX.p8` (APNs) | 푸시 알림 | Firebase Cloud Messaging에 등록 |
| `AuthKey_YYYYYYYY.p8` (Sign in with Apple) | Apple 로그인 | Firebase OAuth에 등록 (Android용) |
| `GoogleService-Info.plist` | iOS Firebase 설정 | Google 로그인 활성화 후 재다운로드 필요 |

#### 📝 Apple Developer 설정 상세

##### 1. APNs 키 생성 (Keys)

- Key Name: 범용적 이름 권장 (예: `APNs Auth Key`)
- Environment: `Sandbox & Production` (개발+프로덕션 모두 지원)
- Key Restriction: `Team Scoped (All Topics)` (모든 앱에서 재사용 가능)

##### 2. App ID 설정 (Identifiers > App IDs)

- Bundle ID와 동일하게 등록
- Capabilities: `Push Notifications`, `Sign in with Apple` 활성화
- Sign in with Apple: `Enable as a primary App ID` 선택

##### 3. 서비스 ID 생성 (Identifiers > Services IDs) - Android Apple 로그인용

- Identifier: `{Bundle ID}.signin` 형식 권장
- Sign in with Apple 활성화 후 Configure:
  - Primary App ID: 위에서 만든 App ID 선택
  - Domains: `{project-id}.firebaseapp.com`
  - Return URLs: `https://{project-id}.firebaseapp.com/__/auth/handler`

##### 4. Sign in with Apple 키 생성 (Keys) - APNs 키와 별도

- Key Name: `Sign in with Apple Key`
- Sign in with Apple 체크 → Configure → Primary App ID 선택

#### 🔥 Firebase Console 설정 상세

##### 1. iOS 앱 등록

- Bundle ID 입력 → `GoogleService-Info.plist` 다운로드

##### 2. APNs 키 업로드 (프로젝트 설정 > Cloud Messaging)

- 개발/프로덕션 APNs 인증 키에 동일한 `.p8` 파일 업로드
- Key ID, Team ID 입력

##### 3. Google 로그인 활성화 (Authentication > Sign-in method)

- 프로젝트 공개용 이름: 앱 이름 (예: `환율아이`)
- 프로젝트 지원 이메일: 운영자 이메일
- ⚠️ 활성화 후 `GoogleService-Info.plist` **재다운로드 필요**

##### 4. Apple 로그인 활성화 (Authentication > Sign-in method)

- iOS 네이티브: 단순 활성화만 하면 됨
- Android용 OAuth 설정 (크로스 플랫폼 구독 동기화 필요 시):
  - 서비스 ID: `{Bundle ID}.signin`
  - Apple 팀 ID: Apple Developer 계정의 Team ID
  - 키 ID: Sign in with Apple 키의 Key ID
  - 비공개 키: `.p8` 파일 내용 붙여넣기

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

**시점**: 첫 외부 사용자의 FCM/알림 데이터가 DB에 기록되기 전

> ⚠️ **중요**: 외부 베타 시작 전에 반드시 RDS로 전환해야 합니다.
> 내부 테스트 데이터는 드롭하고 새로 시작합니다 (아래 절차 참고).

| 작업 | 설명 |
|------|------|
| RDS 인스턴스 생성 | db.t4g.micro (프리티어, ARM64) |
| VPC 설정 | EC2와 같은 네트워크 |
| Alembic 마이그레이션 실행 | 빈 스키마 생성 (SQLite 이관 안 함) |
| EC2 업그레이드 | t2.micro → t3.small |
| 통합 리허설 | Alembic 업/다운, E2E 테스트 ([리허설 절차](#-통합-리허설-체크리스트) 참고) |
| 내부 테스터 안내 | 앱 재설치 공지 발송 |

**비용**: EC2 $15/월 + RDS $0 (프리티어 12개월)

#### 🔄 전환 전략: 데이터 드롭 방식

**기본 전략 (권장)**: Phase 2-3 개발 중 SQLite 사용, Phase 4에서 RDS로 전환 시 내부 테스트 데이터 드롭

```text
왜 데이터를 드롭하는가?
├── 내부 테스터 데이터 (10-20명): 복구 비용보다 재설치가 간단
├── 프리티어 12개월 최대 활용: 실제 서비스 시작 시점부터 시작
├── 깔끔한 시작: 테스트 중 발생한 더미 데이터 제거
└── Firebase Auth uid는 유지됨 (재로그인 불필요)
```

**대안 (엄격)**: Phase 2 시작 전 RDS 전환 → 모든 개발을 PostgreSQL에서 진행
- 장점: 환경 차이 없음
- 단점: 프리티어 조기 소모, 로컬 개발 복잡

#### 📋 데이터 드롭 절차

**1. 서버 측 작업**

```bash
# 1. EC2 업그레이드 (t2.micro → t3.small)
# AWS 콘솔에서 인스턴스 유형 변경

# 2. RDS PostgreSQL 생성 (새 DB, SQLite 이관 안 함)
# AWS 콘솔에서 db.t4g.micro 생성

# 3. 환경변수 변경
# .env
DATABASE_URL=postgresql://user:pass@rds-endpoint:5432/fxi

# 4. Alembic 마이그레이션 (빈 스키마 생성)
alembic upgrade head

# 5. Docker 재배포
docker compose up -d --build
```

**2. 내부 테스터 공지 (템플릿)**

```text
[FXi 베타 테스트 안내]

외부 베타 전환을 위해 서버가 업그레이드됩니다.

📱 필요한 조치:
1. 기존 FXi 앱 삭제
2. 테스트플라이트에서 최신 버전 재설치
3. 다시 로그인 (Firebase 계정 유지됨)

⚠️ 기존 알림 설정은 초기화됩니다.
새 버전에서 다시 설정해 주세요.

적용 일시: YYYY-MM-DD HH:MM
문의: [연락처]
```

**3. 영향 범위**

| 항목 | 드롭 여부 | 복구 방법 |
|------|----------|----------|
| Firebase Auth uid | ❌ 유지 | Firebase 서버에 저장 |
| FCM device_token | ✅ 드롭 | 앱 재설치 시 자동 재발급 |
| notification_settings | ✅ 드롭 | 사용자가 다시 설정 |
| notification_logs | ✅ 드롭 | 히스토리 초기화 |
| 환율 데이터 | ⚠️ 선택 | 필요 시 SQLite에서 이관 |

---

### Phase 5: 베타 + iOS 출시

**목표**: 테스트플라이트 베타 → 앱스토어 출시

| 작업 | 설명 |
|------|------|
| 테스트플라이트 외부 베타 | 외부 테스터 피드백 수집 (1-2주) |
| 버그 수정 및 안정화 | 크래시 프리 세션 > 99% 목표 |
| 앱스토어 심사 제출 | 스크린샷, 설명, 개인정보처리방침 |
| 로그 기반 알람 구현 | 알림 실패율 모니터링 (선택) |

**완료 게이트**:
- 앱스토어 심사 승인
- 크래시 프리 세션 > 99%
- 주요 버그 0건

---

### Phase 6: Android 개발

**목표**: 크로스 플랫폼 완성

| 작업 | 설명 |
|------|------|
| Kotlin + Jetpack Compose | UI 구현 |
| Firebase Auth/FCM SDK | iOS와 동일 플로우 |
| Revenue Cat SDK | 구독 동기화 자동 처리 |
| Google Play Billing | Revenue Cat이 처리 |

**완료 게이트**:
- Play 스토어 심사 승인
- iOS ↔ Android 크로스 플랫폼 동기화 확인

---

### 타임라인 요약

```text
[현재] Phase 1 완료
   └── t2.micro + SQLite, iOS MVP 완성

[Phase 2] Firebase Auth + FCM
   └── 서버 API 추가, 알림 기능, Alembic 준비

[Phase 3] Revenue Cat
   └── 구독 시스템 완성

[Phase 4] RDS 마이그레이션
   └── 첫 외부 사용자 기록 전 전환

[Phase 5] 베타 + iOS 출시
   └── 테스트플라이트 → 앱스토어

[Phase 6] Android
   └── 크로스 플랫폼 완성
```

---

### 📊 Phase 매핑 표

> **참고**: 로드맵과 체크리스트의 Phase 번호가 1:1로 매핑됩니다.

| Phase | 목표 | 주요 작업 | 완료 게이트 | 인프라 |
|-------|------|----------|-------------|--------|
| 1 | iOS MVP ✅ | UI, API 연동, WebSocket | 앱 기본 기능 동작 | t2.micro + SQLite |
| 2 | Firebase Auth + FCM | 로그인, 푸시 알림, 알림 API | 푸시 알림 E2E 성공, 구조화 로그 준비 | t2.micro + SQLite |
| 3 | Revenue Cat | 구독 결제, 유료 기능 | 구매 → 권한 반영 ≤ 1분 | t2.micro + SQLite |
| 4 | RDS 마이그레이션 | DB 전환, 인프라 업그레이드 | Alembic 성공, 통합 E2E 통과 | t3.small + RDS |
| 5 | 베타 + iOS 출시 | 테스트플라이트, QA, 심사 | 앱스토어 승인, 크래시 프리 > 99% | t3.small + RDS |
| 6 | Android | Kotlin UI, SDK 통합 | Play 스토어 승인 | t3.small + RDS |

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

> **참고**: Phase 번호는 위 [Phase 매핑 표](#-phase-매핑-표)와 1:1 대응됩니다.

#### Phase 1: iOS MVP ✅ 완료

- [x] UI/UX 구현 (환율 비교, 그래프, 은행 목록)
- [x] REST API 연동
- [x] WebSocket 실시간 업데이트

#### Phase 2: Firebase Auth + FCM

**Apple Developer 설정:**

- [x] APNs 키 생성 (.p8, Sandbox & Production, Team Scoped)
- [x] App ID 등록 (Push Notifications, Sign in with Apple 활성화)
- [x] 서비스 ID 생성 (Android Apple 로그인용)
- [x] Sign in with Apple 키 생성 (.p8, APNs 키와 별도)

**Firebase Console 설정:**

- [x] Firebase 프로젝트 생성 (FXi)
- [x] iOS 앱 등록 + GoogleService-Info.plist 다운로드
- [x] APNs 키 업로드 (개발/프로덕션)
- [x] Google 로그인 활성화 + GoogleService-Info.plist 재다운로드
- [x] Apple 로그인 활성화 + OAuth 설정 (Android용)

**iOS 앱 작업:**

- [x] Xcode에 Firebase SDK 추가 (SPM)
- [x] GoogleService-Info.plist 프로젝트에 추가
- [x] Capabilities 설정 (Push Notifications, Sign in with Apple)
- [x] Firebase Auth SDK 통합 (Google + Apple 로그인) - `AuthService.swift`
- [x] FCM SDK 통합 (Device Token 등록) - `PushNotificationService.swift`
- [ ] 로그인 UI 구현

**서버 작업:**

- [x] Firebase Admin SDK 설치 (`firebase-admin>=6.5.0`)
- [x] user_devices, notification_settings, notification_logs 테이블 생성
- [x] /api/register-device API 구현 (토큰 소유권 이전 로직 포함)
- [x] /api/notification-settings API 구현 (중복 방지, triggered 재설정)
- [x] CRUD 변화 감지 → FCM 전송 로직 구현
- [ ] 알림 품질 게이트 통과 ([상세](#-알림-품질-게이트))
- [ ] Alembic 마이그레이션 스크립트 준비 (PostgreSQL용)

#### Phase 3: Revenue Cat

- [x] Revenue Cat 도입 결정
- [ ] Revenue Cat 계정 생성 및 앱 등록
- [ ] iOS 앱에 Revenue Cat SDK 통합
- [ ] 유료 기능 설계 (프리미엄 알림 등)
- [ ] Firebase uid ↔ Revenue Cat 연동

#### Phase 4: RDS 마이그레이션

- [ ] 로컬 스테이징 리허설 통과 ([체크리스트](#-통합-리허설-체크리스트))
- [ ] EC2 t2.micro → t3.small 업그레이드
- [ ] RDS PostgreSQL 인스턴스 생성 (db.t4g.micro)
- [ ] VPC 설정 (EC2와 같은 네트워크)
- [ ] Alembic 마이그레이션 실행 (빈 스키마)
- [ ] DATABASE_URL 환경변수 변경
- [ ] 내부 테스터 앱 재설치 공지 발송 ([템플릿](#-데이터-드롭-절차))
- [ ] 통합 E2E 테스트 통과

#### Phase 5: 베타 + iOS 출시

- [ ] 테스트플라이트 외부 베타 배포
- [ ] 베타 피드백 수집 및 버그 수정
- [ ] 크래시 프리 세션 > 99% 확인
- [ ] 앱스토어 심사 제출
- [ ] 앱스토어 승인
- [ ] (선택) 로그 기반 알람 구현

#### Phase 6: Android 개발

- [ ] Kotlin + Jetpack Compose UI 구현
- [ ] Firebase Auth SDK 통합
- [ ] FCM SDK 통합
- [ ] Revenue Cat SDK 통합
- [ ] 크로스 플랫폼 구독 동기화 테스트
- [ ] Play 스토어 심사 제출 및 승인

---

### 💡 비용 전략 (확정)

> **참고**: 아래 "비용 단계"는 구현 Phase와 다릅니다. 서비스 운영 기간 기준입니다.

```text
비용 단계 1 (서비스 시작 ~ 12개월):
├── EC2 t3.small: $15/월
├── RDS db.t4g.micro: $0 (프리티어)
├── Firebase Auth/FCM: $0
└── 합계: $15/월

비용 단계 2 (12개월 후):
├── EC2 t3.small: $15/월
├── RDS db.t4g.micro: ~$15/월
├── Firebase Auth/FCM: $0
└── 합계: $30/월

비용 단계 3 (1,000명+):
├── EC2 t3.medium: $30/월
├── RDS db.t4g.small: ~$25/월
├── Firebase Auth/FCM: $0
└── 합계: $55/월
```

---

## 🧪 통합 리허설 체크리스트

> **목적**: RDS 전환 전 프로덕션 환경과 동일한 조건에서 검증
> **시점**: Phase 4 (RDS 마이그레이션) 시작 전

### 로컬 스테이징 환경 구성

프로덕션 이미지/환경변수와 동일하게 로컬에서 테스트합니다.

> **환경변수 파일**: `.env.staging`은 프로덕션 `.env`와 동일한 키 세트를 사용하고, 값만 스테이징용으로 변경합니다.

```yaml
# docker-compose.staging.yml
version: '3.8'
services:
  app:
    build: .
    platform: linux/amd64  # M1 Mac에서도 x86 에뮬레이션
    environment:
      - ENV=staging
      - DATABASE_URL=postgresql://postgres:dev@postgres:5432/fxi
      - REDIS_URL=redis://redis:6379
      # 프로덕션 .env와 동일한 키 사용
    depends_on:
      - postgres
      - redis

  postgres:
    image: postgres:15
    environment:
      - POSTGRES_DB=fxi
      - POSTGRES_PASSWORD=dev
    ports:
      - "5432:5432"

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
```

### 리허설 체크리스트

```bash
# 1. 스테이징 환경 실행
docker-compose -f docker-compose.staging.yml up -d --build

# 2. Alembic 마이그레이션 테스트
alembic upgrade head
alembic downgrade -1  # 롤백 테스트
alembic upgrade head  # 다시 업그레이드
```

| 항목 | 테스트 내용 | 통과 기준 |
|------|------------|----------|
| ✅ Alembic 업그레이드 | `alembic upgrade head` | 에러 없이 완료 |
| ✅ Alembic 롤백 | `alembic downgrade -1` | 에러 없이 롤백 |
| ✅ 크롤러 동시 INSERT | 10개 크롤러 동시 실행 | UNIQUE 충돌 없음 |
| ✅ WebSocket 브로드캐스트 | 환율 데이터 실시간 전송 | 클라이언트 수신 확인 |
| ✅ 알림 E2E | 조건 충족 → FCM 전송 | 푸시 알림 수신 (실 토큰 1회) |
| ✅ Redis 캐시 | 브로드캐스트 캐시 동작 | Circuit Breaker 정상 |

### 환경 차이 주의사항

| 차이점 | 로컬 | 프로덕션 | 대응 |
|--------|------|----------|------|
| CPU 아키텍처 | M1 Mac (ARM) | EC2 (x86) | `--platform linux/amd64` |
| TLS | 없음 | Let's Encrypt | 스테이징에서는 HTTP |
| 네트워크 지연 | localhost | VPC 1-3ms | 실제 영향 미미 |

---

## 🔔 알림 품질 게이트

> **목적**: 알림 서비스의 최소 품질 보증
> **적용 시점**: Phase 2 (Firebase Auth + FCM) 구현 시

### 1. 멱등성 (중복 알림 방지)

**방식**: `triggered` 플래그로 단일 조건 기준 중복 방지

```python
# notification_settings 테이블
class NotificationSetting(Base):
    __tablename__ = "notification_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False)
    bank = Column(String, nullable=False)
    currency = Column(String, nullable=False)
    condition = Column(String, nullable=False)  # 'greater_than', 'less_than'
    threshold = Column(Float, nullable=False)
    enabled = Column(Boolean, default=True)

    # 멱등성 필드
    triggered = Column(Boolean, default=False)  # 조건 충족 여부
    last_notified_at = Column(DateTime, nullable=True)
    last_notified_rate = Column(Float, nullable=True)
```

```python
# 알림 로직
async def check_and_notify(bank: str, currency: str, new_rate: float):
    settings = await get_notification_settings(bank, currency)

    for s in settings:
        condition_met = (
            (s.condition == 'greater_than' and new_rate >= s.threshold) or
            (s.condition == 'less_than' and new_rate <= s.threshold)
        )

        if condition_met and not s.triggered:
            # 알림 발송
            await send_fcm_notification(s.user_id, bank, currency, new_rate)
            s.triggered = True
            s.last_notified_at = datetime.now()
            s.last_notified_rate = new_rate
        elif not condition_met:
            # 조건 미충족 시 리셋 (다음 충족 시 다시 알림)
            s.triggered = False

        await db.commit()
```

> **확장 계획**: 다중 조건/프리미엄 기능 추가 시 `triggered` 플래그를 별도 테이블 (`notification_states`)로 분리하거나, `notification_logs` 기반 멱등성으로 전환 검토

### 2. 재시도 정책

**방식**: 선형 재시도 (1초, 2초) + 토큰 오류 시 즉시 삭제

```python
async def send_fcm_with_retry(
    token: str,
    message: messaging.Message,
    max_retries: int = 2
) -> bool:
    delays = [1, 2]  # 초

    for attempt in range(max_retries + 1):
        try:
            messaging.send(message)
            return True
        except FirebaseError as e:
            error_code = e.code

            # 토큰 오류: 재시도 무의미, 즉시 삭제
            if error_code in ['UNREGISTERED', 'INVALID_ARGUMENT']:
                await delete_device_token(token)
                logger.warning("무효 토큰 삭제", extra={
                    "token": token[:20] + "...",
                    "error_code": error_code
                })
                return False

            # 서버 오류: 재시도
            if attempt < max_retries:
                await asyncio.sleep(delays[attempt])
            else:
                logger.error("FCM 전송 실패", extra={
                    "token": token[:20] + "...",
                    "error_code": error_code,
                    "attempts": attempt + 1
                })
                return False

    return False
```

### 3. 구조화 로그 (관측 가능성)

**필수 필드**: `event`, `success`, `latency_ms`, `error_code`

```python
import time
from app import logger

async def send_notification_with_logging(user_id: str, bank: str, currency: str, rate: float):
    start = time.time()
    success = False
    error_code = None

    try:
        # FCM 전송 로직
        await send_fcm_notification(user_id, bank, currency, rate)
        success = True
    except FirebaseError as e:
        error_code = e.code
    finally:
        latency_ms = (time.time() - start) * 1000

        logger.info("FCM 알림 발송", extra={
            "event": "fcm_send",
            "user_id": user_id,
            "bank": bank,
            "currency": currency,
            "rate": rate,
            "success": success,
            "latency_ms": round(latency_ms, 2),
            "error_code": error_code
        })
```

**로그 기반 분석 (Phase 5 이후)**:

```bash
# 실패율 확인
grep '"event": "fcm_send"' logs/app.log | \
  jq 'select(.success == false)' | wc -l

# 평균 지연 확인
grep '"event": "fcm_send"' logs/app.log | \
  jq '.latency_ms' | awk '{sum+=$1; count++} END {print sum/count}'

# 에러 코드별 카운트
grep '"event": "fcm_send"' logs/app.log | \
  jq 'select(.error_code != null) | .error_code' | sort | uniq -c
```

### 완료 게이트 (Phase 2)

| 항목 | 테스트 | 통과 기준 |
|------|--------|----------|
| ✅ 유효 토큰 알림 | 조건 충족 시 푸시 수신 | 10초 내 수신 |
| ✅ 무효 토큰 처리 | 잘못된 토큰으로 전송 | 자동 삭제 확인 |
| ✅ 중복 알림 방지 | 같은 조건 연속 충족 | 알림 1회만 발송 |
| ✅ 조건 리셋 | 미충족 → 재충족 | 새 알림 발송 |
| ✅ 구조화 로그 | 로그 파일 확인 | 필수 필드 포함 |

---

## 🔧 기기 토큰 정리 전략 (구현 예정)

> **목적**: FCM device_token 관리 및 정리 전략 (구현 시점에 최종 결정)
> **상태**: 📋 검토 완료, 구현 대기 → **알림 품질 게이트에 통합됨**

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

**마지막 업데이트**: 2025-12-11
**결정 완료**: AWS RDS PostgreSQL + Firebase Auth + FCM
**문서 개선**: Phase 2 상세 가이드 추가 (Apple Developer/Firebase Console 설정 절차)
