# app/news/sources.py

"""뉴스 RSS 소스 정의 — 확장 시 여기에 추가

v2: filter_level 삭제 (전부 noise_only 일원화).
"""

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class NewsSource:
    feed_id: str        # 내부 식별자 (etag 키 등에 사용)
    url: str
    category: str       # forex, global (내부 디버깅용)


NEWS_SOURCES: List[NewsSource] = [
    NewsSource("einfomax_forex",   "https://news.einfomax.co.kr/rss/S1N16.xml", "forex"),
    NewsSource("einfomax_global",  "https://news.einfomax.co.kr/rss/S1N23.xml", "global"),
    NewsSource("einfomax_global2", "https://news.einfomax.co.kr/rss/S1N21.xml", "global"),
    NewsSource("einfomax_global3", "https://news.einfomax.co.kr/rss/S1N2.xml",  "global"),
]
