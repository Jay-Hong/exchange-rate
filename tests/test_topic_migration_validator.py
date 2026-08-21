"""표준 스위트에서 **반드시 돌게** 한다 — scripts/ 독립 실행이면 썩는다.

검증기의 신뢰 근거는 본체가 아니라 **반례 테스트**다(2026-08-09: 검증기가 5라운드 연속 구멍).
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent


def _validator_module():
    path = REPO / "scripts" / "topic_migration_manifest.py"
    spec = importlib.util.spec_from_file_location("topic_migration_validator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ios_provenance_root_honors_ci_override(monkeypatch, tmp_path):
    """Actions checkout은 workspace 내부라 local sibling 경로를 명시적으로 대체한다."""
    module = _validator_module()
    monkeypatch.setenv(module.IOS_ROOT_ENV, str(tmp_path))
    assert module.provenance_root("ios") == tmp_path
    assert module.provenance_root("server") == REPO


def test_canonical_only_citation_change_reaches_codechanged(tmp_path):
    """baseline에 없는 canonical RID 근거도 provenance 변경 감지까지 이어진다."""
    module = _validator_module()
    repo = tmp_path / "server"
    (repo / "app").mkdir(parents=True)
    cited_file = repo / "app" / "canonical_only.py"
    cited_file.write_text("VALUE = 1\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "app/canonical_only.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "pin"], check=True)
    pinned = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    document = tmp_path / "canonical.md"
    document.write_text(
        "<!-- rid: R-TEST-1 -->\n"
        "현재 값은 `app/canonical_only.py:1`에 있다.\n"
        "<!-- /rid: R-TEST-1 -->\n"
    )
    paths = module.canonical_cited_paths([document])
    assert paths == {"server": ["app/canonical_only.py"], "ios": []}

    cited_file.write_text("VALUE = 2\n")
    errors = module.default_provenance(
        "server", paths["server"], pinned, roots={"server": repo, "ios": tmp_path / "ios"}
    )
    assert any("E_CODECHANGED" in error and "canonical_only.py" in error for error in errors)


def test_verify_unions_canonical_citations_into_provenance(monkeypatch):
    """Parser와 runner가 따로 살아 있어도 verify 배선이 빠지면 근거 대조는 공허하다."""
    module = _validator_module()
    sentinel = "app/__canonical_only_probe__.py"
    monkeypatch.setattr(
        module,
        "canonical_cited_paths",
        lambda *args, **kwargs: {"server": [sentinel], "ios": []},
    )
    calls = {}

    def record(repo_key, paths, pinned):
        calls[repo_key] = {"paths": set(paths), "pinned": pinned}
        return []

    assert module.verify(provenance=True, provenance_fn=record) == 0
    assert sentinel in calls["server"]["paths"]
    assert calls["server"]["pinned"] == module._DEFAULT_CTX.pinned_commit["server"]


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
    env = dict(os.environ)
    # 이 자식은 default sibling 분기의 대조군도 검사한다. CI override 자체는 위 테스트가 맡는다.
    env.pop(_validator_module().IOS_ROOT_ENV, None)
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "test_topic_migration_manifest.py")],
        capture_output=True, text=True, cwd=str(REPO), env=env,
    )
    assert r.returncode == 0, f"반례 테스트 실패:\n{r.stdout}\n{r.stderr}"
