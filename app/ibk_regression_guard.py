"""IBK 과거 서비스 기준일 회귀 판정. 기존 인라인 루프를 그대로 옮긴 순수 함수다.

DB를 조회하지 않고 HTTP도 보내지 않으며, 로그도 outcome도 만들지 않는다.
`app.crawlers`나 결과 builder를 import하지 않는다. 활성화 조건, DB 조회 시점·횟수,
저장 대상 필터, 로그 필드, 전부 제외 시 반환은 모두 호출부에 그대로 남는다.

규칙은 추출 시점 동작을 보존한다. 같은 값 판정이 timestamp 처리보다 앞서고,
경계와 같으면 회귀가 아니며 초과일 때만 회귀다. 입력 객체는 바꾸지 않는다.
"""

from __future__ import annotations

import datetime
from typing import Any, Mapping


def find_regressing_pairs(
    current_rates: Mapping[str, Any],
    last_info: Mapping[str, Mapping[str, Any]],
    candidate_completion: datetime.datetime,
    save_lag_tolerance_seconds: float,
) -> list[str]:
    """DB 저장분을 과거 스냅샷으로 되돌리게 되는 통화를 돌려준다.

    반환 순서는 ``last_info`` 순회 순서를 따른다. 호출부의 저장 대상 필터와 로그 필드가
    이 순서에 의존하므로 정렬하지 않는다.

    naive timestamp는 UTC로 해석한다. 후보 완료시각과 저장시각이 모두 aware가 되므로
    지역 시간대로 변환해도 비교 결과가 달라지지 않아, 추출본은 변환 없이 직접 비교한다.
    """
    regressing_pairs: list[str] = []
    for pair, info in last_info.items():
        last_rate = info.get("rate")
        if last_rate is None or current_rates.get(pair) == last_rate:
            continue

        last_timestamp = info.get("timestamp")
        if last_timestamp is None:
            regressing_pairs.append(pair)
            continue
        if last_timestamp.tzinfo is None:
            last_timestamp = last_timestamp.replace(tzinfo=datetime.timezone.utc)
        if last_timestamp > candidate_completion + datetime.timedelta(
            seconds=save_lag_tolerance_seconds
        ):
            regressing_pairs.append(pair)
    return regressing_pairs
