"""D1 slice 4B contract — the policy digest and the review-environment record.

Written before the implementation (Claude), from `d1_detection_policy_v1.txt` §7.3 as amended by
`d1_detection_policy_v1_amendment1.txt` §A2–§A3 (both under `tools/fixture_capture/d1_spec/`), and the slice-4B design
agreement with Codex (2026-09-21). The implementer reads this file and does not edit it.

API (module `tools/fixture_capture/d1_digest.py`):
- `SPEC_PATH`, `AMENDMENT_PATHS`, `IMPLEMENTATION_FILES` — repo-relative POSIX paths, exactly as pinned below.
- `policy_descriptor(root=None) -> dict` — `{"policy_spec_sha256", "policy_amendments", "implementation_files"}` from the
  actual bytes under `root` (default: the checkout this module is in, independent of the working directory). A fresh object
  every call. No runtime information.
- `policy_digest(descriptor) -> str` — validates the closed schema, then SHA-256 (lowercase hex) of `canonical_json`.
- `canonical_json(value) -> bytes` — sorted keys, UTF-8, `ensure_ascii=False`, `(",", ":")`, NaN/Infinity refused.
- `runtime_descriptor() -> dict` — the review-environment record (§A3). Not a digest input. Read at call time through the
  module attributes (`platform.python_implementation()`, `platform.python_version()`, `unicodedata.unidata_version`,
  `bs4.__version__`, `soupsieve.__version__`, `html.parser.__file__`, `_markupbase.__file__`), never cached at import.
Every refusal is `CaptureError`; its text carries no path and no page value, and no OS error is chained into it.

This slice is a library. Nothing here makes admission compare anything — that wiring is slice 5.
"""

import ast
import hashlib
import html.parser
import json
import os
import platform
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import _markupbase
import bs4
import pytest
import soupsieve

from tools.fixture_capture import d1_digest as G
from tools.fixture_capture.errors import CaptureError

REPO = Path(__file__).resolve().parents[1]
SPEC = "tools/fixture_capture/d1_spec/d1_detection_policy_v1.txt"
AMENDMENTS = ("tools/fixture_capture/d1_spec/d1_detection_policy_v1_amendment1.txt",
              # Slice 5c-3a-B: amendment 2 §A4, the reviewed empty unsupported elements cleanup may remove.
              "tools/fixture_capture/d1_spec/d1_detection_policy_v1_amendment2.txt")
IMPLEMENTATION = (
    "tools/fixture_capture/__init__.py",
    "tools/fixture_capture/errors.py",
    "tools/fixture_capture/d1_policy.py",
    "tools/fixture_capture/d1_observe.py",
    "tools/fixture_capture/d1_findings.py",
    "tools/fixture_capture/d1_replace.py",
    "tools/fixture_capture/d1_digest.py",
    # Slice 5b-2: the admission layer and everything it imports (the detector no longer reaches the registry, 5b-1).
    "tools/fixture_capture/queries.py",
    "tools/fixture_capture/limits.py",
    "tools/fixture_capture/detector.py",
    "tools/fixture_capture/d1_approval.py",
    "tools/fixture_capture/admission.py",
)
ALL_FILES = (SPEC, *AMENDMENTS, *IMPLEMENTATION)
V1_SHA256 = "47d090a63997116d8df9af80b3a04a3e99a5a62c487aaba34236d1897e273635"
# Entry points chosen by this contract, not derived from the implementation's own manifest: removing an entry
# module from IMPLEMENTATION_FILES must still be caught.
ENTRY_MODULES = ("d1_findings", "d1_replace", "d1_digest", "admission")
_DIAGNOSTIC = re.compile(r"[A-Za-z0-9_.:\[\] -]*")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def expected_digest(descriptor):
    return sha(json.dumps(descriptor, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8"))


def refused(call, rule=None, location=None):
    with pytest.raises(CaptureError) as caught:
        call()
    error = caught.value
    text = str(error)
    assert _DIAGNOSTIC.fullmatch(text), text.encode("unicode_escape")
    assert "/" not in text and "\\" not in text
    # No OS error rides along: nothing chained, and any exception being handled is suppressed.
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)
    if rule is not None:
        assert error.rule == rule
    if location is not None:
        assert error.location == location
    return error


def copy_root(tmp_path, mutate=None):
    root = tmp_path / "root"
    for rel in ALL_FILES:
        data = (REPO / rel).read_bytes()
        if mutate is not None:
            data = mutate(rel, data)
        if data is None:
            continue
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


# ── the pinned file set ──────────────────────────────────────────────────────

def test_paths_are_pinned():
    assert G.SPEC_PATH == SPEC
    assert tuple(G.AMENDMENT_PATHS) == AMENDMENTS
    assert tuple(G.IMPLEMENTATION_FILES) == IMPLEMENTATION


def test_the_repository_copy_of_the_spec_is_v1_byte_for_byte():
    assert sha((REPO / SPEC).read_bytes()) == V1_SHA256


def _imports(module_path):
    tree = ast.parse(module_path.read_bytes(), filename=str(module_path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").split(".")[0] == "importlib", "dynamic import"
            if node.level == 1 and node.module:                               # from .x import y
                found.add(node.module.split(".")[0])
            elif node.level == 1:                                             # from . import x
                found.update(alias.name for alias in node.names)
            elif node.level == 0 and node.module == "tools.fixture_capture":  # from tools.fixture_capture import x
                found.update(alias.name for alias in node.names)
            elif node.level == 0 and (node.module or "").startswith("tools.fixture_capture."):
                found.add(node.module.split(".")[2])
            else:
                assert node.level == 0, "only one-level relative imports are expected"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "importlib" and not alias.name.startswith("importlib."), "dynamic import"
                if alias.name.startswith("tools.fixture_capture."):
                    found.add(alias.name.split(".")[2])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            # exec/eval can run an import statement held in a string; the walker cannot follow it.
            assert node.func.id not in ("__import__", "exec", "eval"), "dynamic import"
    return found


def _closure(package):
    # The package __init__ runs on every import, so it is a starting point too: hashing it alone does not bind
    # a file it pulls in.
    seen, queue = set(), ["__init__", *ENTRY_MODULES]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        path = package / f"{name}.py"
        assert path.is_file(), name
        queue.extend(_imports(path) - seen)
    return {f"tools/fixture_capture/{name}.py" for name in seen}


def test_the_manifest_is_exactly_the_import_closure():
    # Equality, not a subset (slice 5b-2): a file left in the manifest after nothing imports it is also a mistake.
    closure = _closure(REPO / "tools" / "fixture_capture")
    assert "tools/fixture_capture/__init__.py" in closure
    assert closure == set(IMPLEMENTATION), (sorted(closure - set(IMPLEMENTATION)), sorted(set(IMPLEMENTATION) - closure))


def test_the_closure_follows_what_the_package_init_imports(tmp_path):
    package = tmp_path / "tools" / "fixture_capture"
    package.mkdir(parents=True)
    for name in (Path(rel).stem for rel in IMPLEMENTATION):
        (package / f"{name}.py").write_bytes((REPO / "tools" / "fixture_capture" / f"{name}.py").read_bytes())
    init = package / "__init__.py"
    init.write_bytes(init.read_bytes() + b"from .extra_policy import RULES\n")
    (package / "extra_policy.py").write_text("RULES = ()\n", encoding="utf-8")
    assert "tools/fixture_capture/extra_policy.py" in _closure(package)
    init.write_bytes((REPO / "tools" / "fixture_capture" / "__init__.py").read_bytes() + b"import importlib\n")
    with pytest.raises(AssertionError):
        _closure(package)


def test_the_import_walker_sees_what_it_claims_to_see(tmp_path):
    # The closure test is only as good as its walker: prove it follows every import form it handles,
    # including one nested in a function body.
    path = tmp_path / "probe.py"
    path.write_text("from .a import x\nfrom . import b\nfrom tools.fixture_capture import c\n"
                    "from tools.fixture_capture.d import y\nimport tools.fixture_capture.e\nimport json\n"
                    "def later():\n    from .f import z\n", encoding="utf-8")
    assert _imports(path) == {"a", "b", "c", "d", "e", "f"}
    for dynamic in ("import importlib\n", "from importlib import import_module\n", "__import__('x')\n",
                    "exec('from .hidden import y')\n", "eval('1')\n"):
        path.write_text(dynamic, encoding="utf-8")
        with pytest.raises(AssertionError):
            _imports(path)


# ── the descriptor ───────────────────────────────────────────────────────────

def test_the_descriptor_is_the_actual_bytes_and_nothing_else():
    descriptor = G.policy_descriptor()
    assert set(descriptor) == {"policy_spec_sha256", "policy_amendments", "implementation_files"}
    assert descriptor["policy_spec_sha256"] == V1_SHA256
    assert descriptor["policy_amendments"] == [sha((REPO / rel).read_bytes()) for rel in AMENDMENTS]
    assert type(descriptor["policy_amendments"]) is list
    assert descriptor["implementation_files"] == {rel: sha((REPO / rel).read_bytes()) for rel in IMPLEMENTATION}
    assert G.policy_digest(descriptor) == expected_digest(descriptor)


def test_the_default_root_does_not_depend_on_the_working_directory(tmp_path, monkeypatch):
    before = G.policy_descriptor()
    monkeypatch.chdir(tmp_path)
    assert G.policy_descriptor() == before


def test_the_default_root_is_fixed_by_the_module_not_by_the_import_time_directory(tmp_path):
    # A root taken from the working directory at import time passes every in-process test run from the repo root.
    probe = ("import sys; from tools.fixture_capture import d1_digest as G; "
             "sys.stdout.write(G.policy_digest(G.policy_descriptor()))")
    result = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, capture_output=True, text=True, timeout=60,
                            env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout == G.policy_digest(G.policy_descriptor())


def test_an_explicit_root_with_the_same_bytes_gives_the_same_digest(tmp_path):
    root = copy_root(tmp_path)
    assert G.policy_digest(G.policy_descriptor(root)) == G.policy_digest(G.policy_descriptor())


def test_each_call_returns_a_fresh_object():
    first = G.policy_descriptor()
    digest = G.policy_digest(first)
    first["implementation_files"]["tools/fixture_capture/x.py"] = "0" * 64
    first["policy_amendments"].append("0" * 64)
    first["policy_spec_sha256"] = "0" * 64
    second = G.policy_descriptor()
    assert G.policy_digest(second) == digest


@pytest.mark.parametrize("rel", [SPEC, AMENDMENTS[0], IMPLEMENTATION[0], IMPLEMENTATION[-1]])
def test_the_same_root_is_read_again_on_every_call(tmp_path, rel):
    # Different roots per call would let a per-root cache pass every other test.
    root = copy_root(tmp_path)
    first = G.policy_digest(G.policy_descriptor(root))
    path = root / rel
    original = path.read_bytes()
    path.write_bytes(original + b"\n")
    assert G.policy_digest(G.policy_descriptor(root)) != first
    path.write_bytes(original)
    assert G.policy_digest(G.policy_descriptor(root)) == first
    path.unlink()
    location = ("policy_spec" if rel == SPEC else "policy_amendments[0]" if rel == AMENDMENTS[0]
                else f"implementation_files[{IMPLEMENTATION.index(rel)}]")
    refused(lambda: G.policy_descriptor(root), "d1_digest_unreadable", location)


@pytest.mark.parametrize("rel", ALL_FILES)
def test_one_changed_byte_anywhere_changes_the_digest(tmp_path, rel):
    baseline = G.policy_digest(G.policy_descriptor(copy_root(tmp_path / "a")))

    def flip(path, data):
        return data[:-1] + bytes([data[-1] ^ 1]) if path == rel else data

    assert G.policy_digest(G.policy_descriptor(copy_root(tmp_path / "b", flip))) != baseline


@pytest.mark.parametrize("change", [
    lambda data: data.replace(b"\n", b"\r\n"),     # a checkout that converts newlines is a different policy input
    lambda data: b"\xef\xbb\xbf" + data,            # nor is a BOM stripped
])
def test_bytes_are_read_without_any_conversion(tmp_path, change):
    baseline = G.policy_descriptor(copy_root(tmp_path / "a"))
    root = copy_root(tmp_path / "b", lambda path, data: change(data) if path == SPEC else data)
    assert G.policy_descriptor(root)["policy_spec_sha256"] != baseline["policy_spec_sha256"]


@pytest.mark.parametrize("rel, location", [
    (SPEC, "policy_spec"),
    *[(rel, f"policy_amendments[{i}]") for i, rel in enumerate(AMENDMENTS)],
    *[(rel, f"implementation_files[{i}]") for i, rel in enumerate(IMPLEMENTATION)],
])
def test_a_missing_file_refuses_at_its_manifest_position(tmp_path, rel, location):
    root = copy_root(tmp_path, lambda path, data: None if path == rel else data)
    refused(lambda: G.policy_descriptor(root), "d1_digest_unreadable", location)


def test_a_directory_in_place_of_a_file_refuses(tmp_path):
    root = copy_root(tmp_path, lambda path, data: None if path == IMPLEMENTATION[3] else data)
    (root / IMPLEMENTATION[3]).mkdir()
    refused(lambda: G.policy_descriptor(root), "d1_digest_unreadable", "implementation_files[3]")


# ── the digest and its closed schema ─────────────────────────────────────────

def _valid():
    return {"policy_spec_sha256": "a" * 64, "policy_amendments": ["b" * 64, "c" * 64],
            "implementation_files": {"tools/fixture_capture/x.py": "d" * 64, "tools/fixture_capture/y.py": "e" * 64}}


def test_key_insertion_order_does_not_matter():
    forward = _valid()
    backward = {key: forward[key] for key in reversed(list(forward))}
    backward["implementation_files"] = dict(reversed(list(forward["implementation_files"].items())))
    assert G.policy_digest(forward) == G.policy_digest(backward) == expected_digest(forward)


def test_amendment_order_does_matter():
    forward = _valid()
    swapped = _valid()
    swapped["policy_amendments"] = list(reversed(forward["policy_amendments"]))
    assert G.policy_digest(forward) != G.policy_digest(swapped)


@pytest.mark.parametrize("breaks", [
    lambda d: d.update(runtime={"python_version": "3.13.5"}),       # the projection is explicit, never silent
    lambda d: d.pop("policy_amendments"),
    lambda d: d.update(policy_spec_sha256="A" * 64),                 # lowercase only
    lambda d: d.update(policy_spec_sha256="a" * 63),
    lambda d: d.update(policy_amendments=("b" * 64,)),               # a list, not a tuple
    lambda d: d.update(policy_amendments=["b" * 64, 1]),
    lambda d: d.update(implementation_files=[("x", "d" * 64)]),
    lambda d: d["implementation_files"].update({1: "d" * 64}),
    lambda d: d["implementation_files"].update({"tools/fixture_capture/z.py": True}),
])
def test_the_descriptor_schema_is_closed(breaks):
    descriptor = _valid()
    breaks(descriptor)
    refused(lambda: G.policy_digest(descriptor))


def test_canonical_json_is_the_fixed_serialization():
    value = {"b": ["한", 1], "a": {"d": "x", "c": None}}
    assert G.canonical_json(value) == '{"a":{"c":null,"d":"x"},"b":["한",1]}'.encode("utf-8")


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_refuses_non_finite_numbers(number):
    refused(lambda: G.canonical_json({"x": number}))


# ── the review environment ───────────────────────────────────────────────────

RUNTIME_KEYS = {"python_implementation", "python_version", "unicode_version", "bs4_version", "soupsieve_version",
                "parser", "html_parser_sha256", "markupbase_sha256"}


def test_the_runtime_record_describes_this_interpreter():
    runtime = G.runtime_descriptor()
    assert set(runtime) == RUNTIME_KEYS
    assert runtime == {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "unicode_version": unicodedata.unidata_version,
        "bs4_version": bs4.__version__,
        "soupsieve_version": soupsieve.__version__,
        "parser": "html.parser",
        "html_parser_sha256": sha(Path(html.parser.__file__).read_bytes()),
        "markupbase_sha256": sha(Path(_markupbase.__file__).read_bytes()),
    }
    assert all(type(value) is str and "/" not in value and "\\" not in value for value in runtime.values())


def test_the_runtime_is_not_a_digest_input(tmp_path, monkeypatch):
    digest = G.policy_digest(G.policy_descriptor())
    before = G.runtime_descriptor()
    other = tmp_path / "parser.py"
    other.write_bytes(b"# a different parser module\n")
    monkeypatch.setattr(html.parser, "__file__", str(other))
    monkeypatch.setattr(platform, "python_version", lambda: "3.13.15")
    after = G.runtime_descriptor()
    assert after["html_parser_sha256"] == sha(other.read_bytes()) != before["html_parser_sha256"]
    assert after["python_version"] == "3.13.15"                        # read at call time, not cached at import
    assert G.policy_digest(G.policy_descriptor()) == digest


def _raise():
    raise RuntimeError("unavailable")


# (owner, attribute, field, is_callable) — how each version value is obtained.
VERSION_SOURCES = [
    (platform, "python_implementation", "python_implementation", True),
    (platform, "python_version", "python_version", True),
    (unicodedata, "unidata_version", "unicode_version", False),
    (bs4, "__version__", "bs4_version", False),
    (soupsieve, "__version__", "soupsieve_version", False),
]


@pytest.mark.parametrize("owner, attribute, field, is_callable", VERSION_SOURCES)
def test_every_version_is_read_at_call_time(monkeypatch, owner, attribute, field, is_callable):
    monkeypatch.setattr(owner, attribute, (lambda: "9.9.9-probe") if is_callable else "9.9.9-probe")
    assert G.runtime_descriptor()[field] == "9.9.9-probe"


@pytest.mark.parametrize("owner, attribute, field, is_callable, how", [
    (*source, how) for source in VERSION_SOURCES
    for how in ("missing", "none", "empty", "not_a_string", *(("raises",) if source[3] else ()))])
def test_a_version_that_cannot_be_obtained_refuses(monkeypatch, owner, attribute, field, is_callable, how):
    if how == "missing":
        monkeypatch.delattr(owner, attribute)
    elif how == "raises":
        monkeypatch.setattr(owner, attribute, _raise)
    else:
        value = {"none": None, "empty": "", "not_a_string": 3}[how]
        monkeypatch.setattr(owner, attribute, (lambda: value) if is_callable else value)
    refused(G.runtime_descriptor, "d1_runtime_unavailable", f"runtime.{field}")


@pytest.mark.parametrize("module, field", [(html.parser, "html_parser_sha256"), (_markupbase, "markupbase_sha256")])
def test_an_unreadable_parser_module_refuses(tmp_path, monkeypatch, module, field):
    monkeypatch.setattr(module, "__file__", str(tmp_path / "gone" / "parser.py"))
    refused(G.runtime_descriptor, "d1_runtime_unavailable", f"runtime.{field}")
    monkeypatch.delattr(module, "__file__")                             # a frozen module has no file
    refused(G.runtime_descriptor, "d1_runtime_unavailable", f"runtime.{field}")

