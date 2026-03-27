# NEWS_IMPL_SPEC.md — 환율 뉴스 피드 구현 스펙

> **용도**: 구현 참고용 임시 문서 (모든 구현 완료 후 삭제)
> **작성일**: 2026-03-27
> **최종 업데이트**: 2026-03-28

---

## 1. 개요

연합인포맥스 뉴스를 **2개 경로**(RSS + KB API)로 수집하여 환율 관련 뉴스 API를 제공한다.

**핵심 결정:**
- 저장소: Redis-only (8시간 윈도우 휘발성 데이터)
- 수집 경로: KB API (속보, ~2시간 빠름) + RSS (원문 링크 보유, 백필)
- API 응답: title, link, source, content_type, published_at (author/category 미노출)
- category: 내부 Redis에만 저장 (필터링/디버깅용)

---

## 2. 데이터 소스

### 2.1 RSS 소스 (구현 완료)

| 피드 | 채널명 | category | 필터 수준 |
|------|--------|----------|----------|
| S1N16 | 채권/외환 | `forex` | `noise_only` |
| S1N23 | 국제뉴스 | `global` | `loose` (forex 관련도 OR macro 드라이버) |
| S1N21 | 해외주식 | `stock_global` | `strict` |
| S1N2 | 증권 | `stock_domestic` | `strict` |

- 수집 주기: 5분 (:45초)
- ETag/Last-Modified 조건부 GET (304 지원)
- RSS는 인포맥스 비단말 사용자 대상으로 **~2시간 딜레이** 있음

### 2.2 KB API 소스 (구현 예정)

KB Star FX 시장뉴스 페이지의 JSON API. 동일한 연합인포맥스 뉴스를 **딜레이 없이** 제공.

**API 스펙:**

```
POST https://fx.kbstar.com/quics?page=C110648&QAction=1253470&RType=json
Content-Type: application/x-www-form-urlencoded
Referer: https://fx.kbstar.com/quics?page=C110648
```

| 파라미터 | 값 | 설명 |
|---------|-----|------|
| `inquryDstcd` | `21` | 외환 탭 (22=경제) |
| `newsClsfiDstcd` | 빈값 | 전체 분류 |
| `finalNewsWritYMS` | 빈값 or 커서 | 페이징 (다음최종뉴스작성일시) |
| `pageNo` | `0` | 페이지 번호 |
| `rpsntNews1Uniqno` | nsid | 대표뉴스 1 (AJAX 목록에서 제외됨) |
| `rpsntNews2Uniqno` | nsid | 대표뉴스 2 (AJAX 목록에서 제외됨) |

**응답 필드:**

```json
{
  "뉴스고유번호": "AKR20260327174100016",
  "뉴스제목내용": "중국 선박도 호르무즈서 막혔다",
  "뉴스요약내용": "27일(현지시간) 이란의...",
  "뉴스작성일시": "20260327230322",
  "뉴스해시태그": "원자재|대체|금융|경제|채권|외환|",
  "뉴스분류구분코드": "99",
  "뉴스1사진": "http://newsimage.einfomax.co.kr/..."
}
```

**분류코드 → 필터/category 매핑:**

| KB 코드 | 의미 | 필터 수준 | 내부 category |
|---------|------|----------|--------------|
| `IS` | 외환 | `noise_only` (운영 가설, 모니터링 후 loose로 조정 가능) | `forex` |
| `IY` | 서환 | `noise_only` | `forex` |
| `IT` | 채권 | `loose` | `global` |
| `99` | 기타 | `loose` | `global` |
| 기타 | 미매핑 | `loose` (보수적) | `global` |

> **IS = noise_only는 운영 가설**: 샘플에서 "미시간대 소비심리지수" 같은 거시 지표 기사가
> IS로 분류된 사례가 있음. 직접 외환이 아닌 기사지만 환율 영향이 커서 통과해도 문제없을 수 있음.
> 운영 시작 후 IS의 오탐/누락을 모니터링하여 loose로 상향 여부 결정.

**대표뉴스 2건 주의사항:**

- 초기 HTML에 렌더링되지만 AJAX 목록에서는 제외됨
- `rpsntNews1Uniqno`, `rpsntNews2Uniqno`로 전달되는 기사
- 반드시 초기 HTML에서 별도 파싱하거나, 별도 요청으로 수집해야 함
- 대표뉴스에도 `[인사]` 등 잡음 기사가 올라올 수 있음 → 동일 필터 적용
- **대표뉴스와 AJAX 목록의 중복 제거는 저장 전(nsid 기준)에 처리** — 수집 시 합친 뒤 nsid dedupe 후 필터+저장

**수집 주기:** 5분 (RSS와 동일, 별도 second 오프셋)

**인증:** 불필요 (공개 API)

---

## 3. KB ↔ RSS 병합 (Upsert 규칙)

### 3.1 nsid 기반 중복 제거

KB API와 RSS 모두 동일한 `nsid` (AKR...) 체계를 사용.
같은 nsid가 두 경로로 들어오면 아래 규칙에 따라 병합.

### 3.2 Upsert 우선순위

| 필드 | KB 먼저 → RSS 나중 | RSS 먼저 → KB 나중 |
|------|-------------------|-------------------|
| `title` | RSS로 교체 (유효값일 때) | 유지 |
| `link` | RSS einfomax URL로 교체 | 유지 |
| `category` | RSS category로 교체 | 유지 |
| `published_at` | `min(existing, incoming)` | `min(existing, incoming)` |
| `ingested_via` | `kb_api` → `kb_api+rss` | `rss` → `rss+kb_api` |
| `match_type` | RSS가 fx로 판별되면 fx로 승격, 아니면 유지 | 유지 |

**핵심 원칙:**
- **RSS 유효값 우선**: title/link/category는 RSS의 유효한 값(비어있지 않은)이 있을 때만 교체
- **빈값 overwrite 방지**: RSS의 title/link/category가 null 또는 빈 문자열이면 기존 KB 값 유지
- published_at은 소스 무관, 더 이른 값 우선 (`min(existing, incoming)`)
- RSS가 이미 있으면 KB가 나중에 와도 덮어쓰지 않음
- **match_type 승격**: KB에서 macro_severity로 들어왔더라도, RSS가 직접 외환(fx)으로 판별되면 fx로 승격. RSS가 fx가 아니면 기존 KB 분류 유지

### 3.3 Link 전략

```
[KB API 도착 시]
link = "https://fx.kbstar.com/quics?page=C110648&cc=b113988:b114074&newsUniqNo={nsid}&QSL=F"

[RSS 도착 시 → upsert]
link = "https://news.einfomax.co.kr/news/articleView.html?idxno={idxno}"
```

- KB 상세 페이지: 인증 없이 접근 가능, 뉴스 전문 표시
- RSS 도착 후 einfomax 원문 URL로 자연스럽게 교체
- 8시간 윈도우 내 사용자가 URL 변경을 인지할 가능성 낮음

---

## 4. 필터 시스템

### 4.1 잡음 제외 (모든 소스 공통)

```python
EXCLUDE_TITLE_PREFIXES = ["[인사]", "[부고]", "[게시판]"]
EXCLUDE_TITLE_KEYWORDS = ["신임", "선임", "임명", "전보", "승진", "이동"]
STRONG_FOREX_KEYWORDS = ["환율", "외환", "달러-원", "달러-엔", "달러", ...]
```

### 4.2 환율 관련도

```python
PRIMARY_KEYWORDS = ["환율", "외환", "달러-원", "달러-엔", "엔화", "유로", ...]
CONDITIONAL_KEYWORDS = {"금리": ["달러", "엔", ...], "연준": ["달러", "환율", ...], ...}
```

### 4.3 매크로 이벤트 드라이버

```python
GEOPOLITICAL_KEYWORDS = ["이란", "중동", "호르무즈", "전쟁", "휴전", "종전"]
SEVERITY_KEYWORDS = ["폭격", "공습", "미사일", "핵", "봉쇄", "침공", ...]
TRANSMISSION_KEYWORDS = ["유가", "원유", "달러", "환율", "시장", "증시", ...]
```

`classify_macro(title)` 반환값:
- `"macro_severity"`: 지정학 + 고강도 이벤트 (폭격/봉쇄)
- `"macro"`: 지정학 + 시장 전파 키워드 (증시/유가)
- `""`: 미해당

### 4.4 필터 적용 규칙

| filter_level | 잡음 제외 | 환율 관련도 | 매크로 드라이버 |
|--------------|----------|------------|---------------|
| `noise_only` | O | - | - |
| `loose` | O | `is_forex_relevant(strict=False)` | `classify_macro()` (OR) |
| `strict` | O | `is_forex_relevant(strict=True)` | - |

---

## 5. API 응답

### 5.1 스키마 (app/schemas.py)

```python
class NewsItem(BaseModel):
    id: str
    title: str
    link: Optional[str] = None
    source: str                         # "einfomax", "fxi", ...
    content_type: str = "external_link" # "external_link" | "direct_text"
    published_at: str
    body: Optional[str] = None          # direct_text 전용 (향후 예정, 미구현)
```

### 5.2 정렬 규칙

```
primary (최신순):  fx + macro_severity
secondary (최신순): macro
```

- fx(직접 외환) + macro_severity(전쟁 속보)는 함께 최신순 경쟁
- macro(일반 매크로/증시)는 후순위
- 각 그룹 내에서는 published_at 내림차순

### 5.3 응답 예시

```json
{
  "news": [
    {
      "id": "AKR20260327174100016",
      "title": "이스라엘, 이란 핵시설 폭격",
      "link": "https://fx.kbstar.com/quics?page=C110648&cc=b113988:b114074&newsUniqNo=AKR20260327174100016&QSL=F",
      "source": "einfomax",
      "content_type": "external_link",
      "published_at": "2026-03-27T23:00:34+09:00"
    },
    {
      "id": "AKR20260327150100016",
      "title": "달러-원, 런던장에서 1,500원대 등락",
      "link": "https://news.einfomax.co.kr/news/articleView.html?idxno=4406427",
      "source": "einfomax",
      "content_type": "external_link",
      "published_at": "2026-03-27T19:36:10+09:00"
    }
  ],
  "metadata": {
    "returned_count": 2,
    "window_hours": 8,
    "responded_at": "2026-03-28T01:00:00+09:00"
  }
}
```

---

## 6. Redis 내부 구조

### 6.1 HASH 필드 (news:item:{nsid})

| 필드 | 공개 API | 내부용 | 설명 |
|------|---------|--------|------|
| title | O | | 기사 제목 |
| link | O | | 현재 최선의 URL |
| source | O | | "einfomax" |
| content_type | O | | "external_link" |
| published_at | O | | ISO 8601 |
| category | | O | forex, global, ... (필터링용) |
| match_type | | O | fx, macro, macro_severity |
| ingested_via | | O | kb_api, rss, kb_api+rss |

---

## 7. KB API Fetcher 구현 계획

### 7.1 파일 구조

```
app/news/
├── __init__.py
├── sources.py       # RSS 소스 정의 (구현 완료)
├── filters.py       # 필터 시스템 (구현 완료)
├── fetcher.py       # RSS 수집 (구현 완료)
├── kb_fetcher.py    # KB API 수집 (구현 예정)
└── upsert.py        # 공통 Redis upsert 로직 (구현 예정)
```

### 7.2 kb_fetcher.py 주요 로직

```python
async def fetch_kb_news() -> None:
    """KB API에서 최신 뉴스 수집 (스케줄러에서 5분마다 호출)"""

    # 1. 초기 HTML GET → 대표뉴스 2건 nsid 추출
    # 2. AJAX POST (inquryDstcd=21) → 뉴스목록ARRAY 파싱
    # 3. 대표뉴스 2건 + 목록 합침, nsid 기준 dedupe
    # 4. 분류코드별 필터 적용 (IS→noise_only, IT/99→loose)
    # 5. upsert 규칙에 따라 Redis 저장
```

### 7.3 upsert.py — 공통 저장 로직

기존 fetcher.py의 직접 Redis 저장을 공통 함수로 분리:

```python
async def upsert_news_item(
    nsid: str,
    title: str,
    link: str,
    category: str,
    match_type: str,
    published_at: datetime,
    ingested_via: str,  # "kb_api" | "rss"
) -> None:
    """
    nsid 기준 upsert.
    - 새 기사: 그대로 저장
    - 기존 기사 + RSS 도착: title/link/category를 RSS로 정규화
    - 기존 기사 + KB 도착 (RSS 이미 있음): 무시
    - published_at: 항상 min(existing, incoming)
    - ingested_via: 누적
    """
```

### 7.4 스케줄러 등록

```python
scheduler.add_job(
    fetch_kb_news,
    CronTrigger(minute="*/5", second="15", timezone=KST),  # RSS(:45)와 30초 간격
    id="kb_news_fetcher",
    max_instances=1,
    coalesce=True,
)
```

### 7.5 에러 핸들링

| 상황 | 처리 |
|------|------|
| KB API 타임아웃/5xx | 로그 + 다음 주기 재시도 |
| HTML 파싱 실패 (대표뉴스) | 대표뉴스 스킵, AJAX 목록만 수집 |
| JSON 파싱 실패 | 로그 + 전체 스킵 |
| KB IP 차단 | 로그 경고 + RSS만으로 폴백 |

---

## 8. 구현 순서

### Phase 1: RSS 기반 (구현 완료)

- [x] app/news/sources.py
- [x] app/news/filters.py (잡음 + 관련도 + macro + severity)
- [x] app/cache.py 확장 (ZSET/HASH 메서드, bytes 디코딩)
- [x] app/news/fetcher.py (RSS 수집 + 파싱 + upsert + cleanup)
- [x] app/schemas.py (NewsItem, NewsMetadata, NewsResponse)
- [x] app/main.py (GET /api/news, 정렬: primary/secondary)
- [x] app/scheduler.py (5분 :45초)

### Phase 2: KB API 추가 (구현 예정)

```text
1. app/news/upsert.py         — 공통 upsert 로직 (fetcher.py에서 분리)
2. app/news/fetcher.py 수정    — upsert.py 사용으로 전환
3. app/news/kb_fetcher.py     — KB API 수집 (HTML 대표뉴스 + AJAX 목록)
4. app/scheduler.py           — kb_news_fetcher cron job 등록 (:15초)
5. 테스트                      — KB fetch + RSS dedupe + 정렬 확인
```

---

## 9. 테스트 체크리스트

### Phase 1 (완료)

- [x] RSS fetch_all_news() 실행 → Redis 저장
- [x] 304 Not Modified 동작
- [x] GET /api/news 응답
- [x] ?category= 필터
- [x] 잡음 제외 ([인사] 등)
- [x] macro/macro_severity 분류

### Phase 2 (예정)

- [ ] KB API fetch → Redis 저장
- [ ] KB 대표뉴스 2건 수집
- [ ] KB에만 있는 기사 → link=fx.kbstar.com
- [ ] RSS 도착 후 같은 nsid → link=einfomax로 교체
- [ ] RSS 먼저 → KB 나중 → 덮어쓰지 않음
- [ ] published_at min() 동작
- [ ] ingested_via 누적
- [ ] 분류코드별 필터 (IS=noise_only, IT/99=loose)
- [ ] 정렬: primary(fx+macro_severity) → secondary(macro)
