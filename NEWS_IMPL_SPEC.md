# NEWS_IMPL_SPEC.md — 환율 뉴스 피드 구현 스펙

> **용도**: 구현 참고용 임시 문서 (안정화 후 삭제)
> **작성일**: 2026-03-27
> **최종 업데이트**: 2026-03-29 (v2 단순화 개정)
> **상태**: v2 개정 — 코드 수정 예정

---

## 1. 개요

연합인포맥스 뉴스를 **2개 경로**(RSS + KB API)로 수집하여 뉴스 API를 제공한다.

**v2 핵심 변경: 대폭 단순화**

- 모든 소스 `noise_only` 일원화 (관련도/매크로/산업 필터 삭제)
- 윈도우 24시간, 기본 limit 100, 기본 hours 24.0
- match_type 완전 삭제
- 공개 category 파라미터 삭제
- flash content_type 삭제 → 제목에 "(본문없음)" + link 유지
- 정치 키워드 제외 추가

**유지:**

- 저장소: Redis-only (24시간 윈도우, DB 불필요)
- 수집 경로: KB API (속보) + RSS (원문 링크 백필)
- 정렬: 순수 시간순 (published_at 내림차순)
- 잡음 제외 (인사/부고/정치)
- near-duplicate collapse (초보/상보 접기)
- KB↔RSS nsid 기반 upsert 병합
- `[전문]` report_pdf 처리

---

## 2. 데이터 소스

### 2.1 RSS 소스

| 피드 | 채널명 | 내부 category | 필터 |
|------|--------|-------------|------|
| S1N16 | 채권/외환 | `forex` | `noise_only` |
| S1N23 | 국제뉴스 | `global` | `noise_only` |
| S1N21 | 해외주식 | `global` | `noise_only` |
| S1N2 | 증권 | `global` | `noise_only` |

- 수집 주기: 5분 (:45초)
- ETag/Last-Modified 조건부 GET (304 지원)

### 2.2 KB API 소스

```
POST https://fx.kbstar.com/quics?page=C110648&QAction=1253470&RType=json
```

- 외환탭(21) + 경제탭(22), 각 2페이지 수집
- 수집 주기: 5분 (:15초)

**내부 category 매핑 (탭 기준만):**

| 탭 | category |
|----|----------|
| 21 (외환) | `forex` |
| 22 (경제) | `global` |

> 기존 KB_CODE_MAP (IS/IY/IT/IU/99 세부 매핑) 삭제.
> 분류코드별 filter_level 차등 적용 삭제.

---

## 3. 필터 시스템

### 3.1 잡음 제외 (`is_noise_title`) — 유일한 필터

**접두사 제외:**
```
[인사], [부고], [게시판]
```

**인사 키워드 제외** (시장 키워드 없으면):
```
신임, 선임, 임명, 전보, 승진, 이동,
내정, 취임, 후임, 보임, 영입, 합류, 사임, 퇴임
```

**직책 이동 패턴 제외** (시장 키워드 없으면):
```
CEO로, CFO로, CRO로, CIO로, COO로, CTO로,
대표로, 원장으로, 사장으로, 회장으로, 부행장으로
```

**정치 키워드 제외** (시장 키워드 없으면):
```
국민의힘, 국힘, 더불어민주당, 민주당,
조국혁신당, 개혁신당,
당대표, 원내대표, 총선, 대선, 선거
```

> 트럼프, 정부, 대통령은 시장 영향 가능하므로 제외 안 함.

**시장 키워드 보호** (위 제외 규칙을 무력화):
```
환율, 외환, 달러-원, 달러-엔, 달러, 엔화, 위안, 유로,
DXY, 환시, 환위험, 환헤지, 금리, 증시,
국제유가, WTI, 브렌트유, 유가(경계 매칭)
```

### 3.2 삭제 대상 (v1 → v2)

| 삭제 | 이유 |
|------|------|
| `is_forex_relevant()` + PRIMARY/CONDITIONAL 키워드 | noise_only로 대체 |
| `classify_macro()` + GEOPOLITICAL/SEVERITY/TRANSMISSION 키워드 | noise_only로 대체 |
| `is_industry_impact()` + THEMES/ANCHORS/IMPACT 키워드 | noise_only로 대체 |
| `filter_level` 개념 | 전부 noise_only |
| `match_type` (fx, macro, macro_severity, macro_industry) | 분류 자체 삭제 |

---

## 4. 특수 처리

### 4.1 `*` 접두사 기사 (본문 없음)

**v1**: content_type="flash", link=null
**v2**: content_type="external_link", link 유지, 제목 끝에 " (본문없음)" 추가

```
원본: *달러-엔 160엔 돌파
변환: 달러-엔 160엔 돌파 (본문없음)
link: KB 상세 URL (유지)
```

- 내부 Redis: `is_bodyless=true` 저장 (제목 문자열 의존 방지)
- RSS가 같은 nsid로 오면 → 정상 제목+link로 자연 교체 (is_bodyless 삭제)
- 공개 API에서 `flash` content_type 삭제

### 4.2 `[전문]` 접두사 기사 (은행 보고서)

**유지 (v1과 동일):**

- KB 상세 페이지에서 `rreport.einfomax.co.kr/report/*.pdf` URL 추출
- content_type="report_pdf", link=PDF URL, 제목에서 `[전문]` 제거
- 추출 실패 시 KB 상세 링크로 fallback
- report_pdf는 RSS가 와도 유지

**HTTP 이슈:**
- PDF URL은 `http://` (HTTPS 아님)
- iOS 앱: ATS 정책으로 인앱 WebView에서 HTTP 불가 → 외부 브라우저(Safari)로 열기
- 서버 수정 불필요, 앱에서 URL scheme 판단 후 처리

---

## 5. KB ↔ RSS 병합 (Upsert 규칙)

nsid 기반 중복 제거. match_type 관련 로직 삭제.

| # | 상태 | title/link/category | published_at |
|---|------|-------------------|-------------|
| 1 | 새 기사 | 그대로 저장 | 그대로 |
| 2 | KB→RSS | RSS 유효값으로 교체 | min() |
| 3 | RSS→KB | 유지 | min() |
| 4 | RSS有+KB재수집 | 유지 (RSS 보호) | min() |
| 5 | KB만+KB재수집 | 최신값 갱신 | min() |
| 6 | RSS재수집 | 갱신 | min() |

**정책:**

- RSS 유효값 우선 (빈값 overwrite 방지)
- report_pdf는 RSS가 와도 유지
- `*` 기사(is_bodyless): RSS가 오면 정상 제목+link로 교체, is_bodyless 해제
- published_at: 모든 경로에서 min(existing, incoming)
- ingested_via: 누적 (kb_api, rss, kb_api+rss)

---

## 6. API 응답

### 6.1 엔드포인트

```
GET /api/news
GET /api/news?limit=20
GET /api/news?hours=12
```

| 파라미터 | 타입 | 기본값 | 범위 |
|---------|------|--------|------|
| `limit` | int | 100 | 1~100 (범위 초과 시 clamp) |
| `hours` | float | 24.0 | 0.5~24.0 (범위 초과 시 clamp) |

> `category` 파라미터 삭제 (v2).

### 6.2 스키마

```python
class NewsItem(BaseModel):
    id: str
    title: str
    link: Optional[str] = None
    source: str
    content_type: str = "external_link"  # "external_link" | "report_pdf"
    published_at: str
```

> `body` 필드 삭제 (direct_text 미사용).
> `flash` content_type 삭제.

### 6.3 content_type별 동작

| content_type | link | 앱 동작 |
|-------------|------|--------|
| `external_link` | URL (정상 경로에서 non-null 기대, 클라이언트는 방어적 null 처리) | link 열기 |
| `report_pdf` | PDF URL (http) | 외부 브라우저로 열기 (iOS HTTP 제한) |

### 6.4 정렬 + 중복 제거

- 순수 시간순 (published_at 내림차순)
- near-duplicate collapse 유지:
  - 제목 정규화: "(본문없음)" suffix 제거, 꼬리표(상보/종합/속보) 제거, HTML 엔티티 디코딩
  - 같은 정규화 제목을 시간 클러스터(60분)로 묶기
  - 클러스터 내 꼬리표(상보/종합) 또는 `is_bodyless` 기사가 있으면 대표 1건만 노출

---

## 7. Redis 내부 구조

### 7.1 HASH 필드 (news:item:{nsid})

| 필드 | 공개 API | 내부용 | 설명 |
|------|---------|--------|------|
| title | O | | 기사 제목 ("*" 기사: "(본문없음)" 포함) |
| link | O | | URL |
| source | O | | "einfomax" |
| content_type | O | | "external_link" 또는 "report_pdf" |
| published_at | O | | ISO 8601 |
| category | | O | forex, global (디버깅용) |
| is_bodyless | | O | true/false ("*" 기사 판별) |
| ingested_via | | O | kb_api, rss, kb_api+rss |

> `match_type` 삭제.

---

## 8. 파일 변경 계획

### 수정 파일

| 파일 | 변경 |
|------|------|
| `app/news/filters.py` | 3개 함수+키워드 삭제, 정치 키워드 추가, `is_noise_title()` 중심으로 축소 |
| `app/news/sources.py` | `filter_level` 필드 자체 삭제 (전부 noise_only이므로 불필요) |
| `app/news/fetcher.py` | filter_level 분기 제거, match_type 제거, 24h 윈도우 |
| `app/news/kb_fetcher.py` | KB_CODE_MAP 삭제, 탭 기준 category만, `*` 처리 변경, match_type 제거 |
| `app/news/upsert.py` | match_type 파라미터/로직 삭제, is_bodyless 처리, content_type 교정(flash→삭제) |
| `app/main.py` | category 파라미터 삭제, hours=24.0, limit=50, flash 제거, body 제거 |
| `app/schemas.py` | NewsItem에서 body 삭제, flash 타입 삭제 |

### 삭제 코드

| 대상 | 위치 |
|------|------|
| `is_forex_relevant()` | filters.py |
| `classify_macro()` | filters.py |
| `is_macro_relevant()` | filters.py |
| `is_industry_impact()` | filters.py |
| PRIMARY/CONDITIONAL/GEOPOLITICAL/SEVERITY/TRANSMISSION/INDUSTRY_* 키워드 | filters.py |
| `KB_CODE_MAP`, `_DEFAULT_CODE_MAP` | kb_fetcher.py |
| match_type 관련 로직 전체 | upsert.py, fetcher.py, kb_fetcher.py |
| `_collapse_near_duplicates` 내 flash 의존 | main.py |

### 문서 업데이트 (코드 수정 후)

```
1. NEWS_IMPL_SPEC.md  ← 이 문서 (이미 개정)
2. 코드 수정
3. NEWS_API_SPEC.md    ← 모바일 연동 문서
4. CLAUDE.md
5. DECISIONS.md
6. CHANGELOG.md
```

---

## 9. 구현 순서

```text
1. app/news/filters.py     — 3개 함수+키워드 삭제, 정치 키워드 추가
2. app/news/sources.py     — filter_level 정리
3. app/news/upsert.py      — match_type 삭제, is_bodyless 추가
4. app/news/fetcher.py     — filter 분기 제거, 24h, match_type 삭제
5. app/news/kb_fetcher.py  — KB_CODE_MAP 삭제, * 처리 변경, match_type 삭제
6. app/schemas.py          — body/flash 삭제
7. app/main.py             — category 삭제, limit=50, hours=24, collapse 수정
8. 테스트
9. 배포
10. NEWS_API_SPEC.md + CLAUDE.md + DECISIONS.md + CHANGELOG.md
```
