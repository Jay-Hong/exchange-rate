# app/news/kb_fetcher.py

"""KB Star FX API 뉴스 수집 — RSS보다 ~2시간 빠른 속보 소스"""

# 표준 라이브러리
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

# 서드파티 라이브러리
import requests

# 로컬 애플리케이션
from app.news.filters import classify_macro, is_forex_relevant, is_noise_title
from app.news.upsert import upsert_news_item

logger = logging.getLogger("exchange_rate.news.kb")

KST = timezone(timedelta(hours=9))
NEWS_WINDOW_HOURS = 8
_REQUEST_TIMEOUT = 15

_KB_API_URL = "https://fx.kbstar.com/quics?page=C110648&QAction=1253470&RType=json"
_KB_PAGE_URL = "https://fx.kbstar.com/quics?page=C110648"
_KB_DETAIL_URL = "https://fx.kbstar.com/quics?page=C110648&cc=b113988:b114074&newsUniqNo={nsid}&QSL=F"

_KB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": _KB_PAGE_URL,
    "X-Requested-With": "XMLHttpRequest",
}

# 분류코드 → (필터 수준, 내부 category)
# 분류코드 → (필터 수준, 내부 category)
# 외환탭(21): IS, IY, IT, 99
# 경제탭(22): IU(증권), IT(채권), 99(기타) 등
KB_CODE_MAP = {
    "IS": ("noise_only", "forex"),
    "IY": ("noise_only", "forex"),
    "IT": ("loose", "global"),
    "IU": ("loose", "global"),
    "99": ("loose", "global"),
}
_DEFAULT_CODE_MAP = ("loose", "global")

# 수집 대상 탭
_KB_TABS = ["21", "22"]  # 21=외환, 22=경제


# ── 공개 API ──────────────────────────────────────────

async def fetch_kb_news() -> None:
    """KB API에서 최신 뉴스 수집 (스케줄러에서 5분마다 호출)"""
    try:
        items = await _fetch_kb_all_tabs()
    except Exception:
        logger.exception("KB API 수집 실패")
        return

    if not items:
        logger.debug("KB API 수집 결과 없음")
        return

    cutoff = datetime.now(KST) - timedelta(hours=NEWS_WINDOW_HOURS)
    added = 0

    for item in items:
        title = item["title"]
        published_at = item["published_at"]

        if published_at < cutoff:
            continue
        if is_noise_title(title):
            continue

        filter_level, category = item["filter_category"]

        match_type = "fx"
        if filter_level == "loose":
            fx = is_forex_relevant(title, strict=False)
            macro_type = classify_macro(title)
            if not (fx or macro_type):
                continue
            match_type = "fx" if fx else macro_type

        link = _KB_DETAIL_URL.format(nsid=item["nsid"])

        stored = await upsert_news_item(
            nsid=item["nsid"],
            title=title,
            link=link,
            category=category,
            match_type=match_type,
            published_at=published_at,
            ingested_via="kb_api",
        )
        if stored:
            added += 1

    if added > 0:
        logger.info("KB 뉴스 추가", extra={"added": added, "total_collected": len(items)})


# ── 수집 ──────────────────────────────────────────────

async def _fetch_kb_all_tabs() -> List[dict]:
    """
    KB AJAX 뉴스 외환탭 + 경제탭 모두 수집, nsid 기준 dedupe.
    rpsntNews 파라미터를 보내지 않으면 대표뉴스도 목록에 포함됨.
    """
    all_items = []
    seen_nsids: set = set()

    for tab in _KB_TABS:
        try:
            tab_items = await _fetch_kb_tab(tab)
            for item in tab_items:
                if item["nsid"] not in seen_nsids:
                    seen_nsids.add(item["nsid"])
                    all_items.append(item)
        except Exception:
            logger.warning("KB 탭 수집 실패", exc_info=True, extra={"tab": tab})

    return all_items


async def _fetch_kb_tab(inqury_dstcd: str) -> List[dict]:
    """단일 탭 AJAX 호출"""

    form_data = {
        "inquryDstcd": inqury_dstcd,
        "newsClsfiDstcd": "",
        "finalNewsWritYMS": "",
        "pageNo": "0",
        "newsUniqNo": "",
    }

    def _do_post():
        resp = requests.post(
            _KB_API_URL,
            headers=_KB_HEADERS,
            data=form_data,
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    data = await asyncio.to_thread(_do_post)

    items_raw = data.get("msg", {}).get("servicedata", {}).get("뉴스목록ARRAY", [])

    items = []
    for raw in items_raw:
        parsed = _parse_kb_item(raw)
        if parsed:
            items.append(parsed)

    return items


# ── 파싱 ──────────────────────────────────────────────

def _parse_kb_item(raw: dict) -> Optional[dict]:
    """KB API 응답 아이템 → 내부 형식 변환"""
    nsid = raw.get("뉴스고유번호", "").strip()
    title = raw.get("뉴스제목내용", "").strip()
    write_dt_str = raw.get("뉴스작성일시", "").strip()
    code = raw.get("뉴스분류구분코드", "").strip()

    if not nsid or not title or not write_dt_str:
        return None

    try:
        published_at = datetime.strptime(write_dt_str, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return None

    filter_level, category = KB_CODE_MAP.get(code, _DEFAULT_CODE_MAP)

    return {
        "nsid": nsid,
        "title": title,
        "published_at": published_at,
        "filter_category": (filter_level, category),
    }
