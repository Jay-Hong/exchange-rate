"""C2 실행기 회귀 잠금 — 여기 시험은 전부 **실제로 저지른 실수**를 못 하게 막는다.

각 시험은 "가드를 끄면 빨개지는가" 로 설계했다. 통과만 하고 아무것도 안 잡는 검사는
이 파일의 목적을 정면으로 배신한다.
"""

from __future__ import annotations

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

def test_seal_derives_sequence_from_history_and_fingerprint_ignores_markers(monkeypatch):
    """parent·번호를 **손으로 옮겨 적지 않는다**(세션 중 3회 재입력했다).

    그리고 산문이 안 바뀌었으면 지문도 그대로여야 한다 — 지문은 **marker 를 제외**하고
    의미 표면만 해싱하기 때문이다. 이게 깨지면 marker 하나 붙일 때마다 체인이 흔들린다.
    """
    last_sequence, _, last_fingerprint = topic_c2._history_entries()[-1]
    monkeypatch.setattr(topic_c2, "_prose_added_since_head", lambda: 1)
    sequence, fingerprint = topic_c2.seal_epoch("Claude Code", "OpenAI Codex", dry_run=True)
    assert sequence == last_sequence + 1
    assert fingerprint == last_fingerprint, "의미 변화 0인데 지문이 움직였다 — marker 제외 규칙이 깨졌다"


def test_fingerprint_moves_when_prose_is_added(monkeypatch):
    """앞 시험이 공허하지 않음을 보인다 — 산문을 실제로 더하면 지문은 **반드시** 달라진다."""
    import json
    _, _, last_fingerprint = topic_c2._history_entries()[-1]
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


def test_seal_refuses_without_new_prose():
    """기록할 일이 없으면 epoch 을 열지 않는다."""
    with pytest.raises(topic_c2.C2Error, match="새 산문 작업 단위가 없다"):
        topic_c2.seal_epoch("Claude Code", "OpenAI Codex", dry_run=True)


def test_seal_refuses_to_rewrite_a_committed_epoch(monkeypatch):
    """append-only — 커밋된 epoch 은 다시 쓰지 않고 **앞으로** supersede 한다."""
    next_sequence = topic_c2._history_entries()[-1][0] + 1
    monkeypatch.setattr(topic_c2, "_prose_added_since_head", lambda: 1)
    monkeypatch.setattr(topic_c2, "_committed_text", lambda _p: f"{next_sequence:04d}:")

    module = topic_c2._test_module("test_topic_only_semantic_review", "SEMANTIC_BINDING_PREFIX")
    marker = f"{module.SEMANTIC_BINDING_PREFIX}{next_sequence:04d}:x"
    import json
    data = json.loads(topic_c2.SEMANTIC.read_text())
    data["review_process"]["review_edges"][0]["scope"].append(marker)
    original = topic_c2.SEMANTIC.read_text()
    topic_c2.SEMANTIC.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    try:
        with pytest.raises(topic_c2.C2Error, match="이미 커밋됐다"):
            topic_c2.seal_epoch("Claude Code", "OpenAI Codex", dry_run=True, allow_reseal=True)
    finally:
        topic_c2.SEMANTIC.write_text(original)
    assert topic_c2.SEMANTIC.read_text() == original


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
