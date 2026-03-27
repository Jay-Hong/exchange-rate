# app/news/fetcher.py

"""RSS 뉴스 수집 → 필터 → Redis 저장 + cleanup"""

# 표준 라이브러리
import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

# 서드파티 라이브러리
import requests

# 로컬 애플리케이션
from app.cache import redis_cache
from app.news.filters import is_forex_relevant, is_macro_relevant, is_noise_title
from app.news.sources import NEWS_SOURCES, NewsSource

logger = logging.getLogger("exchange_rate.news")

KST = timezone(timedelta(hours=9))
NEWS_WINDOW_HOURS = 8
_ITEM_TTL_SECONDS = 24 * 3600  # 안전망 TTL
_REQUEST_TIMEOUT = 10


# ── 공개 API ──────────────────────────────────────────

async def fetch_all_news() -> None:
    """모든 소스에서 뉴스 수집 (스케줄러에서 5분마다 호출)"""
    for source in NEWS_SOURCES:
        try:
            await _fetch_single_source(source)
        except Exception:
            logger.exception("뉴스 수집 실패", extra={"source": source.feed_id})

    await _cleanup_old_news()


# ── 단일 소스 수집 ────────────────────────────────────

async def _fetch_single_source(source: NewsSource) -> None:
    """단일 RSS 피드 수집"""

    # 1. 조건부 GET (ETag / Last-Modified)
    xml_text, new_etag, new_last_modified = await _conditional_get(source)
    if xml_text is None:
        logger.debug("뉴스 변경 없음 (304)", extra={"source": source.feed_id})
        return

    # 2. XML 파싱
    items = _parse_rss(xml_text)

    # 3. 필터링 (8h → 잡음 제외 → 소스별 환율 관련도)
    cutoff = datetime.now(KST) - timedelta(hours=NEWS_WINDOW_HOURS)
    filtered = []
    for item in items:
        title = item["title"]
        if item["published_at"] < cutoff:
            continue
        if is_noise_title(title):
            continue
        if source.filter_level == "loose":
            if not (is_forex_relevant(title, strict=False) or is_macro_relevant(title)):
                continue
        if source.filter_level == "strict" and not is_forex_relevant(title, strict=True):
            continue
        filtered.append(item)

    # 4. Redis 저장 (항상 upsert — 기사 수정 대응)
    added = 0
    for item in filtered:
        nsid = item["nsid"]
        published_ts = item["published_at"].timestamp()

        await redis_cache.zadd("news:index", {nsid: published_ts})
        await redis_cache.hset_dict(f"news:item:{nsid}", {
            "title": item["title"],
            "link": item["link"],
            "category": source.category,
            "source": "einfomax",
            "content_type": "external_link",
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


# ── 조건부 GET ────────────────────────────────────────

async def _conditional_get(source: NewsSource) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """ETag/Last-Modified 기반 조건부 GET. 변경 없으면 (None, None, None) 반환."""

    headers = {
        "User-Agent": "FXi-NewsBot/1.0",
        "Accept": "application/xml, text/xml",
    }

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
        return None, None, None

    response.raise_for_status()

    new_etag = response.headers.get("ETag")
    new_last_modified = response.headers.get("Last-Modified")

    return response.text, new_etag, new_last_modified


# ── XML 파싱 ──────────────────────────────────────────

def _parse_rss(xml_text: str) -> List[dict]:
    """RSS XML → item 리스트 파싱"""
    root = ET.fromstring(xml_text)
    items = []

    for item_el in root.findall(".//item"):
        nsid = _get_text(item_el, "nsid")
        title = _get_text(item_el, "title")
        link = _get_text(item_el, "link")
        pub_date_str = _get_text(item_el, "pubDate")

        if not nsid or not title or not pub_date_str:
            continue

        # pubDate: "2026-03-27 19:36:10" (KST, 타임존 없음)
        published_at = datetime.strptime(pub_date_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)

        items.append({
            "nsid": nsid,
            "title": title,
            "link": link or "",
            "published_at": published_at,
        })

    return items


def _get_text(element, tag: str) -> Optional[str]:
    """XML 엘리먼트에서 텍스트 추출"""
    child = element.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return None


# ── Cleanup ───────────────────────────────────────────

async def _cleanup_old_news() -> None:
    """8시간 이전 기사 제거 — fetch 직후 호출"""
    cutoff = time.time() - (NEWS_WINDOW_HOURS * 3600)

    stale_ids = await redis_cache.zrangebyscore("news:index", "-inf", cutoff)
    if not stale_ids:
        return

    await redis_cache.zremrangebyscore("news:index", "-inf", cutoff)

    keys = [f"news:item:{nsid}" for nsid in stale_ids]
    await redis_cache.delete(*keys)

    logger.info("뉴스 정리", extra={"removed": len(stale_ids)})
