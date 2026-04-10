# app/news/filters.py

"""뉴스 필터 — 잡음 제외 (title 기준)

v2: noise_only 일원화. 관련도/매크로/산업 필터 삭제.
"""

import re as _re


# ── 잡음 제외 ─────────────────────────────────────────

EXCLUDE_TITLE_PREFIXES = [
    "[인사]",
    "[부고]",
    "[게시판]",
    "[표]",  # 데이터 테이블 덤프 (시장 키워드 보호 없이 무조건 제외)
]

EXCLUDE_TITLE_KEYWORDS = [
    # 인사/직책 이동
    "신임", "선임", "임명", "전보", "승진", "이동",
    "내정", "취임", "후임", "보임", "영입", "합류", "사임", "퇴임",
    # 정당/선거 (시장 키워드 없으면 제외)
    "국민의힘", "국힘", "더불어민주당", "민주당",
    "조국혁신당", "개혁신당",
    "당대표", "원내대표", "총선", "대선", "선거",
    # 정치인 이름 (시장 키워드 없으면 제외, 운영하면서 갱신)
    "장동혁", "정청래", "오세훈", "정원오",
    "추미애", "김부겸", "이진숙",
]

# 직책 이동 패턴 (직책+로)
POSITION_TRANSFER_PATTERNS = [
    "CEO로", "CFO로", "CRO로", "CIO로", "COO로", "CTO로",
    "대표로", "원장으로", "사장으로", "회장으로", "부행장으로",
]

# 시장 키워드 보호 — 위 제외 규칙을 무력화
STRONG_MARKET_KEYWORDS = [
    "환율", "외환", "달러-원", "달러-엔", "달러", "엔화",
    "위안", "유로", "DXY", "환시", "환위험", "환헤지",
    "금리", "증시",
    "국제유가", "WTI", "브렌트유",
]

# "유가"는 "유가증권" 오탐 방지를 위해 경계 매칭
_YUGA_PATTERN = _re.compile(r'(?:^|[\s\[,.:])유가(?:[\s\],.:!?]|$)')


def _has_strong_market_keyword(title: str) -> bool:
    """강한 시장 키워드 존재 여부 (잡음 필터 보호용)"""
    if any(kw in title for kw in STRONG_MARKET_KEYWORDS):
        return True
    if _YUGA_PATTERN.search(title):
        return True
    return False


def is_noise_title(title: str) -> bool:
    """인사/부고/정치 등 잡음 기사 판단. True면 제외."""
    # 접두사 매칭 → 즉시 제외
    if any(title.startswith(prefix) for prefix in EXCLUDE_TITLE_PREFIXES):
        return True

    # 시장 키워드가 있으면 인사/정치성이어도 살림
    if _has_strong_market_keyword(title):
        return False

    # 인사/정치 키워드 매칭
    if any(kw in title for kw in EXCLUDE_TITLE_KEYWORDS):
        return True

    # 직책 이동 패턴 매칭
    if any(p in title for p in POSITION_TRANSFER_PATTERNS):
        return True

    return False


# ── 제목 정규화 (near-duplicate collapse용) ────────────

_TAIL_PATTERN = _re.compile(r'\s*\((상보|종합|속보|수정|1보|2보|3보|본문없음)\)\s*$')


def normalize_title(title: str) -> str:
    """제목 정규화 — 꼬리표/본문없음 제거, HTML 엔티티 디코딩, 공백 정리"""
    t = title.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = _TAIL_PATTERN.sub("", t)
    return " ".join(t.split())
