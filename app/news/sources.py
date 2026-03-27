# app/news/sources.py

"""뉴스 소스 정의 — 확장 시 여기에 추가"""

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class NewsSource:
    feed_id: str        # 내부 식별자 (etag 키 등에 사용)
    url: str
    category: str       # forex, global, stock_global, stock_domestic
    filter_level: str   # "noise_only" | "loose" | "strict"


NEWS_SOURCES: List[NewsSource] = [
    NewsSource(
        "einfomax_forex",
        "https://news.einfomax.co.kr/rss/S1N16.xml",
        "forex",
        "noise_only",
    ),
    NewsSource(
        "einfomax_global",
        "https://news.einfomax.co.kr/rss/S1N23.xml",
        "global",
        "loose",
    ),
    NewsSource(
        "einfomax_stock_global",
        "https://news.einfomax.co.kr/rss/S1N21.xml",
        "stock_global",
        "strict",
    ),
    NewsSource(
        "einfomax_stock_domestic",
        "https://news.einfomax.co.kr/rss/S1N2.xml",
        "stock_domestic",
        "strict",
    ),
]
