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


# ── 매크로 이벤트 드라이버 (global 전용 보조 필터) ─────
# 환율에 영향을 주는 지정학/매크로 이벤트 기사 판별
# 이슈 종료 시 GEOPOLITICAL_KEYWORDS만 정리하면 됨

GEOPOLITICAL_KEYWORDS = [
    "이란", "중동", "호르무즈", "전쟁", "휴전", "종전",
]

# 고강도 이벤트 — 지정학 키워드와 결합 시 시장 전파 키워드 없이도 통과
SEVERITY_KEYWORDS = [
    "폭격", "공습", "미사일", "핵", "봉쇄", "침공",
    "격추", "전면전", "확전", "보복", "철수", "대피",
]

TRANSMISSION_KEYWORDS = [
    "유가", "원유", "달러", "환율", "원화", "엔화", "위안",
    "시장", "증시", "위험회피", "안전자산",
    "하락", "반등", "급락", "급등",
]


def classify_macro(title: str) -> str:
    """
    환율에 영향을 주는 지정학/매크로 이벤트 기사 분류.

    반환값:
    - "macro_severity": 지정학 + 고강도 이벤트 (폭격, 봉쇄 등)
    - "macro":          지정학 + 시장 전파 키워드 (유가, 증시 등)
    - "":               미해당
    """
    if not any(kw in title for kw in GEOPOLITICAL_KEYWORDS):
        return ""
    if any(kw in title for kw in SEVERITY_KEYWORDS):
        return "macro_severity"
    if any(kw in title for kw in TRANSMISSION_KEYWORDS):
        return "macro"
    return ""


def is_macro_relevant(title: str) -> bool:
    """하위 호환용 래퍼."""
    return classify_macro(title) != ""
