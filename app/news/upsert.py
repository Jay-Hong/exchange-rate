# app/news/upsert.py

"""뉴스 Redis upsert — KB API + RSS 병합 규칙

v2: match_type 삭제, is_bodyless 추가.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.cache import redis_cache

logger = logging.getLogger("exchange_rate.news")

KST = timezone(timedelta(hours=9))
_ITEM_TTL_SECONDS = 48 * 3600  # 안전망 TTL (윈도우 24h의 2배)


async def upsert_news_item(
    nsid: str,
    title: str,
    link: str,
    category: str,
    published_at: datetime,
    ingested_via: str,
    content_type: str = "external_link",
    is_bodyless: bool = False,
) -> bool:
    """
    nsid 기준 upsert. 병합 규칙:

    1. 새 기사: 그대로 저장
    2. KB→RSS: RSS 유효값으로 정규화, is_bodyless 해제
    3. RSS→KB: 덮어쓰지 않음
    4. RSS有+KB재수집: RSS 보호
    5. KB만+KB재수집: 최신값 갱신
    6. RSS재수집: 갱신

    Returns True if item was newly stored.
    """
    key = f"news:item:{nsid}"
    published_ts = published_at.timestamp()

    existing = await redis_cache.hgetall(key)

    if not existing:
        # ── 1. 새 기사 ──
        fields = {
            "title": title,
            "link": link,
            "category": category,
            "source": "einfomax",
            "content_type": content_type,
            "published_at": published_at.isoformat(),
            "ingested_via": ingested_via,
        }
        if is_bodyless:
            fields["is_bodyless"] = "true"

        await redis_cache.zadd("news:index", {nsid: published_ts})
        await redis_cache.hset_dict(key, fields)
        await redis_cache.expire(key, _ITEM_TTL_SECONDS)
        return True

    # 기존 기사 존재 — 병합
    existing_via = existing.get("ingested_via", "")
    has_rss = "rss" in existing_via
    has_kb = "kb_api" in existing_via
    updates = {}

    is_existing_report = existing.get("content_type") == "report_pdf"

    if ingested_via == "rss" and not has_rss:
        # ── 2. KB→RSS: RSS로 정규화 ──
        # report_pdf는 title/link/content_type 모두 보호 (PDF 직링크가 최선의 UX)
        if is_existing_report:
            updates["ingested_via"] = existing_via + "+rss"
        else:
            if title and title.strip():
                updates["title"] = title
            if link and link.strip():
                updates["link"] = link
            if category and category.strip():
                updates["category"] = category
            if existing.get("is_bodyless") == "true":
                updates["is_bodyless"] = "false"
                updates["content_type"] = "external_link"
            updates["ingested_via"] = existing_via + "+rss"

    elif ingested_via == "kb_api" and not has_kb:
        # ── 3. RSS→KB: 덮어쓰지 않음 ──
        updates["ingested_via"] = existing_via + "+kb_api"

    elif ingested_via == "kb_api" and has_rss:
        # ── 4. RSS有+KB재수집: RSS 보호 ──
        pass

    elif ingested_via == "kb_api" and has_kb and not has_rss:
        # ── 5. KB만+KB재수집: 갱신 ──
        # report_pdf 보호: 재수집에서 PDF 추출 실패해도 기존 PDF 링크 유지
        if is_existing_report and content_type != "report_pdf":
            pass  # report_pdf → external_link 강등 방지
        else:
            if title and title.strip():
                updates["title"] = title
            if link and link.strip():
                updates["link"] = link
            if category and category.strip():
                updates["category"] = category
            if content_type and content_type != existing.get("content_type"):
                updates["content_type"] = content_type
            if is_bodyless:
                updates["is_bodyless"] = "true"
            elif existing.get("is_bodyless") == "true" and not is_bodyless:
                updates["is_bodyless"] = "false"

    elif ingested_via == "rss" and has_rss:
        # ── 6. RSS재수집: 갱신 ──
        if title and title.strip():
            updates["title"] = title
        if link and link.strip():
            updates["link"] = link
        if category and category.strip():
            updates["category"] = category

    # published_at: 모든 경로에서 min()
    existing_ts_str = existing.get("published_at", "")
    if existing_ts_str:
        try:
            existing_dt = datetime.fromisoformat(existing_ts_str)
            if published_at < existing_dt:
                updates["published_at"] = published_at.isoformat()
                await redis_cache.zadd("news:index", {nsid: published_ts})
        except ValueError:
            pass

    if updates:
        await redis_cache.hset_dict(key, updates)

    return False
