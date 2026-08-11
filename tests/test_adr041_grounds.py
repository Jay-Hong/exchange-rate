"""ADR-041 **정본**(`DECISIONS.md`)이 §2.2 의 두 근거를 **R-INV-1 에 붙여서** 싣는지 검사한다.

⛔ 정본은 `DECISIONS.md` 의 ADR-041 구간이다(matrix 24행). 초안 파일을 검사 대상으로 삼으면
   임시 초안이 영구 정본이 되거나 통합 후 이 테스트가 깨진다. 초안은 `/tmp` 에서 쓴다.

⛔ **존재 검사만으로는 부족하다** — RID·핵심어가 문서 어딘가에 흩어져 있기만 해도 통과하면
   `R-INV-1 E-INV-1 E-INV-2 anon 무인증 페이월 LEGACY_RATE_SOURCES 손실` 한 줄이 통과한다(실측).
   그래서 **구조화 주석으로 구간을 긋고 그 안에서** 관계를 본다.

   <!-- rid: R-INV-1 -->
   … 불변식 본문 …
   <!-- evidence: E-INV-1 supports=R-INV-1 -->
   … 인가 근거 …
   <!-- evidence: E-INV-2 supports=R-INV-1 -->
   … 제품 근거 …
   <!-- /rid: R-INV-1 -->

⚠️ ADR 이 통합되기 전까지 이 테스트는 **빨강**이다. 그게 정확한 상태 표시다 —
   `skip` 은 "안 썼다" 를 초록으로 위장하는 fail-open 이다.

배경: archive 78-79 행이 이 실패가 이미 일어났다고 기록한다 — *"`DECISIONS.md` ADR-039 요약은
이 문장에서 `anon` 을 떨어뜨렸다. 요약이 원문보다 강하다"*. 같은 재발을 기계로 막는다.
"""
import pathlib
import re
from collections import Counter

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
DECISIONS = REPO / "DECISIONS.md"

ADR_HEADING = re.compile(r"^(#{2,4})\s*ADR-041\b", re.M)
RID_OPEN = "<!-- rid: R-INV-1 -->"
RID_CLOSE = "<!-- /rid: R-INV-1 -->"
EVIDENCE = re.compile(r"<!--\s*evidence:\s*(E-INV-[12])\s+supports=(R-INV-1)\s*-->")

# §2.2 의 두 근거 — 하나만 남기면 다른 하나가 잊힌다(archive 73행).
GROUNDS = {
    "E-INV-1": ("인가 근거", ["anon", "무인증", "페이월"]),
    "E-INV-2": ("제품 근거", ["LEGACY_RATE_SOURCES", "손실"]),
}


def _adr_section() -> str:
    if not DECISIONS.is_file():
        pytest.fail(f"정본이 없다: {DECISIONS.relative_to(REPO)}")
    text = DECISIONS.read_text()
    headings = list(ADR_HEADING.finditer(text))
    if not headings:
        pytest.fail(
            "DECISIONS.md 에 ADR-041 구간이 없다 — 문서 작성/통합 전이면 이 실패가 정상이다.\n"
            "§2.2 는 두 근거가 R-INV-1 에 붙어 렌더될 때까지 미해소다."
        )
    if len(headings) != 1:
        pytest.fail(f"DECISIONS.md 에 ADR-041 제목이 {len(headings)}개다 — 정본은 하나여야 한다")
    m = headings[0]
    level = re.escape(m.group(1))
    nxt = re.compile(rf"^{level}\s*ADR-(?!041)", re.M).search(text, m.end())
    return text[m.start(): nxt.start() if nxt else len(text)]


def _rid_block() -> str:
    sec = _adr_section()
    if sec.count(RID_OPEN) != 1 or sec.count(RID_CLOSE) != 1:
        pytest.fail(
            "ADR-041 안의 R-INV-1 구간 주석은 여는 표식과 닫는 표식이 정확히 하나씩이어야 한다"
        )
    i, j = sec.find(RID_OPEN), sec.find(RID_CLOSE)
    if j <= i:
        pytest.fail(
            f"ADR-041 안에 R-INV-1 구간 주석이 없다({RID_OPEN} … {RID_CLOSE}).\n"
            "근거를 요구에 **붙였는지** 보려면 구간이 필요하다 — 문서 어딘가에 흩어져 있으면 안 된다."
        )
    return sec[i + len(RID_OPEN): j]


def test_adr041_section_exists():
    assert "R-INV-1" in _adr_section(), "ADR-041 구간에 불변식 요구 R-INV-1 이 없다"


def test_both_grounds_are_bound_to_the_invariant():
    """⛔ 두 evidence 가 **R-INV-1 구간 안에서** supports 로 선언돼야 한다."""
    counts = Counter(rid for rid, _ in EVIDENCE.findall(_rid_block()))
    expected = Counter({rid: 1 for rid in GROUNDS})
    assert counts == expected, (
        f"R-INV-1 구간의 supports 선언이 정확히 하나씩이 아니다: {dict(counts)}\n"
        f"기대: {dict(expected)}"
    )


@pytest.mark.parametrize("rid", sorted(GROUNDS))
def test_each_ground_keeps_its_substance(rid):
    """⛔ RID 만 적고 내용을 지우면 '요약이 근거를 지운' 그 실패와 같다.

    각 evidence 주석 **다음부터 그 다음 주석/구간 끝까지** 안에서 표지를 찾는다.
    """
    block = _rid_block()
    matches = list(EVIDENCE.finditer(block))
    own = next((i for i, match in enumerate(matches) if match.group(1) == rid), None)
    if own is None:
        pytest.fail(f"{rid} supports 선언이 R-INV-1 구간에 없다")
    end = matches[own + 1].start() if own + 1 < len(matches) else len(block)
    body = block[matches[own].end(): end]
    label, markers = GROUNDS[rid]
    missing = [m for m in markers if m not in body]
    assert not missing, f"{rid}({label}) 의 내용 표지가 그 근거 본문에 없다: {missing}"
