"""C1a 동작 보존 — 리팩터 전 코드로 만든 특성 기록과 지금 코드의 기록이 같아야 한다.

기준 파일 `tests/fixtures/c1a_pre_refactor_behavior.json` 은 리팩터 전 커밋(`f85eb7d`)의 worktree 에서
`python3 -m tests._c1a_characterize` 로 만들었다(아래 producer blob 이 그 파일들이다). 이후 추출 경로의 로그·관측자 호출·예외·
요청·writer 입력·DB 조회를 **의도적으로** 바꾸면 이 시험이 실패한다 — 그때는 변경을 검토하고 기준 파일을 새 근거와 함께 다시 만든다.
"""

import json
from pathlib import Path

from tests import _c1a_characterize as characterize

FIXTURE = Path(__file__).parent / "fixtures" / "c1a_pre_refactor_behavior.json"
EXPECTED = json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_comes_from_pre_refactor_producer():
    assert EXPECTED["producer"] == {
        "head": "f85eb7dabe5c78e95e41f2e5e479d6e0d9dae77a",
        "blobs": {"app/crawlers/bs.py": "601dfc4a4f07c579c945a09d1129dd2bee981504",
                  "app/crawlers/citi.py": "27591149141a995ea4d422e9a28d768d5ae8df66",
                  "app/crawlers/utils.py": "aa1c379243faf5c9c30c2248155804a50b63a9de"}}
    labels = set(EXPECTED["cases"])
    assert len(labels) == 125
    # 9개 은행의 실제 MIBANK 경계가 모두 들어 있다(공통 함수를 은행 이름만 바꿔 부른 것이 아니다).
    for bank in ("bs", "citi", "hana", "ibk", "kb", "nh", "sc", "shinhan", "woori"):
        assert any(label.startswith(f"bank_{bank}_mibank_") for label in labels), bank


def test_current_behavior_matches_pre_refactor_record():
    current = characterize.characterize()
    assert set(current) == set(EXPECTED["cases"])
    mismatched = [label for label in sorted(current) if current[label] != EXPECTED["cases"][label]]
    assert mismatched == []
