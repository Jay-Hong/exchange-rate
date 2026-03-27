# app/news/upsert.py

"""뉴스 Redis upsert — KB API + RSS 병합 규칙 구현"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.cache import redis_cache

logger = logging.getLogger("exchange_rate.news")

KST = timezone(timedelta(hours=9))
_ITEM_TTL_SECONDS = 24 * 3600  # 안전망 TTL


async def upsert_news_item(
    nsid: str,
    title: str,
    link: str,
    category: str,
    match_type: str,
    published_at: datetime,
    ingested_via: str,
) -> bool:
    """
    nsid 기준 upsert. 병합 규칙:

    - 새 기사: 그대로 저장
    - KB 먼저 + RSS 나중: title/link/category를 RSS 유효값으로 정규화,
      match_type은 RSS가 fx이면 승격, published_at은 min()
    - RSS 먼저 + KB 나중: 덮어쓰지 않음 (ingested_via + published_at min()만)
    - 같은 소스 재수집: title/link/category 유효값 갱신, published_at min()

    Returns True if item was newly stored, False if updated/skipped.
    """
    key = f"news:item:{nsid}"
    published_ts = published_at.timestamp()

    existing = await redis_cache.hgetall(key)

    if not existing:
        # 새 기사 — 그대로 저장
        await redis_cache.zadd("news:index", {nsid: published_ts})
        await redis_cache.hset_dict(key, {
            "title": title,
            "link": link,
            "category": category,
            "source": "einfomax",
            "content_type": "external_link",
            "match_type": match_type,
            "published_at": published_at.isoformat(),
            "ingested_via": ingested_via,
        })
        await redis_cache.expire(key, _ITEM_TTL_SECONDS)
        return True

    # 기존 기사 존재 — 병합 규칙 적용
    existing_via = existing.get("ingested_via", "")
    updates = {}

    if ingested_via == "rss" and "rss" not in existing_via:
        # ── KB 먼저 → RSS 나중: RSS로 정규화 ──
        if title and title.strip():
            updates["title"] = title
        if link and link.strip():
            updates["link"] = link
        if category and category.strip():
            updates["category"] = category
        if match_type == "fx":
            updates["match_type"] = "fx"
        updates["ingested_via"] = existing_via + "+rss"

    elif ingested_via == "kb_api" and "kb_api" not in existing_via:
        # ── RSS 먼저 → KB 나중: 덮어쓰지 않음 ──
        updates["ingested_via"] = existing_via + "+kb_api"

    else:
        # ── 같은 소스 재수집: 기사 수정 대응 ──
        if title and title.strip():
            updates["title"] = title
        if link and link.strip():
            updates["link"] = link
        if category and category.strip():
            updates["category"] = category
        if match_type == "fx" and existing.get("match_type") != "fx":
            updates["match_type"] = "fx"

    # published_at: 항상 min(existing, incoming)
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
