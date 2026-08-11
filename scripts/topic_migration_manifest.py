#!/usr/bin/env python3
"""Topic-only 계약 분할 — manifest 생성 + **기계적** 검증기.

왜 이게 필요한가
----------------
LLM 이 "요구사항을 다 뽑았다"고 말하는 것으로는 **완전성이 증명되지 않는다**.
증명 가능한 형태는 원본을 **행 범위로 분할**하는 것이다 —
archive 의 모든 줄이 정확히 하나의 블록에 속하고(gap·overlap 0),
각 블록에 disposition 이 있고, active 요구사항의 normative owner 가 정확히 하나면
그건 **셀 수 있는 사실**이다.

⛔ **"동결"이라 부르지 않는다.** `chmod 444` 는 git 이 보존하지 않고, 기대 SHA 도 **같은 사람이
   고칠 수 있는 lock** 에 있다. 입력과 lock 을 함께 바꾸면 통과한다 — 이건 **정본(single source)**
   이지 동결이 아니다. 진짜 기준점은 **승인된 commit/tag** 이고, 그 전까지 이 검사는
   "우발적 불일치 탐지"까지만 한다.

⛔ `HEAD 일치` 도 검증기가 아니다 — 문서를 커밋하면 HEAD 는 당연히 달라지는데
   인용한 **코드**는 그대로일 수 있다. 그래서 pinned commit 대비 **인용 경로의 diff** 를 본다.

사용
----
    python3 scripts/topic_migration_manifest.py skeleton    # 분할 초안 생성(1회, lock 유효할 때만)
    python3 scripts/topic_migration_manifest.py verify      # 구조 검증(CI 안전 — 단일 리포)
    python3 scripts/topic_migration_manifest.py preflight   # 구조 + 인용 근거 대조(../ios 또는 CI root override 필요)

⛔ 세 명령은 **옵션이 다르다**(subparser). `--manifest`/`--lock` 은 verify/preflight 전용 —
   예전엔 skeleton 이 `--manifest` 를 조용히 무시하고 운영 manifest 를 건드렸다(실측).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
LOCK_PATH = REPO / "spec" / "topic-only.lock.json"
IOS_ROOT_ENV = "TOPIC_MIGRATION_IOS_ROOT"


def _load_lock(lock_path: pathlib.Path | None = None):
    """⛔ import 시점에 예외를 던지지 않는다 — 검증기는 죽지 않고 **보고**해야 한다.
    ⚠️ 문법만 보면 부족하다: {} / [] / {"archive": 1} 같은 **형태 오류**가 전역 초기화에서 터진다(실측)."""
    try:
        lp = lock_path or LOCK_PATH
        raw = lp.read_text()
    except OSError as e:
        return None, f"[E_LOCK_MISSING] lock 읽기 실패: {e}"
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"[E_LOCK_BROKEN] lock JSON 손상: {e}"
    if not isinstance(d, dict):
        return None, f"[E_LOCK_SHAPE] lock 최상위가 object 가 아님: {type(d).__name__}"
    for key in ("archive", "baseline"):
        ent = d.get(key)
        if not isinstance(ent, dict) or not isinstance(ent.get("path"), str) \
                or not isinstance(ent.get("sha256"), str):
            return None, f"[E_LOCK_SHAPE] lock.{key} 는 {{path:str, sha256:str}} 여야 한다"
        # ⛔ 길이만 보면 'z'*64 가 통과한다(실측)
        if not re.fullmatch(r"[0-9a-f]{64}", ent["sha256"]):
            return None, f"[E_LOCK_SHAPE] lock.{key}.sha256 이 64자 hex 가 아니다"
        rel = pathlib.Path(ent["path"])
        if rel.is_absolute() or ".." in rel.parts:
            return None, f"[E_LOCK_PATH] lock.{key}.path 가 리포 밖을 가리킴: {ent['path']}"
        target = REPO / rel
        if not target.is_file():
            return None, f"[E_LOCK_PATH] lock.{key}.path 대상 없음: {ent['path']}"
        # ⛔ `..` 만 막으면 **리포 안 symlink 가 밖을 가리키는** 경우를 놓친다
        try:
            if not target.resolve().is_relative_to(REPO.resolve()):
                return None, f"[E_LOCK_PATH] lock.{key}.path 가 리포 밖으로 해석됨(symlink?): {ent['path']}"
        except OSError as e:
            return None, f"[E_LOCK_PATH] lock.{key}.path 해석 실패: {e}"
    pc = d.get("pinned_commit")
    if not isinstance(pc, dict) or not {"server", "ios"} <= set(pc):
        return None, "[E_LOCK_SHAPE] lock.pinned_commit 에 server/ios 필요"
    # ⛔ 키 존재만 보면 {'server': 123, 'ios': []} 가 통과한다(실측)
    for k in ("server", "ios"):
        if not isinstance(pc[k], str) or not re.fullmatch(r"[0-9a-f]{40}", pc[k]):
            return None, f"[E_LOCK_SHAPE] lock.pinned_commit.{k} 는 **full 40자 hex** 여야 한다(단축은 충돌 가능): {pc[k]!r}"
    return d, None


class LockCtx:
    """⛔ lock 파생 상태를 전역에 두면 custom lock 검증이 **다음 호출·다른 함수**를 오염시킨다
    (실측: custom lock verify 뒤 build_skeleton 이 가짜 pin 을 썼다). 호출마다 만든다."""

    __slots__ = ("raw", "archive", "baseline", "pinned_sha", "pinned_commit")

    def __init__(self, lk: dict | None):
        self.raw = lk or {}
        self.archive = REPO / (lk["archive"]["path"] if lk else "TOPIC_ONLY_DELIVERY_CONTRACT.archive.md")
        self.baseline = REPO / (lk["baseline"]["path"] if lk else "spec/topic-only-baseline-facts.md")
        self.pinned_sha = {k: self.raw.get(k, {}).get("sha256") for k in ("archive", "baseline")}
        self.pinned_commit = self.raw.get("pinned_commit", {})


_LOCK, _LOCK_ERR = _load_lock()
_DEFAULT_CTX = LockCtx(_LOCK)
DEFAULT_MANIFEST = REPO / "spec" / "topic-only-migration-manifest.json"

_SERVER_CITE = re.compile(
    r"(?<![\w./-])(?:exchange-rate/|\./)?"
    r"(?P<path>(?:app|nginx|scripts)/[\w./-]+\.(?:py|conf))(?!\.?[A-Za-z0-9_-])"
)
_IOS_CITE = re.compile(
    r"(?<![\w./-])(?:ios/|\./)?(?P<path>FXi/[\w./-]+\.swift)(?!\.?[A-Za-z0-9_-])"
)
_CLAUDE_CITE = re.compile(
    r"(?<![\w./-])(?:(?P<repo>exchange-rate|ios)/|\./)?"
    r"(?P<path>CLAUDE\.md)(?!\.?[A-Za-z0-9_-])"
)


def cited_paths(baseline: pathlib.Path | None = None) -> dict[str, list[str]]:
    """⛔ 손으로 적지 않는다 — baseline 본문에서 **도출**한다.
    손 목록은 baseline 이 늘 때 반드시 어긋나고, 그러면 바뀐 파일을 검증기가 놓친다."""
    text = (baseline or _DEFAULT_CTX.baseline).read_text()
    out: dict[str, set] = {"server": set(), "ios": set()}
    for m in _SERVER_CITE.finditer(text):
        out["server"].add(m.group("path"))
    for m in _IOS_CITE.finditer(text):
        out["ios"].add(m.group("path"))
    for m in _CLAUDE_CITE.finditer(text):
        out["ios" if m.group("repo") == "ios" else "server"].add(m.group("path"))
    return {k: sorted(v) for k, v in out.items()}

# ⛔ 인용 경로가 **줄어드는** 퇴행은 개수 하한("0개 금지")으로 못 잡는다 — server 11개를 1개로
#    줄여도 통과했다(실측 rc=0). 그래서 **독립된 넓은 그물**을 따로 두고 narrow 가 그것을 덮는지 본다.
#    ⚠️ 그물이 narrow 와 같은 축(접두사+확장자)을 쓰면 narrow 의 **특례 분기**(CLAUDE.md)를 못 덮는다 —
#       실제로 그 분기만 지워도 통과했다(실측). 그래서 접두사에 기대지 않는 **파일형 토큰**으로 훑는다.
# ⛔ 후행 판정은 `(?![\w.-])` 가 아니다 — 그러면 `app/main.py.` (마침표 직후) 와
#    `app/main.py의` (한글 조사 직결, `\w` 는 유니코드) 를 **narrow·broad 가 함께** 놓쳐
#    교차검증이 무력해진다(실측 공통 맹점). 확장자가 이어지는 경우만 거부한다.
_FILE_TOKEN = re.compile(
    r"(?<![\w./-])(?:(?P<repo>exchange-rate|ios)/|\./)?"
    r"(?P<path>\.?[A-Za-z0-9_][\w./+-]*\.[A-Za-z0-9][A-Za-z0-9_+-]*)"
    r"(?!\.?[A-Za-z0-9_-])"
)
# 파일 확장자로 인정하는 것들. `app.get` `db.t4g.micro` 같은 **식별자**를 걸러내는 역할.
_KNOWN_EXT = {"py", "swift", "conf", "md", "json", "yml", "yaml", "toml", "txt",
              "sh", "cfg", "ini", "ts", "js", "kt", "java", "xml", "plist"}
# 확장자를 몰라도 **경로처럼 생겼으면** 그냥 넘기지 않는다(확장자 목록 drift 를 fail-closed 로).
_PATHY = re.compile(r"^(?:app|nginx|scripts|spec|tests|\.github)/|^FXi/")

# ⛔ 넓은 그물이 잡지만 **근거 대조 대상이 아닌** 것만 여기 적는다. 새 항목이 생기면 자동 통과가
#    아니라 E_CITEMISS 로 터져서 사람이 판단하게 한다(fail-closed).
CITATION_EXCLUSIONS = {
    "spec/topic-only.lock.json",          # lock 자체 — 이 작업이 고치는 파일이라 pinned diff 대상이 아니다
    "topic-only.lock.json",               # 같은 파일의 bare 표기
    "TOPIC_ONLY_DELIVERY_CONTRACT.md",    # 검토 대상 Draft 문서 언급 — 코드 근거가 아니다
    "FXi.xcodeproj/project.pbxproj",       # 사용자 소유 release arming — 이 이주의 근거 대조 범위 밖
}


def broad_cited_paths(baseline: pathlib.Path | None = None) -> dict[str, list[str]]:
    """⛔ `cited_paths` 와 **다른 축**으로 뽑는다(접두사 비의존 + 확장자 allowlist).
    둘이 독립이어야 교차검증이 성립한다 — 같은 축이면 narrow 가 좁아져도 차집합이 늘 빈다."""
    text = (baseline or _DEFAULT_CTX.baseline).read_text()
    out: dict[str, list[str]] = {"server": [], "ios": [], "unknown": []}
    seen = set()
    for match in _FILE_TOKEN.finditer(text):
        repo_hint = match.group("repo")
        tok = match.group("path")
        repo_bucket = "ios" if repo_hint == "ios" or (repo_hint is None and tok.startswith("FXi/")) else "server"
        seen_key = (repo_bucket, tok)
        if tok in CITATION_EXCLUSIONS or seen_key in seen:
            continue
        seen.add(seen_key)
        ext = tok.rsplit(".", 1)[1].lower()
        if ext in _KNOWN_EXT:
            is_ios = repo_hint == "ios" or (repo_hint is None and tok.startswith("FXi/"))
            out["ios" if is_ios else "server"].append(tok)
        elif repo_hint or _PATHY.match(tok):
            out["unknown"].append(tok)     # 경로 같은데 확장자 미상 — 사람이 판단해야 한다
    return {k: sorted(v) for k, v in out.items()}


_UNDERSCORE_EMPHASIS = re.compile(
    r"(?<!\w)(?P<mark>_{1,2})(?P<body>[^\n]+?)(?P=mark)(?!\w)"
)


def _mask_markdown_code_spans(text: str) -> str:
    """Mask Markdown fenced blocks and closed inline code spans.

    Markdown does not interpret underscores in either region. An unclosed fence extends to EOF;
    an unmatched inline delimiter remains ordinary text so it cannot hide a citation.
    """
    masked = list(text)

    # Fences close with the same character and at least the opening width. Tilde fences matter
    # here too even though inline code uses only backticks.
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if fence is None:
            match = re.fullmatch(r" {0,3}(?P<mark>`{3,}|~{3,})(?P<info>.*)", body)
            if match and not (match.group("mark")[0] == "`" and "`" in match.group("info")):
                fence = (match.group("mark")[0], len(match.group("mark")), offset)
        else:
            mark, width, start = fence
            if re.fullmatch(rf" {{0,3}}{re.escape(mark)}{{{width},}}[ \t]*", body):
                for index in range(start, offset + len(line)):
                    if masked[index] not in "\r\n":
                        masked[index] = " "
                fence = None
        offset += len(line)
    if fence is not None:
        _, _, start = fence
        for index in range(start, len(masked)):
            if masked[index] not in "\r\n":
                masked[index] = " "

    inline_text = "".join(masked)
    cursor = 0
    while cursor < len(inline_text):
        opener = inline_text.find("`", cursor)
        if opener < 0:
            break
        opener_end = opener
        while opener_end < len(inline_text) and inline_text[opener_end] == "`":
            opener_end += 1
        width = opener_end - opener

        search = opener_end
        closer = -1
        closer_end = -1
        while search < len(inline_text):
            candidate = inline_text.find("`", search)
            if candidate < 0:
                break
            candidate_end = candidate
            while candidate_end < len(inline_text) and inline_text[candidate_end] == "`":
                candidate_end += 1
            if candidate_end - candidate == width:
                closer, closer_end = candidate, candidate_end
                break
            search = candidate_end

        if closer < 0:
            cursor = opener_end
            continue
        for index in range(opener, closer_end):
            if masked[index] not in "\r\n":
                masked[index] = " "
        cursor = closer_end
    return "".join(masked)


def underscore_citations(baseline: pathlib.Path | None = None) -> list[str]:
    """Find file-like tokens hidden by Markdown underscore emphasis.

    Both citation extractors intentionally treat ``_`` as a filename character. Without a
    separate guard, valid Markdown such as ``__app/main.py__`` is therefore invisible to both
    extractors and defeats their cross-check. Code citations must use backticks instead.
    """
    text = _mask_markdown_code_spans((baseline or _DEFAULT_CTX.baseline).read_text())
    found = set()
    for emphasis in _UNDERSCORE_EMPHASIS.finditer(text):
        for match in _FILE_TOKEN.finditer(emphasis.group("body")):
            repo_hint = match.group("repo")
            token = match.group("path")
            extension = token.rsplit(".", 1)[1].lower()
            if token not in CITATION_EXCLUSIONS and (
                extension in _KNOWN_EXT or repo_hint or _PATHY.match(token)
            ):
                found.add(token)
    return sorted(found)


# ⛔ status 하나에 "구속력"과 "보존 위치"를 함께 담으면 [제안] 을 추적할 수 없다(실측 반례).
#    disposition(구속력) ⊥ destination(보존 위치) ⊥ normative_owner(규범 소유) 로 나눈다.
DISPOSITIONS = {"active", "proposed", "rejected", "superseded", "evidence", "prose"}
DOCS = {"ADR", "HAND", "CLIENT", "LOAD", "HEALTH", "CUT", "BASE"}



# ⛔ `$` 는 **마지막 개행 앞**에서도 매치한다(jsonschema 의 pattern 은 re.search).
#    'R-X-1\\n' 이 통과하면 문자열 완전일치 기반인 E_DUPRID/E_MULTIOWNER 가 우회된다(실측 rc=0).
RID_RE = r"^R-[A-Z]+-[0-9]+\Z"
EID_RE = r"^E-[A-Z]+-[0-9]+\Z"   # evidence 레코드는 별도 ID 공간 — R-ID 재사용은 중복으로 거부되므로

# ⛔ 필드 검사는 **선언적으로** 둔다. 손으로 if 를 쌓으면 매 라운드 새 누락이 나온다(5회 실측).
MANIFEST_SCHEMA = {
    "type": "object",
    "required": ["archive_sha", "baseline_sha", "pinned_commit", "total_lines", "blocks",
                 "source_metadata"],
    "additionalProperties": False,
    "properties": {
        "archive_sha": {"type": "string", "pattern": "^[0-9a-f]{64}\\Z"},
        "baseline_sha": {"type": "string", "pattern": "^[0-9a-f]{64}\\Z"},
        "pinned_commit": {
            "type": "object", "additionalProperties": False,
            "required": ["server", "ios"],
            "properties": {"server": {"type": "string", "pattern": "^[0-9a-f]{40}\\Z"},
                           "ios": {"type": "string", "pattern": "^[0-9a-f]{40}\\Z"}},
        },
        "total_lines": {"type": "integer", "minimum": 1},
        # ⛔ 문서 상태·증거 유효범위는 **요구사항이 아니다**. evidence 로 넣으면 존재하지 않는
        #    supports 관계를 지어내게 된다(실측: as-of 가 §1 논증을 뒷받침한다고 적혔다).
        "source_metadata": {
            "type": "object", "additionalProperties": False,
            "required": ["status", "summary_entry", "as_of", "lines"],
            "properties": {
                "status": {"type": "string", "minLength": 1},
                "summary_entry": {"type": "string", "minLength": 1},
                # ⛔ `$` 는 마지막 개행 앞에서도 매치한다 — 같은 계열 결함을 여기서 또 냈다
                "as_of": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}\\Z"},
                "lines": {"type": "object", "additionalProperties": False,
                          "required": ["start", "end"],
                          "properties": {"start": {"type": "integer", "minimum": 1},
                                         "end": {"type": "integer", "minimum": 1}}},
            },
        },
        "blocks": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "start", "end", "title", "disposition", "requirements"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "title": {"type": "string"},
                    "start": {"type": "integer", "minimum": 1},
                    "end": {"type": "integer", "minimum": 1},
                    "disposition": {"enum": sorted(DISPOSITIONS)},
                    "requirements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["rid", "source", "destination", "normative_owner"],
                            "properties": {
                                "rid": {"type": "string", "pattern": f"({RID_RE}|{EID_RE})"},
                                "supports": {"type": "string", "pattern": RID_RE},
                                # ⛔ 소유는 하나여야 하지만 **다른 문서가 인용해야 하는 줄**은 실재한다
                                #    (예: 클라 deadline 행이 서버 예산 제약 ①② 를 참조).
                                #    겹치는 source 로 표현하면 소유가 갈리므로 **참조**로 분리한다.
                                "references": {
                                    "type": "array", "uniqueItems": True,
                                    "items": {"type": "string", "pattern": f"({RID_RE}|{EID_RE})"},
                                },
                                # ⛔ 확정 요구가 **미확정 요구에 의존**하는 것은 별개 사실이다.
                                #    같은 배열에 섞으면 "이 요구는 제안이 채택돼야 성립한다"가 사라진다.
                                "conditional_references": {
                                    "type": "array", "uniqueItems": True,
                                    "items": {"type": "string", "pattern": f"({RID_RE}|{EID_RE})"},
                                },
                                # ⛔ **연기는 의존이 아니다.** "이건 이번 범위에서 뺀다"는 진술은
                                #    대상이 기각돼도 그대로 성립한다 — conditional 로 적으면 의미가 **반대**가 된다
                                #    (확정 요구가 근거 없이 미결정으로 격하된다).
                                #    이 축이 없으면 active→proposed 인용이 전부 conditional 로 몰린다.
                                "deferred_references": {
                                    "type": "array", "uniqueItems": True,
                                    "items": {"type": "string", "pattern": f"({RID_RE}|{EID_RE})"},
                                },
                                "destination": {"enum": sorted(DOCS)},
                                "normative_owner": {"anyOf": [{"enum": sorted(DOCS - {"BASE"})}, {"type": "null"}]},
                                "source": {
                                    "type": "object", "additionalProperties": False,
                                    "required": ["start", "end"],
                                    "properties": {"start": {"type": "integer", "minimum": 1},
                                                   "end": {"type": "integer", "minimum": 1}},
                                },
                            },
                        },
                    },
                },
                # ⛔ disposition 이 레코드 모양을 **결정**한다. 손 검사로는 조합을 매번 빠뜨린다(실측).
                "allOf": [
                    {"if": {"properties": {"disposition": {"const": "evidence"}}, "required": ["disposition"]},
                     "then": {"properties": {"requirements": {"items": {
                         "required": ["supports"],
                         "properties": {"rid": {"pattern": EID_RE}},
                     }}}}},
                    {"if": {"properties": {"disposition": {"enum": ["active", "proposed", "rejected", "superseded"]}},
                            "required": ["disposition"]},
                     "then": {"properties": {"requirements": {"items": {
                         "not": {"required": ["supports"]},
                         "properties": {"rid": {"pattern": RID_RE}},
                     }}}}},
                    {"if": {"properties": {"disposition": {"const": "prose"}}, "required": ["disposition"]},
                     "then": {"properties": {"requirements": {"maxItems": 0}}}},
                ],
            },
        },
    },
}


def schema_errors(m) -> list:
    try:
        import jsonschema
    except ImportError:
        return ["[스키마] jsonschema 미설치 — `pip install jsonschema` (필드 검사가 비활성이면 통과가 무의미)"]
    v = jsonschema.Draft202012Validator(MANIFEST_SCHEMA)
    return [f"[E_SCHEMA] {'/'.join(str(x) for x in e.path)}: {e.message}" for e in v.iter_errors(m)]


def sha256(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def parse_source_metadata(archive: pathlib.Path) -> tuple[dict | None, str | None]:
    """⛔ 문서 상태·요약 entry·증거 유효범위를 **원문에서 추출**한다.
    손으로 적게 두면 형식만 맞고 값이 거짓인 manifest 가 통과한다
    (실측: status="Approved" / as_of="1900-01-01" 이 전부 rc=0 이었다)."""
    lines = archive.read_text().splitlines()
    if len(lines) < 7:
        return None, "[E_METAPARSE] archive 가 너무 짧아 front-matter 를 읽을 수 없다"
    status = re.search(r"\*\*상태\*\*:\s*(.+?)\s*$", lines[2])
    summary = re.search(r"\*\*요약 entry\*\*:\s*(.+?)\s*$", lines[3])
    as_of = re.search(r"([0-9]{4}-[0-9]{2}-[0-9]{2})\s*실측", "\n".join(lines[5:7]))
    missing = [n for n, v in (("상태", status), ("요약 entry", summary), ("as_of 실측", as_of)) if not v]
    if missing:
        return None, f"[E_METAPARSE] archive front-matter 에서 {missing} 를 찾지 못했다"
    try:
        datetime.date.fromisoformat(as_of.group(1))
    except ValueError:
        return None, f"[E_METAPARSE] as_of 가 달력에 없는 날짜다: {as_of.group(1)}"
    return {"status": status.group(1), "summary_entry": summary.group(1),
            "as_of": as_of.group(1), "lines": {"start": 3, "end": 7}}, None


def build_skeleton(ctx: "LockCtx | None" = None) -> dict:
    """archive 를 heading 경계로 쪼개 **빈틈 없는** 행 범위 분할을 만든다."""
    ctx = ctx or _DEFAULT_CTX
    meta, merr = parse_source_metadata(ctx.archive)
    if merr:
        # ⛔ 호출 경로마다 가드를 두면 하나가 새고, 실제로 CLI 밖 호출이 샜다(실측)
        raise ValueError(merr)
    lines = ctx.archive.read_text().splitlines()
    bounds = [i for i, l in enumerate(lines, 1) if re.match(r"^#{1,3} ", l)]
    if not bounds or bounds[0] != 1:
        bounds = [1] + bounds
    blocks = []
    for idx, start in enumerate(bounds):
        end = (bounds[idx + 1] - 1) if idx + 1 < len(bounds) else len(lines)
        title = lines[start - 1].lstrip("# ").strip()
        blocks.append({
            "id": f"BLK-{idx + 1:03d}",
            "start": start, "end": end, "title": title,
            "disposition": None,     # active/proposed/rejected/superseded/evidence/prose
            "requirements": [],      # [{"rid","source":{"start","end"},"destination","normative_owner"}]
        })
    return {
        "source_metadata": meta,
        "archive_sha": sha256(ctx.archive),
        "baseline_sha": sha256(ctx.baseline),
        "pinned_commit": dict(ctx.pinned_commit),
        "total_lines": len(lines),
        "blocks": blocks,
    }


def provenance_root(repo_key: str, roots: dict | None = None) -> pathlib.Path:
    """리포 루트 선택. ⛔ 순수 함수로 떼어 둔다 — 임시리포 테스트가 `roots` 를 항상 주입하면
    **프로덕션 분기(server=REPO / ios=../ios)** 가 통째로 미검증으로 남는다(실측 지적)."""
    if roots and repo_key in roots:
        return pathlib.Path(roots[repo_key])
    if repo_key == "server":
        return REPO
    # Actions는 checkout 경로를 workspace 밖(로컬의 sibling ../ios)으로 둘 수 없다.
    # CI만 명시적으로 override하고, 로컬 계약은 기존 sibling 경로를 유지한다.
    override = os.environ.get(IOS_ROOT_ENV)
    return pathlib.Path(override).expanduser() if override else REPO.parent / "ios"


def default_provenance(repo_key: str, paths: list[str], pinned: str,
                       roots: dict | None = None) -> list[str]:
    """실제 다중 리포 대조. CI는 ``TOPIC_MIGRATION_IOS_ROOT``에 iOS checkout을 제공한다."""
    out_fail = []
    # ⛔ 빈 경로로 내려오면 아래 `git diff <pin> --` 가 **리포 전체**를 비교한다.
    #    그러면 (a) 인용이 0개인 공허한 통과를 못 보고 (b) 범위 밖 파일(예: iOS arming pbxproj)을
    #    E_CODECHANGED 로 오탐한다 — 실측으로 둘 다 발생했다.
    if not paths:
        return [f"[E_NOCITED] {repo_key} 인용 경로가 0개 — 근거 대조가 공허해진다"]
    root = provenance_root(repo_key, roots)
    try:
        for rel in paths:
            if not (root / rel).exists():
                out_fail.append(f"[E_PATHMISSING] {repo_key} 도출 경로가 현재 없음(오타 의심): {rel}")
            chk = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{pinned}:{rel}"],
                                 capture_output=True)
            if chk.returncode != 0:
                out_fail.append(f"[E_PATHNOTPINNED] {repo_key} 도출 경로가 pinned commit 에 없음: {rel}")
        r = subprocess.run(["git", "-C", str(root), "diff", "--name-only", pinned, "--", *paths],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            out_fail.append(f"[근거] {repo_key} diff 실패: {r.stderr.strip()[:120]}")
        elif r.stdout.strip():
            out_fail.append(f"[E_CODECHANGED] {repo_key} 인용 경로가 pinned 이후 변경됨: "
                            f"{r.stdout.strip().splitlines()}")
    except Exception as e:  # noqa: BLE001 — 검증기는 죽지 않고 보고한다
        out_fail.append(f"[근거] {repo_key} 대조 예외: {e}")
    return out_fail


def verify(manifest_path: pathlib.Path | None = None, *, provenance: bool = False,
           lock_path: pathlib.Path | None = None, provenance_fn=None) -> int:
    """구조 검증. `provenance=True` 면 **sibling 리포까지** 대조한다(CI 단일 checkout 에선 불가)."""
    # ⛔ 전역에 저장하지 않는다 — 같은 프로세스에서 인자 없이 다시 부르면 **이전 임시 manifest** 를 쓴다(실측).
    mpath = manifest_path or DEFAULT_MANIFEST
    fail = []
    # ⛔ import 시점 캐시를 쓰면 같은 프로세스에서 lock 이 바뀌어도 낡은 값을 본다 — 매번 읽는다
    lk, lk_err = _load_lock(lock_path)
    if lk_err:
        fail.append(lk_err)
        return report(fail)
    ctx = LockCtx(lk)   # ⛔ 전역 아님 — 이 호출 안에서만 산다

    # 1. 정본 대조 — 권한이 아니라 SHA 로 (동결은 승인된 commit/tag 만이 만든다)
    for name, path in (("archive", ctx.archive), ("baseline", ctx.baseline)):
        actual = sha256(path)
        if actual != ctx.pinned_sha[name]:
            fail.append(f"[E_LOCK_SHA] {name} SHA 불일치\n    기대 {ctx.pinned_sha[name]}\n    실제 {actual}")

    if not mpath.exists():
        fail.append(f"[E_NOMANIFEST] 파일 없음: {mpath} — `skeleton` 을 먼저 실행하고 분류를 채울 것")
        return report(fail)

    try:
        m = json.loads(mpath.read_text())
    except json.JSONDecodeError as e:
        fail.append(f"[E_JSON] manifest JSON 손상: {e}")
        return report(fail)
    # ⛔ 스키마를 **가장 먼저** 본다. [] / null / "str" / 42 를 넣으면 m.get() 에서 traceback 이 났다(실측).
    if not isinstance(m, dict):
        fail.append(f"[E_SCHEMA] manifest 최상위가 object 가 아님: {type(m).__name__}")
        return report(fail)
    schema_fail = schema_errors(m)
    if schema_fail:
        # ⛔ 형태가 깨진 입력으로 뒤 검사를 계속하면 KeyError/TypeError traceback 이 난다.
        #    traceback 은 "검증기가 잡았다"가 아니라 "검증기가 죽었다"이다 — 즉시 종결한다.
        fail.extend(schema_fail)
        return report(fail)

    for key, path in (("archive_sha", ctx.archive), ("baseline_sha", ctx.baseline)):
        if m.get(key) != sha256(path):
            fail.append(f"[E_MANIFEST_SHA] manifest.{key} 가 실제 파일과 다름")
    if m.get("pinned_commit") != ctx.pinned_commit:
        fail.append(f"[E_PINNED] manifest.pinned_commit 불일치: {m.get('pinned_commit')}")
    blocks = m["blocks"]
    # ⛔ manifest 의 total_lines 를 믿지 않는다 — 축소하면 커버리지 검사가 통째로 무력화된다(실측).
    meta = m.get("source_metadata") or {}
    total = len(ctx.archive.read_text().splitlines())
    ml = meta.get("lines") or {}
    if not (1 <= ml.get("start", 0) <= ml.get("end", 0) <= total):
        fail.append(f"[E_METALINES] source_metadata.lines 가 archive 범위 밖: {ml}")
    # ⛔ 형식만 보면 값이 거짓인 채로 통과한다 — **원문에서 추출해 정확히 대조**한다
    parsed, perr = parse_source_metadata(ctx.archive)
    if perr:
        fail.append(perr)
    elif parsed != meta:
        diff = {k: (meta.get(k), parsed[k]) for k in parsed if meta.get(k) != parsed[k]}
        fail.append(f"[E_METAMISMATCH] source_metadata 가 원문과 다르다(값: manifest vs archive): {diff}")
    if m.get("total_lines") != total:
        fail.append(f"[E_TOTAL] manifest total_lines={m.get('total_lines')} != 실제 {total}")
    for b in blocks:
        if not (1 <= b["start"] <= b["end"] <= total):
            fail.append(f"[E_RANGE] {b['id']} 범위 이탈: {b['start']}~{b['end']} (파일 1~{total})")

    # 2. 완전성 — 모든 줄이 정확히 하나의 블록에 (gap·overlap 0)
    covered = [0] * (total + 1)
    for b in blocks:
        # ⛔ 상한을 total 로 **자른다**. 예전엔 range(start, end+1) 전체를 돌며 `if ln <= total` 로
        #    걸렀는데, end=10**12 인 manifest 는 빠른 rc=1 대신 hang 이 됐다(가용성 실패).
        for ln in range(b["start"], min(b["end"], total) + 1):
            covered[ln] += 1
    gaps = [i for i in range(1, total + 1) if covered[i] == 0]
    overlaps = [i for i in range(1, total + 1) if covered[i] > 1]
    if gaps:
        fail.append(f"[E_GAP] 어느 블록에도 없는 줄 {len(gaps)}개 (예: {gaps[:5]})")
    if overlaps:
        fail.append(f"[E_OVERLAP] 두 블록에 겹친 줄 {len(overlaps)}개 (예: {overlaps[:5]})")

    # 2.5 block id 유일성
    ids = [b.get("id") for b in blocks]
    dup_ids = sorted({i for i in ids if ids.count(i) > 1})
    if dup_ids:
        fail.append(f"[E_DUPID] block id 중복: {dup_ids}")

    # 3. 분류 — disposition 미기입 블록 0
    unclassified = [b["id"] for b in blocks if b.get("disposition") not in DISPOSITIONS]
    if unclassified:
        fail.append(f"[E_DISP] disposition 미기입/오값 블록 {len(unclassified)}개: {unclassified[:8]}")

    # 4. 소유 — active 요구사항의 owner 가 **정확히 하나**
    if not any(b.get("disposition") == "active" for b in blocks):
        fail.append("[E_NOACTIVE] active 블록이 0개 — 전부 prose 로 두면 이주가 0인데 통과한다(실측 반례)")

    owners: dict[str, list[str]] = {}
    seen_rid: dict[str, int] = {}
    for b in blocks:
        disp = b.get("disposition")
        reqs = b.get("requirements", [])
        # ⛔ active 뿐 아니라 **proposed/rejected/superseded 도 레코드가 있어야** 한다 —
        #    없으면 그 결정이 조용히 사라진다(김프 R-DEC-2 실측 반례).
        if disp == "prose" and reqs:
            fail.append(f"[E_PROSEREC] {b['id']} 는 prose 인데 요구사항 보유 — 서술에 R-ID 를 두지 않는다")
        if disp in {"active", "proposed", "rejected", "superseded", "evidence"} and not reqs:
            fail.append(f"[E_NOREC] {b['id']}({b.get('title','')[:24]}) disposition={disp} 인데 요구사항 0개 — "
                        "결정·제안이 기록 없이 사라진다")
        for r in reqs:
            rid = r.get("rid")
            seen_rid[rid] = seen_rid.get(rid, 0) + 1
            # ⛔ 모든 R-ID 에 출처 범위 필수 — 없으면 archive 어디서 왔는지 추적 불가
            if not ("start" in (r.get("source") or {}) and "end" in (r.get("source") or {})):
                fail.append(f"[E_NOSRC] {rid} 에 source.start/end 없음")
            else:
                sr = r["source"]
                if not (b["start"] <= sr["start"] <= sr["end"] <= b["end"]):
                    fail.append(f"[E_SRCOUT] {rid} 출처 범위가 블록 밖: {sr['start']}~{sr['end']} ⊄ {b['start']}~{b['end']}")
            # ⛔ destination 은 **모든 disposition 에 필수** — 제안·기각도 어딘가 기록돼야 잃지 않는다
            if r.get("destination") not in DOCS:
                fail.append(f"[E_DEST] {rid} destination 부적합: {r.get('destination')!r}")
            # ⛔ normative_owner 는 active 에만, 그것도 정확히 하나. 나머지는 null 이어야 한다
            no = r.get("normative_owner")
            if disp == "active":
                if no is None:
                    fail.append(f"[E_NOOWNER] {rid} (active) normative_owner 없음")
                else:
                    # ⛔ active 는 기록 위치와 규범 소유가 갈리면 안 된다
                    if r.get("destination") != no:
                        fail.append(f"[E_OWNMISMATCH] {rid} (active) destination={r.get('destination')} != "
                                    f"normative_owner={no} — 규범 소유자가 기록처와 달라 추적이 갈린다")
                    owners.setdefault(rid, []).append(no)
            elif no is not None:
                fail.append(f"[E_OWNONNONACTIVE] {rid} disposition={disp} 인데 normative_owner={no!r} — null 이어야 한다")
    normative_rids = {r.get("rid") for b in blocks
                      if b.get("disposition") != "evidence"
                      for r in b.get("requirements", [])}
    for b in blocks:
        if b.get("disposition") != "evidence":
            continue
        for r in b.get("requirements", []):
            sup = r.get("supports")
            if not sup:
                fail.append(f"[E_NOSUPPORT] {r.get('rid')} 는 evidence 인데 supports 없음 — "
                            "무엇을 뒷받침하는지 가리켜야 한다")
            elif sup not in normative_rids:
                fail.append(f"[E_BADSUPPORT] {r.get('rid')} 의 supports={sup} 가 실재하는 "
                            "비-evidence 요구사항이 아니다")
            elif sup == r.get("rid"):
                fail.append(f"[E_SELFSUPPORT] {r.get('rid')} 가 자기 자신을 support 한다")
    all_rids = {r.get("rid") for b in blocks for r in b.get("requirements", [])}
    rid_disp = {r.get("rid"): b.get("disposition")
                for b in blocks for r in b.get("requirements", [])}
    for b in blocks:
        for r in b.get("requirements", []):
            if r.get("conditional_references") and b.get("disposition") not in {"active", "proposed"}:
                fail.append(f"[E_CONDSOURCE] {r.get('rid')} 는 {b.get('disposition')} 인데 조건부 참조를 갖는다 "
                            "— 조건부 의존은 **요구/제안**만 진다(사실·서술은 결정에 의존하지 않는다)")
            for ref in r.get("conditional_references", []) or []:
                # ⛔ 조건부 대상은 **proposed 하나**다. rejected/superseded 는 "미결정"이 아니라
                #    이미 결론이 난 상태이고, evidence 는 결정이 아니다 — 조건이 될 수 없다.
                if ref == r.get("rid"):
                    fail.append(f"[E_SELFREF] {ref} 가 자기 자신을 조건부 참조한다")
                elif ref not in all_rids:
                    fail.append(f"[E_BADREF] {r.get('rid')} 의 conditional_references={ref} 가 실재하지 않는다")
                elif rid_disp.get(ref) != "proposed":
                    fail.append(f"[E_CONDTARGET] {r.get('rid')} 의 조건부 대상 {ref} 가 "
                                f"proposed 가 아니다({rid_disp.get(ref)}) — 조건은 **미결정 제안**에만 건다")
            for ref in r.get("references", []) or []:
                if ref == r.get("rid"):
                    fail.append(f"[E_SELFREF] {ref} 가 자기 자신을 참조한다")
                elif ref not in all_rids:
                    fail.append(f"[E_BADREF] {r.get('rid')} 의 references={ref} 가 실재하지 않는다 "
                                "— 끊긴 참조는 문서에서 미정의 기호가 된다")
                elif b.get("disposition") == "active" and rid_disp.get(ref) == "proposed":
                    # ⛔ **확정** 요구가 **미결정 제안**에 기대는 것만 선언 대상이다.
                    #    rejected/superseded 참조는 역사 인용이고, proposed→proposed 는 같은 미결정 안의 참조다.
                    fail.append(f"[E_UNDECLARED_COND] 확정 요구 {r.get('rid')} 가 미결정 {ref} 를 "
                                "일반 참조로 가리킨다 — 의존이면 conditional_references, "
                                "연기·범위 제외면 deferred_references 로 선언해야 한다")
            # ⛔ 연기 축. **의존과 반대 방향**이므로 대상 조건은 같아도(미결정 제안) 의미가 다르다.
            if r.get("deferred_references") and b.get("disposition") not in {"active", "proposed"}:
                fail.append(f"[E_DEFSOURCE] {r.get('rid')} 는 {b.get('disposition')} 인데 연기 참조를 갖는다 "
                            "— 무엇을 미룰지는 **요구/제안**만 정한다(사실·서술은 범위를 정하지 않는다)")
            for ref in r.get("deferred_references", []) or []:
                if ref == r.get("rid"):
                    fail.append(f"[E_SELFREF] {ref} 가 자기 자신을 연기 참조한다")
                elif ref not in all_rids:
                    fail.append(f"[E_BADREF] {r.get('rid')} 의 deferred_references={ref} 가 실재하지 않는다")
                elif rid_disp.get(ref) != "proposed":
                    fail.append(f"[E_DEFTARGET] {r.get('rid')} 의 연기 대상 {ref} 가 "
                                f"proposed 가 아니다({rid_disp.get(ref)}) — 확정된 것은 미룰 수 없다")
            # ⛔ 한 대상을 두 관계로 동시에 적으면 **어느 쪽이 사실인지 모델이 못 고른다**
            #    (의존이면서 동시에 연기일 수 없다).
            _rel = {"references": r.get("references") or [],
                    "conditional_references": r.get("conditional_references") or [],
                    "deferred_references": r.get("deferred_references") or []}
            for ref in set().union(*(set(v) for v in _rel.values())):
                where = sorted(k for k, v in _rel.items() if ref in v)
                if len(where) > 1:
                    fail.append(f"[E_RELDUP] {r.get('rid')} 가 {ref} 를 {'·'.join(where)} 에 중복 선언한다 "
                                "— 관계는 하나여야 한다")

    for rid, cnt in seen_rid.items():
        if cnt != 1:
            fail.append(f"[E_DUPRID] {rid} 가 {cnt}회 정의됨 — 정의는 정확히 하나")
    # ⛔ 같은 줄을 **서로 다른 규범 소유자**가 갖는 것을 금지한다 — 그러면 "공유 인용"인지
    #    "중복 소유"인지 모델이 구별하지 못하고, 두 문서가 같은 줄을 각자 규범으로 적어 갈린다.
    #    (겹치려면 소유자가 같아야 한다. 다른 문서가 참고해야 하면 **인용**이지 소유가 아니다.)
    owned = [(r["rid"], r["source"], r.get("normative_owner"))
             for b in blocks if b.get("disposition") == "active"
             for r in b.get("requirements", []) if isinstance(r.get("source"), dict)]
    for i, (rid_a, sa, oa) in enumerate(owned):
        for rid_b, sb, ob in owned[i + 1:]:
            if oa != ob and sa.get("start", 0) <= sb.get("end", 0) and sb.get("start", 0) <= sa.get("end", 0):
                fail.append(f"[E_OWNOVERLAP] {rid_a}({sa['start']}-{sa['end']}, {oa}) 와 "
                            f"{rid_b}({sb['start']}-{sb['end']}, {ob}) 의 출처가 겹치는데 소유자가 다르다 "
                            "— 공유 인용인지 중복 소유인지 구별할 수 없다")

    for rid, os_ in owners.items():
        if len(os_) != 1:
            fail.append(f"[E_MULTIOWNER] {rid} 의 normative_owner 가 {len(os_)}개: {os_}")

    # 5. 코드 근거 — HEAD 가 아니라 **인용 경로의 diff**
    # 기본 verify 는 단일 리포에서도 안전한 구조 검사다. preflight 는 sibling ../ios 또는
    # TOPIC_MIGRATION_IOS_ROOT 로 명시한 CI checkout까지 대조한다.
    if not provenance:
        if not fail:
            print("[MODE:structure-only]")
            print("✅ **구조** 검증 통과 (lock/커버리지/분류/소유).")
            print("⛔ **인용 경로 근거는 검사하지 않았다** — 다중 리포가 필요하다.")
            print("   전체 대조는 iOS root를 제공한 뒤 `preflight` 로 실행할 것.")
            return 0
        return report(fail)
    runner = provenance_fn or default_provenance
    cited = cited_paths(ctx.baseline)
    broad = broad_cited_paths(ctx.baseline)
    emphasized = underscore_citations(ctx.baseline)
    if emphasized:
        fail.append(
            f"[E_CITEFORMAT] underscore 강조 안의 파일형 토큰 {len(emphasized)}개: "
            f"{emphasized[:5]} — 코드 경로는 backtick 으로 표기해야 한다"
        )
    if broad["unknown"]:
        # ⛔ 확장자 allowlist 가 낡으면 그물이 조용히 좁아진다 — 경로형 미상 토큰은 터뜨린다
        fail.append(f"[E_CITEUNKNOWN] 경로처럼 보이는데 확장자를 모르는 토큰 {len(broad['unknown'])}개: "
                    f"{broad['unknown'][:5]} — _KNOWN_EXT 또는 CITATION_EXCLUSIONS 판단이 필요하다")
    for repo_key in ("server", "ios"):
        # ⛔ 교차검증을 **한 방향만** 보면 넓은 그물이 좁아지는 퇴행을 불변식으로 못 잡는다
        #    (지금은 합성 프로브 행 하나에만 기댄다). 양방향으로 본다 — 현재 두 차집합 모두 공집합이다.
        lost = sorted(set(cited.get(repo_key, [])) - set(broad[repo_key]) - CITATION_EXCLUSIONS)
        if lost:
            fail.append(f"[E_CITEBROADMISS] narrow 는 뽑는데 넓은 그물이 놓친 {repo_key} 경로 "
                        f"{len(lost)}개: {lost[:5]} — 그물이 좁아졌거나 _KNOWN_EXT 가 낡았다")
        missed = sorted(set(broad[repo_key]) - set(cited.get(repo_key, [])))
        if missed:
            fail.append(f"[E_CITEMISS] baseline 이 인용하는데 도출에서 빠진 {repo_key} 경로 "
                        f"{len(missed)}개: {missed[:5]} — 정규식이 좁아졌거나 "
                        f"CITATION_EXCLUSIONS 판단이 필요하다")
    for repo_key in ("server", "ios"):
        paths = sorted(cited.get(repo_key, []))
        if not paths:
            # baseline 서식이 바뀌어 정규식이 빗나가면 **조용히 아무것도 검사하지 않는다**
            fail.append(f"[E_NOCITED] baseline 에서 도출한 {repo_key} 인용 경로가 0개 "
                        f"— 정규식이 빗나갔거나 baseline 이 비었다")
            continue
        fail.extend(runner(repo_key, paths, ctx.pinned_commit[repo_key]))

    # ⛔ 마커는 루프 **완료 후** 찍는다 — 앞에서 찍으면 "진입"만 증명하고 완료는 증명 못 한다(실측).
    print("[MODE:with-provenance]")
    return report(fail)


def report(fail: list[str]) -> int:
    if fail:
        print("⛔ 검증 실패")
        for f in fail:
            print(f"  - {f}")
        return 1
    print("✅ **구조** 검증 통과 — lock SHA / 행 커버리지(gap·overlap 0) / 전 블록 분류 /")
    print("   active 요구사항 단일 소유 / 인용 경로 존재·무변경")
    # ⛔ 예전 문구("전부 prose 로 둬도 통과")는 **거짓**이었다 — E_NOACTIVE 가 막는다(실측).
    #    실제 한계는 배정의 옳음을 못 본다는 것이다: 모든 요구사항을 한 문서로 몰아도 rc=0(실측).
    print("⛔ 이것은 **요구사항 완전성 증명이 아니다** — 모든 요구사항을 한 문서로 몰아넣어도 통과한다(실측).")
    print("   의미 분류가 옳은지는 **독립 리뷰 게이트**가 따로 판정해야 한다.")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="topic-only 분할 manifest 생성/검증")
    # ⛔ 명령별로 옵션을 **제한**한다. 예전엔 `skeleton --manifest /tmp/x` 가 그 플래그를 조용히
    #    무시하고 **운영 manifest** 를 건드렸다(실측 fail-open).
    sub = ap.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("skeleton", help="분할 초안 생성(운영 manifest 경로 고정)")
    sp.add_argument("--force", action="store_true")
    for name, helptext in (("verify", "구조 검증(CI 안전)"), ("preflight", "구조 + 다중리포 근거 대조")):
        q = sub.add_parser(name, help=helptext)
        q.add_argument("--manifest", type=pathlib.Path)
        q.add_argument("--lock", type=pathlib.Path)
    a = ap.parse_args()

    if a.command == "skeleton":
        # ⛔ lock 이 깨졌는데 생성하면 `pinned_commit: {}` 인 manifest 가 나온다(실측 rc=0).
        #    그 manifest 는 근거 대조를 통째로 잃은 채 "정상 산출물"처럼 보인다 — fail-closed.
        lk_now, lk_err_now = _load_lock()          # ⛔ import 시점 캐시가 아니라 지금 읽는다
        if lk_err_now:
            print(f"⛔ lock 이 유효하지 않아 skeleton 을 만들지 않는다\n   {lk_err_now}")
            sys.exit(1)
        # ⛔ 형식만 보면 부족하다 — 형식 유효 + SHA 불일치 lock 으로 만들면 **자기 자신을 검증 못 하는**
        #    manifest 가 나오고(archive_sha != lock.sha256), 분류해 둔 산출물을 그걸로 덮어쓴다(실측 rc=0).
        ctx_now = LockCtx(lk_now)
        for _name, _path in (("archive", ctx_now.archive), ("baseline", ctx_now.baseline)):
            if sha256(_path) != ctx_now.pinned_sha[_name]:
                print(f"⛔ {_name} 이 lock SHA 와 다르다 — skeleton 을 만들지 않는다\n"
                      f"   기대 {ctx_now.pinned_sha[_name]}\n   실제 {sha256(_path)}")
                sys.exit(1)
        if DEFAULT_MANIFEST.exists() and not a.force:
            print(f"⛔ 이미 있음: {DEFAULT_MANIFEST}\n   덮어쓰려면 --force (채워 둔 분류가 날아간다)")
            sys.exit(1)
        DEFAULT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        try:
            _sk = build_skeleton(LockCtx(lk_now))
        except ValueError as e:      # ⛔ 죽지 말고 보고한다 — 그리고 **아무것도 쓰지 않는다**
            print(f"⛔ front-matter 를 읽지 못해 skeleton 을 만들지 않는다\n   {e}")
            sys.exit(1)
        DEFAULT_MANIFEST.write_text(json.dumps(_sk, ensure_ascii=False, indent=2) + "\n")
        m = json.loads(DEFAULT_MANIFEST.read_text())
        print(f"skeleton 생성: {DEFAULT_MANIFEST}")
        print(f"  블록 {len(m['blocks'])}개 / 총 {m['total_lines']}줄")
        print("  ⚠️ disposition·requirements 는 비어 있다 — 채우기 전까지 verify 는 실패한다(의도)")
        sys.exit(0)

    sys.exit(verify(a.manifest, provenance=(a.command == "preflight"), lock_path=a.lock))
