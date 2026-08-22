"""C2 실행기 회귀 잠금 — 여기 시험은 전부 **실제로 저지른 실수**를 못 하게 막는다.

각 시험은 "가드를 끄면 빨개지는가" 로 설계했다. 통과만 하고 아무것도 안 잡는 검사는
이 파일의 목적을 정면으로 배신한다.
"""

from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts import topic_c2  # noqa: E402


# --------------------------------------------------------- 헤더 탐색

def test_grep_without_dash_e_silently_finds_nothing():
    """**사고 재현**: 패턴이 `-` 로 시작하면 grep 이 옵션으로 먹어 0건이 된다.

    이 시험이 도구의 `-e` 를 지키는 이유다 — 잘못된 형태가 **에러가 아니라 침묵**이라
    루프가 "할 일 없음" 으로 읽고 stale 헤더를 통과시켰다.
    """
    wrong = subprocess.run(
        ["grep", "-rl", "- manifest SHA:", "spec", "DECISIONS.md"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert wrong.stdout.split() == [], "잘못된 형태가 파일을 찾았다면 이 회귀 잠금은 무의미하다"

    right = subprocess.run(
        ["grep", "-rl", "-e", "- manifest SHA:", "spec", "DECISIONS.md"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert len(right.stdout.split()) == topic_c2.EXPECTED_DOC_HEADER_FILES


def test_doc_header_files_is_exactly_the_expected_count():
    found = topic_c2._doc_header_files()
    assert len(found) == topic_c2.EXPECTED_DOC_HEADER_FILES
    assert all(path.exists() for path in found)


def test_doc_header_count_mismatch_is_fatal(monkeypatch):
    """0건이든 5건이든 **탐색이 깨진 것**으로 보고 멈춘다."""
    monkeypatch.setattr(topic_c2, "EXPECTED_DOC_HEADER_FILES", 5)
    with pytest.raises(topic_c2.C2Error, match="헤더 문서가"):
        topic_c2._doc_header_files()


# --------------------------------------------------------- pin 치환

OLD = "a" * 40
NEW = "b" * 40


def test_pin_replacement_preserves_evidence_sha():
    lines = [
        f'    "ios": "{OLD}"',                      # pin — 갱신 대상
        f'      "sha": "{OLD}",',                   # 역사 기록 — 보존
        f'  - iOS 기준 commit: `{OLD}`',            # 문서 헤더 pin
        'no pin here',
    ]
    out, replaced, preserved = topic_c2.replace_pin_in_lines(lines, OLD, NEW)
    assert replaced == 2 and preserved == 1
    assert out[0] == f'    "ios": "{NEW}"'
    assert out[1] == f'      "sha": "{OLD}",', "evidence sha 를 덮으면 역사 위조다"
    assert out[2].endswith(f"`{NEW}`")
    assert out[3] == 'no pin here'


def test_pin_replacement_reports_zero_when_absent():
    out, replaced, preserved = topic_c2.replace_pin_in_lines(["nothing"], OLD, NEW)
    assert (out, replaced, preserved) == (["nothing"], 0, 0)


def test_advance_pin_rejects_short_sha():
    with pytest.raises(topic_c2.C2Error, match="40자리"):
        topic_c2.advance_pin("9ba1458")


def test_current_ios_pin_is_a_full_sha():
    assert topic_c2.SHA40.match(topic_c2.current_ios_pin())


def test_pin_files_returns_empty_for_a_normal_no_match(monkeypatch):
    """git grep exit 1은 오류가 아니라 0건이다 — 치환 후에는 이 상태가 성공 조건이다."""
    completed = subprocess.CompletedProcess(["git", "grep"], 1, stdout="", stderr="")
    monkeypatch.setattr(topic_c2.subprocess, "run", lambda *_a, **_k: completed)
    assert topic_c2._pin_files(OLD) == []


def test_advance_pin_accepts_zero_old_matches_after_replacement(monkeypatch, tmp_path):
    """사전에는 old pin이 있어야 하지만 치환 뒤 0건은 정상 완료다."""
    target = tmp_path / "pin.json"
    target.write_text(f'{{"ios": "{OLD}"}}\n')
    searches = iter(([target], []))
    monkeypatch.setattr(topic_c2, "REPO", tmp_path)
    monkeypatch.setattr(topic_c2, "_run", lambda *_a, **_k: NEW + "\n")
    monkeypatch.setattr(topic_c2, "current_ios_pin", lambda: OLD)
    monkeypatch.setattr(topic_c2, "_pin_files", lambda _old: list(next(searches)))

    result = topic_c2.advance_pin(NEW)
    assert result["replaced"] == 1
    assert target.read_text() == f'{{"ios": "{NEW}"}}\n'


# --------------------------------------------------------- 체인

def test_chain_is_at_its_fixed_point():
    """정본이 이미 고정점이어야 한다 — 아니면 커밋 전에 `refresh` 를 안 돌린 것이다."""
    assert topic_c2.refresh_chain(dry_run=True) == []


def test_fixpoint_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(topic_c2, "FIXPOINT_LIMIT", 0)
    with pytest.raises(topic_c2.C2Error, match="고정점"):
        topic_c2.refresh_chain()


def test_missing_helper_symbol_is_fatal():
    with pytest.raises(topic_c2.C2Error, match="판정 로직이 옮겨졌다"):
        topic_c2._test_module("test_topic_only_semantic_review", "__no_such_symbol__")


# --------------------------------------------------------- 봉인

def _use_committed_seal_fixture(monkeypatch, tmp_path) -> list[tuple[int, str, str]]:
    """Run seal unit tests against HEAD, not an in-flight C2 working tree."""
    semantic_text = topic_c2._committed_text("spec/topic-only-semantic-review.json")
    history_text = topic_c2._committed_text("tests/test_topic_only_semantic_review.py")
    assert semantic_text and history_text

    semantic = tmp_path / "head-semantic.json"
    history = tmp_path / "head-history.py"
    semantic.write_text(semantic_text)
    history.write_text(history_text)
    committed = topic_c2._history_entries_from_text(
        history_text, source="HEAD semantic binding history fixture",
    )

    monkeypatch.setattr(topic_c2, "SEMANTIC", semantic)
    monkeypatch.setattr(topic_c2, "HISTORY_FILE", history)
    monkeypatch.setattr(
        topic_c2, "_committed_history_entries", lambda: list(committed),
    )
    return committed


def test_seal_derives_sequence_from_history_and_fingerprint_ignores_markers(monkeypatch, tmp_path):
    """parent·번호를 **손으로 옮겨 적지 않는다**(세션 중 3회 재입력했다).

    그리고 산문이 안 바뀌었으면 지문도 그대로여야 한다 — 지문은 **marker 를 제외**하고
    의미 표면만 해싱하기 때문이다. 이게 깨지면 marker 하나 붙일 때마다 체인이 흔들린다.
    """
    committed = _use_committed_seal_fixture(monkeypatch, tmp_path)
    last_sequence, _, last_fingerprint = committed[-1]
    monkeypatch.setattr(topic_c2, "_prose_added_since_head", lambda: 1)
    sequence, fingerprint = topic_c2.seal_epoch("Claude Code", "OpenAI Codex", dry_run=True)
    assert sequence == last_sequence + 1
    assert fingerprint == last_fingerprint, "의미 변화 0인데 지문이 움직였다 — marker 제외 규칙이 깨졌다"


def test_fingerprint_moves_when_prose_is_added(monkeypatch, tmp_path):
    """앞 시험이 공허하지 않음을 보인다 — 산문을 실제로 더하면 지문은 **반드시** 달라진다."""
    committed = _use_committed_seal_fixture(monkeypatch, tmp_path)
    _, _, last_fingerprint = committed[-1]
    original = topic_c2.SEMANTIC.read_text()
    data = json.loads(original)
    data["review_process"]["review_edges"][0]["scope"].append("임시 산문 작업 단위 — 지문 변화 확인용")
    topic_c2.SEMANTIC.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    try:
        monkeypatch.setattr(topic_c2, "_prose_added_since_head", lambda: 1)
        _, fingerprint = topic_c2.seal_epoch("Claude Code", "OpenAI Codex", dry_run=True)
    finally:
        topic_c2.SEMANTIC.write_text(original)
    assert fingerprint != last_fingerprint
    assert topic_c2.SEMANTIC.read_text() == original


def test_seal_refuses_without_new_prose(monkeypatch, tmp_path):
    """기록할 일이 없으면 epoch 을 열지 않는다."""
    _use_committed_seal_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(topic_c2, "_prose_added_since_head", lambda: 0)
    with pytest.raises(topic_c2.C2Error, match="새 산문 작업 단위가 없다"):
        topic_c2.seal_epoch("Claude Code", "OpenAI Codex", dry_run=True)


def test_allow_reseal_refuses_when_there_is_no_uncommitted_epoch(monkeypatch, tmp_path):
    """커밋된 마지막 epoch 은 재봉인하지 않고 다음 epoch 으로 supersede 한다."""
    _use_committed_seal_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(topic_c2, "_prose_added_since_head", lambda: 1)
    with pytest.raises(topic_c2.C2Error, match="미커밋 epoch 이 정확히 1개"):
        topic_c2.seal_epoch("Claude Code", "OpenAI Codex", dry_run=True, allow_reseal=True)


def test_allow_reseal_replaces_exactly_one_uncommitted_epoch(monkeypatch, tmp_path):
    committed = _use_committed_seal_fixture(monkeypatch, tmp_path)
    sequence = committed[-1][0] + 1
    parent = committed[-1][2]
    old_fingerprint = "c" * 64
    module = topic_c2._test_module(
        "test_topic_only_semantic_review", "_semantic_binding_marker",
    )

    data = json.loads(topic_c2.SEMANTIC.read_text())
    edges = data["review_process"]["review_edges"]
    edges[0]["scope"].append("미커밋 epoch 재봉인 판별용 산문")
    for edge in edges:
        edge["scope"].append(module._semantic_binding_marker(
            sequence, parent, old_fingerprint, edge["author"], edge["reviewer"],
        ))
    semantic = tmp_path / "semantic.json"
    semantic.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    history = topic_c2.HISTORY_FILE.read_text()
    anchor = f'        "{parent}",\n    ),\n)'
    appended = (
        f'        "{parent}",\n    ),\n    (\n        {sequence},\n'
        f'        "{parent}",\n        "{old_fingerprint}",\n    ),\n)'
    )
    history_file = tmp_path / "history.py"
    history_file.write_text(history.replace(anchor, appended))

    monkeypatch.setattr(topic_c2, "SEMANTIC", semantic)
    monkeypatch.setattr(topic_c2, "HISTORY_FILE", history_file)
    monkeypatch.setattr(topic_c2, "_committed_history_entries", lambda: committed)
    monkeypatch.setattr(topic_c2, "_prose_added_since_head", lambda: 1)
    sealed_sequence, fingerprint = topic_c2.seal_epoch(
        "Claude Code", "OpenAI Codex", allow_reseal=True,
    )

    assert sealed_sequence == sequence
    assert fingerprint != old_fingerprint
    assert topic_c2._history_entries()[-1] == (sequence, parent, fingerprint)
    rebound = json.loads(semantic.read_text())
    markers = [
        item
        for edge in rebound["review_process"]["review_edges"]
        for item in edge["scope"]
        if item.startswith(f"semantic-input-binding/v1:{sequence:04d}:")
    ]
    assert len(markers) == 2
    assert all(f":{fingerprint}:" in marker for marker in markers)


def test_review_edges_are_selected_by_roles_not_array_order():
    data = json.loads(topic_c2.SEMANTIC.read_text())
    author_edge, reviewer_edge, _ = topic_c2._select_review_edges(
        data, "OpenAI Codex", "Claude Code",
    )
    assert author_edge["author"] == "OpenAI Codex"
    assert reviewer_edge["author"] == "Claude Code"


@pytest.mark.parametrize(
    ("author", "reviewer", "message"),
    [
        ("Claude Code", "Claude Code", "자기검토"),
        ("Unknown Agent", "Claude Code", "participant"),
    ],
)
def test_review_role_validation_rejects_self_or_unknown(author, reviewer, message):
    data = json.loads(topic_c2.SEMANTIC.read_text())
    with pytest.raises(topic_c2.C2Error, match=message):
        topic_c2._select_review_edges(data, author, reviewer)


def test_prose_append_only_rejects_rewriting_a_committed_item(monkeypatch, tmp_path):
    committed = json.loads(topic_c2.SEMANTIC.read_text())
    current = copy.deepcopy(committed)
    edge = current["review_process"]["review_edges"][0]
    index = next(i for i, item in enumerate(edge["scope"]) if not item.startswith("semantic-input-binding/"))
    edge["scope"][index] += " (rewrite)"
    semantic = tmp_path / "semantic.json"
    semantic.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    monkeypatch.setattr(topic_c2, "SEMANTIC", semantic)
    monkeypatch.setattr(
        topic_c2, "_committed_text",
        lambda path: json.dumps(committed, ensure_ascii=False) if path.endswith("semantic-review.json") else "",
    )
    with pytest.raises(topic_c2.C2Error, match="rewrite/reorder"):
        topic_c2._prose_added_since_head()


def test_prose_append_only_counts_only_new_tail_items(monkeypatch, tmp_path):
    committed = json.loads(topic_c2.SEMANTIC.read_text())
    current = copy.deepcopy(committed)
    current["review_process"]["review_edges"][1]["scope"].append("새 산문 작업 단위")
    semantic = tmp_path / "semantic.json"
    semantic.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    monkeypatch.setattr(topic_c2, "SEMANTIC", semantic)
    monkeypatch.setattr(
        topic_c2, "_committed_text",
        lambda path: json.dumps(committed, ensure_ascii=False) if path.endswith("semantic-review.json") else "",
    )
    assert topic_c2._prose_added_since_head() == 1


def test_seal_rejects_a_missing_committed_marker(monkeypatch, tmp_path):
    data = json.loads(topic_c2.SEMANTIC.read_text())
    edge = data["review_process"]["review_edges"][0]
    marker_index = next(
        index for index, item in enumerate(edge["scope"])
        if item.startswith("semantic-input-binding/")
    )
    edge["scope"].pop(marker_index)
    semantic = tmp_path / "semantic.json"
    semantic.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    monkeypatch.setattr(topic_c2, "SEMANTIC", semantic)
    monkeypatch.setattr(topic_c2, "_prose_added_since_head", lambda: 1)
    with pytest.raises(topic_c2.C2Error, match="marker 이력"):
        topic_c2.seal_epoch("Claude Code", "OpenAI Codex", dry_run=True)


# --------------------------------------------------------- 좌표

def test_coordinate_check_flags_an_edit_above_a_citation(monkeypatch):
    """**pin 전진 ≠ 좌표 재도출.** 인용 위쪽 삽입은 CI 를 통과하면서 12건을 조용히 밀었다."""
    monkeypatch.setattr(topic_c2, "_run", lambda *a, **k: "TOPIC_V2_RELEASE_RUNBOOK.md\n")
    monkeypatch.setattr(topic_c2, "_cited_lines", lambda: {"TOPIC_V2_RELEASE_RUNBOOK.md": {9999}})
    monkeypatch.setattr(
        topic_c2, "_run",
        lambda args, **k: ("TOPIC_V2_RELEASE_RUNBOOK.md\n" if "--name-only" in args
                           else "@@ -10,0 +11,3 @@\n"),
    )
    warnings = topic_c2.check_coordinates("HEAD~1")
    assert warnings and "다시 찾아라" in warnings[0]


def test_coordinate_check_is_quiet_below_every_citation(monkeypatch):
    monkeypatch.setattr(topic_c2, "_cited_lines", lambda: {"TOPIC_V2_RELEASE_RUNBOOK.md": {369}})
    monkeypatch.setattr(
        topic_c2, "_run",
        lambda args, **k: ("TOPIC_V2_RELEASE_RUNBOOK.md\n" if "--name-only" in args
                           else "@@ -493,4 +493,15 @@\n"),
    )
    assert topic_c2.check_coordinates("HEAD~1") == []


def test_coordinate_check_rejects_an_empty_range(monkeypatch):
    monkeypatch.setattr(topic_c2, "_run", lambda *a, **k: "")
    with pytest.raises(topic_c2.C2Error, match="변경 파일이 0"):
        topic_c2.check_coordinates("HEAD")


# --------------------------------------------------------- 복합 C2 안전 경계

def test_c2_coordinate_warning_is_fatal_before_any_write(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(topic_c2, "current_ios_pin", lambda: OLD)
    monkeypatch.setattr(topic_c2, "check_coordinates", lambda *_a, **_k: ["좌표 이동"])
    monkeypatch.setattr(topic_c2, "_c2_mutation_paths", lambda _old: called.append("paths"))
    monkeypatch.setattr(topic_c2, "advance_pin", lambda _new: called.append("pin"))
    monkeypatch.setattr(topic_c2, "refresh_chain", lambda: called.append("refresh"))
    monkeypatch.setattr(topic_c2, "seal_epoch", lambda *_a, **_k: called.append("seal"))

    result = topic_c2.main([
        "c2", "--ios", NEW, "--author", "Claude Code", "--reviewer", "OpenAI Codex",
    ])
    assert result == 2
    assert called == [], "좌표 경고 뒤 어떤 mutation 준비/쓰기라도 실행되면 안 된다"


def test_c2_runs_all_steps_and_post_write_verification(monkeypatch, tmp_path):
    target = tmp_path / "tracked"
    target.write_text("before")
    events: list[str] = []
    monkeypatch.setattr(topic_c2, "current_ios_pin", lambda: OLD)
    monkeypatch.setattr(topic_c2, "check_coordinates", lambda *_a, **_k: [])
    monkeypatch.setattr(topic_c2, "_c2_mutation_paths", lambda _old: [target])
    monkeypatch.setattr(topic_c2, "advance_pin", lambda _new: events.append("pin"))
    monkeypatch.setattr(topic_c2, "refresh_chain", lambda: events.append("refresh"))
    monkeypatch.setattr(topic_c2, "seal_epoch", lambda *_a, **_k: events.append("seal"))
    monkeypatch.setattr(topic_c2, "verify_gates", lambda: events.append("verify"))

    topic_c2.run_c2(NEW, "Claude Code", "OpenAI Codex")
    assert events == ["pin", "refresh", "seal", "verify"]


def test_c2_restores_original_bytes_when_post_write_verification_fails(monkeypatch, tmp_path):
    target = tmp_path / "tracked"
    original = b"original\x00bytes\n"
    target.write_bytes(original)
    monkeypatch.setattr(topic_c2, "current_ios_pin", lambda: OLD)
    monkeypatch.setattr(topic_c2, "check_coordinates", lambda *_a, **_k: [])
    monkeypatch.setattr(topic_c2, "_c2_mutation_paths", lambda _old: [target])
    monkeypatch.setattr(topic_c2, "advance_pin", lambda _new: target.write_text("pin changed"))
    monkeypatch.setattr(topic_c2, "refresh_chain", lambda: target.write_text("refresh changed"))
    monkeypatch.setattr(
        topic_c2, "seal_epoch", lambda *_a, **_k: target.write_text("seal changed"),
    )

    def fail_verification():
        target.write_text("verify observed changed bytes")
        raise topic_c2.C2Error("injected failure")

    monkeypatch.setattr(topic_c2, "verify_gates", fail_verification)
    with pytest.raises(topic_c2.C2Error, match="injected failure"):
        topic_c2.run_c2(NEW, "Claude Code", "OpenAI Codex")
    assert target.read_bytes() == original


def test_verify_gates_runs_preflight_tests_and_both_diff_checks(monkeypatch):
    calls: list[tuple[list[str], pathlib.Path | None]] = []
    monkeypatch.setattr(topic_c2, "refresh_chain", lambda **_k: [])

    def record(args, cwd=None, **_kwargs):
        calls.append((args, cwd))
        return ""

    monkeypatch.setattr(topic_c2, "_run", record)
    topic_c2.verify_gates()

    commands = [args for args, _cwd in calls]
    assert any(any(part.endswith("topic_migration_manifest.py") for part in args) for args in commands)
    pytest_call = next(args for args in commands if "pytest" in args)
    assert set(topic_c2.GATE_TESTS).issubset(pytest_call)
    assert ["git", "diff", "--check"] in commands
    assert ["git", "-C", str(topic_c2.IOS_ROOT), "diff", "--check"] in commands


def test_verify_gates_rejects_a_non_fixed_chain(monkeypatch):
    monkeypatch.setattr(topic_c2, "refresh_chain", lambda **_k: ["stale"])
    with pytest.raises(topic_c2.C2Error, match="고정점이 아니다"):
        topic_c2.verify_gates()
