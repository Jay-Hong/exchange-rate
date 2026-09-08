"""IBK Selenium 수확값의 **관측 전용** 판정. 저장·폴백을 바꾸지 않는다.

Selenium 경로에는 공식 Request 경로가 하는 검증이 없다 — 통화 하나가 실패해도 남은 부분을
저장하고, 날짜를 타이핑만 하고 화면이 그 날짜로 갔는지 확인하지 않는다. 그 강제를 켜기 전에
**거부가 실제로 얼마나 늘어나는지** 알아야 한다. 이 모듈은 그 데이터를 만든다.

⛔ 이 모듈은 판정을 **소비하지 않는다.** shadow 는 여기 결과를 기록만 하고 legacy 동작을
   그대로 둔다. 강제 적용은 별도 커밋이다.

⛔ **parser 통과 ≠ 준비 완료.** `parser_verdict == "accept"` 는 "캡처된 HTML 이 현재 parser
   계약을 통과했다" 는 뜻뿐이다. 요청한 날짜의 표가 실제로 갱신 완료됐다는 증거가 아니다 —
   `page_source` 는 최초 HTTP 응답이 아니라 현재 DOM 직렬화이고, "입력 날짜만 새 날짜로
   바뀌고 표는 이전 조회 결과" 인 중간 상태도 parser 를 통과할 수 있다.
   준비 완료 기준은 이 관측이 모은 데이터를 보고 강제 적용 전에 정한다.

⛔ 실제 Selenium DOM 과 HTTP 응답의 구조 호환성은 **아직 검증되지 않았다.** 리포에 실제
   캡처 fixture 가 없고 기존 IBK 시험의 HTML 은 코드로 조립한 것이다(전수 검색으로 확인).
"""

from dataclasses import dataclass, field
from typing import Any, Mapping


MAPPING_MATCH = "match"
MAPPING_MISMATCH = "mismatch"
MAPPING_INCOMPARABLE = "incomparable"


@dataclass(frozen=True)
class IbkSeleniumObservation:
    """한 번의 Selenium 캡처에서 확인한 사실. **필드마다 독립적으로** 채운다.

    하나의 추출이 실패해도 나머지 관측 사실이 통째로 사라지지 않아야 한다 — 그러면
    "무엇 때문에 못 봤는가" 를 사후에 가릴 수 없다.
    """

    requested_date: str | None = None      # 우리가 요청한 조회일
    input_value_date: str | None = None    # HTML `#inDate` 의 **value 속성**(화면 표시가 아니다)
    completed_at_text: str | None = None   # 표에서 읽은 고시완료시각 문자열(파싱 전 원문)
    observed_pairs: tuple[str, ...] = ()   # 표에서 **통화 코드로** 식별한 통화들
    captured_at: str | None = None         # 캡처 시각(ISO). 사후 분석에서 순서를 보기 위해
    parser_verdict: str | None = None      # "accept" | "reject" | "unavailable"
    parser_reject_reason: str | None = None
    mapping_comparison: str | None = None  # match | mismatch | incomparable
    mapping_diff_pairs: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_log_extra(self) -> dict:
        """구조화 로그로 내보낼 형태. None 도 그대로 남긴다 — 부재가 곧 사실이다."""
        return {
            "ibk_selenium_requested_date": self.requested_date,
            "ibk_selenium_input_value_date": self.input_value_date,
            "ibk_selenium_completed_at_text": self.completed_at_text,
            "ibk_selenium_observed_pairs": list(self.observed_pairs),
            "ibk_selenium_captured_at": self.captured_at,
            "ibk_selenium_parser_verdict": self.parser_verdict,
            "ibk_selenium_parser_reject_reason": self.parser_reject_reason,
            "ibk_selenium_mapping_comparison": self.mapping_comparison,
            "ibk_selenium_mapping_diff_pairs": list(self.mapping_diff_pairs),
            "ibk_selenium_notes": list(self.notes),
        }


def compare_mappings(
    legacy: Mapping[str, Any] | None, validated: Mapping[str, Any] | None
) -> tuple[str, tuple[str, ...]]:
    """legacy 가 저장하려는 mapping 과 검증된 mapping 을 대조한다.

    ⛔ **세 갈래다.** parser 가 후보를 아예 못 냈으면 빈 mapping 으로 취급해 불일치로 세면
       안 된다 — 그러면 "계약 거절" 과 "추출 방식의 차이" 가 섞인다. 그 경우는 비교 불가다.

    ⛔ 불일치를 **"날짜 혼합" 으로 명명하지 않는다.** 통화별로 어느 후보 시도에서 채워졌는지
       (provenance)를 추적하지 않으므로 원인을 단정할 근거가 없다. DOM 갱신 시점 차이일
       수도 있다. 여기서는 차이가 있다는 사실만 기록한다.
    """
    if validated is None or legacy is None:
        return MAPPING_INCOMPARABLE, ()
    diff = tuple(sorted(
        pair for pair in set(legacy) | set(validated)
        if legacy.get(pair) != validated.get(pair)
    ))
    return (MAPPING_MISMATCH, diff) if diff else (MAPPING_MATCH, ())
