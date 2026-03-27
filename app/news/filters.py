# app/news/filters.py

"""뉴스 필터 — 잡음 제외 + 환율 관련도 판단 (title 기준)"""


# ── 잡음 제외 (모든 소스 공통) ──────────────────────────

EXCLUDE_TITLE_PREFIXES = [
    "[인사]",
    "[부고]",
    "[게시판]",
]

EXCLUDE_TITLE_KEYWORDS = [
    "신임", "선임", "임명", "전보", "승진", "이동",
]

STRONG_FOREX_KEYWORDS = [
    "환율", "외환", "달러-원", "달러-엔", "달러", "엔화",
    "위안", "유로", "DXY", "환시", "환위험", "환헤지",
]


def is_noise_title(title: str) -> bool:
    """인사/부고 등 잡음 기사 판단. True면 제외."""
    if any(title.startswith(prefix) for prefix in EXCLUDE_TITLE_PREFIXES):
        return True

    if any(kw in title for kw in EXCLUDE_TITLE_KEYWORDS):
        if not any(fx in title for fx in STRONG_FOREX_KEYWORDS):
            return True

    return False


# ── 환율 관련도 (소스별 적용) ──────────────────────────

PRIMARY_KEYWORDS = [
    "환율", "외환", "달러-원", "달러-엔", "엔화", "유로",
    "위안", "환헤지", "환위험", "DXY", "외환시장",
]

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
