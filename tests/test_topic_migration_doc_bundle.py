import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import topic_migration_doc_bundle as B  # noqa: E402


def _write(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    path = tmp_path / "bundle.md"
    path.write_text(text)
    return path


def test_rendered_bundle_passes_byte_for_byte_check(tmp_path):
    path = _write(tmp_path, B.render_bundle())
    assert B.check_bundle(path) == []


def test_rendered_bundle_contains_every_requirement_once_and_full_source():
    text = B.render_bundle()
    manifest = json.loads((REPO / "spec/topic-only-migration-manifest.json").read_text())
    expected = {
        r["rid"] for block in manifest["blocks"] for r in block.get("requirements", [])
    }
    rendered = re.findall(r"^## ([RE]-[A-Z]+-\d+)  L\d+-\d+", text, re.M)
    assert len(rendered) == len(expected)
    assert set(rendered) == expected

    archive = (REPO / "TOPIC_ONLY_DELIVERY_CONTRACT.archive.md").read_text().splitlines()
    hand = next(
        r
        for block in manifest["blocks"]
        for r in block.get("requirements", [])
        if r["rid"] == "R-HAND-2"
    )
    full_source = "\n".join(archive[hand["source"]["start"] - 1: hand["source"]["end"]])
    assert full_source in text


def test_bundle_carries_metadata_and_supplemental_pinned_inputs():
    text = B.render_bundle()
    manifest = json.loads((REPO / "spec/topic-only-migration-manifest.json").read_text())
    assert "# source_metadata=" + json.dumps(
        manifest["source_metadata"], ensure_ascii=False, sort_keys=True
    ) in text
    assert "# pinned_commit=" + json.dumps(
        manifest["pinned_commit"], ensure_ascii=False, sort_keys=True
    ) in text

    for name in B.PIN_FILES[2:]:
        assert f"## {name}" in text
        assert (REPO / name).read_text().rstrip("\n") in text

    assert "required document header:" in text
    assert "<!-- requirement-meta: disposition=" in text
    assert "<!-- relation: references target=" in text
    assert re.search(r"^- references: \[[RE]-[A-Z]+-\d+\]\([^)]+\)$", text, re.M)


def test_target_document_contract_carries_full_evidence_source():
    text = B.render_bundle()
    manifest = json.loads((REPO / "spec/topic-only-migration-manifest.json").read_text())
    archive = (REPO / "TOPIC_ONLY_DELIVERY_CONTRACT.archive.md").read_text().splitlines()
    evidence = next(
        r
        for block in manifest["blocks"]
        for r in block.get("requirements", [])
        if r["rid"] == "E-INV-1"
    )
    source = evidence["source"]
    body = "\n".join(archive[source["start"] - 1:source["end"]])
    rendered = (
        "<!-- evidence: E-INV-1 supports=R-INV-1 -->\n"
        f"{body}\n"
        "<!-- /evidence: E-INV-1 -->"
    )
    assert rendered in text
    assert "[substantive evidence prose from E-INV-1]" not in text


def test_source_fence_is_longer_than_nested_markdown_fences():
    source = "before\n```text\ninside\n```\nafter"
    fence = B.source_fence(source)
    assert fence == "````"
    assert fence not in source


def test_check_rejects_a_missing_pin(tmp_path):
    text = re.sub(
        r"^# SHA256 \S+  TOPIC_ONLY_MIGRATION_MATRIX\.md$\n",
        "",
        B.render_bundle(),
        flags=re.M,
    )
    errors = B.check_bundle(_write(tmp_path, text))
    assert any("missing pins" in error for error in errors)


def test_check_rejects_a_duplicate_pin(tmp_path):
    text = B.render_bundle()
    line = next(line for line in text.splitlines() if line.endswith("spec/topic-only.lock.json"))
    text = text.replace(line, f"{line}\n{line}", 1)
    errors = B.check_bundle(_write(tmp_path, text))
    assert any("duplicate pins" in error for error in errors)


def test_check_rejects_a_forged_sha(tmp_path):
    text = re.sub(r"(?m)^(# SHA256 )[0-9a-f]{64}", rf"\g<1>{'0' * 64}", B.render_bundle(), count=1)
    errors = B.check_bundle(_write(tmp_path, text))
    assert any("SHA mismatch" in error for error in errors)


def test_check_rejects_a_forged_count(tmp_path):
    text = re.sub(r"# total_lines=\d+", "# total_lines=1", B.render_bundle(), count=1)
    errors = B.check_bundle(_write(tmp_path, text))
    assert any("fresh render" in error for error in errors)


def test_check_rejects_truncated_requirement_content(tmp_path):
    text = B.render_bundle().replace("## R-HAND-19", "## REMOVED-HAND-19", 1)
    errors = B.check_bundle(_write(tmp_path, text))
    assert any("fresh render" in error for error in errors)
