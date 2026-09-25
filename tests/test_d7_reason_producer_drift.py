"""D7 L6.2 / L9.39, addendum A6: AST-only producer reason drift gate.

REASON_ENUM below is copied verbatim from ledger_contract_r3.md L6.2. There is
deliberately no ledger or application import (the ledger need not exist).
When the contract changes, review the enum, source-to-axis routes, exemptions,
and mutation tests together; never learn an allowlist from the sources at test
runtime. Exemptions identify file, qualified function, destination, and exact
source expression, NOT globally exempt strings.

This is a closed-world checker, not a general Python interpreter. It enumerates
reason supplies and binds positional/keyword/default arguments. Reviewed
indirect edges cover observations, skipped attempts, and blocked writer modes.
AST fingerprints seal only the listed reason-routing functions and the module
bindings they use, after replacing successfully checked axis string/None leaves.
Crawler/CRUD bodies, unrelated helpers and module declarations are not sealed;
reason supplies throughout all seven files are still analyzed, and unresolved
supplies fail. CRUD blocked-mode values follow the actual branch conditions and
early returns. This bounded analysis does not prove arbitrary Python aliases or
new opaque helper routes safe; those require review, and L6.2's runtime
other/reason_other diagnostic remains necessary.

Run normally, with repository conftest isolation enabled:
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
tests/test_d7_reason_producer_drift.py
"""

import ast
import builtins
import copy
import hashlib
import symtable
from pathlib import Path

import pytest


REASON_ENUM = frozenset("""
validated not_started not_observed empty_or_placeholder nan_value
out_of_range parse_failed selector_missing http_403 attempt_failed attempt_interrupted
cooldown mibank_untrusted_window per_currency_write_unverified not_submitted_to_writer
telemetry_error validation_rejected not_instrumented evidence_incomplete
v2_evidence_unconfirmed no_value unobserved previous_attempt_succeeded
value_none equal_to_last_record committed commit_outcome_unknown
commit_not_reached guard_unrecorded staging_incomplete
withheld_before_writer write_mode_uninitialized write_mode_halt
reason_unrecorded report_malformed other
""".split())

INV = "app/crawlers/investing_report.py"
BANK = "app/crawlers/bank_report.py"
INV_CALL = "app/crawlers/investing.py"
BS = "app/crawlers/bs.py"
CITI = "app/crawlers/citi.py"
CRUD = "app/crud.py"
CONTROL = "app/atomic_write_control.py"
FILES = (INV, BANK, INV_CALL, BS, CITI, CRUD, CONTROL)
ROOT = Path(__file__).resolve().parents[1]


class ReasonDrift(AssertionError):
    """An unreviewed source, route, or value reached the static gate."""


def _name(node):
    return ast.unparse(node)


class Source:
    def __init__(self, filename, text):
        self.filename, self.text = filename, text
        self.tree = ast.parse(text, filename=filename)
        self.parents, self.scopes, self.functions = {}, {}, {}

        def index(node, scope="<module>"):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = node.name if scope == "<module>" else scope + "." + node.name
                self.functions[scope] = node
            self.scopes[node] = scope
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
                index(child, scope)

        index(self.tree)

    def raw(self, node):
        return ast.get_source_segment(self.text, node) or _name(node)

    def route(self, node):
        """Keep the destination, including dictionary keys and assignment target."""
        parts = []
        while node in self.parents:
            parent = self.parents[node]
            if isinstance(parent, ast.Dict):
                for key, value in zip(parent.keys, parent.values):
                    if value is node:
                        parts.append("dict[" + (_name(key) if key else "**") + "]")
            elif isinstance(parent, ast.keyword):
                parts.append("keyword[" + str(parent.arg) + "]")
            elif isinstance(parent, (ast.Assign, ast.AnnAssign)):
                targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                parts.append("assign " + ", ".join(map(_name, targets)))
                break
            elif isinstance(parent, ast.Return):
                parts.append("return")
                break
            elif isinstance(parent, ast.Expr):
                parts.append("expression")
                break
            node = parent
        return " -> ".join(parts)

    def fail(self, node, message):
        raise ReasonDrift(
            f"{self.filename}:{getattr(node, 'lineno', 1)} "
            f"{self.scopes[node]}: {message}; expression={self.raw(node)!r}"
        )


class Supply:
    def __init__(self, source, node, value, kind):
        self.source, self.node, self.value, self.kind = source, node, value, kind

    @property
    def key(self):
        s = self.source
        return (s.filename, s.scopes[self.node], self.kind, s.route(self.node), s.raw(self.node))


def _bind(source, call, names, *, offset=0):
    """Conservative binding; **kwargs cannot hide a second reason argument."""
    values = {}
    for name, value in zip(names, call.args[offset:]):
        if isinstance(value, ast.Starred):
            source.fail(call, "unresolved starred reason supplier")
        values[name] = value
    if len(call.args) - offset > len(names):
        source.fail(call, "unresolved positional reason supplier")
    for keyword in call.keywords:
        if keyword.arg is None:
            # Only these reviewed local evidence dictionaries are expanded by
            # _result; their construction is sealed along with their callers.
            if _name(call.func) != "_result" or _name(keyword.value) != "evidence":
                source.fail(call, "unresolved keyword expansion at reason sink")
            continue
        if keyword.arg in values:
            source.fail(call, "duplicate reason/sink keyword")
        values[keyword.arg] = keyword.value
    return values


def _supplies(source):
    """Discover sinks/suppliers throughout all seven files, including new code."""
    handled_keywords = set()
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Call):
            target = _name(node.func)
            method = target.rsplit(".", 1)[-1]
            if target == "_result":
                bound = _bind(source, node, ("status", "reason"))
                if "reason" not in bound:
                    source.fail(node, "new sink without an explicit reason")
                yield Supply(source, node, bound["reason"], "result")
                handled_keywords.update(node.keywords)
            elif method == "writer_guard":
                names = (("path", "decision", "mode") if target.startswith("self.")
                         and source.filename == BANK else ("decision", "mode"))
                bound = _bind(source, node, names)
                if not {"decision", "mode"} <= bound.keys():
                    source.fail(node, "unresolved guard default/keyword call")
                yield Supply(source, node, bound["mode"], "guard")
                handled_keywords.update(node.keywords)
            elif method == "safely_report":
                # A dynamic method name is not silently ignored, including
                # one introduced in an unsealed crawler/CRUD caller.
                if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
                    source.fail(node, "unresolved report dispatch / new sink")
                method = node.args[1].value
                signatures = {
                    "observation": ("attempt_id", "pair"),
                    "finish_attempt": ("attempt_id", "error", "reason"),
                    "policy_skipped": ("path", "reason"),
                }
                if method in signatures:
                    bound = _bind(source, node, signatures[method], offset=2)
                    if "reason" in bound:
                        yield Supply(source, node, bound["reason"], "dispatch:" + method)
                    handled_keywords.update(node.keywords)
                elif method in ("missed", "item_missed", "adoption", "writer_guard"):
                    # Exact PathObserver forwarding expressions are reviewed
                    # edges, never a blanket exemption for the method name.
                    yield Supply(source, node, node, "forward:" + method)
                    handled_keywords.update(node.keywords)
            elif method in ("missed", "item_missed", "adoption", "observation", "policy_skipped"):
                names = {
                    "missed": ("pair", "reason"), "item_missed": ("item_key", "reason"),
                    "adoption": ("decision", "reason"), "observation": ("attempt_id", "pair"),
                    "policy_skipped": ("path", "reason"),
                }[method]
                bound = _bind(source, node, names)
                if "reason" in bound:
                    yield Supply(source, node, bound["reason"], "call:" + method)
                handled_keywords.update(node.keywords)

        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in ("reason", "guard_reason"):
                    yield Supply(source, node, value, "dict:" + key.value)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                is_reason = isinstance(target, ast.Name) and target.id.endswith("reason")
                is_reason |= isinstance(target, ast.Attribute) and target.attr.endswith("reason")
                if isinstance(target, ast.Subscript):
                    is_reason |= isinstance(target.slice, ast.Constant) and target.slice.value in (
                        "reason", "guard_reason")
                if is_reason and node.value is not None:
                    yield Supply(source, node, node.value, "assignment:" + _name(target))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.posonlyargs + args.args
            defaults = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
            defaults += list(zip(args.kwonlyargs, args.kw_defaults))
            for arg, value in defaults:
                if arg.arg.endswith("reason") and value is not None:
                    yield Supply(source, value, value, "default:" + arg.arg)

    # A second pass avoids depending on ast.walk's parent/child order.
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in ("reason", "guard_reason") and keyword not in handled_keywords:
                    yield Supply(source, node, keyword.value, "keyword:" + keyword.arg)


# Populated from an author-time review, never recomputed/accepted at test runtime.
# Each entry is (file, qualified function, supply kind, destination, source text).
# See the explanations next to the route groups below.
NON_AXIS = {
    (
        'app/atomic_write_control.py', 'evaluate_preflight',
        'assignment:reason', '',
        'reason = "control row 없음 — required_writer_protocol 미상"'
    ): 'admin preflight diagnostic; not effective-mode output or axis reason',
    (
        'app/atomic_write_control.py', 'evaluate_preflight',
        'assignment:reason', '',
        'reason = None if passed else (\n'
        '            f"image protocol range [{image_min}, {image_max}]가 "\n'
        '            f"required_writer_protocol={required} 미충족"\n'
        '        )'
    ): 'admin preflight diagnostic; not effective-mode output or axis reason',
    (
        'app/atomic_write_control.py', 'evaluate_preflight',
        'dict:reason', 'return',
        '{\n'
        '        "image_min_protocol": image_min,\n'
        '        "image_max_protocol": image_max,\n'
        '        "required_writer_protocol": required,\n'
        '        "passed": passed,\n'
        '        "reason": reason,\n'
        '    }'
    ): 'admin preflight diagnostic; not effective-mode output or axis reason',
    (
        'app/crawlers/bank_report.py', 'BankReport.__init__',
        'result', 'assign self.execution',
        '_result("running", "round_started")'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/bank_report.py', 'BankReport.__init__',
        'result', "dict['execution'] -> assign self.attempts",
        '_result("not_attempted", "not_started")'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/bank_report.py', 'BankReport.adoption',
        'dict:reason', "assign self.attempts[path]['adoption']",
        '{"decision": decision, "reason": reason}'
    ): 'adoption.reason -> writing detail only; axis reason is withheld_before_writer',
    (
        'app/crawlers/bank_report.py', 'BankReport.deviation_evaluated',
        'dict:reason', 'assign pairs[pair]',
        '{"compared": False, "reason": "prior_missing"}'
    ): 'prior_missing is deviation pair evidence; never an axis reason',
    (
        'app/crawlers/bank_report.py', 'BankReport.finish',
        'keyword:reason', 'expression',
        'attempt.update(status="unknown", reason="attempt_end_unrecorded")'
    ): 'unknown attempt metadata; excluded by the not_attempted selection guard',
    (
        'app/crawlers/bank_report.py', 'BankReport.finish',
        'result', "assign attempt['execution']",
        '_result("unknown", "attempt_end_unrecorded")'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/bank_report.py', 'BankReport.finish_attempt',
        'keyword:reason', 'expression',
        'attempt.update(status="failed" if error is not None else "succeeded",\n'
        '                       reason="exception" if error is not None else "routine_returned")'
    ): 'failed/succeeded attempt metadata; axis selection does not use this reason',
    (
        'app/crawlers/bank_report.py', 'BankReport.item_missed',
        'dict:reason', 'expression',
        '{"sequence": self._next_sequence(), "pair": None, "item_key": item_key,\n'
        '             "reason": reason, "selector": selector}'
    ): 'miss.reason -> _judge detail only; collection reason is no_value',
    (
        'app/crawlers/bank_report.py', 'BankReport.missed',
        'dict:reason', 'assign entry',
        '{"sequence": self._next_sequence(), "pair": pair, "reason": reason,\n'
        '                 "selector": selector}'
    ): 'miss.reason -> _judge detail only; collection reason is no_value',
    (
        'app/crawlers/bank_report.py', 'BankReport.start_attempt',
        'keyword:reason', 'expression',
        'attempt.update(status="attempted", reason="started")'
    ): 'attempted status is not eligible for attempt.reason -> collection',
    (
        'app/crawlers/bank_report.py', 'BankReport.start_attempt',
        'result', "assign attempt['execution']",
        '_result("running", "attempt_started")'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/bank_report.py', 'PathObserver.adoption',
        'forward:adoption', 'expression',
        'safely_report(self.report, "adoption", self.path, decision, reason)'
    ): 'adoption.reason -> writing detail only; axis reason is withheld_before_writer',
    (
        'app/crawlers/bank_report.py', 'PathObserver.item_missed',
        'forward:item_missed', 'expression',
        'safely_report(self.report, "item_missed", self.path, item_key, reason, **evidence)'
    ): 'miss.reason -> _judge detail only; collection reason is no_value',
    (
        'app/crawlers/bank_report.py', 'PathObserver.missed',
        'forward:missed', 'expression',
        'safely_report(self.report, "missed", self.path, pair, reason, **evidence)'
    ): 'miss.reason -> _judge detail only; collection reason is no_value',
    (
        'app/crawlers/bs.py', '_crawl_and_save_bs',
        'call:adoption', 'expression',
        'mibank.adoption("submitted", "deviation_not_hard_fail")'
    ): 'adoption.reason -> writing detail only; axis reason is withheld_before_writer',
    (
        'app/crawlers/bs.py', '_crawl_and_save_bs',
        'call:adoption', 'expression',
        'mibank.adoption("withheld", "deviation_hard_fail")'
    ): 'adoption.reason -> writing detail only; axis reason is withheld_before_writer',
    (
        'app/crawlers/bs.py', '_crawl_and_save_bs',
        'dict:reason', 'keyword[extra] -> expression',
        '{\n'
        '                    "reason": "is_mibank_rate_reliable failed",\n'
        '                    "action": "DB 마지막 환율 데이터 유지 (클라이언트가 재사용)"\n'
        '                }'
    ): 'logger.warning extra only, not report evidence',
    (
        'app/crawlers/citi.py', '_citi_first_routine_events.on_event',
        'call:item_missed', 'expression',
        'observer.item_missed(order, "selector_miss", selector=facts["selector"])'
    ): 'Citi extraction miss -> PathObserver -> BankReport miss detail only',
    (
        'app/crawlers/citi.py', '_citi_first_routine_events.on_event',
        'call:missed', 'expression',
        'observer.missed(facts["pair"], "parse_error", item_key=order, rate_text=facts["rate_text"])'
    ): 'Citi extraction miss -> PathObserver -> BankReport miss detail only',
    (
        'app/crawlers/citi.py', '_citi_first_routine_events.on_event',
        'call:missed', 'expression',
        'observer.missed(facts["pair"], "selector_miss", item_key=order, selector=facts["selector"])'
    ): 'Citi extraction miss -> PathObserver -> BankReport miss detail only',
    (
        'app/crawlers/citi.py', '_crawl_and_save_citi',
        'call:adoption', 'expression',
        'mibank.adoption("submitted", "deviation_not_hard_fail")'
    ): 'adoption.reason -> writing detail only; axis reason is withheld_before_writer',
    (
        'app/crawlers/citi.py', '_crawl_and_save_citi',
        'call:adoption', 'expression',
        'mibank.adoption("withheld", "deviation_hard_fail")'
    ): 'adoption.reason -> writing detail only; axis reason is withheld_before_writer',
    (
        'app/crawlers/citi.py', '_crawl_and_save_citi',
        'dict:reason', 'keyword[extra] -> expression',
        '{\n'
        '                        "reason": "is_mibank_rate_reliable failed",\n'
        '                        "action": "DB 마지막 환율 데이터 유지 (클라이언트가 재사용)"\n'
        '                    }'
    ): 'logger.warning extra only, not report evidence',
    (
        'app/crawlers/investing_report.py', 'InvestingReport.__init__',
        'result', 'assign self.execution',
        '_result("running", "round_started")'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/investing_report.py', 'InvestingReport.__init__',
        'result', "dict['execution'] -> assign self.attempts",
        '_result("not_attempted", "not_started")'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/investing_report.py', 'InvestingReport.finish',
        "assignment:self.execution['reason']", '',
        'self.execution["reason"] = "attempts_exhausted"'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/investing_report.py', 'InvestingReport.finish',
        "assignment:self.execution['reason']", '',
        'self.execution["reason"] = "cooldown"'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/investing_report.py', 'InvestingReport.finish_attempt',
        'keyword:reason', 'expression',
        'attempt.update(status="failed" if error else "succeeded",\n'
        '                       reason=reason or ("exception" if error else "routine_returned"))'
    ): 'failed/succeeded attempt metadata; axis selection does not use this reason',
    (
        'app/crawlers/investing_report.py', 'InvestingReport.start_attempt',
        'keyword:reason', 'expression',
        'attempt.update(status="attempted", reason="started")'
    ): 'attempted status is not eligible for attempt.reason -> collection',
    (
        'app/crawlers/investing_report.py', 'InvestingReport.start_attempt',
        'result', "assign attempt['execution']",
        '_result("running", "attempt_started")'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/investing_report.py', 'InvestingReport.telemetry_failed',
        'result', "assign attempt['execution']",
        '_result("unknown", "telemetry_error")'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/investing_report.py', '_execution',
        'result', 'return',
        '_result("abnormal", "exception", error_type=type(error).__name__)'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/investing_report.py', '_execution',
        'result', 'return',
        '_result("cancelled", "cancelled", error_type=type(error).__name__)'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/investing_report.py', '_execution',
        'result', 'return',
        '_result("normal", "returned")'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crawlers/investing_report.py', '_execution',
        'result', 'return',
        '_result("timeout", "timeout", error_type=type(error).__name__)'
    ): 'execution only; never selected as collection/writing reason',
    (
        'app/crud.py', '_log_token_purge_rejection',
        'dict:reason', 'keyword[extra] -> expression',
        '{\n'
        '            "event": "device_token_purge",\n'
        '            "outcome": "invalid_input",\n'
        '            "reason": reason,\n'
        '        }'
    ): 'device token rejection log only',
    (
        'app/crud.py', '_run_topic_emission',
        'assignment:reason', '',
        'reason = (\n'
        '            FX_TRIGGER_REASON_INVESTING_CHANGE\n'
        '            if source == "investing"\n'
        '            else FX_TRIGGER_REASON_BANK_CHANGE\n'
        '        )'
    ): 'topic trigger reason, not collection/writing report reason',
    (
        'app/crud.py', '_run_topic_emission',
        'keyword:reason', 'expression',
        'tether_topic_trigger.request_tether_topic_trigger(\n'
        '                    source=source,\n'
        '                    asset=asset,\n'
        '                    reason=TETHER_TRIGGER_REASON_BANK_INVESTING_FX_CHANGE,\n'
        '                )'
    ): 'topic trigger reason, not collection/writing report reason',
}  # REVIEWED_NON_AXIS
INDIRECT = {
    (
        'app/crawlers/bank_report.py', 'BankReport._judge',
        'result', 'return',
        '_result("not_attempted", attempt["reason"])'
    ): 'attempt.reason -> collection only for not_reached/policy_skipped/unnecessary',
    (
        'app/crawlers/bank_report.py', 'BankReport.policy_skipped',
        'keyword:reason', 'expression',
        'self.attempts[path].update(status="policy_skipped", reason=reason)'
    ): 'policy_skipped caller reason -> attempt.reason -> collection.not_attempted',
    (
        'app/crawlers/bank_report.py', 'BankReport.writer_guard',
        "assignment:call['guard_reason']", '',
        'call["guard_reason"] = f"write_mode_{getattr(mode, \'value\', mode)}"'
    ): 'blocked modes from CRUD/control -> exact write_mode f-string -> call.guard_reason',
    (
        'app/crawlers/bank_report.py', 'PathObserver.writer_guard',
        'forward:writer_guard', 'expression',
        'safely_report(self.report, "writer_guard", self.path, decision, mode)'
    ): 'PathObserver decision/mode -> BankReport.writer_guard with path inserted',
    (
        'app/crawlers/bank_report.py', '_call_pair_result',
        'result', 'return',
        '_result("policy_blocked", call["guard_reason"])'
    ): 'writer_guard blocked mode -> call.guard_reason -> writing.policy_blocked',
    (
        'app/crawlers/bank_report.py', '_result',
        'dict:reason', 'return',
        '{"status": status, "reason": reason, **evidence}'
    ): '_result.reason -> collection/writing reason; every call is checked',
    (
        'app/crawlers/investing_report.py', 'InvestingReport.finish_attempt',
        'result', "assign attempt['collection'][pair]",
        '_result("missing", reason)'
    ): 'finish_attempt.reason -> collection only under reason == http_403',
    (
        'app/crawlers/investing_report.py', 'InvestingReport.observation',
        'result', "assign self.attempts[attempt_id]['collection'][pair]",
        '_result(\n'
        '            "missing" if reason else "valid", reason or "validated", **evidence\n'
        '        )'
    ): 'observation.reason/default/local assignments -> collection; or validated',
    (
        'app/crawlers/investing_report.py', '_result',
        'dict:reason', 'return',
        '{"status": status, "reason": reason, **evidence}'
    ): '_result.reason -> collection/writing reason; every call is checked',
}  # REVIEWED_INDIRECT
# Explicit reason routes only: no crawler/CRUD body or whole report class.
# NON_AXIS writers remain sealed where their evidence could be promoted to an
# axis by a consumer. Presentation/extraction helpers such as _snippet do not.
ROUTING_SCOPES = {
    INV: (
        "_result", "safely_report", "InvestingReport.__init__",
        "InvestingReport.telemetry_failed", "InvestingReport.start_attempt",
        "InvestingReport.observation", "InvestingReport.finish_attempt",
        "InvestingReport.cooldown", "InvestingReport._collection",
        "InvestingReport._writing", "InvestingReport.emit",
    ),
    BANK: (
        "_result", "_call_pair_result", "_summarize_writing",
        "PathObserver.missed", "PathObserver.item_missed",
        "PathObserver.adoption", "PathObserver.writer_guard",
        "BankReport.__init__", "BankReport.telemetry_failed",
        "BankReport.start_attempt", "BankReport.finish_attempt",
        "BankReport.policy_skipped", "BankReport.missed", "BankReport.item_missed",
        "BankReport.deviation_evaluated", "BankReport.adoption",
        "BankReport.writer_started", "BankReport._active_call",
        "BankReport.writer_guard", "BankReport._judge",
        "BankReport._attempt_payloads", "BankReport._summary",
        "BankReport.finish", "BankReport.emit",
    ),
    CONTROL: ("WriterMode", "compute_effective_mode"),
}
ROUTING_FINGERPRINTS = {
    ('app/atomic_write_control.py', '<reason bindings>'):
        'd940c65382f1946d0c647c9b9aa0ba4b1dfbdd0a5a6bff78d0ad217138b4ec4c',
    ('app/atomic_write_control.py', 'WriterMode'):
        '8a14393e9d210cb6a33ed13b50a4bd0209c2ebaa83bd473b4a6ebde731ad3b85',
    ('app/atomic_write_control.py', 'compute_effective_mode'):
        '872ae8f2667d0b8cd291e2cca9eebb9e0454dcdc2e71e7f7dcaa3ac929520798',
    ('app/crawlers/bank_report.py', '<reason bindings>'):
        'fac8c799c6adc5ca7862fd094f36ebe6596f0fc2da11563273c97217de9dcbed',
    ('app/crawlers/bank_report.py', 'BankReport.__init__'):
        '7602b86cd74080aac319f80bca80382db1aa93741ab138c0e8735cf1f4bf27d2',
    ('app/crawlers/bank_report.py', 'BankReport._active_call'):
        '753d23dd8b9063840016f6d24e5593c8accda88ad50c7ae850e763bbd4ead9f0',
    ('app/crawlers/bank_report.py', 'BankReport._attempt_payloads'):
        '0086e3c68dbb0df6ebc88a060ee6135facd0d3c82e15d5a693f0d087cc2ae9a9',
    ('app/crawlers/bank_report.py', 'BankReport._judge'):
        'e7febe098d3485c00905a28880bbdd33efb8b2b6198bf0703a65916c0a5ce849',
    ('app/crawlers/bank_report.py', 'BankReport._summary'):
        '66d14d441fefb6a03763ff2ca6f364acb875efecfde567863789720c4c8234d2',
    ('app/crawlers/bank_report.py', 'BankReport.adoption'):
        '25d10d23aa52631d03e4f52adc3ffd4c35575743ebb46a410cc30bd29043373f',
    ('app/crawlers/bank_report.py', 'BankReport.deviation_evaluated'):
        'fa694cb64c4f6e515f89dccc0a182e7103571289e83305658a27f89256ead121',
    ('app/crawlers/bank_report.py', 'BankReport.emit'):
        '149047cfbdc3122e639e14303afbd99df412373863e13e6e9d107a3a5fa2212f',
    ('app/crawlers/bank_report.py', 'BankReport.finish'):
        'a1af4f1c8d811851edf7a271deff9a04f6b9ac6c2a12dacb4c07196bbcac92c2',
    ('app/crawlers/bank_report.py', 'BankReport.finish_attempt'):
        '3db9898e94bc4ad3475027b1ff31ac5cecc63505e9cf2126175939c54b31402d',
    ('app/crawlers/bank_report.py', 'BankReport.item_missed'):
        '5db2bddcd8364934439d885ff62f11ea726c78777d3d32b2b0cd2b39000e1937',
    ('app/crawlers/bank_report.py', 'BankReport.missed'):
        '5d31b6d87c63a7c8bc53a3c288382301d157facd83e9fdfda1eadab89b4dac41',
    ('app/crawlers/bank_report.py', 'BankReport.policy_skipped'):
        'd685ff38749daf04ccb0e67225ff97bac32e7ffb43261cdf6b2522d36d24afbe',
    ('app/crawlers/bank_report.py', 'BankReport.start_attempt'):
        'f79192e1b88d330febd6c9c1cf94f2afeb450890ee8520989fe98aeb7bbbd2c2',
    ('app/crawlers/bank_report.py', 'BankReport.telemetry_failed'):
        '5fa652760a5affe4159a48565c20525add098492e5447359c3f2baf5f30e3907',
    ('app/crawlers/bank_report.py', 'BankReport.writer_guard'):
        'b1aa3b00475d5814983d51298d61a0cae4bcbf2dfddd190a50b3c9bff31cf687',
    ('app/crawlers/bank_report.py', 'BankReport.writer_started'):
        '5e4c631b537fbdce38b0d40eb74c7634976a7bd0cfbd0352a13f5f40d9fc70b9',
    ('app/crawlers/bank_report.py', 'PathObserver.adoption'):
        '3ce285af3ce75440945a43e77c0b637fbb069a1405ed67c4d9526a9add3ec766',
    ('app/crawlers/bank_report.py', 'PathObserver.item_missed'):
        'b730caa64774697f5d8ba9012d1b11f4aafc10287e66bbc0b863eaa2c02b762b',
    ('app/crawlers/bank_report.py', 'PathObserver.missed'):
        '3909ecc1797d8c72ac195b9dcfe236043e5d0e73e82f34c6121b677168414b6b',
    ('app/crawlers/bank_report.py', 'PathObserver.writer_guard'):
        '948760f9e32e6f25dd729233c3d12307b095137a43444b147c5d5962a2dd2c0c',
    ('app/crawlers/bank_report.py', '_call_pair_result'):
        '254585a6949044b531cf0f90bba48d42ff1e8662ccf7544bcaef243217bd4c18',
    ('app/crawlers/bank_report.py', '_result'):
        '79f234b9b704c6ce2c04156c5ab2020449b2d9f0ac3d317c48efbe57b2c81de3',
    ('app/crawlers/bank_report.py', '_summarize_writing'):
        'aee52ff0541bd7f58377f4a3c979fc01ad3b982027f1d706798fb8815676866d',
    ('app/crawlers/investing_report.py', '<reason bindings>'):
        'f9fea4f576a2427ff2e75e015f07c48775ab0de6baffa38816fbba4660ee20f1',
    ('app/crawlers/investing_report.py', 'InvestingReport.__init__'):
        '0a6572672fc1c42506008d5d0eddd2ba966e54af655fef91c7b903f49cde7927',
    ('app/crawlers/investing_report.py', 'InvestingReport._collection'):
        'a887a4f7ba16956d4ecb62db77d5b286fa74e7d7f87c490bd14b12a7590f1ce5',
    ('app/crawlers/investing_report.py', 'InvestingReport._writing'):
        '905cc4118c3089b8589dce6bf045a92ea70856d308f8ef4323fde06cc201b984',
    ('app/crawlers/investing_report.py', 'InvestingReport.cooldown'):
        '9b7469bf9f1d3079287eaa8d1f6ab8d93b54563a1345fb02821d14889d6b5298',
    ('app/crawlers/investing_report.py', 'InvestingReport.emit'):
        '4460fcafa6fe639f57278ed8b17803a29f41d4c0d93efa2f5a0b56d626befedc',
    ('app/crawlers/investing_report.py', 'InvestingReport.finish_attempt'):
        '64cd7e12fba453287261d0ef7c8e30b88203e869d731224eda1b80e7e70baf61',
    ('app/crawlers/investing_report.py', 'InvestingReport.observation'):
        '7a384145cd3588000e10793d334e104f0cee819f93c9db95f2e652b985f610c3',
    ('app/crawlers/investing_report.py', 'InvestingReport.start_attempt'):
        '8291846b14bc36e1b07f6882bce5bd32f0c8097e3c8d07df4ff69a663157b575',
    ('app/crawlers/investing_report.py', 'InvestingReport.telemetry_failed'):
        'fdc9ebdf501b5c7e93ede516644467fc137edb8dc6bab5abe55fecc71d0dc338',
    ('app/crawlers/investing_report.py', '_result'):
        '79f234b9b704c6ce2c04156c5ab2020449b2d9f0ac3d317c48efbe57b2c81de3',
    ('app/crawlers/investing_report.py', 'safely_report'):
        '5ab227638ecd63a18d0b1dd8b89098a384ccc13598c56e91e80e09a19c245026',
    ('app/crud.py', '<reason bindings>'):
        '3ef264f9181952af3450dbc40062e319a0b133e13c69ae015ad806bda7e265ca',
}  # REVIEWED_ROUTING


def _mode_at_guard(source, call, domain, modes):
    """Finite-value flow for the local `enforced` supplier, not a body seal.

    Unknown unrelated conditions explore both branches. Early returns remove
    their paths; comparisons filter values. Unsupported writes/control flow
    involving the supplier fail rather than assuming the old guard topology.
    """
    found = set()

    def value(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Attribute) and _name(node.value) == "WriterMode":
            if node.attr in modes:
                return {modes[node.attr]}
        if _name(node) == "atomic_write_runtime.snapshot().enforced_action":
            return set(domain)
        source.fail(node, "unresolved enforced mode supplier")

    def split(test, current):
        if (isinstance(test, ast.Compare) and len(test.ops) == 1
                and isinstance(test.left, ast.Name) and test.left.id == "enforced"
                and isinstance(test.ops[0], (ast.Eq, ast.NotEq))):
            rhs = value(test.comparators[0])
            equal, unequal = current & rhs, current - rhs
            return (equal, unequal) if isinstance(test.ops[0], ast.Eq) else (unequal, equal)
        if any(isinstance(n, ast.Name) and n.id == "enforced" for n in ast.walk(test)):
            source.fail(test, "unresolved enforced mode condition")
        return set(current), set(current)

    def flow(statements, current):
        for statement in statements:
            if not current:
                break
            if isinstance(statement, ast.If):
                yes, no = split(statement.test, current)
                current = flow(statement.body, yes) | flow(statement.orelse, no)
            elif isinstance(statement, (ast.Return, ast.Raise)):
                return set()
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                writes = [n for target in targets for n in ast.walk(target)
                          if isinstance(n, ast.Name) and n.id == "enforced"]
                if writes:
                    if len(targets) != 1 or not isinstance(targets[0], ast.Name) or statement.value is None:
                        source.fail(statement, "unresolved enforced mode assignment")
                    current = value(statement.value)
            elif isinstance(statement, ast.Expr) and statement.value is call:
                found.update(current)
                return set()
            else:
                for node in ast.walk(statement):
                    if node is call or (isinstance(node, ast.Name) and node.id == "enforced"
                                        and isinstance(node.ctx, (ast.Store, ast.Del))):
                        source.fail(statement, "unresolved enforced mode control flow")
        return current

    flow(source.functions["insert_bank_rates_into_db"].body, set(domain))
    if not found:
        source.fail(call, "unresolved/unreachable blocked guard supplier")
    return found


def _guard_values(supply, sources):
    source, node = supply.source, supply.node
    bound = _bind(source, node, ("decision", "mode"))
    decision, mode = bound["decision"], bound["mode"]
    if not isinstance(decision, ast.Constant):
        source.fail(node, "unresolved guard decision branch")
    if decision.value in ("legacy", "atomic"):
        if not isinstance(mode, ast.Constant) or mode.value is not None:
            source.fail(node, "unreviewed nonblocking guard mode")
        return set()
    if decision.value != "blocked":
        source.fail(node, "new guard decision branch")
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return {"write_mode_" + mode.value}
    if source.filename == CRUD and source.scopes[node] == "insert_bank_rates_into_db" and _name(mode) == "enforced":
        # Read the control domain, then follow the actual CRUD conditions and
        # returns. Inverting a guard must not retain an assumed HALT-only domain.
        control = sources[CONTROL]
        mode_class = control.functions["WriterMode"]
        domain = {}
        for statement in mode_class.body:
            if isinstance(statement, ast.Assign):
                if (len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name)
                        or not isinstance(statement.value, ast.Constant)
                        or not isinstance(statement.value.value, str)):
                    control.fail(statement, "unresolved WriterMode supplier")
                domain[statement.targets[0].id] = statement.value.value
        returned = set()
        for statement in ast.walk(control.functions["compute_effective_mode"]):
            if isinstance(statement, ast.Return):
                branches = [statement.value]
                while branches:
                    value = branches.pop()
                    if isinstance(value, ast.IfExp):
                        branches.extend((value.body, value.orelse))
                    elif isinstance(value, ast.Attribute) and _name(value.value) == "WriterMode" and value.attr in domain:
                        returned.add(domain[value.attr])
                    else:
                        control.fail(statement, "unresolved effective-mode return supplier")
        blocked = _mode_at_guard(source, node, returned, domain)
        return {"write_mode_" + value for value in blocked}
    source.fail(node, "unresolved dynamic guard supplier")


def _literal_values(supply, node, checked):
    source = supply.source
    if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, str)):
        checked.add(node)
        return set() if node.value is None else {node.value}
    if isinstance(node, ast.IfExp):
        return (_literal_values(supply, node.body, checked)
                | _literal_values(supply, node.orelse, checked))
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        result = set()
        for value in node.values:
            result |= _literal_values(supply, value, checked)
        return result
    source.fail(node, "unresolved reason supplier (name/call/subscript/dynamic f-string)")


class _Shape(ast.NodeTransformer):
    def __init__(self, checked):
        self.checked = checked

    def visit_Constant(self, node):
        if node in self.checked:
            return ast.copy_location(ast.Constant(value="<checked-axis-reason>"), node)
        return node

    def visit_Expr(self, node):
        # Python treats a leading string as a docstring; other bare strings
        # also cannot supply a reason. No executable statements are discarded.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return None
        return self.generic_visit(node)


def _module_bindings(source, names):
    """Project only bindings used by the sealed routes (including dependencies).

    Split import aliases so adding an unrelated name to an existing import is
    harmless. Compound statements binding a relevant name stay intact: their
    conditions can select a different supplier. Function bodies are deliberately
    excluded; their selected reason routes are fingerprinted separately.
    """
    declarations = []
    for statement in source.tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                part = copy.copy(statement)
                part.names = [alias]
                bound = alias.asname or alias.name.split(".")[0]
                declarations.append(({bound}, part))
        else:
            bound = {n.id for n in ast.walk(statement)
                     if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            bound |= {a.asname or a.name.split(".")[0]
                      for n in ast.walk(statement)
                      if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
            declarations.append((bound, statement))
    wanted, selected = set(names), set()
    while True:
        added = {i for i, (bound, _) in enumerate(declarations) if bound & wanted} - selected
        if not added:
            break
        selected |= added
        for i in added:
            wanted |= {n.id for n in ast.walk(declarations[i][1])
                       if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return ast.Module(body=[node for i, (_, node) in enumerate(declarations) if i in selected],
                      type_ignores=[])


def _global_reads(source, selected):
    """Local variables with the same spelling must not seal module bindings."""
    root = symtable.symtable(source.text, source.filename, "exec")
    names = set()
    for scope, node in selected.items():
        table = root
        for part in scope.split("."):
            table = next(child for child in table.get_children() if child.get_name() == part)
        pending = [table]
        while pending:
            current = pending.pop()
            names |= {symbol.get_name() for symbol in current.get_symbols()
                      if symbol.is_global() and symbol.is_referenced()}
            pending.extend(current.get_children())
        # Defaults/decorators/annotations are evaluated outside the body.
        for field, value in ast.iter_fields(node):
            if field != "body":
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, ast.AST):
                        names |= {n.id for n in ast.walk(item)
                                  if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return names


def _routing_shapes(source, checked):
    selected = {}
    for scope in ROUTING_SCOPES.get(source.filename, ()):
        if scope not in source.functions:
            source.fail(source.tree, f"missing reason routing scope: {scope}")
        selected[scope] = source.functions[scope]
    names = _global_reads(source, selected) if selected else set()
    if source.filename == CRUD:
        # These two bindings supply the mode analysis; other CRUD imports and
        # constants, and all CRUD function bodies, are outside the seal.
        names |= {"WriterMode", "atomic_write_runtime"}
    if names:
        selected["<reason bindings>"] = _module_bindings(source, names)
    result = {}
    for name, node in selected.items():
        memo = {}
        cloned = copy.deepcopy(node, memo)
        cloned_checked = {memo[id(n)] for n in checked if id(n) in memo}
        shape = _Shape(cloned_checked).visit(cloned)
        result[(source.filename, name)] = hashlib.sha256(
            ast.dump(shape, include_attributes=False).encode("utf-8")
        ).hexdigest()
    return result


def _analyze(texts):
    if set(texts) != set(FILES):
        raise ReasonDrift("missing/new source files in the D7 reason analysis boundary")
    sources = {path: Source(path, text) for path, text in texts.items()}
    checked = {path: set() for path in FILES}
    values, seen_exemptions, seen_edges = set(), set(), set()
    for source in sources.values():
        for supply in _supplies(source):
            if supply.key in NON_AXIS:
                seen_exemptions.add(supply.key)
                continue
            if supply.key in INDIRECT:
                # Suppliers are enumerated separately and the routing seal
                # prevents changing the source or destination of this edge.
                seen_edges.add(supply.key)
                if (source.filename == INV
                        and source.scopes[supply.node] == "InvestingReport.observation"):
                    # The caller/default/local assignments supply `reason`;
                    # this particular forwarding edge also adds `validated`.
                    for leaf in ast.walk(supply.value):
                        if isinstance(leaf, ast.Constant):
                            found = _literal_values(supply, leaf, checked[source.filename])
                            if found - REASON_ENUM:
                                source.fail(leaf, "indirect literal outside REASON_ENUM")
                            values |= found
                continue
            if supply.kind == "guard":
                found = _guard_values(supply, sources)
            else:
                found = _literal_values(supply, supply.value, checked[source.filename])
            outside = found - REASON_ENUM
            if outside:
                source.fail(supply.node, f"reason outside REASON_ENUM: {sorted(outside)!r}; "
                            f"destination={supply.kind} / {source.route(supply.node)}")
            values |= found
    stale = (set(NON_AXIS) - seen_exemptions) | (set(INDIRECT) - seen_edges)
    if stale:
        raise ReasonDrift(f"removed/changed reviewed source-to-destination expression: {sorted(stale)!r}")
    shapes = {}
    for source in sources.values():
        shapes.update(_routing_shapes(source, checked[source.filename]))
    for key in sorted(set(shapes) | set(ROUTING_FINGERPRINTS)):
        if shapes.get(key) != ROUTING_FINGERPRINTS.get(key):
            path, scope = key
            source = sources[path]
            node = source.functions.get(scope, source.tree)
            source.fail(node, "unreviewed reason routing AST: new/changed sink, supplier, "
                        "guard branch, default, alias, or keyword expansion; review the "
                        "source-to-axis path before updating its fingerprint")
    return frozenset(values), shapes


def assert_reason_contract(texts):
    """Same decision function for real sources and every in-memory mutation."""
    return _analyze(texts)[0]


@pytest.fixture(scope="module")
def producer_sources():
    return {path: (ROOT / path).read_text(encoding="utf-8") for path in FILES}


def test_current_producers_satisfy_L6_2_and_L9_39(producer_sources):
    reached = assert_reason_contract(producer_sources)
    assert reached <= REASON_ENUM
    # Non-vacuity: both indirect collection routes and both dynamic guard
    # results must actually be discovered from the source AST.
    assert {"selector_missing", "mibank_untrusted_window", "write_mode_halt",
            "write_mode_uninitialized", "validated", "attempt_failed",
            "attempt_interrupted", "previous_attempt_succeeded"} <= reached
    assert not reached & {"prior_missing", "selector_miss", "deviation_hard_fail",
                          "round_started", "not_checked", "write_mode_atomic", "write_mode_legacy"}


def _replace(texts, path, before, after):
    assert texts[path].count(before) == 1, f"mutation anchor drift: {path}: {before!r}"
    changed = dict(texts)
    changed[path] = changed[path].replace(before, after, 1)
    ast.parse(changed[path], filename=path)  # SyntaxError is not a killed mutant.
    return changed


MUTATIONS = [
    # The six escaped mutations from ledger_tests_r1.verdict.txt, item 3.
    ("new-blocked-guard-call", CRUD,
     'observer.writer_guard("blocked", "uninitialized")',
     'observer.writer_guard("blocked", "uninitialized")\n            observer.writer_guard("blocked", "new_mode")',
     "write_mode_new_mode"),
    ("guard-reason-subscript-write", BANK,
     'call["guard_decision"] = decision',
     'call["guard_decision"] = decision\n        call["guard_reason"] = "new_guard_reason"',
     "new_guard_reason"),
    ("attempt-reason-subscript-write", BANK,
     'attempt.update(status="attempted", reason="started")',
     'attempt.update(status="attempted", reason="started")\n        attempt["reason"] = "new_attempt_reason"',
     "new_attempt_reason"),
    ("result-keyword-call", BANK,
     '_result("unknown", "not_instrumented")',
     '_result(status="unknown", reason="brand_new_reason")',
     "brand_new_reason"),
    ("non-axis-string-in-axis", BANK,
     '_result("unknown", "not_instrumented")',
     '_result("unknown", "prior_missing")',
     "prior_missing"),
    ("observation-reason-default", INV,
     'def observation(self, attempt_id, pair, *, reason=None, text=None, rate=None):',
     'def observation(self, attempt_id, pair, *, reason="new_default", text=None, rate=None):',
     "new_default"),
    ("new-dynamic-f-string", BANK,
     'f"write_mode_{getattr(mode, \'value\', mode)}"',
     'f"write_mode_{mode}_{decision}"', "unresolved"),
    ("unknown-result-supplier", BANK,
     '_result("unknown", "not_instrumented")',
     '_result("unknown", compute_new_reason())', "unresolved"),
    ("keyword-expansion", BANK,
     '_result("unknown", "not_instrumented")',
     '_result("unknown", **new_reason_payload)', "keyword expansion"),
    ("guard-branch-widens", CRUD,
     'if enforced != WriterMode.LEGACY:\n        _record_write_mode_skip(bank_name, enforced)',
     'if enforced == WriterMode.LEGACY:\n        _record_write_mode_skip(bank_name, enforced)',
     "write_mode_legacy"),
    ("effective-mode-return", CONTROL,
     'return WriterMode.HALT if row.activation_epoch > 0 else WriterMode.LEGACY',
     'return "new_mode" if row.activation_epoch > 0 else WriterMode.LEGACY',
     "effective-mode return"),
    ("unknown-dictionary-sink", BANK,
     'return _result("unknown", "unobserved")',
     'return {"status": "unknown", "reason": "new_dict_reason"}', "new_dict_reason"),
    ("unknown-reason-key", BANK,
     'return _result("unknown", "unobserved")',
     'return {"status": "unknown", dynamic_key: "hidden_reason"}', "unreviewed reason routing AST"),
    ("unknown-helper-sink", BANK,
     'return _result("unknown", "unobserved")',
     'return new_result("unknown", "hidden_reason")', "unreviewed reason routing AST"),
    ("changed-selection-guard", BANK,
     'if attempt["status"] in ("not_reached", "policy_skipped", "unnecessary"):',
     'if attempt["status"] in ("not_reached", "policy_skipped", "unnecessary", "attempted"):',
     "unreviewed reason routing AST"),
    ("detail-rerouted-into-reason", BANK,
     '_result("missing", "no_value", detail=misses[-1]["reason"],',
     '_result("missing", misses[-1]["reason"], detail="no_value",', "unresolved"),
    ("new-local-reason-supplier", INV,
     'reason = "nan_value"', 'reason = dynamic_reason()', "unresolved"),
    ("new-callsite-keyword", INV_CALL,
     'pair, reason="selector_missing")', 'pair, reason="new_selector_reason")',
     "new_selector_reason"),
    ("policy-skipped-positional", BS,
     '"mibank_untrusted_window")', '"new_skip_reason")', "new_skip_reason"),
    ("default-positional-reason", INV,
     'def finish_attempt(self, attempt_id, error=None, reason=None):',
     'def finish_attempt(self, attempt_id, error=None, reason="new_finish_default"):',
     "new_finish_default"),
    ("new-guard-keyword-call", CRUD,
     'observer.writer_guard("blocked", "uninitialized")',
     'observer.writer_guard(decision="blocked", mode="new_mode")', "write_mode_new_mode"),
    ("reason-update-keyword", BANK,
     'attempt.update(status="unnecessary", reason="previous_attempt_succeeded")',
     'attempt.update(status="unnecessary", reason="new_previous_reason")',
     "new_previous_reason"),
    ("investing-compact-reason", INV,
     'self.attempts[2].update(status="unnecessary", reason="previous_attempt_succeeded")',
     'self.attempts[2].update(status="unnecessary", reason="new_compact_reason")',
     "new_compact_reason"),
    ("exempt-evidence-rerouted", BANK,
     'self.attempts[path]["deviation"] = {',
     'self.attempts[path]["collection"] = pairs\n        self.attempts[path]["deviation"] = {',
     "unreviewed reason routing AST"),
    ("new-same-scope-unknown-sink", INV,
     'writing[pair] = _result("unknown", "telemetry_error", attempt_ids=[])',
     'writing[pair] = dict(status="unknown", reason=unresolved_reason)', "unresolved"),
]


@pytest.mark.parametrize("label,path,before,after,diagnostic", MUTATIONS,
                         ids=[case[0] for case in MUTATIONS])
def test_mutation_sensitivity(producer_sources, label, path, before, after, diagnostic):
    mutated = _replace(producer_sources, path, before, after)
    with pytest.raises(ReasonDrift) as caught:
        assert_reason_contract(mutated)
    assert path in str(caught.value)
    assert diagnostic in str(caught.value)


def test_reviewed_axis_literal_can_change_to_another_enum_member(producer_sources):
    # Proves the gate analyzes reasons, rather than just rejecting any source
    # checksum change. Route topology stays the same; the new value is legal.
    changed = _replace(producer_sources, BANK,
                       '_result("unknown", "not_instrumented")',
                       '_result("unknown", "evidence_incomplete")')
    assert "evidence_incomplete" in assert_reason_contract(changed)


def test_comments_do_not_change_the_ast_contract(producer_sources):
    changed = {path: "# AST-only formatting control\n" + text
               for path, text in producer_sources.items()}
    assert assert_reason_contract(changed) == assert_reason_contract(producer_sources)


@pytest.mark.parametrize("path", FILES)
def test_unrelated_module_bindings_do_not_require_resealing(producer_sources, path):
    changed = dict(producer_sources)
    changed[path] += ("\nimport decimal as unrelated_decimal\nUNRELATED_RETRY_LIMIT = 7\n"
                      "reason = None\npair = None\n")
    assert assert_reason_contract(changed) == assert_reason_contract(producer_sources)


@pytest.mark.parametrize("path,scope", [
    (BANK, "_snippet"), (INV, "start_report"),
    (BS, "_crawl_and_save_bs"), (CITI, "_crawl_and_save_citi"),
    (INV_CALL, "crawl_and_save_investing_exchange_rates"),
    (CRUD, "insert_bank_rates_into_db"), (CRUD, "_insert_bank_rates_atomic"),
    (CRUD, "_stage_bank_rate_changes"),
])
def test_unrelated_body_edits_do_not_require_resealing(producer_sources, path, scope):
    source = Source(path, producer_sources[path])
    function = source.functions[scope]
    # Insert an executable local calculation, not merely a comment/docstring.
    statement = function.body[0]
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
        statement = function.body[1]
    lines = source.text.splitlines(keepends=True)
    lines.insert(statement.lineno - 1, " " * statement.col_offset + "unrelated_retry_delay = 2 * 3\n")
    changed = dict(producer_sources)
    changed[path] = "".join(lines)
    assert assert_reason_contract(changed) == assert_reason_contract(producer_sources)


@pytest.mark.parametrize("label,path,before,after,diagnostic", [
    ("atomic-early-return-lost", CRUD,
     "return _insert_bank_rates_atomic(db, current_rates, bank_name, observer=observer)",
     "_insert_bank_rates_atomic(db, current_rates, bank_name, observer=observer)",
     "write_mode_atomic"),
    ("guard-domain-reassigned", CRUD,
     'observer.writer_guard("blocked", enforced)',
     'enforced = "new_mode"\n            observer.writer_guard("blocked", enforced)',
     "write_mode_new_mode"),
    ("guard-domain-unknown", CRUD,
     "enforced = atomic_write_runtime.snapshot().enforced_action\n"
     "    if enforced == WriterMode.ATOMIC:\n        if observer is not None:",
     "enforced = new_mode_supplier()\n"
     "    if enforced == WriterMode.ATOMIC:\n        if observer is not None:",
     "unresolved enforced mode supplier"),
    ("guard-no-filter", CRUD,
     'if enforced != WriterMode.LEGACY:\n        _record_write_mode_skip(bank_name, enforced)',
     'if observer is not None:\n        _record_write_mode_skip(bank_name, enforced)',
     "write_mode_legacy"),
    ("relevant-import-redirected", BANK,
     "from app.crawlers.investing_report import safely_report",
     "from app.crawlers.other_report import safely_report", "unreviewed reason routing AST"),
    ("new-reason-in-unsealed-crawler", CITI,
     'mibank.adoption("submitted", "deviation_not_hard_fail")',
     'mibank.adoption("submitted", "deviation_not_hard_fail")\n'
     '                        report.policy_skipped("mibank", "new_skip_reason")', "new_skip_reason"),
])
def test_narrowed_boundary_sensitivity(producer_sources, label, path, before, after, diagnostic):
    changed = _replace(producer_sources, path, before, after)
    with pytest.raises(ReasonDrift) as caught:
        assert_reason_contract(changed)
    assert path in str(caught.value)
    assert diagnostic in str(caught.value)


def test_analysis_does_not_import_application_modules(producer_sources, monkeypatch):
    original_import = builtins.__import__

    def refuse_application_import(name, *args, **kwargs):
        assert name != "app" and not name.startswith("app."), name
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_application_import)
    assert assert_reason_contract(producer_sources) <= REASON_ENUM
