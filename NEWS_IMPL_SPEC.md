# NEWS_IMPL_SPEC.md — 환율 뉴스 피드 구현 스펙

> **용도**: 구현 참고용 임시 문서 (구현 완료 후 삭제)
> **작성일**: 2026-03-27
> **상태**: 최종 (코덱스 리뷰 반영 완료)

---

## 1. 개요

연합인포맥스 RSS 4개 피드에서 환율 관련 뉴스를 수집하여 API로 제공한다.

**핵심 결정:**
- 저장소: Redis-only (DB 불필요 — 8시간 윈도우 휘발성 데이터)
- description 필드: 저장하지 않음 (잘린 본문, UX 가치 없음)
- Slack/Google Drive: 사용하지 않음

---

## 2. 데이터 소스

### 2.1 소스 목록 및 필터 전략

| 피드 | 채널명 | category | 필터 수준 | 설명 |
|------|--------|----------|----------|------|
| S1N16 | 채권/외환 | `forex` | 잡음 제외만 | 8h + 인사/부고 등 잡음 제외 |
| S1N23 | 국제뉴스 | `global` | 느슨한 필터 | 8h + 잡음 제외 + 환율 관련 키워드 (PRIMARY + CONDITIONAL) |
| S1N21 | 해외주식 | `stock_global` | 엄격한 필터 | 8h + 잡음 제외 + 환율 직접 키워드 (PRIMARY만) |
| S1N2 | 증권 | `stock_domestic` | 엄격한 필터 | 8h + 잡음 제외 + 환율 직접 키워드 (PRIMARY만) |

> **S1N16도 무필터가 아닌 이유**: "[인사] 재정경제부", "신임 경제정책국장에 유병희" 등
> 환율 사용자에게 불필요한 인사 뉴스가 섞임. 잡음 제외 필터로 제거한다.
>
> **S1N23을 무필터로 두지 않는 이유**: "유럽증시 혼조세", "중국증시 마감" 등 환율과
> 무관한 일반 국제시장 기사가 다수 포함됨. 느슨한 필터(PRIMARY + CONDITIONAL)로
> "달러-엔 159.8엔", "OECD 금리 인상+달러" 같은 기사만 통과시킨다.

### 2.2 RSS 응답 특성 (실측)

- 각 피드 20개 item 고정
- HEAD 요청: 404 (지원 안 함)
- GET 요청: ETag, Last-Modified 헤더 제공
- 조건부 GET (If-None-Match): 304 Not Modified 정상 동작
- pubDate 형식: `2026-03-27 19:36:10` (KST, 타임존 정보 없음)
- item 필드: nsid, title, link, description, author, pubDate

### 2.3 소스 설정 구조

```python
# app/news/sources.py

from dataclasses import dataclass
from typing import List

@dataclass(frozen=True)
class NewsSource:
    feed_id: str          # 내부 식별자 (etag 키 등에 사용)
    url: str
    category: str         # forex, global, stock_global, stock_domestic
    filter_level: str     # "noise_only" | "loose" | "strict"

NEWS_SOURCES: List[NewsSource] = [
    NewsSource("einfomax_forex",          "https://news.einfomax.co.kr/rss/S1N16.xml", "forex",          "noise_only"),
    NewsSource("einfomax_global",         "https://news.einfomax.co.kr/rss/S1N23.xml", "global",         "loose"),
    NewsSource("einfomax_stock_global",   "https://news.einfomax.co.kr/rss/S1N21.xml", "stock_global",   "strict"),
    NewsSource("einfomax_stock_domestic", "https://news.einfomax.co.kr/rss/S1N2.xml",  "stock_domestic", "strict"),
]
```

---

## 3. 필터 시스템 (3단계)

모든 소스에 공통 잡음 제외 필터를 적용하고, 소스별로 환율 관련도 필터를 추가한다.
**title 기준으로만 판단** (author, description 등은 사용하지 않음 — 잡음 증가 방지).

### 3.1 잡음 제외 필터 (모든 소스 공통)

```python
# app/news/filters.py

# 제목 접두사로 즉시 제외
EXCLUDE_TITLE_PREFIXES = [
    "[인사]", "[부고]", "[게시판]",
]

# 이 키워드가 포함되면 제외 후보 (강한 외환 키워드 없으면 제외)
EXCLUDE_TITLE_KEYWORDS = [
    "신임", "선임", "임명", "전보", "승진", "이동",
]

# 제외 후보에서 살려주는 강한 외환 키워드
STRONG_FOREX_KEYWORDS = [
    "환율", "외환", "달러-원", "달러-엔", "달러", "엔화",
    "위안", "유로", "DXY", "환시", "환위험", "환헤지",
]


def is_noise_title(title: str) -> bool:
    """인사/부고 등 잡음 기사 판단. True면 제외."""
    # 1. 접두사 매칭 → 즉시 제외
    if any(title.startswith(prefix) for prefix in EXCLUDE_TITLE_PREFIXES):
        return True

    # 2. 인사 키워드 + 강한 외환 키워드 없음 → 제외
    if any(kw in title for kw in EXCLUDE_TITLE_KEYWORDS):
        if not any(fx in title for fx in STRONG_FOREX_KEYWORDS):
            return True

    return False
```

### 3.2 환율 관련도 필터

```python
# 단독으로 통과 가능 — 환율 직접 관련 키워드
PRIMARY_KEYWORDS = [
    "환율", "외환", "달러-원", "달러-엔", "엔화", "유로",
    "위안", "환헤지", "환위험", "DXY", "외환시장",
]

# 통화 키워드와 함께 있을 때만 통과 — 간접 관련 키워드
CONDITIONAL_KEYWORDS = {
    "금리": ["달러", "엔", "유로", "위안", "환율"],
    "연준": ["달러", "환율", "외환"],
    "Fed": ["달러", "환율", "외환"],
    "BOJ": ["엔", "환율"],
    "PBOC": ["위안", "환율"],
    "ECB": ["유로", "환율"],
}


def is_forex_relevant(title: str, strict: bool = False) -> bool:
    """
    기사의 환율 관련도 판단 (title 기준).

    strict=False (느슨한 필터, global용): PRIMARY + CONDITIONAL 모두 허용
    strict=True  (엄격한 필터, stock용):  PRIMARY만 허용
    """
    if any(kw in title for kw in PRIMARY_KEYWORDS):
        return True
    if strict:
        return False
    for kw, requires in CONDITIONAL_KEYWORDS.items():
        if kw in title and any(r in title for r in requires):
            return True
    return False
```

### 3.3 필터 적용 규칙 (3단계)

| filter_level | 잡음 제외 | 환율 관련도 | 설명 |
|--------------|----------|------------|------|
| `noise_only` (forex) | `is_noise_title()` | 안 함 | 환율 전문 채널이므로 잡음만 제거 |
| `loose` (global) | `is_noise_title()` | `is_forex_relevant(strict=False)` | PRIMARY + CONDITIONAL |
| `strict` (stock_*) | `is_noise_title()` | `is_forex_relevant(strict=True)` | PRIMARY만 |

**필터 순서**: 잡음 제외 → 환율 관련도 (잡음 기사는 관련도 체크 전에 제거)

---

## 4. Redis 자료구조

### 4.1 키 설계

```
news:index                    ZSET   score=published_at (unix timestamp)
                                     member=nsid
news:item:{nsid}              HASH   title, link, author, category, source, published_at
news:etag:{feed_id}           STRING ETag 값
news:last_modified:{feed_id}  STRING Last-Modified 값
```

### 4.2 ZSET index

```
ZADD news:index <published_at_unix> <nsid>
```

- score: pubDate를 UTC unix timestamp로 변환
- member: nsid (기사 고유 ID)

### 4.3 HASH per item

```text
HSET news:item:AKR20260327150100016
    title        "달러-원, 런던장에서 1,500원대 등락…미·이란 협상 초점"
    link         "https://news.einfomax.co.kr/news/articleView.html?idxno=4406427"
    category     "forex"          # 내부 필터링용 (API 응답에서 제외)
    source       "einfomax"       # 데이터 제공자
    content_type "external_link"  # 렌더링 방식
    published_at "2026-03-27T19:36:10+09:00"
```

### 4.4 ETag/Last-Modified 캐시

```
SET news:etag:einfomax_forex "\"4bdd-64e006311740d\""
SET news:last_modified:einfomax_forex "Fri, 27 Mar 2026 12:10:29 GMT"
```

### 4.5 TTL 전략

| 키 | TTL | 설명 |
|----|-----|------|
| `news:index` | TTL 없음 | cleanup job이 직접 관리 |
| `news:item:{nsid}` | 24h (안전망) | cleanup job이 먼저 삭제하되, 누락 시 TTL이 잡아줌 |
| `news:etag:{feed_id}` | 1h | 피드 ETag 캐시 |
| `news:last_modified:{feed_id}` | 1h | 피드 Last-Modified 캐시 |

### 4.6 Cleanup 로직

```python
async def cleanup_old_news():
    """8시간 이전 기사 제거 — fetch 직후 호출"""
    cutoff = time.time() - (8 * 3600)

    # 1. 만료된 nsid 목록 조회
    stale_ids = await redis_cache.zrangebyscore("news:index", "-inf", cutoff)

    if not stale_ids:
        return

    # 2. ZSET에서 제거
    await redis_cache.zremrangebyscore("news:index", "-inf", cutoff)

    # 3. 개별 HASH 일괄 삭제 (variadic delete로 round trip 1회)
    keys = [f"news:item:{nsid}" for nsid in stale_ids]
    await redis_cache.delete(*keys)

    logger.info("뉴스 정리", extra={"removed": len(stale_ids)})
```

---

## 5. Fetcher 구현

### 5.1 파일 위치

```
app/news/
├── __init__.py
├── sources.py      # NewsSource 데이터클래스 + NEWS_SOURCES 리스트
├── filters.py      # 키워드 필터 (is_forex_relevant)
└── fetcher.py      # RSS 수집 + 파싱 + Redis 저장 + cleanup
```

### 5.2 fetcher.py 주요 함수

```python
# app/news/fetcher.py

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from app.cache import redis_cache
from app.news.sources import NEWS_SOURCES, NewsSource
from app.news.filters import is_noise_title, is_forex_relevant

logger = logging.getLogger("exchange_rate.news")

KST = timezone(timedelta(hours=9))
NEWS_WINDOW_HOURS = 8
_ITEM_TTL_SECONDS = 24 * 3600  # 안전망 TTL


async def fetch_all_news() -> None:
    """모든 소스에서 뉴스 수집 (스케줄러에서 5분마다 호출)"""
    for source in NEWS_SOURCES:
        try:
            await _fetch_single_source(source)
        except Exception:
            logger.exception("뉴스 수집 실패", extra={"source": source.feed_id})

    await _cleanup_old_news()


async def _fetch_single_source(source: NewsSource) -> None:
    """단일 RSS 피드 수집"""

    # 1. 조건부 GET (ETag / Last-Modified)
    xml_text, new_etag, new_last_modified = await _conditional_get(source)
    if xml_text is None:
        return  # 304 Not Modified — 변경 없음

    # 2. XML 파싱
    items = _parse_rss(xml_text)

    # 3. 필터링 (8h → 잡음 제외 → 소스별 환율 관련도)
    cutoff = datetime.now(KST) - timedelta(hours=NEWS_WINDOW_HOURS)
    filtered = []
    for item in items:
        title = item["title"]
        if item["published_at"] < cutoff:
            continue
        # 잡음 제외 (모든 소스 공통)
        if is_noise_title(title):
            continue
        # 환율 관련도 (소스별)
        if source.filter_level == "loose" and not is_forex_relevant(title, strict=False):
            continue
        if source.filter_level == "strict" and not is_forex_relevant(title, strict=True):
            continue
        # filter_level == "noise_only" → 잡음 제외만 통과하면 OK
        filtered.append(item)

    # 4. Redis 저장 (항상 upsert — 기사 수정 대응)
    added = 0
    for item in filtered:
        nsid = item["nsid"]
        published_ts = item["published_at"].timestamp()

        # ZADD: score 동일하면 덮어쓰기 (idempotent)
        await redis_cache.zadd("news:index", {nsid: published_ts})
        # HSET mapping: 필드값 덮어쓰기 (기사 제목 수정 등 반영)
        await redis_cache.hset_dict(f"news:item:{nsid}", {
            "title": item["title"],
            "link": item["link"],
            "author": item["author"],
            "category": source.category,
            "source": source.feed_id,
            "published_at": item["published_at"].isoformat(),
        })
        await redis_cache.expire(f"news:item:{nsid}", _ITEM_TTL_SECONDS)
        added += 1

    # 5. ETag/Last-Modified 저장
    if new_etag:
        await redis_cache.set(f"news:etag:{source.feed_id}", new_etag, ex=3600)
    if new_last_modified:
        await redis_cache.set(f"news:last_modified:{source.feed_id}", new_last_modified, ex=3600)

    if added > 0:
        logger.info("뉴스 추가", extra={
            "source": source.feed_id,
            "added": added,
            "filtered_total": len(filtered),
        })
```

### 5.3 조건부 GET 구현

```python
import asyncio
import requests  # 동기 — asyncio.to_thread로 감싸서 사용

_REQUEST_TIMEOUT = 10

async def _conditional_get(source: NewsSource) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """ETag/Last-Modified 기반 조건부 GET. 변경 없으면 (None, None, None) 반환."""

    headers = {
        "User-Agent": "FXi-NewsBot/1.0",
        "Accept": "application/xml, text/xml",
    }

    # 저장된 ETag / Last-Modified 가져오기
    saved_etag = await redis_cache.get(f"news:etag:{source.feed_id}")
    saved_last_modified = await redis_cache.get(f"news:last_modified:{source.feed_id}")

    if saved_etag:
        headers["If-None-Match"] = saved_etag
    if saved_last_modified:
        headers["If-Modified-Since"] = saved_last_modified

    def _do_get():
        return requests.get(source.url, headers=headers, timeout=_REQUEST_TIMEOUT)

    response = await asyncio.to_thread(_do_get)

    if response.status_code == 304:
        return None, None, None  # 변경 없음

    response.raise_for_status()

    new_etag = response.headers.get("ETag")
    new_last_modified = response.headers.get("Last-Modified")

    return response.text, new_etag, new_last_modified
```

### 5.4 XML 파싱

```python
def _parse_rss(xml_text: str) -> List[dict]:
    """RSS XML → item 리스트 파싱"""
    root = ET.fromstring(xml_text)
    items = []

    for item_el in root.findall(".//item"):
        nsid = _get_text(item_el, "nsid")
        title = _get_text(item_el, "title")
        link = _get_text(item_el, "link")
        author = _get_text(item_el, "author")
        pub_date_str = _get_text(item_el, "pubDate")

        if not nsid or not title or not pub_date_str:
            continue

        # pubDate 파싱: "2026-03-27 19:36:10" (KST, 타임존 없음)
        published_at = datetime.strptime(pub_date_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)

        items.append({
            "nsid": nsid,
            "title": title,
            "link": link or "",
            "author": author or "",
            "published_at": published_at,
        })

    return items


def _get_text(element, tag: str) -> Optional[str]:
    """XML 엘리먼트에서 텍스트 추출"""
    child = element.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return None
```

---

## 6. Redis 클라이언트 확장

### 6.1 app/cache.py에 추가할 메서드

현재 `RedisCache`에는 `get`, `set`, `hget`, `hset` 등이 있지만,
뉴스 기능에 필요한 메서드가 몇 개 부족하다.

> **중요: `decode_responses=False` (cache.py line 98)**
>
> Redis 클라이언트가 `decode_responses=False`로 연결되어 있으므로,
> 모든 반환값이 `bytes`이다. 기존 메서드는 `_decode()`로 처리하고 있고,
> 새 메서드도 반드시 동일하게 디코딩해야 한다.
>
> - `zrangebyscore`, `zrevrangebyscore`: member가 `bytes` → `str` 디코딩 필요
> - `hgetall`: key/value 모두 `bytes` → `str` 디코딩 필요
>
> 디코딩하지 않으면:
> - `f"news:item:{nsid}"` → `"news:item:b'AKR...'"` (잘못된 키)
> - `item.get("category")` → `None` (키가 `b"category"`)

```python
# 추가 필요 메서드 (app/cache.py의 RedisCache 클래스에)

async def exists(self, key: str) -> bool:
    """키 존재 여부 확인"""

async def zadd(self, key: str, mapping: dict) -> Optional[int]:
    """Sorted Set에 추가 — mapping: {member: score}"""

async def zrangebyscore(self, key: str, min_score, max_score) -> list:
    """Sorted Set에서 score 범위로 조회. 반환값: List[str] (bytes → str 디코딩)"""

async def zrevrangebyscore(self, key: str, max_score, min_score,
                           start: int = 0, num: int = -1) -> list:
    """Sorted Set에서 score 역순 조회 (최신순). 반환값: List[str] (bytes → str 디코딩)"""

async def zremrangebyscore(self, key: str, min_score, max_score) -> Optional[int]:
    """Sorted Set에서 score 범위로 제거"""

async def hset_dict(self, key: str, mapping: dict) -> None:
    """Hash에 여러 필드를 한번에 설정"""

async def hgetall(self, key: str) -> Optional[dict]:
    """Hash의 모든 필드 조회. 반환값: Dict[str, str] (key/value bytes → str 디코딩)"""

async def expire(self, key: str, seconds: int) -> None:
    """키에 TTL 설정"""

async def delete(self, *keys: str) -> None:
    """키 삭제 (variadic — 여러 키를 한 번에 삭제, round trip 1회)"""
```

**구현 패턴**: 기존 `get`/`set` (cache.py line 114~134) 패턴을 그대로 따른다.

```python
# 예시 1: zadd (반환값 디코딩 불필요 — int)
async def zadd(self, key: str, mapping: dict) -> Optional[int]:
    if not self.client or not await self.circuit.can_attempt():
        return None
    try:
        result = await self.client.zadd(key, mapping)
        await self.circuit.record_success()
        return result
    except Exception:
        await self.circuit.record_failure()
        logger.debug("Redis ZADD 실패", exc_info=True, extra={"key": key})
        return None

# 예시 2: zrevrangebyscore (반환값 bytes → str 디코딩 필수)
async def zrevrangebyscore(self, key: str, max_score, min_score,
                           start: int = 0, num: int = -1) -> list:
    if not self.client or not await self.circuit.can_attempt():
        return []
    try:
        result = await self.client.zrevrangebyscore(
            key, max_score, min_score, start=start, num=num
        )
        await self.circuit.record_success()
        # bytes → str 디코딩 (decode_responses=False)
        return [v.decode("utf-8") if isinstance(v, bytes) else str(v) for v in result]
    except Exception:
        await self.circuit.record_failure()
        logger.debug("Redis ZREVRANGEBYSCORE 실패", exc_info=True, extra={"key": key})
        return []

# 예시 3: hgetall (key/value 모두 bytes → str 디코딩 필수)
async def hgetall(self, key: str) -> Optional[dict]:
    if not self.client or not await self.circuit.can_attempt():
        return None
    try:
        result = await self.client.hgetall(key)
        await self.circuit.record_success()
        if not result:
            return None
        # key/value 모두 bytes → str 디코딩
        return {
            (k.decode("utf-8") if isinstance(k, bytes) else str(k)):
            (v.decode("utf-8") if isinstance(v, bytes) else str(v))
            for k, v in result.items()
        }
    except Exception:
        await self.circuit.record_failure()
        logger.debug("Redis HGETALL 실패", exc_info=True, extra={"key": key})
        return None
```

> **주의**: `self._client`가 아니라 `self.client`, `self._circuit`가 아니라 `self.circuit`.
> `record_success()`와 `record_failure()`는 `await` 필요 (async 메서드).
> 실패 시 로그 레벨은 `debug` (기존 패턴과 동일).
> **반환값이 bytes인 메서드는 반드시 디코딩**한다 (`zrangebyscore`, `zrevrangebyscore`, `hgetall`).

---

## 7. 스케줄러 연동

### 7.1 scheduler.py 수정

```python
# app/scheduler.py — 뉴스 수집 job 추가

from app.news.fetcher import fetch_all_news

def _add_news_jobs():
    """뉴스 수집 cron job 등록 — 모드 무관 (항상 실행)"""
    scheduler.add_job(
        fetch_all_news,
        CronTrigger(minute="*/5", second="45", timezone=KST),  # 5분마다, :45초
        id="news_fetcher",
        replace_existing=True,
    )
```

### 7.2 모드 전환과의 관계

- 뉴스 수집은 **모든 모드에서 동일하게 실행** (IN/BREAK1/BREAK2/OUT 무관)
- `switch_jobs()`에서 제거/재등록하지 않음
- `start_scheduler()` 또는 `_add_common_jobs()`에서 한 번 등록

### 7.3 타이밍

- **`:45`초에 실행**: 브로드캐스트(`:00`)와 충돌 방지, 기존 크롤러 타이밍과 분산
- 이 프로젝트는 모든 job이 초 단위까지 명시적으로 분산되어 있으므로 반드시 second 지정
- 실행 시간: 4개 RSS GET + 파싱 + Redis 저장 = ~2-3초 예상

---

## 8. Pydantic 스키마

이 프로젝트의 모든 공개 API는 `response_model=`을 사용한다 (schemas.py 참고).
뉴스 API도 동일한 패턴을 따른다.

### 8.1 app/schemas.py에 추가

```python
# ============================================================
# News: 뉴스 피드 스키마
# ============================================================

class NewsItem(BaseModel):
    """개별 뉴스 아이템"""
    id: str
    title: str
    link: Optional[str] = None       # 원문 URL (external_link일 때)
    source: str                      # "einfomax", "fxi", ...
    content_type: str = "external_link"  # "external_link" | "direct_text"
    published_at: str                # ISO 8601
    body: Optional[str] = None       # 뉴스 본문 (direct_text일 때, 향후 예정)

class NewsMetadata(BaseModel):
    """뉴스 API 메타데이터"""
    returned_count: int  # 이번 응답에 포함된 기사 수
    window_hours: float
    responded_at: str      # ISO 8601

class NewsResponse(BaseModel):
    """뉴스 API 응답"""
    news: List[NewsItem]
    metadata: NewsMetadata
```

---

## 9. API 엔드포인트

### 9.1 스펙

```
GET /api/news
GET /api/news?category=forex,global
GET /api/news?limit=20
GET /api/news?hours=4
```

| 파라미터 | 타입 | 기본값 | 설명 |
|---------|------|--------|------|
| category | string (comma-separated) | 전체 | 카테고리 필터 |
| limit | int | 30 | 최대 반환 건수 (1~100) |
| hours | float | 8 | 시간 윈도우 (0.5~24) |

### 9.2 응답 형식

```json
{
  "news": [
    {
      "id": "AKR20260327150100016",
      "title": "달러-원, 런던장에서 1,500원대 등락…미·이란 협상 초점",
      "link": "https://news.einfomax.co.kr/news/articleView.html?idxno=4406427",
      "source": "einfomax",
      "content_type": "external_link",
      "published_at": "2026-03-27T19:36:10+09:00"
    }
  ],
  "metadata": {
    "returned_count": 15,
    "window_hours": 8,
    "responded_at": "2026-03-27T20:55:00+09:00"
  }
}
```

> `returned_count`: 이번 응답에 포함된 기사 수 (ZSET 전체 수가 아님)
> `content_type`: `"external_link"` (RSS) 또는 `"direct_text"` (향후 예정, 미구현)
> `body`: `direct_text`일 때만 포함 (RSS 뉴스에서는 없음)

### 9.3 main.py 구현

```python
@app.get("/api/news", response_model=schemas.NewsResponse)
async def get_news(
    category: Optional[str] = None,
    limit: int = 30,
    hours: float = 8.0,
):
    limit = max(1, min(100, limit))
    hours = max(0.5, min(24.0, hours))

    cutoff_ts = time.time() - (hours * 3600)

    # 1. ZSET에서 최신순으로 nsid 목록 조회
    nsids = await redis_cache.zrevrangebyscore(
        "news:index", "+inf", cutoff_ts, start=0, num=limit * 2  # 여유분 (카테고리 필터 고려)
    )

    if not nsids:
        return {"news": [], "metadata": {"returned_count": 0, "window_hours": hours, "responded_at": _now_iso()}}

    # 2. 각 nsid의 HASH 조회
    categories = {c.strip() for c in category.split(",") if c.strip()} if category else None
    news_items = []

    for nsid in nsids:
        if len(news_items) >= limit:
            break

        item = await redis_cache.hgetall(f"news:item:{nsid}")
        if not item:
            continue  # stale index entry (HASH가 TTL로 먼저 만료됨)

        if categories and item.get("category") not in categories:
            continue

        news_items.append({
            "id": nsid,
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "author": item.get("author", ""),
            "category": item.get("category", ""),
            "source": item.get("source", ""),
            "published_at": item.get("published_at", ""),
        })

    return {
        "news": news_items,
        "metadata": {
            "returned_count": len(news_items),
            "window_hours": hours,
            "responded_at": _now_iso(),
        }
    }
```

---

## 10. 에러 핸들링

### 10.1 수집 실패 시

| 상황 | 처리 |
|------|------|
| 네트워크 타임아웃 | 로그 + 다음 소스 계속 (5분 뒤 재시도) |
| HTTP 4xx/5xx | 로그 + 다음 소스 계속 |
| XML 파싱 실패 | 로그 + 해당 소스 스킵 |
| Redis 장애 | Circuit Breaker 동작, API는 빈 배열 반환 |

### 10.2 Redis 장애 시 API 동작

```python
# Redis 불가 시 → 빈 응답 (뉴스는 critical하지 않음)
if not nsids:
    return {"news": [], "metadata": {"returned_count": 0, ...}}
```

뉴스는 환율 데이터와 달리 **비핵심 기능**이므로, Redis 장애 시 빈 배열을 반환하고 에러를 내지 않는다.

---

## 11. 로깅

### 11.1 로거 이름

```python
logger = logging.getLogger("exchange_rate.news")
```

### 11.2 주요 로그 메시지

| 레벨 | 메시지 | extra |
|------|--------|-------|
| INFO | "뉴스 추가" | source, added, filtered_total |
| INFO | "뉴스 정리" | removed |
| DEBUG | "뉴스 변경 없음 (304)" | source |
| WARNING | "뉴스 수집 실패" | source, error |
| ERROR | "뉴스 파싱 실패" | source, error |

---

## 12. 의존성

### 12.1 새 패키지

**없음.** 표준 라이브러리 `xml.etree.ElementTree`로 RSS 파싱, `requests`로 HTTP GET.

### 12.2 기존 패키지 재사용

- `requests` (이미 설치됨)
- `redis.asyncio` (cache.py에서 이미 사용)

---

## 13. 파일 변경 목록

### 신규 파일

| 파일 | 역할 | 예상 줄수 |
|------|------|----------|
| `app/news/__init__.py` | 패키지 초기화 | ~1 |
| `app/news/sources.py` | 소스 정의 (NewsSource + NEWS_SOURCES) | ~25 |
| `app/news/filters.py` | 키워드 필터 (is_forex_relevant) | ~40 |
| `app/news/fetcher.py` | RSS 수집 + 파싱 + Redis 저장 + cleanup | ~160 |

### 수정 파일

| 파일 | 변경 내용 | 예상 변경량 |
|------|----------|-----------|
| `app/cache.py` | ZSET/HASH 관련 메서드 추가 | ~80줄 추가 |
| `app/schemas.py` | NewsItem, NewsMetadata, NewsResponse 추가 | ~20줄 추가 |
| `app/scheduler.py` | `news_fetcher` cron job 등록 | ~10줄 추가 |
| `app/main.py` | `GET /api/news` 엔드포인트 추가 (response_model 포함) | ~50줄 추가 |

---

## 14. 구현 순서

```text
1. app/news/sources.py        — 소스 정의 (가장 단순)
2. app/news/filters.py        — 키워드 필터 (strict 파라미터 포함)
3. app/cache.py 확장           — ZSET/HASH 메서드 추가 (실제 코드 패턴 준수)
4. app/news/fetcher.py        — 핵심 로직 (수집+파싱+upsert+cleanup)
5. app/schemas.py             — NewsItem, NewsMetadata, NewsResponse 추가
6. app/main.py                — API 엔드포인트 (response_model=schemas.NewsResponse)
7. app/scheduler.py           — 5분 cron job 등록 (second="45")
8. 수동 테스트                  — curl로 fetch_all_news 실행 + API 확인
```

---

## 15. 테스트 체크리스트

### 수동 테스트

- [ ] `fetch_all_news()` 1회 실행 → Redis에 기사 저장 확인
- [ ] 동일 피드 재수집 → 304 Not Modified (로그 확인)
- [ ] `GET /api/news` → 기사 목록 반환
- [ ] `GET /api/news?category=forex` → forex만 필터
- [ ] `GET /api/news?hours=2` → 2시간 이내만 반환
- [ ] `GET /api/news?limit=5` → 5개만 반환
- [ ] 보조 소스(S1N21) 기사 중 환율 무관 기사 → 필터링 확인
- [ ] Redis 중지 후 API 호출 → 빈 배열 반환 (에러 없음)
- [ ] 8시간 경과 후 cleanup 동작 확인

---

## 16. 미결 사항 / 토론 포인트

### 16.1 수집 주기 — **결정: 5분**

5분이면 외환 뉴스에 충분히 빠르고, ETag/Last-Modified로 변경 없으면 304 응답이므로 부하 무시 수준.

### 16.2 카테고리 필터 — **결정: 서버에서 제공, 앱은 필요시 추가 필터**

`?category=` 파라미터를 서버가 제공. 앱은 그 위에서 추가 가공 가능.

### 16.3 향후 확장 시 소스 추가 방법

현재 `NEWS_SOURCES` 리스트에 추가하면 끝이지만, 향후 RSS가 아닌 소스 (API, 웹 크롤링 등)가 추가되면 fetcher 인터페이스를 분리해야 할 수 있다.

- Phase 1: 현재 구조 유지 (RSS only)
- Phase 2: `BaseFetcher` -> `RSSFetcher`, `APIFetcher` 등으로 분리 (필요 시)

### 16.4 앱 UI에서 카테고리별 색상/아이콘 구분

API 응답의 `category` 필드를 기반으로 앱에서 시각적 구분 가능.
서버에서 추가 작업 불필요하지만, 앱 개발 시 참고.

| category         | 표시명   | 색상 (안) |
| ---------------- | -------- | --------- |
| forex            | 외환     | 파랑      |
| global           | 국제     | 초록      |
| stock_global     | 해외증시 | 보라      |
| stock_domestic   | 국내증시 | 주황      |
