"""KB 뉴스 제목 파싱 회귀 테스트 — [일반] division 접두사 대응 (2026-06-15).

KB API가 모든 제목 앞에 "[일반] " division 접두사를 붙이는 형식 변경 이후,
* (본문없음) / [전문] (보고서) / [인사][부고][표] (노이즈) 판별이 다시 동작하는지 잠근다.
접두사 없는 옛 형식도 하위 호환되는지 함께 검증.
"""

from app.news.filters import is_noise_title
from app.news.kb_fetcher import _parse_kb_item


def _raw(title: str, nsid: str = "AKR20260615000000016") -> dict:
    return {"뉴스고유번호": nsid, "뉴스제목내용": title, "뉴스작성일시": "20260615120000"}


# ── division 접두사 제거 (새 형식) ─────────────────────────

def test_division_prefix_stripped_plain():
    parsed = _parse_kb_item(_raw("[일반] 케빈 워시, 이번주 첫 타석"), "forex")
    assert parsed["title"] == "케빈 워시, 이번주 첫 타석"
    assert parsed["is_bodyless"] is False
    assert parsed["is_report"] is False


def test_division_prefix_with_content_tag_preserved():
    # [외환] 같은 콘텐츠 태그는 보존되어야 함 (division만 제거)
    parsed = _parse_kb_item(_raw("[일반] [외환] 종전 합의에 급락 출발"), "forex")
    assert parsed["title"] == "[외환] 종전 합의에 급락 출발"


# ── * 본문없음 (새 형식) ──────────────────────────────────

def test_bodyless_after_division_prefix():
    parsed = _parse_kb_item(_raw("[일반] *달러-원, 위험선호 회복에 한때 1,504원까지 급락"), "forex")
    assert parsed["is_bodyless"] is True
    assert parsed["title"] == "달러-원, 위험선호 회복에 한때 1,504원까지 급락"


# ── [전문] 보고서 (새 형식) ───────────────────────────────

def test_report_after_division_prefix():
    parsed = _parse_kb_item(_raw("[일반] [전문] MUFG 달러-원 전망 보고서"), "forex")
    assert parsed["is_report"] is True
    assert parsed["title"] == "MUFG 달러-원 전망 보고서"


# ── 노이즈 prefix (새 형식) — 파싱 후 is_noise_title 통과 ──

def test_noise_bugo_after_division_prefix():
    parsed = _parse_kb_item(_raw("[일반] [부고] 유제상(생명보험협회)씨 모친상"), "global")
    assert parsed["title"] == "[부고] 유제상(생명보험협회)씨 모친상"
    assert is_noise_title(parsed["title"]) is True


def test_noise_table_after_division_prefix():
    parsed = _parse_kb_item(_raw("[일반] [표] 은행간 단기금리 기준-코리보(15일)"), "global")
    assert is_noise_title(parsed["title"]) is True


def test_noise_insa_after_division_prefix():
    parsed = _parse_kb_item(_raw("[일반] [인사] 한국은행"), "global")
    assert is_noise_title(parsed["title"]) is True


# ── 하위 호환 (옛 형식, 접두사 없음) ──────────────────────

def test_legacy_no_prefix_bodyless():
    parsed = _parse_kb_item(_raw("*달러-원 급락"), "forex")
    assert parsed["is_bodyless"] is True
    assert parsed["title"] == "달러-원 급락"


def test_legacy_no_prefix_report():
    parsed = _parse_kb_item(_raw("[전문] 보고서"), "forex")
    assert parsed["is_report"] is True
    assert parsed["title"] == "보고서"


def test_legacy_no_prefix_noise():
    parsed = _parse_kb_item(_raw("[부고] 모친상"), "global")
    assert is_noise_title(parsed["title"]) is True


# ── collapse 전제조건: 파싱된 KB 제목이 RSS 제목과 동일 그룹 key를 갖는지 ──
# 주의: 이 테스트는 _collapse_near_duplicates()를 호출하지 않는다 (collapse 로직은
# 이번 변경 대상이 아니다). 파서가 [일반]을 제거해 KB 제목이 RSS와 일치해야,
# 서로 다른 nsid 초보/상보가 같은 normalize_title 그룹 key로 묶일 수 있다는
# "전제조건"만 보장한다.

def test_kb_parsed_title_equals_rss_title():
    kb = _parse_kb_item(_raw("[일반] 달러-원, 美·이란 종전에 8.40원 하락 출발"), "forex")
    rss_title = "달러-원, 美·이란 종전에 8.40원 하락 출발"
    assert kb["title"] == rss_title
