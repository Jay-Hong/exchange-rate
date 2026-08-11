"""launcher 반례 — "게이트가 있다"가 아니라 **막는다**를 시험한다.

⛔ launcher 의 존재 이유는 워크플로 결과에 "입력이 고정됐다"는 근거를 붙이는 것이다.
   게이트가 하나라도 통과 방향으로 새면 그 근거가 사라지므로, 각 게이트를 개별 반례로 잠근다.
"""
import importlib.util
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import topic_migration_manifest as M  # noqa: E402
import topic_migration_workflow_launcher as L  # noqa: E402

PASS_PREFLIGHT = staticmethod(lambda _lock: 0)


def _lock_variant(tmp_path: pathlib.Path, **overrides) -> pathlib.Path:
    lk = json.loads((REPO / "spec" / "topic-only.lock.json").read_text())
    for key, value in overrides.items():
        if key == "pinned_commit":
            lk["pinned_commit"] = value
        else:
            lk[key]["sha256"] = value
    p = tmp_path / "variant.lock.json"
    p.write_text(json.dumps(lk, ensure_ascii=False))
    return p


def test_gate_blocks_when_preflight_fails():
    """⛔ 리포의 진행 상태에 기대지 않는다 — 분류가 끝나면 그 상태는 사라진다(실측으로 깨졌다).
    preflight 실패 자체를 주입해 게이트가 막는지만 본다."""
    record, fail = L.gate(preflight_fn=lambda *_a: 1)
    assert record is None
    assert any("[L_PREFLIGHT]" in f for f in fail), fail


def test_gate_blocks_on_lock_sha_mismatch(tmp_path):
    """형식은 유효하고 **값만 틀린** lock — 파일을 지금 해싱해 잡아야 한다."""
    lock = _lock_variant(tmp_path, archive="0" * 64)
    record, fail = L.gate(lock_path=lock, preflight_fn=lambda *_a: 0)
    assert record is None
    assert any("[L_SHA]" in f for f in fail), fail


def test_gate_blocks_on_broken_lock(tmp_path):
    lock = tmp_path / "broken.lock.json"
    lock.write_text("{bad json")
    record, fail = L.gate(lock_path=lock, preflight_fn=lambda *_a: 0)
    assert record is None
    assert any("[L_LOCK]" in f for f in fail), fail


def test_gate_blocks_when_manifest_does_not_match_lock(tmp_path):
    """lock 과 manifest 가 서로 다른 리비전을 가리키면 근거가 갈린다."""
    lock = _lock_variant(tmp_path, pinned_commit={"server": "a" * 40, "ios": "b" * 40})
    record, fail = L.gate(lock_path=lock, preflight_fn=lambda *_a: 0)
    assert record is None
    assert any("[L_PROPAGATE]" in f for f in fail), fail


def test_gate_passes_only_when_every_precondition_holds():
    """⛔ 대조군 — 실패 반례들이 '무조건 실패'로 통과한 것이 아님을 보인다."""
    record, fail = L.gate(preflight_fn=lambda *_a: 0)
    assert fail == [], fail
    assert record is not None
    lk = json.loads((REPO / "spec" / "topic-only.lock.json").read_text())
    assert record["archive"]["sha256"] == lk["archive"]["sha256"]
    assert record["baseline"]["sha256"] == lk["baseline"]["sha256"]
    assert record["pinned_commit"] == lk["pinned_commit"]


def test_generated_script_pins_the_exact_sha_as_enum():
    """트립와이어의 핵심 — SHA 가 **enum 으로** 박혀야 다른 리비전 보고가 하드 실패한다."""
    record, fail = L.gate(preflight_fn=lambda *_a: 0)
    assert not fail
    script = L.render_workflow_script(record)
    a_sha = record["archive"]["sha256"]
    b_sha = record["baseline"]["sha256"]
    assert f"enum: ['{a_sha}']" in script
    assert f"enum: ['{b_sha}']" in script
    # 경로도 고정돼야 한다 — 에이전트가 사본을 고를 여지를 없앤다
    assert record["archive"]["path"] in script
    assert record["baseline"]["path"] in script
    # 손으로 고치면 근거가 사라지므로 생성물임을 본문이 밝혀야 한다
    assert "생성물" in script


def test_generated_script_is_deterministic():
    """타임스탬프·난수가 섞이면 같은 입력에서 다른 산출물이 나와 대조가 불가능해진다."""
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    assert L.render_workflow_script(record) == L.render_workflow_script(record)


def test_main_writes_nothing_when_gate_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "gate", lambda **_kw: (None, ["[L_PREFLIGHT] 주입된 실패"]))
    out = tmp_path / "never.js"
    assert L.main(["--out", str(out)]) == 1
    assert not out.exists()


def test_main_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    out = tmp_path / "existing.js"
    out.write_text("KEEP")
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    monkeypatch.setattr(L, "gate", lambda **_kw: (record, []))
    assert L.main(["--out", str(out)]) == 1
    assert out.read_text() == "KEEP"
    assert L.main(["--out", str(out), "--force"]) == 0
    assert out.read_text() != "KEEP"


def test_check_mode_never_writes(tmp_path, monkeypatch):
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    monkeypatch.setattr(L, "gate", lambda **_kw: (record, []))
    out = tmp_path / "should_not_exist.js"
    assert L.main(["--check", "--out", str(out)]) == 0
    assert not out.exists()


def test_launcher_reuses_the_validator_lock_loader():
    """⛔ 검증기와 **다른** lock 해석을 들이면 둘이 어긋난 채 통과할 수 있다.
    복제 대신 재사용하고 있는지 심볼로 확인한다."""
    src = (REPO / "scripts" / "topic_migration_workflow_launcher.py").read_text()
    assert "M._load_lock(" in src
    assert "M.LockCtx(" in src
    assert "M.verify(" in src
    # lock 형식 검사를 자체 구현하면 검증기와 갈린다
    assert "json.loads(lock" not in src


# ── 변이 반례 — "게이트가 있다"가 아니라 **없으면 통과한다**를 보인다 ─────────────────────
#    ⛔ 위 반례들이 다른 이유로 실패해 통과처럼 보일 수 있다. 각 게이트를 지운 사본이 실제로
#       통과 방향으로 새는지 확인한다. 안 새면 그 반례는 아무것도 지키지 않은 것이다.

def _mutant(tmp_path, old, new):
    src = (REPO / "scripts" / "topic_migration_workflow_launcher.py").read_text()
    assert src.count(old) == 1, "앵커가 코드와 어긋났다(수정 후 재동기화 누락)"
    path = tmp_path / "mutant_launcher.py"
    path.write_text(src.replace(old, new, 1))
    spec = importlib.util.spec_from_file_location("mutant_launcher", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SHA_GATE = """        if actual[name] != ctx.pinned_sha[name]:
            fail.append(f"[L_SHA] {name} 이 lock 과 다르다\\n"
                        f"    기대 {ctx.pinned_sha[name]}\\n    실제 {actual[name]}")
"""

PROPAGATE_GATE = """            if man.get("archive_sha") != ctx.pinned_sha["archive"] \\
                    or man.get("baseline_sha") != ctx.pinned_sha["baseline"] \\
                    or man.get("pinned_commit") != ctx.pinned_commit:
                fail.append("[L_PROPAGATE] manifest 의 sha/pinned_commit 이 lock 과 불일치")
"""

PREFLIGHT_GATE = """    if rc != 0:
        fail.append(f"[L_PREFLIGHT] preflight 실패(rc={rc}) — 분류를 채우고 근거 대조를 통과시킬 것. "
                    f"단독 실행: python3 scripts/topic_migration_manifest.py preflight")
"""


def _agreeing_lock_and_manifest(tmp_path):
    """⛔ lock 과 manifest 를 **서로 일치**시키되 실제 파일과는 어긋나게 만든다.
    이래야 L_PROPAGATE 가 가리지 않고 **L_SHA 만** 판정 대상이 된다 —
    이게 docstring 이 경고한 "입력과 lock 을 함께 바꾸면 통과" 시나리오다."""
    lock = _lock_variant(tmp_path, archive="0" * 64)
    man = json.loads((REPO / "spec" / "topic-only-migration-manifest.json").read_text())
    man["archive_sha"] = "0" * 64
    mpath = tmp_path / "agreeing.manifest.json"
    mpath.write_text(json.dumps(man, ensure_ascii=False))
    return lock, mpath


def test_sha_gate_catches_lock_and_manifest_rotated_together(tmp_path):
    """정상 코드: 파일만 다르면 L_SHA 하나로 막아야 한다(다른 게이트에 기대지 않는다)."""
    lock, mpath = _agreeing_lock_and_manifest(tmp_path)
    record, fail = L.gate(lock_path=lock, manifest_path=mpath, preflight_fn=lambda *_a: 0)
    assert record is None
    assert any("[L_SHA]" in f for f in fail), fail
    assert not any("[L_PROPAGATE]" in f for f in fail), "다른 게이트가 가리면 판정이 흐려진다"


def test_sha_gate_is_load_bearing(tmp_path):
    mut = _mutant(tmp_path, SHA_GATE, "")
    lock, mpath = _agreeing_lock_and_manifest(tmp_path)
    record, fail = mut.gate(lock_path=lock, manifest_path=mpath, preflight_fn=lambda *_a: 0)
    assert record is not None and fail == [], \
        "SHA 게이트를 지웠는데도 막혔다 — 위 반례가 다른 이유로 통과했을 수 있다"


def test_propagation_gate_is_load_bearing(tmp_path):
    mut = _mutant(tmp_path, PROPAGATE_GATE, "            pass\n")
    lock = _lock_variant(tmp_path, pinned_commit={"server": "a" * 40, "ios": "b" * 40})
    _record, fail = mut.gate(lock_path=lock, preflight_fn=lambda *_a: 0)
    assert not any("[L_PROPAGATE]" in f for f in fail)


def test_preflight_gate_is_load_bearing(tmp_path):
    mut = _mutant(tmp_path, PREFLIGHT_GATE, "")
    record, fail = mut.gate(preflight_fn=lambda *_a: 1)   # 실패를 주입해도
    assert record is not None and fail == [], \
        "preflight 게이트가 없으면 통과해야 한다 — 안 그러면 그 반례가 다른 것을 보고 있다"


def test_enum_pin_is_load_bearing(tmp_path):
    mut = _mutant(tmp_path,
                  "    archive_sha: {{ type: 'string', enum: ['{a_sha}'] }},",
                  "    archive_sha: {{ type: 'string' }},")
    record, _fail = mut.gate(preflight_fn=lambda *_a: 0)
    script = mut.render_workflow_script(record)
    assert "enum: ['%s']" % record["archive"]["sha256"] not in script


def test_write_happens_only_after_gate(tmp_path):
    """게이트 **전에** 쓰면 실패해도 산출물이 남는다 — 순서가 하중을 받는지 본다."""
    mut = _mutant(tmp_path, "    record, fail = gate(lock_path=a.lock)",
                  '    if a.out:\n'
                  '        a.out.parent.mkdir(parents=True, exist_ok=True)\n'
                  '        a.out.write_text("premature")\n'
                  "    record, fail = gate(lock_path=a.lock)")
    out = tmp_path / "premature.js"
    assert mut.main(["--out", str(out)]) == 1
    assert out.exists(), "순서를 뒤집었는데도 파일이 안 생겼다 — 반례가 순서를 안 지키고 있다"


def test_generated_script_has_no_raw_newline_inside_js_strings():
    """⛔ 렌더러가 f-string 이라 소스의 `\\n` 이 **렌더 시 진짜 개행**이 된다 —
    그러면 JS 홑따옴표 문자열이 줄을 넘어가 워크플로 파서가 거부한다(실측으로 거부당했다).
    ⚠️ 재측정: `node` 는 실제로 **잡았다**. 내 판정이 `head` 의 exit code 로 뒤집혔던 것이고,
    plain `node --check` 는 top-level `return` 을 오탐한다 — 그래서 감싸서 검사한다(아래 테스트)."""
    record, _fail = L.gate(preflight_fn=lambda *_a: 0)
    script = L.render_workflow_script(record)
    # 리터럴 두 글자 `\n` 이 남아 있어야 한다(진짜 개행으로 접히면 안 된다)
    assert "\\n\\n## 이번 렌즈\\n" in script
    # 홑따옴표가 홀수인 줄 = 문자열이 줄을 넘어간 흔적
    odd = [i + 1 for i, line in enumerate(script.splitlines()) if line.count("'") % 2 == 1]
    assert not odd, f"홑따옴표가 닫히지 않은 줄: {odd}"


def test_preflight_receives_the_same_manifest_that_gets_recorded(tmp_path):
    """⛔ record 에 적는 manifest 와 preflight 가 검사한 manifest 가 갈리면,
    "검증된 산출물"이라는 근거가 **다른 파일**을 가리킨다(실측 지적)."""
    seen = []

    def spy(lock_path, manifest_path):
        seen.append(manifest_path)
        return 0

    real = REPO / "spec" / "topic-only-migration-manifest.json"
    record, fail = L.gate(manifest_path=real, preflight_fn=spy)
    assert not fail, fail
    assert seen == [real], seen
    assert record["manifest"]["path"] == str(real)


def test_manifest_sha_is_pinned_as_enum():
    """리뷰 **대상**이 manifest 다 — 그 SHA 를 안 잠그면 생성 후 바뀌어도 통과한다."""
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    script = L.render_workflow_script(record)
    assert f"enum: ['{record['manifest']['sha256']}']" in script
    # 해시 명령에도 manifest 가 있어야 에이전트가 실제로 계산한다
    assert "PINNED.manifest.path}\"" in script


def test_review_schema_forces_full_block_coverage():
    """⛔ `findings: []` 가 유효한 이상, **검토 범위**를 따로 강제하지 않으면 스키마가 공허하다.
    ⚠️ 날조는 막지 못한다 — '조용히 일부만 보고 빈 findings' 를 막는 것이 목적이다."""
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    n = len(record["block_ids"])
    assert n >= 30, n
    script = L.render_workflow_script(record)
    assert "reviewed_blocks" in script
    assert f"minItems: {n}, maxItems: {n}" in script
    assert "uniqueItems: true" in script
    # block_id 가 자유 문자열이면 존재하지 않는 블록도 보고할 수 있다
    assert "block_id: { type: 'string' }," not in script
    # 결함 상한이 블록 수보다 작으면 다 보고할 수 없다
    assert "maxItems: 20," not in script


def test_manifest_sha_pin_is_load_bearing(tmp_path):
    mut = _mutant(tmp_path, "    manifest_sha: {{ type: 'string', enum: ['{m_sha}'] }},\n", "")
    record, _ = mut.gate(preflight_fn=lambda *_a: 0)
    assert f"enum: ['{record['manifest']['sha256']}']" not in mut.render_workflow_script(record)


def test_block_coverage_requirement_is_load_bearing(tmp_path):
    mut = _mutant(tmp_path, "'reviewed_blocks', ", "")
    record, _ = mut.gate(preflight_fn=lambda *_a: 0)
    script = mut.render_workflow_script(record)
    assert "required: ['archive_sha', 'baseline_sha', 'manifest_sha', 'findings']" in script


def test_preflight_manifest_passing_is_load_bearing(tmp_path):
    """게이트가 mpath 를 안 넘기면 preflight 는 **기본 manifest** 를 본다."""
    mut = _mutant(tmp_path, "(preflight_fn or _default_preflight)(lock_path, mpath)",
                  "(preflight_fn or _default_preflight)(lock_path, None)")
    seen = []
    real = REPO / "spec" / "topic-only-migration-manifest.json"
    mut.gate(manifest_path=real, preflight_fn=lambda l, m: (seen.append(m), 0)[1])
    assert seen == [None], f"mpath 가 여전히 전달됐다: {seen}"


def test_generated_script_parses_as_javascript():
    """⛔ 휴리스틱(홑따옴표 짝 세기)은 backtick 템플릿 리터럴 파손을 못 잡았다 —
    실제로 워크플로 파서가 두 번 거부했다(`\\n` 이스케이프 / 본문 backtick).
    진짜 파서로 검사한다. 워크플로 런타임은 스크립트를 async 함수 본문으로 실행하므로
    top-level `return` 이 합법이다 — 그대로 `node --check` 하면 **오탐**한다."""
    import shutil
    import subprocess

    if not shutil.which("node"):
        import pytest

        pytest.skip("node 없음 — 이 검사는 실제 파서를 요구한다")
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    script = L.render_workflow_script(record)
    wrapped = "async function __wf(){\n" + script.replace("export const meta", "const meta", 1) + "\n}\n"
    r = subprocess.run(["node", "--input-type=module", "--check"], input=wrapped,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"생성물이 JS 로 파싱되지 않는다:\n{r.stderr[:500]}"


def test_js_parse_check_is_load_bearing(tmp_path):
    """대조군 — 본문에 backtick 을 넣으면 이 검사가 실제로 거부해야 한다."""
    import shutil
    import subprocess

    if not shutil.which("node"):
        import pytest

        pytest.skip("node 없음")
    mut = _mutant(tmp_path, "⛔ reviewed_blocks 에는", "⛔ `reviewed_blocks` 에는")
    record, _ = mut.gate(preflight_fn=lambda *_a: 0)
    script = mut.render_workflow_script(record)
    wrapped = "async function __wf(){\n" + script.replace("export const meta", "const meta", 1) + "\n}\n"
    r = subprocess.run(["node", "--input-type=module", "--check"], input=wrapped,
                       capture_output=True, text=True)
    assert r.returncode != 0, "본문 backtick 을 넣었는데 파서가 통과시켰다 — 검사가 공허하다"


def test_gate_reports_non_object_manifest_instead_of_crashing(tmp_path):
    """⛔ 유효 JSON 이지만 object 가 아닌 manifest 에서 traceback 이 나면
    "검증기는 죽지 않고 보고한다"는 계약이 깨진다."""
    for body in ("[]", '"text"', "42", "null"):
        mpath = tmp_path / f"m{len(body)}{body[0]}.json"
        mpath.write_text(body)
        record, fail = L.gate(manifest_path=mpath, preflight_fn=lambda *_a: 0)
        assert record is None
        assert any("[L_MANIFEST]" in f for f in fail), (body, fail)


def test_gate_reports_manifest_without_blocks(tmp_path):
    """blocks 가 없으면 리뷰 커버리지 enum 을 만들 수 없다 — record 를 만들면 안 된다."""
    lk = json.loads((REPO / "spec" / "topic-only.lock.json").read_text())
    mpath = tmp_path / "noblocks.json"
    mpath.write_text(json.dumps({"archive_sha": lk["archive"]["sha256"],
                                 "baseline_sha": lk["baseline"]["sha256"],
                                 "pinned_commit": lk["pinned_commit"]}))
    record, fail = L.gate(manifest_path=mpath, preflight_fn=lambda *_a: 0)
    assert record is None
    assert any("[L_MANIFEST]" in f for f in fail), fail


def test_generated_script_binds_lens_before_filtering():
    """⛔ `filter(Boolean)` 뒤에 인덱스로 렌즈를 붙이면 한 렌즈가 실패했을 때
    이후 finding 이 **다른 렌즈 이름**을 단다. 실패 렌즈도 조용히 사라지면 안 된다."""
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    script = L.render_workflow_script(record)
    assert "reviews.map((r, i) => ({ lens: LENSES[i].key" in script
    assert "failed_lenses" in script and "complete:" in script
    # 필터 후 인덱스로 렌즈를 붙이는 형태가 남아 있으면 안 된다
    assert "filter(Boolean).flatMap((r, i)" not in script
    assert "LENSES[i].key }))" not in script.split("const labelled")[1]


def test_generated_script_fails_closed_on_lens_failure():
    """⛔ 실패 렌즈를 기록만 하고 정상 반환하면 fail-open 이다 —
    소비자가 `complete` 를 따로 거부하지 않으면 불완전한 리뷰가 완료로 처리된다."""
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    script = L.render_workflow_script(record)
    i_guard = script.index("if (failedLenses.length)")
    i_throw = script.index("throw new Error(")
    i_return = script.index("return {")
    assert i_guard < i_throw < i_return, "실패 분기 안에서, 반환 전에 던져야 한다"


def test_generated_script_reviews_the_references_model():
    """⛔ 소유/누락 렌즈는 `destination`·`normative_owner` 만 묻는다 — 참조의 **원자성·상태 승격**은
    그 질문에 걸리지 않는다. 참조 모델을 도입했으면 그것을 보는 렌즈가 있어야 한다."""
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    script = L.render_workflow_script(record)
    assert "key: 'references'" in script
    for must in ("원자적", "상태 승격", "proposed"):
        assert must in script, must
    # 렌즈가 늘면 실패 시 fail-closed 도 그만큼 넓어져야 한다(throw 는 렌즈 수와 무관해야 한다)
    assert "throw new Error(" in script


def test_delta_focuses_without_shrinking_coverage():
    """⛔ delta 는 **초점**이지 범위 축소가 아니다 — 전 블록 열거 강제가 남아야 한다.
    안 그러면 '일부만 보고 통과' 가 되어 커버리지 계약이 무너진다."""
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    n = len(record["block_ids"])
    plain = L.render_workflow_script(record)
    delta = L.render_workflow_script(record, ["R-CUT-20"])
    assert "우선 판정 대상(focus)" not in plain
    assert "우선 판정 대상(focus)" in delta and "R-CUT-20" in delta
    # ⛔ 이건 diff 가 아니라 초점이다 — "새 지적만" 이 아니라 **전부** 보고하게 해야 한다
    assert "찾은 지적은 전부" in delta
    for script in (plain, delta):
        assert f"minItems: {n}, maxItems: {n}" in script


def test_references_lens_covers_conditional_semantics():
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    script = L.render_workflow_script(record)
    for must in ("conditional_references", "rejected/superseded", "조건부 **범위**"):
        assert must in script, must


def test_references_lens_distinguishes_deferred_from_conditional():
    """⛔ 렌즈가 **연기** 축을 모르면 다음 리뷰가 새 관계를 심사하지 못한다.

    실제로 이 구분이 없어 "phased 로 미룬다" 를 conditional 로 적었고, 그 결과
    확정 요구가 근거 없이 미결정으로 격하됐다(2026-08-10, R-CLI-15 / R-GATE-7).
    문구는 지우기 쉬우므로 여기서 잠근다.
    """
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    script = L.render_workflow_script(record)
    for must in ("deferred_references",
                 "연기는 의존이 아니다",          # 방향이 반대라는 진술
                 "기각돼도",                      # 연기 대상이 죽어도 source 는 성립
                 "둘 이상"):                      # 관계 중복 금지
        assert must in script, must


def test_focus_rejects_unknown_rid(tmp_path, monkeypatch):
    """⛔ 오타 RID 를 통과시키면 '초점 없음' 이 조용히 되고, 아무도 그걸 모른다."""
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    monkeypatch.setattr(L, "gate", lambda **_kw: (record, []))
    out = tmp_path / "never.js"
    assert L.main(["--out", str(out), "--focus", "R-NOPE-999"]) == 2
    assert not out.exists()


def test_focus_rejects_injection_characters(tmp_path, monkeypatch):
    """⛔ 값이 그대로 JS 템플릿 리터럴에 들어가면 backtick·${} 로 스크립트가 깨지거나 평가된다."""
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    monkeypatch.setattr(L, "gate", lambda **_kw: (record, []))
    out = tmp_path / "never.js"
    for bad in ("X`y", "${PINNED.archive.path}", "R-CUT-20`+process.exit(1)+`"):
        assert L.main(["--out", str(out), "--focus", bad]) == 2, bad
        assert not out.exists(), bad


def test_focus_accepts_real_rid_and_keeps_full_coverage(tmp_path, monkeypatch):
    record, _ = L.gate(preflight_fn=lambda *_a: 0)
    monkeypatch.setattr(L, "gate", lambda **_kw: (record, []))
    out = tmp_path / "ok.js"
    rid = record["rids"][0]
    assert L.main(["--out", str(out), "--focus", rid]) == 0
    script = out.read_text()
    n = len(record["block_ids"])
    assert rid in script and f"minItems: {n}, maxItems: {n}" in script


def test_focus_validation_is_load_bearing(tmp_path):
    """대조군 — 검증을 지우면 없는 RID 가 통과해야 한다(안 그러면 반례가 다른 걸 보고 있다)."""
    mut = _mutant(tmp_path,
                  '        bad = [d for d in delta if not re.fullmatch(r"[RE]-[A-Z]+-[0-9]+", d) or d not in known]',
                  "        bad = []")
    record, _ = mut.gate(preflight_fn=lambda *_a: 0)
    mut.render_workflow_script(record, ["R-NOPE-999"])   # 예외가 나면 안 된다
