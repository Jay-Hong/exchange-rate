"""변이 하네스 판정기 — **순수 함수**라 영구 테스트로 잠근다.

⛔ 이 판정기가 틀리면 배터리 전체 결론이 틀린다. 한때 `any(rc == 1)` 을 먼저 봐서
   {1, 2}(= 한 probe 는 죽였고 다른 probe 는 **인프라 오류**)를 SURVIVED 로 접었다 —
   인프라 실패를 판별력 결론으로 바꾸는 오분류다.
"""
import importlib.util
import pathlib
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_bat", pathlib.Path(__file__).resolve().parent.parent
    / "scripts" / "mutation_auth_executor_ledger.py")
_BAT = importlib.util.module_from_spec(_SPEC)
sys.modules["_bat"] = _BAT
try:
    _SPEC.loader.exec_module(_BAT)
except SystemExit:      # __main__ 가드가 없으면 실행될 수 있다
    pass


@pytest.mark.parametrize("rcs,expected", [
    ({"a": 1, "b": 1}, "KILLED"),      # 전 probe 가 각각 죽였다
    ({"a": 1}, "KILLED"),
    ({"a": 1, "b": 0}, "SURVIVED"),    # 한 probe 가 공허하다
    ({"a": 0, "b": 0}, "SURVIVED"),
    ({"a": 1, "b": 2}, "INFRA"),       # ⛔ 인프라 우선 — 구 판정기는 SURVIVED 로 접었다
    ({"a": 0, "b": 2}, "INFRA"),
    ({"a": 3}, "INFRA"),
    ({"a": 5}, "INFRA"),               # pytest 수집 0건
    ({}, "INFRA"),                     # probe 가 없으면 판정 불가
])
def test_classifier(rcs, expected):
    assert _BAT.classify(rcs) == expected


def test_mapping_is_valid_as_shipped():
    assert _BAT.validate_mapping() == []


def test_invalid_mapping_refuses_before_running_anything(monkeypatch):
    """⛔ 이름이 '검증한다' 인데 `validate_mapping() == []` 만 보면 **아무것도 안 돌린다**는
    부분을 검증하지 않는다(codex 지적). `_run` 을 mock 으로 두고 **호출 0**을 잠근다."""
    calls = []
    monkeypatch.setattr(_BAT, "_run", lambda pr: calls.append(pr))
    monkeypatch.setitem(_BAT.MUTANT_PROBES, "없는변이", ("tripwire",))
    assert _BAT.main() == 2
    assert calls == [], f"mapping 이 잘못됐는데 probe 를 돌렸다: {calls}"


@pytest.mark.parametrize("rcs,expect_counter", [
    ({"first": 0, "second": 2}, "infra"),      # ⛔ 구 코드는 첫 probe 만 보고 SURVIVED 로 접었다
    ({"first": 1, "second": 2}, "infra"),
    ({"first": 1, "second": 1}, "killed"),
    ({"first": 1, "second": 0}, "survived"),
])
def test_main_counters_follow_classify_not_the_first_probe(monkeypatch, rcs, expect_counter, capsys):
    """⛔ `classify()` 단독 테스트는 **메인 루프가 그걸 쓰는지**를 못 잡는다 — 실제로 판정기를
    분리하고도 구 분기가 남아 결론이 갈렸다(codex 재현). main() 의 최종 카운터까지 본다."""
    import types
    monkeypatch.setattr(_BAT, "PROBES", {"first": ["x"], "second": ["y"]})
    monkeypatch.setattr(_BAT, "DEFAULT_PROBES", ("first", "second"))
    monkeypatch.setattr(_BAT, "MUTANT_PROBES", {})
    monkeypatch.setattr(_BAT, "MUTANTS", [("단일변이", [("AAA", "BBB")])])
    # ⛔ `_run` 은 **기준선 확인에도** 불린다 — 거기서 rc 를 그대로 주면 baseline INFRA 로
    #    중단돼 변이 판정에 도달하지 못한다(실측). 기준선은 green, 변이 때만 주입한다.
    state = {"mutating": False}

    def fake_run(pr):
        rc = rcs.get(pr, 0) if state["mutating"] else 0
        return types.SimpleNamespace(returncode=rc, stdout="")
    monkeypatch.setattr(_BAT, "_run", fake_run)

    class _FakeSrc:                     # ⛔ PosixPath 속성은 read-only 라 SRC 자체를 갈아끼운다
        def __init__(self):
            self.text = "AAA\n"
        def read_text(self):
            return self.text
        def write_text(self, s):
            self.text = s
    fake = _FakeSrc()

    def _write(s, _orig=fake.write_text):
        state["mutating"] = True        # 변이본이 써진 뒤부터 주입
        _orig(s)
    fake.write_text = _write
    monkeypatch.setattr(_BAT, "SRC", fake)
    rc = _BAT.main()
    out = capsys.readouterr().out
    assert f"{expect_counter}=1" in out.replace(" ", ""), out[-400:]
    # ⛔ 게이트 계약은 **반환값**이다 — KILLED-only 만 0, SURVIVED/INFRA 는 1.
    #    카운터 출력만 보면 반환값 회귀를 놓친다(codex 지적).
    assert rc == (0 if expect_counter == "killed" else 1), f"rc={rc} · {expect_counter}"


def test_unknown_mutant_key_is_rejected(monkeypatch):
    """⛔ 구 이름은 `unknown_probe` 였는데 실제로는 알 수 없는 **변이 키**를 넣었다 —
    이름이 검증보다 강했다(codex 지적). 이 테스트는 그 키를 시험하고, 아래가 probe 를 시험한다."""
    monkeypatch.setitem(_BAT.MUTANT_PROBES, "없는변이", ("tripwire",))
    assert any("오타" in e for e in _BAT.validate_mapping())


def test_unknown_probe_name_is_rejected(monkeypatch):
    real = next(iter(_BAT.MUTANTS))[0]
    monkeypatch.setitem(_BAT.MUTANT_PROBES, real, ("does-not-exist",))
    assert any("모르는 probe" in e for e in _BAT.validate_mapping())


def test_empty_default_probes_is_rejected(monkeypatch):
    monkeypatch.setattr(_BAT, "DEFAULT_PROBES", ())
    assert any("DEFAULT_PROBES 가 비었다" in e for e in _BAT.validate_mapping())


def test_unknown_default_probe_is_rejected(monkeypatch):
    monkeypatch.setattr(_BAT, "DEFAULT_PROBES", ("does-not-exist",))
    assert any("DEFAULT_PROBES 가 모르는" in e for e in _BAT.validate_mapping())


def test_duplicate_mutant_name_is_rejected(monkeypatch):
    """최종 계약에 '변이 이름 중복' 을 넣었으니 양성 대조로 잠근다."""
    dup = list(_BAT.MUTANTS) + [_BAT.MUTANTS[0]]
    monkeypatch.setattr(_BAT, "MUTANTS", dup)
    assert any("이름 중복" in e for e in _BAT.validate_mapping())


def test_empty_probe_tuple_for_a_real_mutant_is_rejected(monkeypatch):
    real = next(iter(_BAT.MUTANTS))[0]
    monkeypatch.setitem(_BAT.MUTANT_PROBES, real, ())
    assert any("probe 가 비었다" in e for e in _BAT.validate_mapping())


def test_blank_selector_is_rejected(monkeypatch):
    monkeypatch.setitem(_BAT.PROBES, "blank", [""])
    assert any("빈 선택자" in e for e in _BAT.validate_mapping())


class TestApplicationGuard:
    """⛔ 방어선은 **참조하는 테스트가 있어야** 방어선이다. 이 두 가드를 지워도 현행 24변이는
    전부 KILLED 라 배터리만으로는 삭제를 못 잡는다(codex 지적) — 합성 반례로 잠근다."""

    SRC = "class C:\n    def f(self):\n        x = 1\n        y = 2\n"

    def test_unique_but_not_line_start_anchor_is_invalid(self):
        """8칸 줄 안의 4칸 부분문자열 — `count == 1` 은 통과하지만 들여쓰기가 깨진다."""
        anchor = "x = 1\n"                       # 줄머리가 아니다(앞에 8칸)
        assert self.SRC.count(anchor) == 1
        out, why = _BAT.apply_pairs(self.SRC, [(anchor, "x = 99\n")])
        assert out is None and "line-start" in why, why

    def test_declared_mid_line_anchor_is_allowed(self, monkeypatch):
        anchor = "x = 1\n"
        monkeypatch.setattr(_BAT, "MID_LINE_ANCHORS", frozenset({anchor}))
        out, why = _BAT.apply_pairs(self.SRC, [(anchor, "x = 99\n")])
        assert out is not None and why == "ok", why

    def test_line_start_anchor_passes(self):
        anchor = "        x = 1\n"
        out, why = _BAT.apply_pairs(self.SRC, [(anchor, "        x = 99\n")])
        assert out is not None and why == "ok", why

    @pytest.mark.parametrize("pairs,expect", [
        ([("        x = 1\n", "        x = 1\n")], "no-op"),
        ([("nope", "y")], "앵커 발생 0회"),
        ([("        x = 1\n", "      bad indent\n")], "구문 파괴"),
    ])
    def test_other_application_rejections(self, pairs, expect):
        out, why = _BAT.apply_pairs(self.SRC, pairs)
        assert out is None and expect in why, why


class TestFalseKillGuard:
    LANE = _BAT.LANE_OWNED[0]

    def test_lane_owned_nameerror_on_killed_is_invalid(self):
        out = f"NameError: name '{self.LANE}' is not defined"
        assert _BAT.false_kill_name("KILLED", out) == self.LANE

    @pytest.mark.parametrize("verdict", ["SURVIVED", "INFRA"])
    def test_same_nameerror_is_not_touched_when_not_killed(self, verdict):
        """⛔ verdict 앞에 두면 `{1,0}`·`{1,2}` 결과까지 덮는다 — KILLED 일 때만 적용한다."""
        out = f"NameError: name '{self.LANE}' is not defined"
        assert _BAT.false_kill_name(verdict, out) is None

    def test_unrelated_nameerror_keeps_the_kill(self):
        """lane-owned 가 아닌 이름은 정상 KILLED 를 유지한다."""
        assert _BAT.false_kill_name("KILLED", "NameError: name '_totally_unrelated' is not defined") is None

