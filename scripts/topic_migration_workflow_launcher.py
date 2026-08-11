#!/usr/bin/env python3
"""Topic-only 분할 워크플로 **launcher** — 입력 고정을 기계로 강제한다.

왜 필요한가 (Launch Blocker)
----------------------------
워크플로 에이전트에게 "archive 를 읽고 판정하라"고 시키면, 그 에이전트가 **어느 리비전을
읽었는지** 결과만 보고는 알 수 없다. 스크래치패드에 남은 옛 사본, 다른 경로, 편집 중인 파일 —
어느 쪽을 읽어도 "통과"라고 보고할 수 있고 그러면 워크플로 결과 전체가 근거를 잃는다.

이 launcher 는 그 공백을 세 겹으로 좁힌다:

1. **launch 전 기계 검증** — lock 이 가리키는 파일을 지금 해싱해 lock·manifest 와 대조하고,
   preflight(구조 + 인용 경로 근거)까지 통과해야만 스크립트를 만든다. 하나라도 어긋나면
   **아무것도 만들지 않는다**(fail-closed).
2. **경로 고정** — 생성된 스크립트에 절대경로를 박아 에이전트가 경로를 고를 여지를 없앤다.
3. **schema enum 트립와이어** — 각 에이전트가 자기가 읽은 파일의 SHA 를 **직접 계산해**
   보고하게 하고, 그 필드를 `enum: ["<정확한 sha>"]` 로 잠근다. 다른 리비전을 읽고
   정직하게 보고하면 구조화 출력 검증에서 **하드 실패**한다.

⛔ **과장 금지** — 3번은 *정직한* 에이전트의 오독을 잡는 트립와이어지, **날조를 막지 못한다**
   (schema 에 값이 보이므로 베껴 적을 수 있다). 기계가 보장하는 것은 1·2번이다:
   launch 시점에 입력이 lock 과 일치했고, 에이전트가 받은 경로가 그 파일이라는 것.

사용
----
    python3 scripts/topic_migration_workflow_launcher.py --check          # 게이트만 (아무것도 안 만든다)
    python3 scripts/topic_migration_workflow_launcher.py --out run.js     # 게이트 통과 시 생성
"""
from __future__ import annotations

import argparse
import hashlib
import re
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import topic_migration_manifest as M  # noqa: E402 — 검증기와 **같은** lock/preflight 를 쓴다(중복 구현 금지)

REPO = M.REPO


def sha256_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gate(*, lock_path: pathlib.Path | None = None,
         manifest_path: pathlib.Path | None = None,
         preflight_fn=None) -> tuple[dict | None, list[str]]:
    """launch 선행조건을 전부 확인한다. 실패 목록이 비어야만 record 를 돌려준다.

    ⛔ 검증 로직을 여기서 다시 구현하지 않는다 — 검증기를 그대로 부른다.
       복제하면 둘이 어긋나고, 어긋나면 어느 쪽이 진실인지 알 수 없다.
    """
    fail: list[str] = []
    lk, lk_err = M._load_lock(lock_path)
    if lk_err:
        return None, [f"[L_LOCK] {lk_err}"]
    ctx = M.LockCtx(lk)

    # 1. lock 이 가리키는 파일을 **지금** 해싱해 대조 (lock 은 우발적 drift 만 잡는다)
    actual = {}
    for name, path in (("archive", ctx.archive), ("baseline", ctx.baseline)):
        if not path.is_file():
            fail.append(f"[L_MISSING] {name} 파일 없음: {path}")
            continue
        actual[name] = sha256_of(path)
        if actual[name] != ctx.pinned_sha[name]:
            fail.append(f"[L_SHA] {name} 이 lock 과 다르다\n"
                        f"    기대 {ctx.pinned_sha[name]}\n    실제 {actual[name]}")

    # 2. manifest 가 같은 lock 을 가리키는지 (셋이 어긋난 채 launch 하면 근거가 갈린다)
    mpath = manifest_path or M.DEFAULT_MANIFEST
    if not mpath.exists():
        fail.append(f"[L_NOMANIFEST] manifest 없음: {mpath}")
    else:
        # ⛔ `man is not None` 로 분기하면 **JSON `null`** 이 모든 검사를 비껴간다(실측 TypeError).
        #    파싱 실패와 "파싱 결과가 None" 을 분리한다.
        man = None
        try:
            parsed = json.loads(mpath.read_text())
        except json.JSONDecodeError as e:
            fail.append(f"[L_MANIFEST] manifest JSON 손상: {e}")
        else:
            if not isinstance(parsed, dict):
                fail.append(f"[L_MANIFEST] manifest 최상위가 object 가 아님: {type(parsed).__name__}")
            elif not isinstance(parsed.get("blocks"), list):
                fail.append("[L_MANIFEST] manifest.blocks 가 배열이 아님 — 리뷰 커버리지를 만들 수 없다")
            else:
                man = parsed
        if man is not None:
            if man.get("archive_sha") != ctx.pinned_sha["archive"] \
                    or man.get("baseline_sha") != ctx.pinned_sha["baseline"] \
                    or man.get("pinned_commit") != ctx.pinned_commit:
                fail.append("[L_PROPAGATE] manifest 의 sha/pinned_commit 이 lock 과 불일치")

    # 3. preflight — 구조 + 인용 경로 근거 대조까지 통과해야 한다
    #    ⛔ record 에 적는 manifest 와 preflight 가 검사한 manifest 가 **같아야** 한다 —
    #       안 그러면 "검증된 산출물"이라는 근거가 다른 파일을 가리킨다(실측 지적).
    rc = (preflight_fn or _default_preflight)(lock_path, mpath)
    if rc != 0:
        fail.append(f"[L_PREFLIGHT] preflight 실패(rc={rc}) — 분류를 채우고 근거 대조를 통과시킬 것. "
                    f"단독 실행: python3 scripts/topic_migration_manifest.py preflight")

    if fail:
        return None, fail
    return {
        "archive": {"path": str(ctx.archive), "sha256": actual["archive"]},
        "baseline": {"path": str(ctx.baseline), "sha256": actual["baseline"]},
        "manifest": {"path": str(mpath), "sha256": sha256_of(mpath)},
        "block_ids": [b["id"] for b in man["blocks"]],
        "rids": sorted(r["rid"] for b in man["blocks"] for r in b.get("requirements", [])),
        "pinned_commit": dict(ctx.pinned_commit),
    }, []


def _default_preflight(lock_path: pathlib.Path | None,
                      manifest_path: pathlib.Path | None = None) -> int:
    """preflight 출력은 launcher 요약을 어지럽히므로 삼킨다 — rc 만 쓴다."""
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return M.verify(manifest_path, provenance=True, lock_path=lock_path)


def render_workflow_script(record: dict, delta: list[str] | None = None) -> str:
    if delta:
        # 형식·멤버십을 여기서 막지 않으면 오타가 조용히 "초점 없음" 이 되고, 특수문자는 JS 를 깬다
        known = set(record.get("rids") or [])
        bad = [d for d in delta if not re.fullmatch(r"[RE]-[A-Z]+-[0-9]+", d) or d not in known]
        if bad:
            raise ValueError(f"delta RID 가 형식에 안 맞거나 manifest 에 없다: {bad}")
    """⛔ 결정론적이어야 한다 — 타임스탬프·난수를 넣지 않는다(테스트가 바이트로 대조한다).
    워크플로 스크립트 안에서는 `Date.now()`/`Math.random()` 이 금지되기도 한다."""
    pinned = json.dumps(record, ensure_ascii=False, indent=2)
    a_sha = record["archive"]["sha256"]
    b_sha = record["baseline"]["sha256"]
    m_sha = record["manifest"]["sha256"]
    # ⛔ 리뷰 **대상**이 manifest 인데 manifest SHA 를 안 잠그면, 생성 후 manifest 가 바뀌어도
    #    archive/baseline SHA 와 빈 findings 만 내면 통과한다(실측 지적).
    blocks = json.dumps(record["block_ids"], ensure_ascii=False)
    n_blocks = len(record["block_ids"])
    # ⛔ delta 는 **범위 축소가 아니라 초점**이다 — 전 블록 열거는 그대로 강제한다.
    #    안 그러면 "일부만 보고 통과" 가 되어 커버리지 계약이 무너진다.
    # ⛔ 이건 diff 가 아니라 **초점**이다. 이전 판을 고정하지 않으므로 "새 지적만" 을 판별할 수 없다 —
    #    그래서 focus 라 부르고 **모든 지적을 보고**하게 한다(무엇이 새 것인지는 사람이 판단한다).
    delta_note = ("" if not delta else
                  "\n\n## 우선 판정 대상(focus)\n마지막 변경에서 새로 생기거나 바뀐 RID: "
                  + json.dumps(sorted(delta), ensure_ascii=False)
                  + "\n이 RID 와 그것이 참조하거나 그것을 참조하는 것을 **먼저** 본다. "
                    "⛔ 범위 축소가 아니다 — 전 블록을 훑고(reviewed_blocks 전수) **찾은 지적은 전부** 낸다.")
    return f'''export const meta = {{
  name: 'topic-only-split-review',
  description: 'topic-only 분할 manifest 를 고정된 입력에서 검토한다',
  phases: [{{ title: 'Review', detail: '고정 입력을 읽고 분류 타당성을 판정' }}],
}}

// ⛔ 이 파일은 **생성물**이다. 손으로 고치지 말 것 —
//    scripts/topic_migration_workflow_launcher.py 가 lock 을 검증한 뒤 만들어야 한다.
//    손으로 SHA 를 적으면 "입력이 고정됐다"는 근거가 사라진다.
const PINNED = {pinned}

// 각 에이전트는 **자기가 읽은 파일**의 SHA 를 직접 계산해 보고한다.
// enum 이라 다른 리비전을 읽고 정직하게 보고하면 구조화 출력에서 하드 실패한다.
// ⚠️ 날조까지 막지는 못한다 — 기계가 보장하는 것은 launch 시점의 입력 동일성이다.
const SCHEMA = {{
  type: 'object',
  additionalProperties: false,
  required: ['archive_sha', 'baseline_sha', 'manifest_sha', 'reviewed_blocks', 'findings'],
  properties: {{
    archive_sha: {{ type: 'string', enum: ['{a_sha}'] }},
    baseline_sha: {{ type: 'string', enum: ['{b_sha}'] }},
    manifest_sha: {{ type: 'string', enum: ['{m_sha}'] }},
    // ⛔ `findings: []` 는 유효하다(억지로 채우면 안 되므로). 그래서 **검토 범위**를 따로 강제한다 —
    //    전 블록 id 를 빠짐없이·중복 없이 열거해야 구조화 출력이 통과한다.
    //    ⚠️ 날조는 막지 못한다 — "조용히 일부만 보고 빈 findings" 를 막는 것이 목적이다.
    reviewed_blocks: {{
      type: 'array', uniqueItems: true,
      minItems: {n_blocks}, maxItems: {n_blocks},
      items: {{ type: 'string', enum: {blocks} }},
    }},
    findings: {{
      type: 'array', maxItems: 200,
      items: {{
        type: 'object', additionalProperties: false,
        required: ['block_id', 'issue', 'evidence'],
        properties: {{
          block_id: {{ type: 'string', enum: {blocks} }},
          issue: {{ type: 'string' }},
          evidence: {{ type: 'string' }},
        }},
      }},
    }},
  }},
}}

const PROMPT = `아래 **절대경로** 파일만 읽는다. 다른 사본을 찾지 마라.

- archive:  ${{PINNED.archive.path}}
- baseline: ${{PINNED.baseline.path}}
- manifest: ${{PINNED.manifest.path}}

시작 전에 반드시 실행해 네가 읽은 파일의 해시를 **직접** 확인하고 그 값을 보고하라:
  shasum -a 256 "${{PINNED.archive.path}}" "${{PINNED.baseline.path}}" "${{PINNED.manifest.path}}"

⛔ 파일을 수정하지 마라(읽기 전용).
⛔ 값을 추측하거나 schema 에서 베껴 적지 마라 — 실제로 계산한 값을 적는다.

배경: manifest 는 archive 를 **행 범위로 빠짐없이 분할**하고 각 블록에 disposition 과 요구사항을 단다.
기계 검증은 커버리지·형식·단일 소유까지만 본다 — **분류가 옳은지는 못 본다**(모든 요구사항을 한
문서로 몰아넣어도 통과한다). 그 판정이 네 임무다.

⛔ reviewed_blocks 에는 manifest 의 **모든 블록 id 를 빠짐없이** 적는다(${{PINNED.block_ids.length}}개).
   실제로 본 것만 적어라 — 안 본 블록을 적는 것은 날조다.
⛔ 문제가 없으면 findings 를 **빈 배열**로 둔다. 억지로 채우지 마라.
⛔ 각 finding 의 evidence 에는 archive **행 번호**를 인용한다.{delta_note}`

const LENSES = [
  {{ key: 'disposition', ask: `각 블록의 **disposition** 이 본문과 맞는지만 본다.
active/proposed/rejected/superseded/evidence/prose 중 틀린 배정을 찾아라.
특히 **[제안·결정 대기]** 인 것이 active 로, 확정된 것이 proposed 로 잘못 들어갔는지 본다.` }},
  {{ key: 'ownership', ask: `각 요구사항의 **destination / normative_owner** 배정이 맞는지만 본다.
문서 책임: ADR=불변식·결정·게이트 / HAND=서버 build·ack·close 계약 / CLIENT=클라 상태기계·재시도·재검증 /
LOAD=부하·동시성·nginx / HEALTH=publisher health·SLO / CUT=삭제 범위·문서 정정·테스트·검증 / BASE=검증된 사실.
서버 계약이 CLIENT 로, 클라 계약이 HAND 로 간 것을 찾아라.` }},
  {{ key: 'references', ask: `**참조** 만 본다. 모델은 **네 축**이다 — source(규범 소유, 하나) ·
references(비규범 인용) · conditional_references(**미결정 제안에 대한 의존**) ·
deferred_references(**명시적 연기·범위 제외**).
⛔ **연기는 의존이 아니다 — 방향이 반대다.** conditional 은 "대상이 채택돼야 이 요구가 성립한다"이고,
   deferred 는 "대상을 이번 범위에서 뺀다"라 **대상이 기각돼도 이 요구는 그대로 성립한다**.
   원문이 "phased 로 미룬다 / 후속 / 이번 출시 아님" 이면 deferred 다. conditional 로 적으면
   확정 요구가 근거 없이 **미결정으로 격하**된다.
⛔ 조건부·연기 대상은 둘 다 proposed 하나뿐이다(rejected/superseded 는 결론이 난 상태, evidence 는 결정이 아니다).
⛔ 확정(active) 요구가 미결정을 일반 참조로 가리키면 안 된다 — 의존이면 conditional_references,
   연기면 deferred_references 로 **선언**해야 한다.
⛔ 같은 대상이 references·conditional·deferred 중 **둘 이상**에 중복되면 안 된다 — 관계는 하나다.
⛔ 조건부 **범위**도 본다: 표의 한 행만 조건부인데 표 전체에 걸면 무관한 행까지 미결정이 된다.
다음 셋을 각각 판정하라:
① **필요한가** — 그 문서가 실제로 그 줄을 참조해야 하는가, 아니면 장식인가.
② **원자적인가** — 필요한 것이 한 줄인데 넓은 범위의 RID 를 가리켜 무관한 내용까지 끌고 오는가.
③ ⛔ **상태 승격이 아닌가** — active 요구사항이 proposed RID 를 참조해 그 제안을 사실상 구속력
   있는 것처럼 만들지 않는가. (사용자가 [제안·결정 대기] 로 남기기로 한 것을 우회하면 안 된다.)
④ ⛔ **관계 종류가 원문과 맞는가** — 의존인데 deferred 로, 연기인데 conditional 로 적혀 있지 않은가.
⑤ ⛔ **격하가 아닌가** — deferred 선언이 source 요구 자체의 효력을 미결정으로 끌어내리지 않는가.
그리고 **참조가 있어야 하는데 없는 곳**(다른 문서의 정의를 이름만 쓰는 곳)도 찾아라.` }},
  {{ key: 'omission', ask: `**역방향으로** 훑는다 — archive 본문의 규범 문장(⛔/→/**결정**/"채택"/"금지"/"해야 한다")
중에서 **어떤 요구사항 source 범위에도 안 잡힌 것**을 찾아라. 이게 이 리뷰의 핵심이다:
구조 검증은 커버리지만 보므로 한 블록에 요구사항 하나만 달아도 통과한다.` }},
]

phase('Review')
const reviews = await parallel(LENSES.map(l => () =>
  agent(PROMPT + '\\n\\n## 이번 렌즈\\n' + l.ask,
        {{ label: `review:${{l.key}}`, phase: 'Review', schema: SCHEMA }})))

// ⛔ 렌즈는 **필터 전 원래 인덱스**로 묶는다. filter(Boolean) 뒤에 인덱스를 쓰면 한 렌즈가
//    실패했을 때 이후 finding 이 **다른 렌즈 이름**을 달고 나온다(실측 지적).
// ⛔ 실패한 렌즈를 조용히 버리면 "전 렌즈 검토 완료"가 거짓이 된다 — 명시적으로 남긴다.
const labelled = reviews.map((r, i) => ({{ lens: LENSES[i].key, review: r }}))
const failedLenses = labelled.filter(x => !x.review).map(x => x.lens)
if (failedLenses.length) {{
  log(`⛔ 실패한 렌즈 ${{failedLenses.length}}/${{LENSES.length}}: ${{failedLenses.join(', ')}}`)
  // ⛔ 기록만 하고 정상 반환하면 fail-open 이다 — 소비자가 complete 를 안 보면 불완전한 리뷰가
  //    완료로 처리된다. 성공한 렌즈의 결과는 journal.jsonl 에 남으므로 던져도 잃지 않는다.
  throw new Error(`리뷰 불완전: ${{failedLenses.join(', ')}} 렌즈 실패 — 재실행할 것`)
}}
const ok = labelled.filter(x => x.review)

return {{
  pinned: PINNED,
  complete: failedLenses.length === 0,
  failed_lenses: failedLenses,
  reported: ok.map(x => ({{
    lens: x.lens, archive: x.review.archive_sha, baseline: x.review.baseline_sha,
    manifest: x.review.manifest_sha, reviewed: (x.review.reviewed_blocks || []).length,
  }})),
  findings: ok.flatMap(x => (x.review.findings || []).map(f => ({{ ...f, lens: x.lens }}))),
}}
'''



def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="topic-only 워크플로 launcher (입력 고정 강제)")
    ap.add_argument("--out", type=pathlib.Path, help="생성할 워크플로 스크립트 경로")
    ap.add_argument("--check", action="store_true", help="게이트만 확인하고 아무것도 만들지 않는다")
    ap.add_argument("--force", action="store_true", help="--out 대상이 이미 있으면 덮어쓴다")
    ap.add_argument("--lock", type=pathlib.Path, help="대체 lock 경로(테스트용)")
    ap.add_argument("--focus", help="우선 판정할 RID(쉼표 구분) — **범위 축소가 아니라 초점**이다")
    ap.add_argument("--delta", help="(구 이름) --focus 를 쓸 것 — diff 가 아니라 focus 라 이름을 바꿨다")
    a = ap.parse_args(argv)

    if not a.check and not a.out:
        print("⛔ --out 또는 --check 중 하나가 필요하다")
        return 2

    record, fail = gate(lock_path=a.lock)
    if fail:
        print("⛔ launch 게이트 실패 — 워크플로 스크립트를 만들지 않는다")
        for f in fail:
            print(f"  - {f}")
        return 1

    print("✅ launch 게이트 통과")
    print(f"  archive  {record['archive']['sha256'][:16]}…  {record['archive']['path']}")
    print(f"  baseline {record['baseline']['sha256'][:16]}…  {record['baseline']['path']}")
    print(f"  pinned   server={record['pinned_commit']['server'][:12]}… "
          f"ios={record['pinned_commit']['ios'][:12]}…")
    if a.check:
        print("  (--check 이므로 생성하지 않음)")
        return 0

    if a.out.exists() and not a.force:
        print(f"⛔ 이미 있음: {a.out} (덮어쓰려면 --force)")
        return 1
    a.out.parent.mkdir(parents=True, exist_ok=True)
    focus_raw = a.focus or a.delta
    if a.delta and not a.focus:
        print("⚠️ --delta 는 구 이름이다 — 계약은 diff 가 아니라 focus 이므로 --focus 를 쓸 것")
    try:
        script = render_workflow_script(
            record, [x.strip() for x in focus_raw.split(",") if x.strip()] if focus_raw else None)
    except ValueError as e:      # ⛔ 죽지 말고 보고한다 — 검증기와 같은 계약
        print(f"⛔ {e}")
        return 2
    a.out.write_text(script)
    print(f"  생성: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
