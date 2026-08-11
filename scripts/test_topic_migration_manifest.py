#!/usr/bin/env python3
"""검증기의 **반례 테스트** — 검증기가 공허하지 않음을 증명한다.

왜 이게 본체보다 중요한가
------------------------
2026-08-09 실측: 첫 검증기는 아래 세 변이에서 **전부 rc=0** 이었다.
  · 31개 블록을 전부 prose + 요구사항 0
  · 같은 RID·owner 를 두 번 정의
  · total_lines=1 로 축소하고 1행만 매핑
"검증기를 만들었다"는 것과 "그 검증기가 무언가를 잡는다"는 것은 **다른 명제**이고,
후자는 **반례가 실패하는 걸 보여야만** 참이 된다.

⛔ 통과(rc=0)만 확인하는 테스트는 이 파일의 목적이 아니다.
**각 반례가 rc==1 + stderr 없음 + 겨냥한 오류 코드**인지 본다.

    python3 scripts/test_topic_migration_manifest.py
"""
from __future__ import annotations

import copy
import hashlib
import os
import json
import pathlib
import signal
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
VALIDATOR = REPO / "scripts" / "topic_migration_manifest.py"

sys.path.insert(0, str(REPO / "scripts"))
import topic_migration_manifest as MOD   # noqa: E402 — 상수/함수 계약을 직접 잠그기 위해

# ⛔ 통합시험이 조용히 0행을 내면 "빨강 0" 이라 초록이 된다 — 기대 행수를 계약으로 박는다
EXPECTED_PROV_ROWS = 9
MANIFEST = REPO / "spec" / "topic-only-migration-manifest.json"
ARCHIVE = REPO / json.loads((REPO / "spec" / "topic-only.lock.json").read_text())["archive"]["path"]


_TD = tempfile.TemporaryDirectory()       # ⛔ 고정 파일명은 동시 실행 시 충돌한다
TMP = pathlib.Path(_TD.name) / "manifest.json"


def run() -> tuple[int, str, str]:
    """⛔ 운영 manifest 를 건드리지 않는다 — 임시 파일로만 검증한다."""
    r = subprocess.run([sys.executable, str(VALIDATOR), "verify", "--manifest", str(TMP)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def good() -> dict:
    """통과해야 하는 최소 manifest — 전 행 커버 + active 1 + **proposed 추적 가능**."""
    import hashlib
    lock = json.loads((REPO / "spec" / "topic-only.lock.json").read_text())
    lines = len(ARCHIVE.read_text().splitlines())
    return {
        "archive_sha": lock["archive"]["sha256"],
        "baseline_sha": lock["baseline"]["sha256"],
        "pinned_commit": lock["pinned_commit"],
        "total_lines": lines,
        "source_metadata": MOD.parse_source_metadata(MOD._DEFAULT_CTX.archive)[0],
        "blocks": [
            {"id": "BLK-001", "start": 1, "end": 10, "title": "a", "disposition": "active",
             "requirements": [{"rid": "R-X-1", "source": {"start": 2, "end": 5},
                               "destination": "ADR", "normative_owner": "ADR"}]},
            {"id": "BLK-002", "start": 11, "end": 20, "title": "김프 [제안]", "disposition": "proposed",
             "requirements": [{"rid": "R-DEC-2", "source": {"start": 12, "end": 18},
                               "destination": "ADR", "normative_owner": None}]},
            {"id": "BLK-004", "start": 21, "end": 30, "title": "근거", "disposition": "evidence",
             "requirements": [{"rid": "E-X-1", "source": {"start": 22, "end": 25},
                               "destination": "ADR", "normative_owner": None, "supports": "R-X-1"}]},
            {"id": "BLK-003", "start": 31, "end": lines, "title": "b", "disposition": "prose",
             "requirements": []},
        ],
    }


# (이름, 변이, 기대 오류코드) — 코드까지 봐야 '다른 이유로 실패'를 걸러낸다
MUTATIONS = [
    ("전부 prose + 요구사항 0", "E_NOACTIVE",
     lambda m: [b.update(disposition="prose", requirements=[]) for b in m["blocks"]]),
    ("total_lines 축소", "E_TOTAL",
     lambda m: (m.update(total_lines=1), m.update(blocks=[dict(m["blocks"][0], start=1, end=1)]))),
    ("gap — 중간 구간 미할당", "E_GAP",
     lambda m: m["blocks"].__setitem__(3, dict(m["blocks"][3], start=35))),
    ("overlap — 두 블록이 같은 줄", "E_OVERLAP",
     lambda m: m["blocks"].__setitem__(3, dict(m["blocks"][3], start=5))),
    ("범위 초과 — end 가 파일 밖", "E_RANGE",
     lambda m: m["blocks"].__setitem__(3, dict(m["blocks"][3], end=m["total_lines"] + 50))),
    ("같은 RID 를 두 번 정의", "E_DUPRID",
     lambda m: m["blocks"][0]["requirements"].append(
         {"rid": "R-X-1", "source": {"start": 6, "end": 8}, "destination": "ADR", "normative_owner": "ADR"})),
    ("active 인데 normative_owner 누락", "E_NOOWNER",
     lambda m: m["blocks"][0]["requirements"][0].update(normative_owner=None)),
    ("proposed 인데 normative_owner 보유(구속력 오기재)", "E_OWNONNONACTIVE",
     lambda m: m["blocks"][1]["requirements"][0].update(normative_owner="ADR")),
    ("R-ID 에 source range 없음", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0].pop("source")),
    ("source range 가 블록 밖", "E_SRCOUT",
     lambda m: m["blocks"][0]["requirements"][0].update(source={"start": 900, "end": 950})),
    # ⛔ gap/overlap 을 **단독**으로만 시험하면 "블록 길이 합 == 총행수" 같은 산술 치환이 통과한다
    #    (두 오류가 같은 크기면 상쇄). 동시 발생을 반드시 본다.
    ("gap 과 overlap 이 동시에(상쇄 위장)", "E_GAP",
     lambda m: (m["blocks"][1].__setitem__("start", m["blocks"][1]["start"] - 2),
                m["blocks"][3].__setitem__("start", m["blocks"][3]["start"] + 2))),
    # ⛔ 앵커(^$) 없는 pattern 은 re.search 라 '유효 ID + 잡음' 을 합법화한다 —
    #    그러면 문자열 완전일치 기반인 E_DUPRID/E_MULTIOWNER 가 공백 한 칸으로 우회된다
    ("rid 에 후행 공백(앵커 우회)", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0].update(rid="R-X-1 ")),
    # ⛔ 공백 반례는 `$` → `\Z` 퇴행을 못 본다 — `$` 는 **마지막 개행 앞**에서도 매치한다(실측 rc=0 우회)
    ("rid 에 후행 개행(`$` 우회)", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0].update(rid="R-X-1\n")),
    ("archive_sha 에 후행 개행", "E_SCHEMA",
     lambda m: m.update(archive_sha=m["archive_sha"] + "\n")),
    # ⛔ RID 만 겨냥하면 EID_RE **단독** 퇴행을 못 본다 — evidence ID 도 같은 우회가 성립한다(실측)
    ("evidence rid 에 후행 개행(EID 앵커)", "E_SCHEMA",
     lambda m: m["blocks"][2]["requirements"][0].update(rid="E-X-1\n")),
    ("destination 이 null", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0].update(destination=None)),
    # ⛔ 위 행은 **null 거부**만 잠근다 — schema `required` 에서 destination 을 빼는 퇴행은
    #    여전히 enum 위반으로 걸려 통과처럼 보인다(실측 지적). 키 자체를 없애는 행을 따로 둔다.
    ("destination 키 누락", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0].pop("destination")),
    ("manifest 가 archive_sha 를 거짓 기재", "E_MANIFEST_SHA",
     lambda m: m.update(archive_sha="0" * 64)),
    ("pinned_commit 변조(형식은 유효)", "E_PINNED",
     lambda m: m.update(pinned_commit={"server": "d" * 40, "ios": "e" * 40})),
    ("pinned_commit 단축 해시", "E_SCHEMA",
     lambda m: m.update(pinned_commit={"server": "3e693c8", "ios": "8aadc2f"})),
    ("active 인데 요구사항 0", "E_NOREC",
     lambda m: m["blocks"][0].update(requirements=[])),
    ("중복 block id", "E_DUPID",
     lambda m: m["blocks"][1].update(id=m["blocks"][0]["id"])),
    ("proposed 블록 requirements 비움(제안이 사라짐)", "E_NOREC",
     lambda m: m["blocks"][1].update(requirements=[])),
    ("R-ID 빈 문자열", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0].update(rid="")),
    ("R-ID 키 누락", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0].pop("rid")),
    ("R-ID 형식 위반", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0].update(rid="nope")),
    # ⛔ 같은 줄을 서로 다른 소유자가 가지면 "공유 인용"인지 "중복 소유"인지 구별할 수 없다
    ("같은 출처를 다른 소유자가 가짐", "E_OWNOVERLAP",
     lambda m: m["blocks"][0]["requirements"].append(
         {"rid": "R-OVL-1", "source": dict(m["blocks"][0]["requirements"][0]["source"]),
          "destination": "CUT", "normative_owner": "CUT"})),
    # ⛔ 겹침 판정은 **경계에서** 틀린다 — 부분 겹침·한 줄 접촉을 각각 잠근다(동일 범위 하나론 부족)
    ("소유 다른 **부분** 겹침", "E_OWNOVERLAP",
     lambda m: m["blocks"][0]["requirements"].append(
         {"rid": "R-OVL-2", "source": {"start": 4, "end": 7},
          "destination": "CUT", "normative_owner": "CUT"})),
    ("소유 다른 **한 줄 접촉** 겹침", "E_OWNOVERLAP",
     lambda m: m["blocks"][0]["requirements"].append(
         {"rid": "R-OVL-3", "source": {"start": 5, "end": 6},
          "destination": "CUT", "normative_owner": "CUT"})),
    # 참조 무결성 — 끊긴 참조는 문서에서 미정의 기호가 된다
    # ⛔ 확정 요구가 미확정 요구에 기대는 것은 **선언**돼야 한다 — 평범한 참조로 두면 사라진다
    ("active 가 proposed 를 일반 참조", "E_UNDECLARED_COND",
     lambda m: m["blocks"][0]["requirements"][0].update(
         references=[m["blocks"][1]["requirements"][0]["rid"]])),
    ("조건부 참조가 **확정** 대상을 가리킴", "E_CONDTARGET",
     lambda m: m["blocks"][1]["requirements"][0].update(
         conditional_references=[m["blocks"][0]["requirements"][0]["rid"]])),
    ("조건부 참조가 evidence 를 가리킴", "E_CONDTARGET",
     lambda m: m["blocks"][1]["requirements"][0].update(
         conditional_references=[m["blocks"][2]["requirements"][0]["rid"]])),
    # ⛔ 사실(evidence)·서술은 결정에 의존하지 않는다 — 조건부는 요구/제안만 진다
    ("evidence 가 조건부 참조를 가짐", "E_CONDSOURCE",
     lambda m: m["blocks"][2]["requirements"][0].update(
         conditional_references=[m["blocks"][1]["requirements"][0]["rid"]])),
    # ⛔ **연기 축** — 의존과 반대 방향이라 별 관계다. 여기가 뚫리면 "미룬다"가 "채택돼야 성립한다"로 뒤집힌다.
    ("연기 대상이 **확정** 대상을 가리킴", "E_DEFTARGET",
     lambda m: m["blocks"][1]["requirements"][0].update(
         deferred_references=[m["blocks"][0]["requirements"][0]["rid"]])),
    ("연기 대상이 evidence 를 가리킴", "E_DEFTARGET",
     lambda m: m["blocks"][1]["requirements"][0].update(
         deferred_references=[m["blocks"][2]["requirements"][0]["rid"]])),
    ("evidence 가 연기 참조를 가짐", "E_DEFSOURCE",
     lambda m: m["blocks"][2]["requirements"][0].update(
         deferred_references=[m["blocks"][1]["requirements"][0]["rid"]])),
    ("연기 참조가 실재하지 않음", "E_BADREF",
     lambda m: m["blocks"][0]["requirements"][0].update(
         deferred_references=["R-NOPE-1"])),
    ("자기 자신을 연기 참조", "E_SELFREF",
     lambda m: m["blocks"][1]["requirements"][0].update(
         deferred_references=[m["blocks"][1]["requirements"][0]["rid"]])),
    # ⛔ 한 대상이 두 관계를 동시에 가지면 어느 쪽이 사실인지 모델이 못 고른다.
    #    각각은 적법해서 개별 검사로는 안 걸린다 — 교차 검사가 있어야 잡힌다.
    ("같은 대상을 조건부·연기에 중복 선언", "E_RELDUP",
     lambda m: m["blocks"][0]["requirements"][0].update(
         conditional_references=[m["blocks"][1]["requirements"][0]["rid"]],
         deferred_references=[m["blocks"][1]["requirements"][0]["rid"]])),
    # ⛔ 형식만 맞고 **값이 거짓인** 메타데이터를 각각 잠근다(실측: 셋 다 rc=0 통과했다)
    ("source_metadata.status 가 원문과 다름", "E_METAMISMATCH",
     lambda m: m["source_metadata"].update(status="Approved")),
    ("source_metadata.as_of 가 원문과 다름", "E_METAMISMATCH",
     lambda m: m["source_metadata"].update(as_of="1900-01-01")),
    ("source_metadata.summary_entry 가 원문과 다름", "E_METAMISMATCH",
     lambda m: m["source_metadata"].update(summary_entry="DECISIONS.md ADR-999")),
    # ⛔ 모양만 맞고 **달력에 없는** 날짜 — 정규식은 통과한다
    ("as_of 가 달력에 없는 날짜", "E_METAMISMATCH",
     lambda m: m["source_metadata"].update(as_of="2026-02-30")),
    ("as_of 뒤 개행(앵커 우회)", "E_SCHEMA",
     lambda m: m["source_metadata"].update(as_of=m["source_metadata"]["as_of"] + "\n")),
    ("source_metadata 범위가 archive 밖", "E_METALINES",
     lambda m: m.__setitem__("source_metadata",
                             dict(m["source_metadata"], lines={"start": 1, "end": 99999}))),
    ("조건부 참조가 자기 자신", "E_SELFREF",
     lambda m: m["blocks"][1]["requirements"][0].update(
         conditional_references=[m["blocks"][1]["requirements"][0]["rid"]])),
    ("references 가 실재하지 않는 RID", "E_BADREF",
     lambda m: m["blocks"][0]["requirements"][0].update(references=["R-NOPE-9"])),
    ("references 가 자기 자신", "E_SELFREF",
     lambda m: m["blocks"][0]["requirements"][0].update(
         references=[m["blocks"][0]["requirements"][0]["rid"]])),
    ("active 의 destination != normative_owner", "E_OWNMISMATCH",
     lambda m: m["blocks"][0]["requirements"][0].update(destination="CUT")),
    ("BASE 를 normative_owner 로", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0].update(destination="BASE", normative_owner="BASE")),
    ("malformed — block.start 누락", "E_SCHEMA",
     lambda m: m["blocks"][0].pop("start")),
    ("malformed — source.end 가 문자열", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0]["source"].update(end="bad")),
    ("malformed — blocks 가 문자열", "E_SCHEMA",
     lambda m: m.update(blocks="bad")),
    ("malformed — disposition 오값", "E_SCHEMA",
     lambda m: m["blocks"][0].update(disposition="nonsense")),
    # ⛔ 기존 E_NOREC 반례는 active/proposed/evidence 뿐이라 **rejected·superseded 를 집합에서 빼는**
    #    퇴행이 통과했다(실측). 두 상태도 각각 잠근다.
    ("rejected 인데 레코드 0(결정이 사라짐)", "E_NOREC",
     lambda m: m["blocks"][1].update(disposition="rejected", requirements=[])),
    ("superseded 인데 레코드 0", "E_NOREC",
     lambda m: m["blocks"][1].update(disposition="superseded", requirements=[])),
    # ⛔ "전부 prose" 반례는 E_NOACTIVE 를 active|proposed 로 **완화**하는 퇴행을 못 본다 —
    #    proposed 가 남아 있는 상태를 따로 본다.
    ("active 0 · proposed 는 있음", "E_NOACTIVE",
     lambda m: [b.update(disposition="proposed") for b in m["blocks"] if b["disposition"] == "active"]),
    ("evidence 인데 레코드 0", "E_NOREC",
     lambda m: m["blocks"][3].update(disposition="evidence")),
    ("active 가 E-* ID 사용", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0].update(rid="E-Z-9")),
    ("evidence 가 R-* ID 사용", "E_SCHEMA",
     lambda m: m["blocks"][2]["requirements"][0].update(rid="R-Z-9")),
    ("active 가 supports 보유", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0].update(supports="R-X-1")),
    ("evidence 에 supports 없음", "E_SCHEMA",
     lambda m: m["blocks"][2]["requirements"][0].pop("supports")),
    ("evidence 의 supports 대상 부재", "E_BADSUPPORT",
     lambda m: m["blocks"][2]["requirements"][0].update(supports="R-NONE-9")),
    ("evidence 가 다른 evidence 를 support", "E_SCHEMA",   # supports 패턴이 R-* 라 스키마가 먼저 막음
     lambda m: (m["blocks"][2]["requirements"].append(
         {"rid": "E-X-2", "source": {"start": 26, "end": 27}, "destination": "ADR",
          "normative_owner": None, "supports": "E-X-1"}))),
    ("manifest 가 배열", "E_SCHEMA", lambda m: None),   # 아래 special 처리
    ("title 이 숫자", "E_SCHEMA",
     lambda m: m["blocks"][0].update(title=42)),
    ("구 status 필드 잔존", "E_SCHEMA",
     lambda m: m["blocks"][0].update(status="LEGACY")),
    ("요구사항에 미지 필드", "E_SCHEMA",
     lambda m: m["blocks"][0]["requirements"][0].update(owner="BOGUS")),
    ("prose 블록이 normative_owner 보유", "E_SCHEMA",     # prose maxItems=0 을 스키마가 먼저 막음
     lambda m: m["blocks"][3].update(requirements=[
         {"rid": "R-Y-1", "source": {"start": 22, "end": 23}, "destination": "CUT", "normative_owner": "CUT"}])),
]

# ⛔ 금지 반례만 모으면 **과잉 차단**을 못 본다. 새 축이 실제로 **쓸 수 있는지**는
#    통과해야 하는 형태로만 증명된다 — 연기 축 6행이 전부 금지였다(2026-08-10 실측 누락).
#    여기 항목은 rc==0 을 요구한다: 하나라도 빨강이면 검증기가 정상 사용을 막고 있다는 뜻이다.
ALLOWED = [
    ("active 가 proposed 를 **연기** 참조(정상 사용)",
     lambda m: m["blocks"][0]["requirements"][0].update(
         deferred_references=[m["blocks"][1]["requirements"][0]["rid"]])),
    # R-GATE-7 형태 — 같은 요구가 확정 대상을 인용(references)하면서 미결정 후속을 미룬다(deferred).
    ("references 와 deferred 가 **서로 다른** 대상(R-GATE-7 형태)",
     lambda m: (m["blocks"][0]["requirements"].append(
                    {"rid": "R-X-2", "source": {"start": 6, "end": 8},
                     "destination": "ADR", "normative_owner": "ADR"}),
                m["blocks"][0]["requirements"][0].update(
                    references=["R-X-2"],
                    deferred_references=[m["blocks"][1]["requirements"][0]["rid"]]))),
]


LOCK_MUTATIONS = [
    ("lock 없음", None, "E_LOCK_MISSING"),
    ("lock JSON 손상", "{bad", "E_LOCK_BROKEN"),
    ("lock 최상위가 배열", "[]", "E_LOCK_SHAPE"),
    ("lock.archive 형태 오류", '{"archive":1,"baseline":{},"pinned_commit":{}}', "E_LOCK_SHAPE"),
    ("sha256 이 hex 아님", "SHA_NOT_HEX", "E_LOCK_SHAPE"),
    ("pinned_commit 값 타입 오류", "PC_BAD_TYPE", "E_LOCK_SHAPE"),
    ("lock.path 가 리포 밖", "PATH_ESCAPE", "E_LOCK_PATH"),
    # 단축 해시는 **접두 충돌**로 다른 객체를 가리킬 수 있다 — 길이를 강제하는지 본다
    ("sha256 이 단축(63자)", "SHA_SHORT", "E_LOCK_SHAPE"),
    ("pinned_commit 이 단축(12자)", "PC_SHORT", "E_LOCK_SHAPE"),
    # ⛔ 위 반례는 전부 _load_lock 형태 검사에서 끝난다 — **SHA 대조 코드는 한 번도 안 돈다**.
    #    형식 유효 + 값 오류 반례가 있어야 E_LOCK_SHA 가 살아 있는지 증명된다.
    ("archive sha 형식은 맞고 값이 틀림", "SHA_WRONG", "E_LOCK_SHA"),
]


def lock_variant(kind):
    """실제 lock 을 건드리지 않고 **임시 lock** 으로만 변이한다."""
    base = json.loads((REPO / "spec" / "topic-only.lock.json").read_text())
    if kind == "SHA_NOT_HEX":
        base["archive"]["sha256"] = "z" * 64
    elif kind == "PC_BAD_TYPE":
        base["pinned_commit"] = {"server": 123, "ios": []}
    elif kind == "PATH_ESCAPE":
        base["archive"]["path"] = "../etc/passwd"
    elif kind == "SHA_SHORT":
        base["archive"]["sha256"] = base["archive"]["sha256"][:63]
    elif kind == "SHA_WRONG":
        base["archive"]["sha256"] = "0" * 64      # 64자 hex — 형태 검사는 통과, 실제 파일과 불일치
    elif kind == "PC_SHORT":
        base["pinned_commit"] = {k: v[:12] for k, v in base["pinned_commit"].items()}
    else:
        return kind
    return json.dumps(base, ensure_ascii=False)


def run_lock(tmpdir, content) -> tuple[int, str, str]:
    lk = pathlib.Path(tmpdir) / "lock.json"
    if content is not None:
        lk.write_text(content)
    TMP.write_text(json.dumps(good(), ensure_ascii=False))
    r = subprocess.run([sys.executable, str(VALIDATOR), "verify",
                        "--manifest", str(TMP), "--lock", str(lk)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


# ⛔ 사람 문장("인용 경로")으로 판별하면 verify 의 **부정문**에도 걸린다(실측).
PROV_MARK = "[MODE:with-provenance]"
STRUCT_MARK = "[MODE:structure-only]"


def run_cmd(cmd) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(VALIDATOR), cmd, "--manifest", str(TMP)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


CORPUS_META_CHILD_ENV = "TMM_CORPUS_META_CHILD"
CORPUS_META_CHILD_TOKEN = "topic-migration-meta-control-v1"
CORPUS_SUBJECT_TIMEOUT = 120


def run_bounded(argv, *, cwd=None, env=None, timeout=CORPUS_SUBJECT_TIMEOUT):
    """프로세스 트리 전체에 시간 상한을 건다.

    `subprocess.run(timeout=...)` 은 직계 자식만 죽여 손자가 pipe 를 잡은 채 남을 수 있다.
    코퍼스는 의도적으로 깨진 코드를 실행하므로 새 세션의 process group 전체를 종료한다.
    """
    p = subprocess.Popen(
        argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = p.communicate()
        return 124, out, err, True


def provenance_integration():
    """⛔ 주입(provenance_fn)은 **호출 배선**만 본다 — `default_provenance` 본문이 통째로
    `return []` 로 퇴행해도 잡지 못했다(실측). 여기서 **실제 구현**을 임시 Git 저장소로 시험한다.
    임시 리포라 CI 의 단일 checkout 제약과 무관하다."""
    sys.path.insert(0, str(REPO / "scripts"))
    import topic_migration_manifest as M

    out = []
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        # ⛔ 사용자 전역 설정을 상속하면 서명·hook 때문에 **반례가 아니라 셋업이** 죽는다.
        g = ["git", "-C", str(root),
             "-c", "user.email=t@t", "-c", "user.name=t",
             "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
             "-c", "core.hooksPath=/nonexistent", "-c", "init.defaultBranch=main",
             "-c", "protocol.file.allow=always"]
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["HOME"] = str(root)          # 전역 ~/.gitconfig 도 차단
        run_g = lambda *a, **kw: subprocess.run(g + list(a), env=env, capture_output=True, **kw)
        try:
            # `-b` 는 구형 git 에 없다 → init.defaultBranch 로 대체
            run_g("init", "-q", check=True)
            (root / "app").mkdir()
            (root / "app" / "keep.py").write_text("x = 1\n")
            run_g("add", "-A", check=True)
            run_g("commit", "-qm", "base", check=True)
            pin = run_g("rev-parse", "HEAD", check=True, text=True).stdout.strip()
        except Exception as e:  # noqa: BLE001 — 셋업 실패도 **보고 행**이지 traceback 이 아니다
            return [("prov — 임시 Git 셋업", False, f"⚠️ 셋업 실패: {type(e).__name__}")]
        roots = {"server": root}

        def call(paths, pinned=pin):
            return M.default_provenance("server", paths, pinned, roots=roots)

        # 대조군 — 변경 없으면 무소식이어야 한다(아니면 아래 반례가 무의미)
        base_ok = call(["app/keep.py"]) == []
        out.append(("prov — 변경 없음은 통과(대조군)", base_ok, "" if base_ok else "⚠️ 정상인데 실패"))

        # 없는 경로 → 미존재 + pinned 미포함 둘 다
        r = call(["app/nope.py"])
        out.append(("prov — 없는 경로 검출",
                    any("E_PATHMISSING" in x for x in r) and any("E_PATHNOTPINNED" in x for x in r),
                    f"{len(r)}건"))

        # pinned 이후 변경을 **커밋** → working tree 는 clean.
        # ⛔ 워킹트리만 더럽히면 `git diff HEAD` 로 바꾸는 퇴행도 잡혀서 **구분되지 않는다**(실측).
        #    커밋 후 clean 이어야 pinned 비교만 검출한다 = 퇴행이 드러난다.
        (root / "app" / "keep.py").write_text("x = 2\n")
        run_g("add", "-A"); run_g("commit", "-qm", "after")
        clean = run_g("status", "--porcelain", text=True).stdout.strip() == ""
        r = call(["app/keep.py"])
        out.append(("prov — pinned 이후 **커밋된** 변경 검출(HEAD 치환 퇴행 검출)",
                    clean and any("E_CODECHANGED" in x for x in r),
                    f"{len(r)}건" + ("" if clean else " ⚠️tree dirty=대조 무효")))
        run_g("revert", "--no-edit", "-q", "HEAD")

        # 없는 pin → 죽지 말고 **겨냥한 코드**로 보고 (bool(r) 만 보면 아무 실패나 통과시킨다)
        r = call(["app/keep.py"], pinned="0" * 40)
        out.append(("prov — 잘못된 pin 은 E_PATHNOTPINNED 로 보고",
                    any("E_PATHNOTPINNED" in x for x in r), f"{len(r)}건"))

        # ⛔ 경로가 **1개뿐인** 호출만 있으면 `" ".join(paths)` 같은 pathspec 퇴행이 안 보인다
        #    (1개일 때는 바이트 단위로 같은 명령이 된다). 반드시 2개 이상으로 한 번 호출한다.
        (root / "app" / "second.py").write_text("y = 1\n")
        run_g("add", "-A"); run_g("commit", "-qm", "add second")
        pin2 = run_g("rev-parse", "HEAD", text=True).stdout.strip()
        (root / "app" / "keep.py").write_text("x = 9\n")
        (root / "app" / "second.py").write_text("y = 9\n")
        run_g("add", "-A"); run_g("commit", "-qm", "change both")
        r = M.default_provenance("server", ["app/keep.py", "app/second.py"], pin2, roots=roots)
        both = [x for x in r if "E_CODECHANGED" in x]
        out.append(("prov — **경로 2개** 동시 변경 검출(pathspec join 퇴행 검출)",
                    bool(both) and "keep.py" in str(both) and "second.py" in str(both),
                    f"{len(r)}건"))
        run_g("revert", "--no-edit", "-q", "HEAD")

        # ⛔ 빈 경로를 그대로 넘기면 `git diff <pin> --` 가 **리포 전체**를 비교한다 —
        #    공허 통과 + 범위 밖 파일 오탐(실측: 사용자 소유 pbxproj 가 E_CODECHANGED 로 잡혔다)
        r = call([])
        out.append(("prov — 인용 0개는 E_NOCITED 로 거절(리포 전체 diff 금지)",
                    any("E_NOCITED" in x for x in r), f"{len(r)}건"))

        # Medium 1 — 루트 **선택 규칙** 자체. 임시리포만 쓰면 프로덕션 분기가 통째로 미검증이다.
        pr = M.provenance_root
        out.append(("prov — 루트 선택: server 는 리포 자신",
                    pr("server") == M.REPO, str(pr("server").name)))
        out.append(("prov — 루트 선택: ios 는 sibling ../ios",
                    pr("ios") == M.REPO.parent / "ios", str(pr("ios").name)))
        out.append(("prov — 루트 선택: roots 는 **키별**로 적용",
                    pr("server", roots) == root and pr("ios", roots) == M.REPO.parent / "ios",
                    "server만 주입됨"))
    return out


def main() -> int:
    # 코퍼스 meta-test 가 변이된 러너의 대조군을 실행할 때 전체 meta 를 다시 부르면 무한 재귀다.
    # 이 표식은 그 **대조군 자식 한 단계만** 값싼 초록으로 만들고, 바깥 meta 판정은 그대로 돈다.
    if os.environ.get(CORPUS_META_CHILD_ENV) == CORPUS_META_CHILD_TOKEN:
        print("  ✅ corpus meta child control")
        return 0

    results = []
    try:
        # 0. 대조군 — 정상은 통과해야 한다(아니면 반례 결과가 무의미)
        TMP.write_text(json.dumps(good(), ensure_ascii=False, indent=2))
        rc, out, err = run()
        results.append(("대조군(정상)", rc == 0 and not err.strip(), f"rc={rc}"))

        for name, code, mutate in MUTATIONS:
            if name == "manifest 가 배열":
                TMP.write_text("[]")
            else:
                m = good()
                mutate(m)
                TMP.write_text(json.dumps(m, ensure_ascii=False, indent=2))
            rc, out, err = run()
            # ⛔ rc!=0 만 보면 **traceback 으로 죽은 것**도 성공으로 센다(실측 반례).
            #    rc==1 + stderr 비어 있음 + **겨냥한 오류 코드**가 실제로 나왔는지까지 본다.
            ok = (rc == 1) and (not err.strip()) and (code in out)
            why = f"rc={rc}"
            if err.strip():
                why += " ⚠️traceback"
            elif code not in out:
                why += f" ⚠️{code} 미발생"
            results.append((name, ok, why))
        # ⛔ 허용 대조군 — 금지만 확인하면 과잉 차단을 못 본다(rc==0 을 요구한다).
        for name, mutate in ALLOWED:
            m = good()
            mutate(m)
            TMP.write_text(json.dumps(m, ensure_ascii=False, indent=2))
            rc, out, err = run()
            ok = (rc == 0) and (not err.strip())
            why = f"rc={rc}"
            if err.strip():
                why += " ⚠️traceback"
            elif rc != 0:
                why += " ⚠️과잉 차단 — 정상 사용이 막힌다"
            results.append((f"허용 대조군 — {name}", ok, why))
        # lock 반례 — 실제 lock 을 건드리지 않고 임시 lock 주입
        for name, kind, code in LOCK_MUTATIONS:
            content = lock_variant(kind) if kind else None
            rc, out, err = run_lock(_TD.name, content)
            ok = (rc == 1) and (not err.strip()) and (code in out)
            results.append((f"lock — {name}", ok, f"rc={rc}" + ("" if ok else f" ⚠️{code} 미발생/traceback")))

        # 모드 회귀 — 구조는 provenance 건너뜀 / preflight 는 실행 / 미지 명령은 거부
        TMP.write_text(json.dumps(good(), ensure_ascii=False))
        rc_v, out_v = run_cmd("verify")
        # provenance 호출 계약은 **주입**으로 결정론 검사하고, 실제 다중리포 대조는 별도
        # integration/CI checkout에서 본다. 둘을 섞으면 환경 실패가 배선 단위 테스트를 가린다.
        harness = pathlib.Path(_TD.name) / "prov_test.py"
        harness.write_text(
            "import json, pathlib, sys, io, contextlib\n"
            f"sys.path.insert(0, {str(REPO / 'scripts')!r})\n"
            "import topic_migration_manifest as M\n"
            "calls = []\n"
            # ⛔ repo_key 만 기록하면 **경로 0개·pin 위조** 퇴행이 통과한다(실측 지적).
            "import json as _j\n"
            "def _rec(repo_key, paths, pinned):\n"
            "    calls.append({'k': repo_key, 'n': len(paths), 'pin': pinned})\n"
            "def ok(repo_key, paths, pinned):\n"
            "    _rec(repo_key, paths, pinned); return []\n"
            "def bad(repo_key, paths, pinned):\n"
            "    _rec(repo_key, paths, pinned); return ['[E_PATHMISSING] 주입된 실패']\n"
            "mode = sys.argv[1]; mf = pathlib.Path(sys.argv[2])\n"
            "if mode == 'nocited':\n"
            "    M.cited_paths = lambda *a, **k: {'server': [], 'ios': []}\n"
            # ⛔ realwire: provenance_fn 을 **주입하지 않고** 실제 default_provenance 가 불리는지 본다.
            #    이 배선이 no-op 람다로 바뀌면 근거 대조가 통째로 의례가 된다.
            # ⛔ 개수 하한('0개 금지')만 있으면 11개를 1개로 줄여도 통과한다(실측) — 넓은 그물과 대조
            # 확장자 미상 경로 토큰이 verify 층에서 **터지는지**(격리만 하고 조용하면 소용없다)
            "if mode == 'unknownext':\n"
            "    _rb = M.broad_cited_paths()\n"
            "    M.broad_cited_paths = lambda *a, **k: dict(_rb, unknown=['app/mystery.zzz'])\n"
            "if mode == 'badformat':\n"
            "    M.underscore_citations = lambda *a, **k: ['app/hidden.py']\n"
            # 넓은 그물이 **좁아지는** 방향 — narrow 가 뽑는데 broad 가 놓치면 불변식 위반이다
            "if mode == 'broadnarrowed':\n"
            "    _rb2 = M.broad_cited_paths()\n"
            "    M.broad_cited_paths = lambda *a, **k: dict(_rb2, server=_rb2['server'][1:])\n"
            "if mode == 'narrowed':\n"
            "    _real = M.cited_paths()\n"
            "    M.cited_paths = lambda *a, **k: {k2: v[:1] for k2, v in _real.items()}\n"
            "if mode == 'realwire':\n"
            "    M.cited_paths = lambda *a, **k: {'server': ['app/__NO_SUCH__.py'],\n"
            "                                     'ios': ['FXi/__NO_SUCH__.swift']}\n"
            "    b = io.StringIO()\n"
            "    with contextlib.redirect_stdout(b): rc = M.verify(pathlib.Path(sys.argv[2]), provenance=True)\n"
            "    print(b.getvalue()); print('CALLS='); print('DETAIL=[]'); sys.exit(rc)\n"
            "rc = M.verify(mf, provenance=(mode != 'verify'), provenance_fn=(bad if mode == 'fail' else ok))\n"
            "print('CALLS=' + ','.join(c['k'] for c in calls))\n"
            "print('DETAIL=' + _j.dumps(calls)); sys.exit(rc)\n")

        def harness_run(mode):
            r = subprocess.run([sys.executable, str(harness), mode, str(TMP)],
                               capture_output=True, text=True)
            return r.returncode, r.stdout, r.stderr

        TMP.write_text(json.dumps(good(), ensure_ascii=False))
        rcH_v, outH_v, errH_v = harness_run("verify")
        rcH_p, outH_p, errH_p = harness_run("preflight")
        rcH_f, outH_f, errH_f = harness_run("fail")
        rcH_n, outH_n, errH_n = harness_run("nocited")
        rcH_w, outH_w, errH_w = harness_run("realwire")
        rcH_nr, outH_nr, errH_nr = harness_run("narrowed")
        rcH_u, outH_u, errH_u = harness_run("unknownext")
        rcH_bn, outH_bn, errH_bn = harness_run("broadnarrowed")
        rcH_fmt, outH_fmt, errH_fmt = harness_run("badformat")

        results.append(("mode — verify 는 provenance 를 호출하지 않음",
                        rcH_v == 0 and "CALLS=" in outH_v
                        and outH_v.split("CALLS=")[1].splitlines()[0].strip() == ""
                        and STRUCT_MARK in outH_v and not errH_v.strip(),
                        f"rc={rcH_v} struct_marker={'O' if STRUCT_MARK in outH_v else 'X'}"))
        detail = json.loads(outH_p.split("DETAIL=")[1].splitlines()[0]) if "DETAIL=" in outH_p else []
        lock_pins = json.loads((REPO / "spec" / "topic-only.lock.json").read_text())["pinned_commit"]
        paths_ok = len(detail) == 2 and all(c["n"] > 0 for c in detail)
        pin_ok = all(c["pin"] == lock_pins[c["k"]] for c in detail) and bool(detail)
        results.append(("mode — preflight 는 provenance 를 **호출·완료**",
                        rcH_p == 0 and "server" in outH_p and "ios" in outH_p
                        and PROV_MARK in outH_p and not errH_p.strip(),
                        f"rc={rcH_p} marker={'O' if PROV_MARK in outH_p else 'X'}"))
        # ⛔ "호출됐다"만으로는 부족 — **무엇을** 넘겼는지 본다
        results.append(("preflight — 각 리포에 인용 경로를 **0개 아닌** 채로 넘김", paths_ok,
                        ",".join(f"{c['k']}={c['n']}" for c in detail) or "⚠️호출 기록 없음"))
        results.append(("preflight — 넘긴 pin 이 **lock 의 pinned_commit** 과 일치", pin_ok,
                        "일치" if pin_ok else "⚠️pin 불일치/누락"))
        results.append(("preflight — 주입 없으면 **실제** default_provenance 가 불린다(배선)",
                        rcH_w == 1 and "E_PATHMISSING" in outH_w and not errH_w.strip(),
                        f"rc={rcH_w}" + ("" if "E_PATHMISSING" in outH_w else " ⚠️no-op 가능")))
        # ⛔ 교차검증은 두 추출기가 **다른 규칙**일 때만 의미가 있다 — 넓은 그물을 narrow 와 같게
        #    만들면 차집합이 늘 비어 장식이 된다(실측 rc=0). 합성 baseline 으로 "더 넓은가"를 직접 본다.
        probe_bl = pathlib.Path(_TD.name) / "probe-baseline.md"
        probe_bl.write_text(
            "`app/probe.yml` `scripts/probe.sh` `spec/probe.toml` `FXi/Probe.plist`\n"
            "`CLAUDE.md` `FXi/Probe.m` `app/probe.unknownext` `ios/App.entitlements`\n"
            "`ios/Package.swift` `.github/workflows/probe.yml` `ios/.swiftlint.yml`\n"
            "`./app/relative.cfg` `./app/relative.longextension`\n"
            "`exchange-rate/FXi/server.swift` `ios/app/server.py` `ios/FXi/client.swift`\n"
            "`ios/FXi.xcodeproj/project.pbxproj`\n"
            "`app.get` `db.t4g.micro`\n"
        )
        pb = MOD.broad_cited_paths(probe_bl)
        pn = MOD.cited_paths(probe_bl)
        wider = ({"app/probe.yml", "scripts/probe.sh", "spec/probe.toml", "app/relative.cfg",
                  "FXi/server.swift"}
                 <= set(pb["server"])
                 and ".github/workflows/probe.yml" in pb["server"]
                 and "FXi/Probe.plist" in pb["ios"]
                 # ⛔ narrow 의 **특례 분기**(접두사 없는 CLAUDE.md)도 그물이 덮어야 한다 —
                 #    안 덮으면 그 분기를 지워도 차집합이 비어 통과한다(실측)
                 and "CLAUDE.md" in pb["server"]
                 # 확장자 미상 경로는 조용히 빠지지 말고 unknown 으로 격리돼야 한다(fail-closed)
                 and pb["unknown"] == ["App.entitlements", "FXi/Probe.m", "app/probe.unknownext",
                                       "app/relative.longextension"]
                 # 식별자(app.get / db.t4g.micro)는 파일로 오인하지 않는다
                 and not any(x.startswith(("app.get", "db.t4g")) for x in pb["server"])
                 and set(pn["server"]) == {"CLAUDE.md"}
                 and set(pn["ios"]) == {"FXi/client.swift"})
        results.append(("넓은 그물이 narrow 를 **덮는다**(특례 분기 포함) + 미상 확장자는 격리", wider,
                        f"broad s={len(pb['server'])} i={len(pb['ios'])} unknown={pb['unknown']} "
                        f"/ narrow s={len(pn['server'])}"))
        ios_roots = {"Package.swift", ".swiftlint.yml"} <= set(pb["ios"])
        results.append(("넓은 그물이 `ios/` 루트·숨김 경로를 iOS 로 보존", ios_roots,
                        f"ios={pb['ios']}"))
        excluded_arming = not any("project.pbxproj" in x for xs in pb.values() for x in xs)
        results.append(("범위 제외 project.pbxproj 는 넓은 그물에서도 제외", excluded_arming,
                        "제외" if excluded_arming else "⚠️ 인용 근거로 오인"))
        dual_repo_bl = pathlib.Path(_TD.name) / "dual-repo-baseline.md"
        dual_repo_bl.write_text("`exchange-rate/CLAUDE.md` 와 `ios/CLAUDE.md`\n")
        dual_narrow = MOD.cited_paths(dual_repo_bl)
        dual_broad = MOD.broad_cited_paths(dual_repo_bl)
        dual_ok = all(dual_narrow[key] == ["CLAUDE.md"] and dual_broad[key] == ["CLAUDE.md"]
                      for key in ("server", "ios"))
        results.append(("같은 상대 경로를 server·ios 양쪽에서 인용해도 각각 보존",
                        dual_ok, f"narrow={dual_narrow} broad={dual_broad}"))

        underscore_bl = pathlib.Path(_TD.name) / "underscore-baseline.md"
        underscore_bl.write_text("__app/hidden.py__ 와 _FXi/Hidden.swift_, __CLAUDE.md__\n")
        underscore_hidden = MOD.underscore_citations(underscore_bl)
        results.append(("underscore 강조가 양쪽 그물의 공통 맹점이 되지 않음",
                        underscore_hidden == ["CLAUDE.md", "FXi/Hidden.swift", "app/hidden.py"],
                        f"검출={underscore_hidden}"))
        code_span_bl = pathlib.Path(_TD.name) / "code-span-baseline.md"
        code_span_bl.write_text(
            "`__app/inline.py__` 와 ``_FXi/Inline.swift_`` 는 code span 이다.\n"
            "```text\n__app/fenced.py__\n```\n"
            "````text\n__app/long-close.py__\n`````\n"
            "~~~text\n__app/tilde.py__\n~~~\n"
            "__app/outside.py__ 는 실제 underscore 강조다.\n"
        )
        code_span_hidden = MOD.underscore_citations(code_span_bl)
        results.append(("code span 안 underscore 는 E_CITEFORMAT 오탐하지 않음",
                        code_span_hidden == ["app/outside.py"],
                        f"검출={code_span_hidden}"))
        unclosed_fence_bl = pathlib.Path(_TD.name) / "unclosed-fence-baseline.md"
        unclosed_fence_bl.write_text("```text\n__app/still-code.py__\n")
        results.append(("닫히지 않은 fenced block 도 EOF까지 code로 처리",
                        MOD.underscore_citations(unclosed_fence_bl) == [], "오탐 없음"))

        # ⛔ 후행 문자 판정은 **양쪽 그물의 공통 맹점**이 될 수 있다 — `(?![\w.-])` 였을 때
        #    `app/main.py.`(마침표 직후)와 `app/main.py의`(한글 조사 직결)를 둘 다 놓쳤고,
        #    공통이라 E_CITEMISS 가 못 잡았다(실측). 한국어 문서라 조사 직결은 실제로 일어난다.
        tail_bl = pathlib.Path(_TD.name) / "tail-baseline.md"
        tail_bl.write_text("app/trailing.py. 그리고 app/josa.py의 함수와 FXi/Josa.swift의 뷰,\n"
                           "CLAUDE.md. 끝. 다른 파일 app/other.py.bak 은 별 파일이다.\n")
        tn = MOD.cited_paths(tail_bl)
        tb = MOD.broad_cited_paths(tail_bl)
        tail_ok = ({"app/trailing.py", "app/josa.py", "CLAUDE.md"} <= set(tn["server"])
                   and "FXi/Josa.swift" in tn["ios"]
                   and {"app/trailing.py", "app/josa.py", "CLAUDE.md"} <= set(tb["server"])
                   and "FXi/Josa.swift" in tb["ios"]
                   # 확장자가 이어지면 **다른 파일**이다 — 잘라서 오인하지 않는다
                   and "app/other.py" not in tn["server"] and "app/other.py" not in tb["server"]
                   and "app/other.py.bak" in tb["unknown"])
        results.append(("마침표·한글 조사 직후 인용도 **양쪽 그물이** 잡는다(공통 맹점 차단)", tail_ok,
                        f"narrow s={len(tn['server'])} i={len(tn['ios'])} / broad unknown={tb['unknown']}"))

        results.append(("preflight — 확장자 미상 경로는 E_CITEUNKNOWN 으로 종결(격리만으론 부족)",
                        rcH_u == 1 and "E_CITEUNKNOWN" in outH_u and not errH_u.strip(),
                        f"rc={rcH_u}" + ("" if "E_CITEUNKNOWN" in outH_u else " ⚠️조용히 통과")))
        results.append(("preflight — underscore 강조 인용은 E_CITEFORMAT 으로 종결",
                        rcH_fmt == 1 and "E_CITEFORMAT" in outH_fmt and not errH_fmt.strip(),
                        f"rc={rcH_fmt}" + ("" if "E_CITEFORMAT" in outH_fmt else " ⚠️조용히 통과")))
        results.append(("preflight — **넓은 그물이** 좁아지면 E_CITEBROADMISS(역방향 교차검증)",
                        rcH_bn == 1 and "E_CITEBROADMISS" in outH_bn and not errH_bn.strip(),
                        f"rc={rcH_bn}" + ("" if "E_CITEBROADMISS" in outH_bn else " ⚠️역방향 미검출")))
        results.append(("preflight — 도출이 **줄어들면** E_CITEMISS(넓은 그물 교차검증)",
                        rcH_nr == 1 and "E_CITEMISS" in outH_nr and not errH_nr.strip(),
                        f"rc={rcH_nr}" + ("" if "E_CITEMISS" in outH_nr else " ⚠️축소가 통과")))
        results.append(("preflight — baseline 도출이 0개면 E_NOCITED(정규식 퇴행 방어)",
                        rcH_n == 1 and "E_NOCITED" in outH_n and not errH_n.strip(), f"rc={rcH_n}"))
        results.append(("mode — provenance 실패가 rc 로 전파",
                        rcH_f == 1 and "E_PATHMISSING" in outH_f and not errH_f.strip(),
                        f"rc={rcH_f}"))
        rc_x = subprocess.run([sys.executable, str(VALIDATOR), "prefligth"],
                              capture_output=True, text=True).returncode
        results.append(("mode — 미지 명령 거부", rc_x == 2, f"rc={rc_x}"))

        # ⛔ 아래 셋은 **CLI 배선** 을 잠근다. 위 harness 는 `M.verify()` 를 직접 부르므로
        #    argparse 쪽 퇴행(전역 --manifest 복귀 / provenance 플래그 오배선)을 못 본다(실측 지적).
        # ⛔ 운영 리포의 validator 로 skeleton 을 부르면, `--manifest` 를 받아들이는 **퇴행 상태**에서
        #    이 한 행이 운영 manifest 를 덮어쓴다(코퍼스 주입 시 실제로 그 경로가 열린다). 샌드박스에서만.
        sk_sand = make_sandbox("skeleton-cli")
        rc_sk = subprocess.run([sys.executable, str(sk_sand / "scripts" / VALIDATOR.name), "skeleton",
                                "--force", "--manifest", str(sk_sand / "never.json")],
                               capture_output=True, text=True, cwd=str(sk_sand)).returncode
        never = sk_sand / "never.json"
        results.append(("CLI — skeleton 은 --manifest 를 **거부**(운영 manifest 파괴 차단)",
                        rc_sk == 2, f"rc={rc_sk}" + ("" if rc_sk == 2 else " ⚠️조용히 무시됨")))
        results.append(("CLI — 거부된 skeleton 은 파일을 만들지 않음", not never.exists(),
                        "미생성" if not never.exists() else "⚠️ 생성됨"))

        TMP.write_text(json.dumps(good(), ensure_ascii=False))
        rcC_v, outC_v = run_cmd("verify")
        rcC_p, outC_p = run_cmd("preflight")
        results.append(("CLI — verify 는 구조 모드로 배선",
                        STRUCT_MARK in outC_v and PROV_MARK not in outC_v,
                        f"rc={rcC_v}"))
        results.append(("CLI — preflight 는 근거 모드로 배선",
                        PROV_MARK in outC_p and STRUCT_MARK not in outC_p,
                        f"rc={rcC_p}"))

        # ⛔ 전역 오염 회귀 — 반례는 매번 새 subprocess 라 **한 프로세스 안** 오염을 못 본다.
        leak = pathlib.Path(_TD.name) / "leak_test.py"
        leak.write_text(
            "import io, sys, pathlib, contextlib\n"
            f"sys.path.insert(0, {str(REPO / 'scripts')!r})\n"
            "import topic_migration_manifest as M\n"
            "bad = pathlib.Path(sys.argv[1]); bad.write_text('[]')\n"
            "def cap(*a):\n"
            "    b = io.StringIO()\n"
            "    with contextlib.redirect_stdout(b): M.verify(*a)\n"
            "    return b.getvalue()\n"
            "o_bad = cap(bad); o_implicit = cap(); o_explicit = cap(M.DEFAULT_MANIFEST)\n"
            # ⛔ custom lock 검증이 전역을 오염시키면 그 뒤 build_skeleton 이 **가짜 pin** 을 쓴다(실측)
            "import json as _j, pathlib as _p, tempfile as _t\n"
            "real = _j.loads((_p.Path(M.REPO) / 'spec' / 'topic-only.lock.json').read_text())\n"
            "fake = _j.loads(_j.dumps(real)); fake['pinned_commit'] = {'server': 'a'*40, 'ios': 'b'*40}\n"
            "lp = _p.Path(_t.mkdtemp()) / 'fake.lock.json'; lp.write_text(_j.dumps(fake))\n"
            "cap(M.DEFAULT_MANIFEST, )\n"
            "b = io.StringIO()\n"
            "with contextlib.redirect_stdout(b): M.verify(M.DEFAULT_MANIFEST, lock_path=lp)\n"
            "after = M.build_skeleton()['pinned_commit']\n"
            "if after != real['pinned_commit']: sys.exit(2)\n"
            # 인자 없는 호출은 **운영 manifest** 를 봐야 한다 — 직전 임시 경로를 기억하면 오염이다
            "sys.exit(0 if (o_implicit == o_explicit and o_implicit != o_bad) else 1)\n")
        rc_leak = subprocess.run([sys.executable, str(leak),
                                  str(pathlib.Path(_TD.name) / "leaky.json")],
                                 capture_output=True, text=True).returncode
        results.append(("전역 오염 — verify(임시) 뒤 verify() 가 운영 manifest 를 봄",
                        rc_leak == 0, "격리" if rc_leak == 0 else "⚠️ 임시 경로를 기억함"))
        # ── lock 이 깨졌을 때 **생성 경로**도 fail-closed 인가 (verify 만 막아선 부족했다)
        _lk = json.loads((REPO / "spec" / "topic-only.lock.json").read_text())
        _lk_wrong = json.loads(json.dumps(_lk)); _lk_wrong["archive"]["sha256"] = "0" * 64
        for label, lock_body, want_rc, want_file in (
                ("정상 lock(대조군)", None, 0, True),
                ("손상 lock", "{bad json", 1, False),
                # ⛔ 형식만 보면 통과한다 — 자기 자신을 검증 못 하는 manifest 를 만들고 덮어쓴다(실측)
                ("형식 유효 + SHA 불일치 lock", json.dumps(_lk_wrong), 1, False)):
            sand = make_sandbox(f"lock-{want_rc}", lock_body=lock_body)
            rr = subprocess.run([sys.executable, str(sand / "scripts" / VALIDATOR.name), "skeleton", "--force"],
                                capture_output=True, text=True, cwd=str(sand))
            made = (sand / "spec" / "topic-only-migration-manifest.json").exists()
            results.append((f"skeleton — {label} → rc={want_rc}/생성={want_file}",
                            rr.returncode == want_rc and made == want_file,
                            f"rc={rr.returncode} 생성={made}"))
            # ⛔ "만들어졌다" 만 보면 **생성 즉시 verify 가 깨지는 산출물**을 못 본다(실측 구멍).
            #    schema 가 요구하는 필드를 skeleton 이 빠뜨리면 여기서 드러난다.
            if made:
                rv = subprocess.run([sys.executable, str(sand / "scripts" / VALIDATOR.name), "verify"],
                                    capture_output=True, text=True, cwd=str(sand))
                missing = [w for w in ("source_metadata", "archive_sha", "baseline_sha",
                                       "pinned_commit", "total_lines", "blocks")
                           if f"'{w}' is a required property" in rv.stdout]
                gen = json.loads((sand / "spec" / "topic-only-migration-manifest.json").read_text())
                want, _ = MOD.parse_source_metadata(sand / "TOPIC_ONLY_DELIVERY_CONTRACT.archive.md")
                # ⛔ 키 존재만 보면 모든 키를 가진 **거짓 값**("x")이 통과한다(실측 구멍)
                same = gen.get("source_metadata") == want
                results.append((f"skeleton — {label} 산출물의 metadata 가 **원문 추출값과 동일**",
                                not missing and same,
                                "OK" if (not missing and same) else f"⚠️ 누락{missing} 값일치={same}"))

        # ⛔ 게이트가 지키려는 상태(=분류를 채운 manifest 가 **이미 있는** 상태)를 검사하는 행이 없었다 —
        #    "기존 산출물이 있으면 lock 검사를 건너뛴다"는 fast-path 를 얹어도 전부 초록이었다(실측).
        sand_re = make_sandbox("regen")
        (sand_re / "spec" / "topic-only-migration-manifest.json").write_text('{"분류":"채워둔 산출물"}')
        before_md5 = hashlib.sha256((sand_re / "spec" / "topic-only-migration-manifest.json").read_bytes()).hexdigest()
        arch = sand_re / "TOPIC_ONLY_DELIVERY_CONTRACT.archive.md"
        arch.write_text(arch.read_text() + "\n<!-- lock 과 어긋나게 만든다 -->\n")
        rr_re = subprocess.run([sys.executable, str(sand_re / "scripts" / VALIDATOR.name), "skeleton", "--force"],
                               capture_output=True, text=True, cwd=str(sand_re))
        after_md5 = hashlib.sha256((sand_re / "spec" / "topic-only-migration-manifest.json").read_bytes()).hexdigest()
        results.append(("skeleton — 기존 산출물이 있어도 SHA 어긋나면 **덮지 않는다**(재생성 fast-path 차단)",
                        rr_re.returncode == 1 and before_md5 == after_md5,
                        f"rc={rr_re.returncode} 산출물={'불변' if before_md5 == after_md5 else '⚠️덮어씀'}"))

        # ⛔ `..` 만 막고 resolve() 검사를 지우면 **리포 안 symlink** 로 리포 밖 파일을 archive 로 삼을 수 있다
        sand_sl = make_sandbox("symlink")
        outside = pathlib.Path(_TD.name) / "outside.md"
        outside.write_text("바깥 파일\n두 줄\n")
        link = sand_sl / "spec" / "sneaky.md"
        link.symlink_to(outside)
        lk_sl = json.loads((sand_sl / "spec" / "topic-only.lock.json").read_text())
        lk_sl["archive"] = {"path": "spec/sneaky.md",
                            "sha256": hashlib.sha256(outside.read_bytes()).hexdigest()}
        (sand_sl / "spec" / "topic-only.lock.json").write_text(json.dumps(lk_sl))
        rr_sl = subprocess.run([sys.executable, str(sand_sl / "scripts" / VALIDATOR.name), "skeleton", "--force"],
                               capture_output=True, text=True, cwd=str(sand_sl))
        made_sl = (sand_sl / "spec" / "topic-only-migration-manifest.json").exists()
        results.append(("lock.path 가 **symlink 로 리포 밖**을 가리키면 거절(E_LOCK_PATH)",
                        rr_sl.returncode == 1 and "E_LOCK_PATH" in rr_sl.stdout and not made_sl,
                        f"rc={rr_sl.returncode} 생성={made_sl}"))

        # ⛔ import 시점 lock 스냅샷을 재사용하면 같은 프로세스에서 lock 이 깨져도 낡은 값을 본다
        sand_lk = make_sandbox("lockreload")
        probe = sand_lk / "probe.py"
        probe.write_text(
            "import sys, io, contextlib, pathlib, json\n"
            "sys.path.insert(0, 'scripts')\n"
            "import topic_migration_manifest as M\n"
            "mf = pathlib.Path('spec/manifest.json')\n"
            "mf.write_text(json.dumps(M.build_skeleton()))\n"
            "def run():\n"
            "    b = io.StringIO()\n"
            "    with contextlib.redirect_stdout(b): rc = M.verify(mf)\n"
            "    return rc, b.getvalue()\n"
            "rc1, _ = run()\n"
            "pathlib.Path('spec/topic-only.lock.json').write_text('{bad json')\n"
            "rc2, o2 = run()\n"
            "print('R1=%d R2=%d BROKEN=%s' % (rc1, rc2, 'E_LOCK_BROKEN' in o2))\n")
        rr_lk = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True, cwd=str(sand_lk))
        lk_line = next((l for l in rr_lk.stdout.splitlines() if l.startswith("R1=")), "")
        results.append(("verify 는 lock 을 **매 호출 다시 읽는다**(같은 프로세스 손상 감지)",
                        "R2=1" in lk_line and "BROKEN=True" in lk_line,
                        lk_line or f"⚠️출력 없음 {rr_lk.stderr[:60]}"))

        # ⛔ destination 반례(null/키 누락)는 enum 이 **늘어나는** 방향을 못 본다.
        #    'TBD' 같은 sink 가 하나 생기면 모든 요구사항이 실 문서 밖으로 배출되는데 통과한다.
        #    값 하나를 겨냥하는 대신 **집합 자체**를 계약으로 고정한다.
        # ⛔ 개별 반례는 집합이 **늘어나는** 방향을 못 본다('deferred' 추가가 통과했다 — 실측)
        expected_disp = {"active", "proposed", "rejected", "superseded", "evidence", "prose"}
        results.append(("DISPOSITIONS 집합이 계약과 **정확히** 일치(상태 추가·누락 차단)",
                        MOD.DISPOSITIONS == expected_disp,
                        "일치" if MOD.DISPOSITIONS == expected_disp
                        else f"⚠️ 차이 {MOD.DISPOSITIONS ^ expected_disp}"))

        expected_docs = {"ADR", "HAND", "CLIENT", "LOAD", "HEALTH", "CUT", "BASE"}
        results.append(("DOCS 집합이 계약과 **정확히** 일치(sink 추가·문서 누락 차단)",
                        MOD.DOCS == expected_docs,
                        "일치" if MOD.DOCS == expected_docs
                        else f"⚠️ 차이 {MOD.DOCS ^ expected_docs}"))

        # ⛔ 반환 dict 가 내부 상태의 alias 면 호출자가 고칠 때 **다음 호출**이 오염된다(실측)
        sk1 = MOD.build_skeleton()
        real_pin = json.loads((REPO / "spec" / "topic-only.lock.json").read_text())["pinned_commit"]
        sk1["pinned_commit"]["server"] = "0" * 40
        sk2 = MOD.build_skeleton()
        results.append(("build_skeleton 반환값 변조가 다음 호출을 오염시키지 않음(alias 금지)",
                        sk2["pinned_commit"] == real_pin,
                        "복사본" if sk2["pinned_commit"] == real_pin else "⚠️ alias 오염"))

        prov_rows = provenance_integration()
        # ⛔ 셋업 실패를 조용히 skip 하면 실 구현 검증이 통째로 증발하는데 초록이다(행 개수를 아무도 안 봄)
        results.append((f"prov 통합시험이 **{EXPECTED_PROV_ROWS}행 전부** 산출(조용한 skip 차단)",
                        len(prov_rows) == EXPECTED_PROV_ROWS, f"{len(prov_rows)}행"))
        # ⛔ 스키마 검사는 이 스위트 반례의 대부분을 떠받친다. jsonschema 가 없을 때 조용히
        #    "선택적 검사"로 격하되면 그 반례들이 **실행 환경에서** 통째로 사라진다(설치된 인터프리터에선 안 보임).
        stub = pathlib.Path(_TD.name) / "nojson"
        stub.mkdir(exist_ok=True)
        (stub / "jsonschema.py").write_text("raise ImportError('simulated: not installed')\n")
        env2 = dict(os.environ, PYTHONPATH=str(stub))
        rj = subprocess.run([sys.executable, str(VALIDATOR), "verify", "--manifest", str(TMP)],
                            capture_output=True, text=True, env=env2)
        # ⛔ traceback 은 "검출 성공"이 아니라 "검증기가 죽었다"이다 — stderr 가 비어야 한다
        js_ok = rj.returncode == 1 and "jsonschema" in rj.stdout and not rj.stderr.strip()
        results.append(("jsonschema 부재 시 **제어된** fail-closed(격하·traceback 둘 다 차단)",
                        js_ok, f"rc={rj.returncode}" + ("" if js_ok else
                        (" ⚠️traceback" if rj.stderr.strip() else " ⚠️조용히 통과"))))

        # ⛔ 위 행수 계약은 **정상 git 환경**만 지킨다. 셋업 실패를 `return []` 로 삼키는 퇴행은
        #    git 이 깨졌을 때만 발현하므로 개발 머신에선 영원히 안 보인다 — git 을 고장 내고 확인한다.
        fakebin = pathlib.Path(_TD.name) / "fakebin"
        fakebin.mkdir(exist_ok=True)
        fg = fakebin / "git"
        fg.write_text("#!/bin/sh\nexit 1\n"); fg.chmod(0o755)
        probe = pathlib.Path(_TD.name) / "gitfail_probe.py"
        probe.write_text(
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('S', {str(pathlib.Path(__file__).resolve())!r})\n"
            "S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)\n"
            "rows = S.provenance_integration()\n"
            # 셋업이 깨졌으면 **행이 사라지는 게 아니라 빨강이 남아야** 한다
            "print('ROWS=%d FAILS=%d' % (len(rows), sum(1 for r in rows if not r[1])))\n")
        rp = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                            env=dict(os.environ, PATH=f"{fakebin}:{os.environ.get('PATH', '')}"))
        line = next((l for l in rp.stdout.splitlines() if l.startswith("ROWS=")), "")
        gitfail_ok = bool(line) and " FAILS=0" not in line and "ROWS=0" not in line
        results.append(("git 셋업 실패 시 통합시험이 **행을 남기고 빨강**(조용한 skip 차단)",
                        gitfail_ok, line or "⚠️출력 없음"))

        # ⛔ 범위 오류를 **적기만 하고** 커버리지 루프를 그대로 돌면 end=10**12 은 hang 이 된다
        #    (빠른 rc=1 대신 가용성 실패). 시간 상한을 걸어 "제때 끝나는가"까지 본다.
        huge = good(); huge["blocks"][-1]["end"] = 10 ** 12
        TMP.write_text(json.dumps(huge, ensure_ascii=False))
        try:
            rh = subprocess.run([sys.executable, str(VALIDATOR), "verify", "--manifest", str(TMP)],
                                capture_output=True, text=True, timeout=25)
            huge_ok = rh.returncode == 1 and "E_RANGE" in rh.stdout and not rh.stderr.strip()
            huge_why = f"rc={rh.returncode}"
        except subprocess.TimeoutExpired:
            huge_ok, huge_why = False, "⚠️ 25초 초과(hang) — 범위 상한 미적용"
        results.append(("거대한 end 는 **즉시** E_RANGE 로 종결(hang 금지)", huge_ok, huge_why))
        TMP.write_text(json.dumps(good(), ensure_ascii=False))

        rc_to, _, _, timed_out = run_bounded(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2,
        )
        results.append(("코퍼스 실행 상한이 자식 process group 을 종료", timed_out and rc_to == 124,
                        f"rc={rc_to} timeout={timed_out}"))

        # ⛔ 코퍼스 러너 자체는 어떤 반례로도 보호되지 않았다 — 복원 finally 를 지워도 일반 스위트는
        #    전부 초록이었다(실측). 그러면 앞 항목의 test 변이가 뒤 항목에 남아 **거짓 적중**을 만든다.
        #    합성 코퍼스 2건(반드시 잡혀야 하는 것 / 아무것도 안 바꾸는 것)으로 판정식을 직접 시험한다.
        probe_f = pathlib.Path(_TD.name) / "restore_probe.txt"
        probe_f.write_text("ORIGINAL")
        restored_ok = True
        try:
            def _boom():
                raise RuntimeError("주입된 실행 실패")
            apply_and_run(probe_f, "ORIGINAL", "MUTATED", _boom)
        except RuntimeError:
            pass
        restored_ok &= probe_f.read_text() == "ORIGINAL"
        apply_and_run(probe_f, "ORIGINAL", "MUTATED", lambda: None)
        restored_ok &= probe_f.read_text() == "ORIGINAL"
        results.append(("변이 주입은 **예외가 나도** 원본을 복원한다(거짓 적중 차단)",
                        restored_ok, "복원" if restored_ok else "⚠️ 변이 잔류"))

        # ⛔ 코퍼스 러너 자체는 어떤 반례로도 보호되지 않았다 — 복원·대조군·적중판정·스니펫 유일성을
        #    각각 지워도 스위트는 전부 초록이었다(실측 4건). 합성 코퍼스 + **스텁 스위트**로 직접 시험한다.
        #    변이된 대조군이 현재 파일을 재호출하는 경우만 전용 토큰으로 meta 재귀를 끊는다.
        stub = pathlib.Path(_TD.name) / "stub_suite.py"
        stub.write_text(
            "import pathlib, sys\n"
            "t = pathlib.Path('scripts/test_topic_migration_manifest.py').read_text()\n"
            "lock = pathlib.Path('spec/topic-only.lock.json').read_text()\n"
            "bad = []\n"
            "if not sys.dont_write_bytecode: bad.append('  ⛔ bytecode cache enabled')\n"
            "if lock.lstrip().startswith('{bad'): bad.append('  ⛔ 사본 lock 손상')\n"
            f"if {('EXPECTED_PROV_ROWS = %d' % (EXPECTED_PROV_ROWS + 990))!r} in t: bad.append('  ⛔ SYN-A 주입 감지')\n"
            "print(chr(10).join(bad) if bad else '  ✅ 정상')\n"
            "sys.exit(1 if bad else 0)\n")

        def syn_corpus(name, entries):
            f = pathlib.Path(_TD.name) / f"syn-{name}.json"
            f.write_text(json.dumps({"note": "meta 전용", "mutations": entries}, ensure_ascii=False))
            return f

        SYN_A = {"name": "SYN-A: 반드시 적중", "file": "test",
                 "old_snippet": f"EXPECTED_PROV_ROWS = {EXPECTED_PROV_ROWS}",
                 "new_snippet": f"EXPECTED_PROV_ROWS = {EXPECTED_PROV_ROWS + 990}"}
        SYN_B = {"name": "SYN-B: 무해(구멍 표기가 정상)", "file": "validator",
                 "old_snippet": "# 파일 확장자로 인정하는 것들.",
                 "new_snippet": "# 파일 확장자로 인정하는 것들(무해)."}
        SYN_C = {"name": "SYN-C: 없는 스니펫(stale)", "file": "validator",
                 "old_snippet": "###__없는_스니펫__###", "new_snippet": "x"}
        SYN_D = {"name": "SYN-D: 여러 번 등장(유일성 위반)", "file": "validator",
                 "old_snippet": "    fail.append(", "new_snippet": "    fail.append("}

        def run_meta(corpus_file, env_extra=None):
            # 외부 코퍼스가 설정한 값을 지워야 **내부 러너 자신의 배선**을 시험할 수 있다.
            # 그대로 상속하면 PYTHONDONTWRITEBYTECODE 설정 줄을 지워도 meta-test 가 초록이다.
            meta_env = {
                k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"
            }
            r = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()),
                                "--mutations", "--corpus", str(corpus_file),
                                "--suite-stub", str(stub)],
                               capture_output=True, text=True, cwd=str(REPO),
                               env={**meta_env, **(env_extra or {}),
                                    CORPUS_META_CHILD_ENV: CORPUS_META_CHILD_TOKEN})
            return r.returncode, r.stdout, r.stderr

        # ⛔ stale 은 이제 **시작 전에** 거부한다 — 그래서 적중/구멍 판정과 같은 실행에 섞을 수 없다.
        rc_m, out_m, err_m = run_meta(syn_corpus("main", [SYN_A, SYN_B]))
        hit_a = any(l.startswith("  ✅") and "SYN-A" in l for l in out_m.splitlines())
        hole_b = any(l.startswith("  🕳️") and "SYN-B" in l for l in out_m.splitlines())
        results.append(("코퍼스 meta — 적중/구멍 판정이 각각 동작",
                        rc_m == 1 and hit_a and hole_b and not err_m.strip(),
                        f"A={'O' if hit_a else 'X'} B={'O' if hole_b else 'X'} rc={rc_m}"))

        # ⛔ 앵커가 어긋난 항목은 **주입조차 되지 않는다** → 실행 전에 막고 빨강이어야 한다.
        #    끝에서 알면 20분을 버린다(실측 3회). 없는 스니펫·중복 스니펫 양쪽을 본다.
        for label, entry in (("없는 스니펫", SYN_C), ("여러 번 등장", SYN_D)):
            rc_s, out_s, _ = run_meta(syn_corpus(f"stale-{len(label)}", [entry]))
            _ls = out_s.splitlines()
            went_on = []
            if any(l.startswith("대조군(사본) 초록 확인") for l in _ls):
                went_on.append("대조군 실행")
            if any(l.startswith("코퍼스 ") and " / 구멍 " in l for l in _ls):
                went_on.append("요약 출력")
            if any(l.startswith(("  ✅", "  🕳️")) for l in _ls):
                went_on.append("항목 판정")
            blocked = rc_s == 1 and "앵커가 코드와 어긋난" in out_s and not went_on
            results.append((f"코퍼스 meta — {label} 앵커는 **실행 전** 거부", blocked, f"rc={rc_s}"))

        # 대조군은 **사본**을 봐야 한다 — 사본만 망가뜨리고 원본은 정상인 상황
        rc_g, out_g, _ = run_meta(syn_corpus("sabotage", [SYN_A, SYN_B]),
                                  {"TMM_SABOTAGE_SANDBOX": "1"})
        aborted = "사본 대조군이 초록이 아니다" in out_g
        results.append(("코퍼스 meta — 대조군이 **사본** 건강을 본다(사보타주 시 즉시 무효화)",
                        rc_g == 1 and aborted, f"rc={rc_g} 중단={'O' if aborted else '⚠️X'}"))

        # ⛔ 금지 반례만 있으면 **과잉 차단**을 못 본다 — 허용돼야 하는 두 형태를 대조군으로 잠근다.
        for label, extra, want_pass in (
                ("인접하지만 겹치지 않음(허용)",
                 {"rid": "R-ADJ-1", "source": {"start": 6, "end": 8},
                  "destination": "CUT", "normative_owner": "CUT"}, True),
                # ⛔ 이 대조군은 **실제로 겹쳐야** 의미가 있다(2-5 와 4-7). 안 겹치면 공허하다.
                # ⛔ 과잉 차단 방지 — 미결정끼리의 일반 참조는 조건부 선언 대상이 아니다
                ("proposed → proposed 일반 참조(허용)",
                 None, True),
                ("같은 소유자끼리 겹침(허용)",
                 {"rid": "R-SAME-1", "source": {"start": 4, "end": 7},
                  "destination": "ADR", "normative_owner": "ADR"}, True)):
            g = good()
            if extra is None:      # ⛔ 미결정끼리 참조 — blocks[1] 이 proposed 다(blocks[2] 는 evidence)
                g["blocks"][1]["requirements"].append(
                    {"rid": "R-DEC-9", "source": dict(g["blocks"][1]["requirements"][0]["source"]),
                     "destination": "ADR", "normative_owner": None,
                     "references": [g["blocks"][1]["requirements"][0]["rid"]]})
            else:
                g["blocks"][0]["requirements"].append(extra)
            TMP.write_text(json.dumps(g, ensure_ascii=False))
            rc_a, out_a, err_a = run()
            ok = (rc_a == 0) if want_pass else (rc_a == 1)
            results.append((f"겹침 판정 — {label}", ok and not err_a.strip(),
                            f"rc={rc_a}" + ("" if ok else " ⚠️과잉 차단")))
        TMP.write_text(json.dumps(good(), ensure_ascii=False))

        # ⛔ #66·#67 은 manifest 만 조작해선 겨냥한 방어를 못 본다 —
        #    **archive 자체**를 망가뜨려야 달력 검증과 front-matter 가드가 판정 대상이 된다.
        import shutil as _sh2
        for label, body, want_err in (
                ("front-matter 없음", "# t\n\n> nope\n> nope\n\n> x\n> y\n\n---\n",
                 "front-matter"),
                ("as_of 가 달력에 없는 날짜",
                 "# t\n\n> **상태**: Draft\n> **요약 entry**: x\n\n> **작성 근거**: a\n"
                 "> 이 문서의 모든 코드 단정은 2026-02-30 실측이다.\n\n---\n", "달력")):
            sand_m = make_sandbox(f"meta-{len(label)}")
            arch = sand_m / "TOPIC_ONLY_DELIVERY_CONTRACT.archive.md"
            arch.write_text(body)
            lk2 = json.loads((sand_m / "spec" / "topic-only.lock.json").read_text())
            lk2["archive"]["sha256"] = hashlib.sha256(arch.read_bytes()).hexdigest()
            (sand_m / "spec" / "topic-only.lock.json").write_text(json.dumps(lk2))
            rr2 = subprocess.run([sys.executable, str(sand_m / "scripts" / VALIDATOR.name),
                                  "skeleton", "--force"], capture_output=True, text=True, cwd=str(sand_m))
            made2 = (sand_m / "spec" / "topic-only-migration-manifest.json").exists()
            ok2 = rr2.returncode == 1 and not made2 and want_err in rr2.stdout
            results.append((f"skeleton — archive 의 {label} 이면 **만들지 않는다**", ok2,
                            f"rc={rr2.returncode} 생성={made2}"))

        results.extend(prov_rows)
    finally:
        _TD.cleanup()

    print("반례 테스트 — 각 변이가 **겨냥한 검사**에 잡히는가\n")
    ok_all = True
    for name, passed, why in results:
        print(f"  {'✅' if passed else '⛔'} {name:44s} {why}")
        ok_all &= passed
    print("\n" + ("✅ 검증기는 공허하지 않다 — 모든 반례가 의도한 코드로 차단된다" if ok_all
                  else "⛔ 구멍 또는 오탐이 남아 있다"))
    return 0 if ok_all else 1


def make_sandbox(name: str, *, with_suite: bool = False, lock_body: str | None = None) -> pathlib.Path:
    """⛔ validator 를 **운영 리포에서** 실행하면 퇴행 상태에서 운영 manifest 를 덮어쓴다(실측 경로).
    필요한 입력만 복사한 격리 사본을 만든다. `with_suite=True` 면 반례 스위트까지 복사해
    사본 안에서 전체 스위트를 돌릴 수 있다(코퍼스 주입용)."""
    import shutil
    sand = pathlib.Path(_TD.name) / f"sand-{name}"
    shutil.rmtree(sand, ignore_errors=True)
    (sand / "scripts").mkdir(parents=True)
    (sand / "spec").mkdir()
    shutil.copy(VALIDATOR, sand / "scripts")
    shutil.copy(REPO / "TOPIC_ONLY_DELIVERY_CONTRACT.archive.md", sand)
    shutil.copy(REPO / "spec" / "topic-only-baseline-facts.md", sand / "spec")
    (sand / "spec" / "topic-only.lock.json").write_text(
        lock_body if lock_body is not None else (REPO / "spec" / "topic-only.lock.json").read_text())
    if with_suite:
        shutil.copy(pathlib.Path(__file__).resolve(), sand / "scripts")
        shutil.copy(REPO / "scripts" / "vacuity_mutations.json", sand / "scripts")
        mf = REPO / "spec" / "topic-only-migration-manifest.json"
        if mf.exists():
            shutil.copy(mf, sand / "spec")
    if os.environ.get("TMM_SABOTAGE_SANDBOX"):
        # ⛔ test 전용: 사본만 망가뜨린다. 대조군이 원본을 보면 이 손상을 **놓친다**(그게 판정 대상)
        (sand / "spec" / "topic-only.lock.json").write_text("{bad json")
    # ⛔ 원본 archive/baseline 은 444 라 복사본도 읽기전용이 된다 — 사본은 시험용이므로 쓰기 가능해야 한다
    for f in sand.rglob("*"):
        if f.is_file():
            f.chmod(0o644)
    return sand


def apply_and_run(tgt: pathlib.Path, base: str, new_text: str, run_fn):
    """변이 주입 → 실행 → **반드시 복원**. 예외가 나도 복원한다.
    ⛔ 복원이 빠지면 앞 항목의 변이가 뒤 항목에 남아 **거짓 적중**을 만든다."""
    tgt.write_text(new_text)
    try:
        return run_fn()
    finally:
        tgt.write_text(base)


def run_mutation_corpus(corpus_path: pathlib.Path | None = None,
                        suite_argv: list[str] | None = None,
                        sandbox_name: str = "corpus") -> int:
    """⛔ 이 스위트가 **공허하지 않다**는 주장의 증거. scripts/vacuity_mutations.json 의 각 퇴행을
    주입해 스위트가 잡는지 본다. 하나라도 통과하면 그 계약은 지켜지지 않는 것이다.

    ⛔ **격리 사본에서만 주입한다.** 운영 파일을 직접 고치면 (a) 타임아웃·SIGKILL 이 변이 상태를
       남기고(실측) (b) 'skeleton 에 --manifest 복귀' 변이 상태에서 스위트가 **운영 manifest 를
       덮어쓴다**. finally 는 SIGKILL 을 못 막는다 — 애초에 안 건드리는 게 유일한 방어다.
    ⚠️ 느리다(항목당 전체 스위트 1회) — CI 에 넣지 않는다. 검증기를 고친 뒤 손으로 돌린다."""
    corpus = json.loads((corpus_path or REPO / "scripts" / "vacuity_mutations.json").read_text())["mutations"]
    # ⛔ stale 앵커를 **시작 전에** 거부한다 — 끝에서 알면 20분을 버린다(실측 3회).
    #    앵커가 어긋난 항목은 주입조차 되지 않으므로 그 실행 결과는 애초에 무의미하다.
    _src = {"validator": VALIDATOR.read_text(),
            "test": (REPO / "scripts" / pathlib.Path(__file__).name).read_text()}
    _stale = [(i, m["name"], _src[m["file"]].count(m["old_snippet"]))
              for i, m in enumerate(corpus, 1) if _src[m["file"]].count(m["old_snippet"]) != 1]
    if _stale:
        print(f"⛔ 앵커가 코드와 어긋난 항목 {len(_stale)}건 — 주입조차 되지 않으므로 실행하지 않는다")
        for i, name, c in _stale:
            print(f"  #{i} count={c} {name}")
        return 1
    sand = make_sandbox(sandbox_name, with_suite=True)
    files = {"validator": sand / "scripts" / VALIDATOR.name,
             "test": sand / "scripts" / pathlib.Path(__file__).name}
    originals = {k: v.read_text() for k, v in files.items()}

    def run_suite():
        # ⛔ 변이된 대조군이 현재 파일을 재호출해도 run_meta 의 전용 토큰이 meta 재귀를 끊는다
        rc, out, err, timed_out = run_bounded(
            suite_argv or [sys.executable, str(files["test"])], cwd=str(sand),
            # ⛔ 사보타주 훅은 **상속시키지 않는다** — 안쪽 사본까지 망가지면 판정이 흐려진다
            env={
                **{k: v for k, v in os.environ.items() if k != "TMM_SABOTAGE_SANDBOX"},
                # ⛔ 같은 sandbox 에 같은 크기 변이를 빠르게 연속 기록하면 timestamp+size 기반
                #    pyc 가 앞 변이를 재사용할 수 있다. 전체 코퍼스는 캐시를 만들지 않는다.
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        red = [l for l in out.splitlines() if l.startswith("  ⛔")]
        if timed_out:
            red.append("  ⛔ 코퍼스 대상 스위트 시간 초과")
        return rc, red, err

    rc0, red0, err0 = run_suite()
    if rc0 != 0 or red0 or err0.strip():
        # ⛔ 대조군이 초록이 아니면 아래 "적중" 은 전부 무의미하다 — 변이가 아니라 환경이 잡은 것이다
        print(f"⛔ 사본 대조군이 초록이 아니다 (rc={rc0}, 빨강 {len(red0)}건) — 코퍼스 결과를 신뢰할 수 없다")
        print(err0[:600] or "\n".join(red0[:5]))
        return 1
    print(f"대조군(사본) 초록 확인 — 코퍼스 {len(corpus)}건 주입")

    holes = []
    for i, m in enumerate(corpus, 1):
        tgt = files[m["file"]]
        base = originals[m["file"]]
        if base.count(m["old_snippet"]) != 1:
            holes.append((i, m["name"], "⚠️ 스니펫이 코드와 어긋남(코퍼스 stale)"))
            print(f"  ⚠️ {i:2}. 스니펫 불일치 — {m['name'][:58]}")
            continue
        rc, red, err = apply_and_run(tgt, base, base.replace(m["old_snippet"], m["new_snippet"], 1),
                                     run_suite)
        # ⛔ rc!=0 만 보면 traceback·구문오류·환경실패까지 "적중"으로 센다 —
        #    **제어된 실패**(rc==1 + stderr 없음 + 빨강 행 존재)만 적중이다
        hit = rc == 1 and not err.strip() and bool(red)
        why = "" if hit else (" ⚠️traceback" if err.strip() else
                              (" ⚠️통과(구멍)" if rc == 0 else f" ⚠️rc={rc}/빨강{len(red)}"))
        if not hit:
            holes.append((i, m["name"], why.strip() or "미적중"))
        print(f"  {'✅' if hit else '🕳️'} {i:2}. rc={rc} 빨강{len(red)}{why}  {m['name'][:52]}")

    print(f"\n코퍼스 {len(corpus)}건 / 구멍 {len(holes)}건")
    for i, name, why in holes:
        print(f"  {why}  {i}. {name}")
    return 1 if holes else 0


if __name__ == "__main__":
    if "--mutations" in sys.argv:
        _cp = None
        if "--corpus" in sys.argv:
            _cp = pathlib.Path(sys.argv[sys.argv.index("--corpus") + 1])
        _stub = None
        if "--suite-stub" in sys.argv:
            _stub = [sys.executable, sys.argv[sys.argv.index("--suite-stub") + 1]]
        sys.exit(run_mutation_corpus(_cp, suite_argv=_stub,
                                     sandbox_name="corpus-meta" if _stub else "corpus"))
    sys.exit(main())
