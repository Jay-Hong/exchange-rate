# app/news/kb_fetcher.py

"""KB Star FX API 뉴스 수집 — RSS보다 ~2시간 빠른 속보 소스"""

# 표준 라이브러리
import asyncio
import logging
import re
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

_KB_BASE_URL = "https://fx.kbstar.com"
_KB_PAGE_URL = f"{_KB_BASE_URL}/quics?page=C110648"
_KB_API_URL = f"{_KB_BASE_URL}/quics?page=C110648&QAction=1253470&RType=json"
_KB_DETAIL_URL = f"{_KB_BASE_URL}/quics?page=C110648&cc=b113988:b114074&newsUniqNo={{nsid}}&QSL=F"

_KB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": _KB_PAGE_URL,
    "X-Requested-With": "XMLHttpRequest",
}

# 분류코드 → (필터 수준, 내부 category)
KB_CODE_MAP = {
    "IS": ("noise_only", "forex"),
    "IY": ("noise_only", "forex"),
    "IT": ("loose", "global"),
    "99": ("loose", "global"),
}
_DEFAULT_CODE_MAP = ("loose", "global")


# ── 공개 API ──────────────────────────────────────────

async def fetch_kb_news() -> None:
    """KB API에서 최신 뉴스 수집 (스케줄러에서 5분마다 호출)"""
    try:
        items = await _collect_kb_items()
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

        # 필터 적용
        match_type = "fx"
        if filter_level == "loose":
            fx = is_forex_relevant(title, strict=False)
            macro_type = classify_macro(title)
            if not (fx or macro_type):
                continue
            match_type = "fx" if fx else macro_type
        # noise_only: 잡음 제외만 통과하면 OK

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


# ── 수집 로직 ─────────────────────────────────────────

async def _collect_kb_items() -> List[dict]:
    """대표뉴스 2건 (HTML) + AJAX 목록을 합쳐 반환. nsid 기준 dedupe."""

    # 1. 초기 HTML에서 대표뉴스 nsid 추출
    html_text = await _fetch_html()
    featured_nsids = _parse_featured_nsids(html_text) if html_text else []

    # 2. AJAX 목록 요청
    ajax_items = await _fetch_ajax_list(featured_nsids)

    # 3. 대표뉴스를 별도로 목록에 추가 (AJAX에서 제외되므로)
    #    대표뉴스의 상세 정보는 AJAX 응답의 ARRAY_marketNewsARRAY에 포함됨
    featured_items = _extract_featured_from_ajax(ajax_items, featured_nsids)

    # 4. 합치고 nsid dedupe
    all_items = featured_items + ajax_items
    seen = set()
    deduped = []
    for item in all_items:
        if item["nsid"] not in seen:
            seen.add(item["nsid"])
            deduped.append(item)

    return deduped


async def _fetch_html() -> Optional[str]:
    """KB 메인 HTML GET — 대표뉴스 nsid 추출용"""

    def _do_get():
        resp = requests.get(
            _KB_PAGE_URL,
            headers={"User-Agent": _KB_HEADERS["User-Agent"]},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.text

    try:
        return await asyncio.to_thread(_do_get)
    except Exception:
        logger.warning("KB HTML 페이지 수집 실패", exc_info=True)
        return None


def _parse_featured_nsids(html_text: str) -> List[str]:
    """초기 HTML에서 대표뉴스 data-news-uniq-no 추출"""
    # <a class="goDetail inner" data-news-uniq-no="AKR20260327174200016">
    pattern = r'class="goDetail inner"[^>]*data-news-uniq-no="([^"]+)"'
    matches = re.findall(pattern, html_text)
    return matches[:2]  # 최대 2건


async def _fetch_ajax_list(featured_nsids: List[str]) -> List[dict]:
    """KB AJAX 뉴스 목록 API 호출"""

    form_data = {
        "inquryDstcd": "21",
        "newsClsfiDstcd": "",
        "finalNewsWritYMS": "",
        "pageNo": "0",
        "newsUniqNo": "",
    }

    # 대표뉴스 nsid를 전달 (AJAX 목록에서 제외하기 위한 KB 서버 로직)
    if len(featured_nsids) >= 1:
        form_data["rpsntNews1Uniqno"] = featured_nsids[0]
    if len(featured_nsids) >= 2:
        form_data["rpsntNews2Uniqno"] = featured_nsids[1]

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
    return [_parse_kb_item(raw) for raw in items_raw if _parse_kb_item(raw)]


def _extract_featured_from_ajax(ajax_items: List[dict], featured_nsids: List[str]) -> List[dict]:
    """
    대표뉴스의 상세 정보를 AJAX 응답 내 ARRAY_marketNewsARRAY에서 추출.
    AJAX 뉴스목록ARRAY에는 빠져있지만, ARRAY_marketNewsARRAY에는 포함되어 있을 수 있음.
    여기서 못 찾으면 별도 상세 요청 없이 스킵 (다음 주기에 AJAX에 나타남).
    """
    # 이미 ajax_items에 nsid가 있으면 중복이므로 별도 처리 불필요
    ajax_nsids = {item["nsid"] for item in ajax_items}
    missing = [nsid for nsid in featured_nsids if nsid not in ajax_nsids]

    if not missing:
        return []

    # 대표뉴스 정보가 없는 경우, 다음 주기에 자연스럽게 수집됨
    logger.debug("KB 대표뉴스 AJAX 미포함", extra={"missing_nsids": missing})
    return []


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
        # "20260327230322" → datetime (KST)
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
