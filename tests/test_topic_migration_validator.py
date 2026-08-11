"""표준 스위트에서 **반드시 돌게** 한다 — scripts/ 독립 실행이면 썩는다.

검증기의 신뢰 근거는 본체가 아니라 **반례 테스트**다(2026-08-09: 검증기가 5라운드 연속 구멍).
"""
import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent


def test_mutation_corpus_anchors_are_unique():
    """⛔ 검증기를 고치고 코퍼스를 재동기화하지 않으면 앵커가 **조용히 stale** 이 된다.
    그 상태로 `--mutations` 를 돌리면 그 항목은 주입조차 안 된 채 시간만 쓴다(2026-08-10 두 번 발생).

    ⚠️ 이 검사는 **반례 스위트가 아니라 pytest 에** 있어야 한다 — 스위트에 넣으면 코퍼스가
       사본에 변이를 주입할 때마다 그 앵커가 어긋나 **모든 변이가 엉뚱한 이유로 빨강**이 되고,
       적중 판정이 겨냥한 방어와 무관해진다(공허해진다).
    """
    src = {
        "validator": (REPO / "scripts" / "topic_migration_manifest.py").read_text(),
        "test": (REPO / "scripts" / "test_topic_migration_manifest.py").read_text(),
    }
    corpus = json.loads((REPO / "scripts" / "vacuity_mutations.json").read_text())["mutations"]
    stale = [
        (i, m["name"], src[m["file"]].count(m["old_snippet"]))
        for i, m in enumerate(corpus, 1)
        if src[m["file"]].count(m["old_snippet"]) != 1
    ]
    assert not stale, "코퍼스 앵커가 코드와 어긋났다(검증기 수정 후 재동기화 누락):\n" + "\n".join(
        f"  #{i} count={c} {name}" for i, name, c in stale
    )


def test_migration_validator_is_not_vacuous():
    """대조군 통과 + 전 반례가 **의도한 오류 코드**로 차단되는지."""
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "test_topic_migration_manifest.py")],
        capture_output=True, text=True, cwd=str(REPO),
    )
    assert r.returncode == 0, f"반례 테스트 실패:\n{r.stdout}\n{r.stderr}"
