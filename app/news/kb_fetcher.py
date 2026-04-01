# app/news/kb_fetcher.py

"""KB Star FX API 뉴스 수집 — RSS보다 ~2시간 빠른 속보 소스

v2: noise_only 일원화. KB_CODE_MAP/match_type 삭제.
    * 기사: 제목에 " (본문없음)" 추가 + link 유지.
    [전문] 기사: PDF 직링크 추출 유지.
"""

# 표준 라이브러리
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

# 서드파티 라이브러리
import requests

# 로컬 애플리케이션
from app.news.filters import is_noise_title
from app.news.upsert import upsert_news_item

logger = logging.getLogger("exchange_rate.news.kb")

KST = timezone(timedelta(hours=9))
NEWS_WINDOW_HOURS = 24
_REQUEST_TIMEOUT = 15
_KB_PAGES_PER_TAB = 2

_KB_API_URL = "https://fx.kbstar.com/quics?page=C110648&QAction=1253470&RType=json"
_KB_PAGE_URL = "https://fx.kbstar.com/quics?page=C110648"
_KB_DETAIL_URL = "https://fx.kbstar.com/quics?page=C110648&cc=b113988:b114074&newsUniqNo={nsid}&QSL=F"

_KB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": _KB_PAGE_URL,
    "X-Requested-With": "XMLHttpRequest",
}

# 탭 → 내부 category (디버깅용)
_KB_TAB_CATEGORY = {"21": "forex", "22": "global"}
_KB_TABS = list(_KB_TAB_CATEGORY.keys())

# [전문] 보고서 PDF URL 패턴
_REPORT_PDF_PATTERN = re.compile(
    r'https?://rreport\.einfomax\.co\.kr/report/[^\s"<>]+\.pdf'
)


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

        # [전문] 보고서: PDF 직링크 추출
        if item["is_report"]:
            pdf_url = await _extract_report_pdf(item["nsid"])
            if pdf_url:
                stored = await upsert_news_item(
                    nsid=item["nsid"],
                    title=title,
                    link=pdf_url,
                    category=item["category"],
                    published_at=published_at,
                    ingested_via="kb_api",
                    content_type="report_pdf",
                )
            else:
                logger.warning("보고서 PDF 추출 실패", extra={"nsid": item["nsid"]})
                link = _KB_DETAIL_URL.format(nsid=item["nsid"])
                stored = await upsert_news_item(
                    nsid=item["nsid"],
                    title=title,
                    link=link,
                    category=item["category"],
                    published_at=published_at,
                    ingested_via="kb_api",
                )
        # * 기사 (본문 없음): 제목에 "(본문없음)" 추가, link 유지
        elif item["is_bodyless"]:
            link = _KB_DETAIL_URL.format(nsid=item["nsid"])
            stored = await upsert_news_item(
                nsid=item["nsid"],
                title=f"{title} (본문없음)",
                link=link,
                category=item["category"],
                published_at=published_at,
                ingested_via="kb_api",
                is_bodyless=True,
            )
        # 일반 기사
        else:
            link = _KB_DETAIL_URL.format(nsid=item["nsid"])
            stored = await upsert_news_item(
                nsid=item["nsid"],
                title=title,
                link=link,
                category=item["category"],
                published_at=published_at,
                ingested_via="kb_api",
            )

        if stored:
            added += 1

    if added > 0:
        logger.info("KB 뉴스 추가", extra={"added": added, "total_collected": len(items)})


# ── [전문] 보고서 PDF 추출 ────────────────────────────

async def _extract_report_pdf(nsid: str) -> Optional[str]:
    detail_url = _KB_DETAIL_URL.format(nsid=nsid)

    def _do_get():
        resp = requests.get(
            detail_url,
            headers={"User-Agent": _KB_HEADERS["User-Agent"]},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.text

    try:
        html = await asyncio.to_thread(_do_get)
    except Exception:
        logger.warning("보고서 상세 페이지 수집 실패", exc_info=True, extra={"nsid": nsid})
        return None

    content_match = re.search(r'id="content"[^>]*>(.*?)</div>', html, re.DOTALL)
    if not content_match:
        return None

    pdf_matches = _REPORT_PDF_PATTERN.findall(content_match.group(1))
    return pdf_matches[0] if len(pdf_matches) == 1 else None


# ── 수집 ──────────────────────────────────────────────

async def _fetch_kb_all_tabs() -> List[dict]:
    all_items = []
    seen_nsids: set = set()

    for tab in _KB_TABS:
        category = _KB_TAB_CATEGORY[tab]
        try:
            tab_items = await _fetch_kb_tab_pages(tab, category)
            for item in tab_items:
                if item["nsid"] not in seen_nsids:
                    seen_nsids.add(item["nsid"])
                    all_items.append(item)
        except Exception:
            logger.warning("KB 탭 수집 실패", exc_info=True, extra={"tab": tab})

    return all_items


async def _fetch_kb_tab_pages(inqury_dstcd: str, category: str) -> List[dict]:
    all_items = []
    cursor = ""

    for page_no in range(_KB_PAGES_PER_TAB):
        form_data = {
            "inquryDstcd": inqury_dstcd,
            "newsClsfiDstcd": "",
            "finalNewsWritYMS": cursor,
            "pageNo": str(page_no),
            "newsUniqNo": "",
        }

        def _do_post(fd=form_data):
            resp = requests.post(
                _KB_API_URL, headers=_KB_HEADERS, data=fd, timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()

        data = await asyncio.to_thread(_do_post)
        servicedata = data.get("msg", {}).get("servicedata", {})

        items_raw = servicedata.get("뉴스목록ARRAY", [])
        if not items_raw:
            break

        for raw in items_raw:
            parsed = _parse_kb_item(raw, category)
            if parsed:
                all_items.append(parsed)

        next_cursor = servicedata.get("다음최종뉴스작성일시", "")
        if not next_cursor:
            break
        cursor = next_cursor

    return all_items


# ── 파싱 ──────────────────────────────────────────────

def _parse_kb_item(raw: dict, category: str) -> Optional[dict]:
    nsid = raw.get("뉴스고유번호", "").strip()
    title = raw.get("뉴스제목내용", "").strip()
    write_dt_str = raw.get("뉴스작성일시", "").strip()

    if not nsid or not title or not write_dt_str:
        return None

    try:
        published_at = datetime.strptime(write_dt_str, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return None

    # * 접두사 → 본문 없음
    is_bodyless = title.startswith("*")
    if is_bodyless:
        title = title.lstrip("*").strip()

    # [전문] 접두사 → 은행 보고서
    is_report = title.startswith("[전문]")
    if is_report:
        title = title.removeprefix("[전문]").strip()

    return {
        "nsid": nsid,
        "title": title,
        "published_at": published_at,
        "category": category,
        "is_bodyless": is_bodyless,
        "is_report": is_report,
    }
