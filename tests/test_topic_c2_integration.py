"""C2 실행기 **실사용** 회귀 잠금 — 임시 clone 에서 진짜 파일을 고쳐 본다.

⛔ **왜 단위 시험만으로 부족한가.** `topic_c2` 는 mock 기반 회귀 29건과 정본 고정점 검증을
전부 통과한 상태로 커밋됐는데, **첫 실사용에서 두 번 막혔다**.

  1. `advance_pin` 이 치환 **뒤** 구 pin 을 다시 찾는데, "0건" 이 `grep` exit 1 이라
     치명 오류로 올라갔다 → 정상 완료가 **항상 실패**했다(그것도 파일을 이미 쓴 뒤에).
  2. 최종 게이트가 도는 self-test 가 "미커밋 epoch 없음" 을 전제해, **정상적인 C2 중간
     상태**를 실패로 판정했다 → 매번 롤백됐다.

둘 다 mock 이 실제 경로를 대신하는 바람에 안 잡혔다. 그래서 이 파일은 **아무것도
대신하지 않는다** — 두 리포를 복제해 진짜 commit 을 만들고 진짜 CLI 를 돌린다.

⚠️ 이 파일은 `topic_c2.GATE_TESTS` 에 **넣지 않는다**. 복제본 안에서 다시 복제가 돌아
비용이 곱해진다. 대신 복제본에서 `test_topic_c2.py` 를 직접 돌려 2번을 재현한다.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts import topic_c2  # noqa: E402

GIT_IDENTITY = ("-c", "user.name=C2 Integration", "-c", "user.email=c2@example.invalid")


def _git(*args: str, cwd: pathlib.Path) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr.strip()}"
    return done.stdout


@pytest.fixture
def clones(tmp_path):
    if not (topic_c2.IOS_ROOT / ".git").exists():
        pytest.skip(f"iOS 리포가 없다: {topic_c2.IOS_ROOT}")
    server, ios = tmp_path / "server", tmp_path / "ios"
    for source, destination in ((REPO, server), (topic_c2.IOS_ROOT, ios)):
        _git("clone", "-q", "--no-hardlinks", str(source), str(destination), cwd=tmp_path)
    # 작업트리의 **현재** 도구를 복제본에 들여온다 — 커밋된 판이 아니라 지금 판을 시험한다.
    for relative in ("scripts/topic_c2.py", "tests/test_topic_c2.py"):
        (server / relative).write_bytes((REPO / relative).read_bytes())
    return server, ios


def _new_ios_commit_below_citations(ios: pathlib.Path) -> str:
    """인용 좌표를 밀지 않는 곳(파일 끝)에 붙인다 — 좌표 경고 없이 pin 만 전진시킨다."""
    runbook = ios / "TOPIC_V2_RELEASE_RUNBOOK.md"
    runbook.write_text(runbook.read_text() + "\n<!-- C2 integration sentinel -->\n")
    _git("add", "-A", cwd=ios)
    _git(*GIT_IDENTITY, "commit", "-q", "-m", "test: C2 integration sentinel", cwd=ios)
    return _git("rev-parse", "HEAD", cwd=ios).strip()


def _append_prose(server: pathlib.Path) -> None:
    path = server / "spec" / "topic-only-semantic-review.json"
    data = json.loads(path.read_text())
    edges = data["review_process"]["review_edges"]
    edges[0]["scope"].append("C2 실사용 통합 시험 작업 단위 — pin·refresh·seal 을 실제로 돌린다.")
    by_name = {person["name"]: person for person in data["review_process"]["participants"]}
    for edge in edges:
        by_name[edge["author"]]["authored_or_modified"] = list(edge["scope"])
        by_name[edge["reviewer"]]["independently_reviewed"] = list(edge["scope"])
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _cli(server: pathlib.Path, ios: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, TOPIC_MIGRATION_IOS_ROOT=str(ios))
    return subprocess.run(
        [sys.executable, "scripts/topic_c2.py", *args],
        cwd=server, env=env, capture_output=True, text=True,
    )


def test_real_pin_refresh_seal_moves_the_whole_chain(clones):
    """**첫 실사용 blocker 1** 재현 방지 — 진짜 pin 전진이 성공으로 끝나야 한다."""
    server, ios = clones
    old_pin = json.loads((server / "spec/topic-only.lock.json").read_text())["pinned_commit"]["ios"]
    new_pin = _new_ios_commit_below_citations(ios)
    _append_prose(server)

    coords = _cli(server, ios, "coords", "--ios-from", old_pin, "--ios-to", new_pin)
    assert coords.returncode == 0, f"인용 아래 변경인데 경고가 났다:\n{coords.stdout}{coords.stderr}"

    pin = _cli(server, ios, "pin", "--ios", new_pin)
    assert pin.returncode == 0, f"실제 pin 전진이 실패했다:\n{pin.stdout}{pin.stderr}"
    assert "치환" in pin.stdout

    assert _cli(server, ios, "refresh").returncode == 0
    seal = _cli(server, ios, "seal", "--author", "Claude Code", "--reviewer", "OpenAI Codex")
    assert seal.returncode == 0, f"봉인 실패:\n{seal.stdout}{seal.stderr}"

    for name in ("topic-only.lock.json", "topic-only-migration-manifest.json"):
        pinned = json.loads((server / "spec" / name).read_text())["pinned_commit"]["ios"]
        assert pinned == new_pin, f"{name} pin 이 전진하지 않았다"

    review = json.loads((server / "spec/topic-only-semantic-review.json").read_text())
    committed_history = topic_c2._history_entries_from_text(
        _git("show", "HEAD:tests/test_topic_only_semantic_review.py", cwd=server),
        source="clone HEAD history",
    )
    sequence = committed_history[-1][0] + 1
    markers = [
        item
        for edge in review["review_process"]["review_edges"]
        for item in edge["scope"]
        if item.startswith(f"semantic-input-binding/v1:{sequence:04d}:")
    ]
    assert len(markers) == 2, "양방향 marker 가 아니다"
    assert len({marker.split(":")[3] for marker in markers}) == 1, "두 방향 지문이 다르다"

    history = topic_c2._history_entries_from_text(
        (server / "tests/test_topic_only_semantic_review.py").read_text(), source="clone history",
    )
    assert history[-1][0] == sequence and history[-1][2] == markers[0].split(":")[3]


def test_self_tests_stay_green_in_the_mid_c2_state(clones):
    """**첫 실사용 blocker 2** 재현 방지 — 최종 게이트는 미커밋 epoch 이 **있는** 상태에서 돈다.

    self-test 가 평상시 작업트리를 전제하면 정상 C2 를 실패로 판정하고 전부 롤백시킨다.
    """
    server, ios = clones
    new_pin = _new_ios_commit_below_citations(ios)
    _append_prose(server)
    assert _cli(server, ios, "pin", "--ios", new_pin).returncode == 0
    assert _cli(server, ios, "refresh").returncode == 0
    assert _cli(server, ios, "seal", "--author", "Claude Code",
                "--reviewer", "OpenAI Codex").returncode == 0

    env = dict(os.environ, TOPIC_MIGRATION_IOS_ROOT=str(ios))
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_topic_c2.py", "-q", "-p", "no:asyncio"],
        cwd=server, env=env, capture_output=True, text=True,
    )
    assert done.returncode == 0, f"미커밋 epoch 상태에서 self-test 가 깨졌다:\n{done.stdout[-3000:]}"


def test_integration_file_is_not_in_the_composite_gate():
    """복제본 안에서 다시 복제가 돌지 않게 — 이 파일은 GATE_TESTS 밖이어야 한다."""
    assert "tests/test_topic_c2_integration.py" not in topic_c2.GATE_TESTS
