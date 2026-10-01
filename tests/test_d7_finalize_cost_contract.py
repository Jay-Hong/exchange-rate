"""Finalize output and JSONL decoding cost on a real streamed churn fixture."""

import base64
import copy
import gzip
import importlib.util
import json
import zlib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "d7_ledger_measure_gate.py"
spec = importlib.util.spec_from_file_location("d7_finalize_cost_gate", GATE_PATH)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

# Sorted, compact JSON bytes from HEAD 5d93dc0 finalize_acceptance on the
# streamed 41-minute churn fixture below. The compressed literal keeps this
# independent of Git history and compares the complete result, including reasons.
BASELINE_JSON = zlib.decompress(base64.b64decode(
    "eNq9l1tP2zAUx79K5GeEml6g9I1ykZCmaRraXqbJcu0TOCKxM9vpqCq++05aoeJQoMTJnpraOb+/j3Mu9poJKaH0Qkvg8h7kQ47Os9mvNYMlKqiHLWSORn4fMQvCGc1mrEDnUN8lORboHaMZ85ejopnzQUp/nRe+IiP24+vPq+831zdXl+zp6DBmacG5ygLP8NHTbwM/7Bc/6hc/7hc/6Rd/0i/+NBLvwXnupMjh2JnKyiZ/2h0fHilNaIx7K7RDj0Y3xM563at00J0vBc2RgKK/oIVF05BKu5PyUJTGCrt6S2vYnZY2thA5p+c8EJlHVyh5X1nNtSigAe5w9YvK0rMCL7C5/FF3KkqsGvCWBUpXeR5wgkr07fz29tOEk2jCaTRhGk04iyWEmd6KkEYThp8l7As2C38qrOvMolJ34HlpUIeHh3k66ib80nG0y9EBnEYHcBodwOm09YfbW6Dn6Vl3xScTVe75A2oVagzb9jZYgvZJfYBNnuWMTVBLU5Q5eAhVoruAKIVEv+KlNSYL2b0eUufDbrLkYpC2jo7Q+ePNzeF1Pl9Ed8RDdbrakXHvOzL5TzsSe1a3sKgwV3yTVA32aY/saW9peRF9JqeyhQsr6tN+AE7bV6ztXEIsVxery8HkjYJ1GWbr9fnNlw/xGYV3YiudKFSJNj7ZD26bpAfiR/3iW1+pt9eeou4ZdJ2r8QrqxlEIL+9DjcnrrSeUWYKl28Xz0BGjkFuio+io7x10I6ROROorNlszhVm9gvXOaM0ypBf3Wm8r0xOJ5ihhIoa8FNajCMxe+Pqu8aiN0fgTRmS12TNqU7NM5A5ogLxUKD3fbvfG89e+vPuJd6s/5LXxu6/tFvTWSgIXX0jvGx83Au3pH0NackU="
))


@pytest.fixture(scope="module")
def churn_input(tmp_path_factory):
    root = tmp_path_factory.mktemp("d7-finalize-churn")
    fixture = gate.run_churn_fixture(
        "churn_most_finished_short_ascii", limit=128, churn_minutes=41,
        churn_stride_minutes=60, fixture_detail_divisor=256, attachment_dir=str(root),
    )
    attachments = {str(path.relative_to(root)): path.read_bytes()
                   for path in root.rglob("*") if path.is_file()}
    report = {"mode": "predicate_unit", "churn_fixtures": [fixture],
              "test_scale": {"hours": 41, "pre_prune_hours": 3}}
    stdout = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    return fixture, stdout, attachments


def _finalize(churn_input):
    _, stdout, attachments = churn_input
    return gate.finalize_acceptance(
        stdout_bytes=stdout,
        stderr_bytes=b"start record_baseline\nend record_baseline PASS\n",
        exit_code=0, attachments=attachments,
    )


def test_streamed_churn_final_result_matches_head_5d93dc0(churn_input):
    result = _finalize(churn_input)
    rows = {row["row_id"]: row["status"] for row in result["acceptance_checklist"]}
    assert [rows[row] for row in ("B06", "B14", "B15")] == ["PASS"] * 3
    assert json.dumps(result, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode() == BASELINE_JSON


def test_streamed_churn_trace_deserialization_is_bounded_by_rows(churn_input, monkeypatch):
    fixture, _, _ = churn_input
    trace_ref = fixture["fixture_provenance"]["api_trace_ref"]
    row_count = trace_ref["rows"]
    assert row_count > 1000 and len(fixture["checkpoints"]) > 100
    trace_spools = set()
    original_init = gate._JsonlView.__init__
    original_load = gate.pickle.load
    calls = 0

    def record_trace_spool(view, ref, source):
        original_init(view, ref, source)
        if ref.get("path") == trace_ref["path"]:
            trace_spools.add(view.plain.name)

    def counted_load(stream):
        nonlocal calls
        if stream.name in trace_spools:
            calls += 1
        return original_load(stream)

    monkeypatch.setattr(gate._JsonlView, "__init__", record_trace_spool)
    monkeypatch.setattr(gate.pickle, "load", counted_load)
    result = _finalize(churn_input)
    rows = {row["row_id"]: row["status"] for row in result["acceptance_checklist"]}
    assert all(rows[row] == "PASS" for row in
               ("B06", "B08", "B09", "B10", "B12", "B14", "B15", "B16", "B17", "B18"))
    assert trace_spools
    # Full finalize traverses the same trace for several independent B rows;
    # 24 permits those fixed passes but never a pass per 178 checkpoints.
    assert calls <= row_count * 24


def _variant_input(churn_input, variant):
    fixture, _, attachments = churn_input
    fixture = copy.deepcopy(fixture)
    ref = fixture["fixture_provenance"]["api_trace_ref"]
    trace = [json.loads(line) for line in gzip.decompress(attachments[ref["path"]]).splitlines()]
    fixture["fixture_provenance"]["api_trace"] = trace
    if variant == "b08_target_after_cut":
        target = next(i for i, event in enumerate(trace)
                      if event.get("id") == "c00000000" and event.get("action") == "finish")
        event = trace.pop(target)
        event["event_order"] = trace[-1]["event_order"] + 2
        trace.append(event)
    elif variant == "b09_target_after_cut":
        target = copy.deepcopy(next(event for event in trace
                                    if event.get("id") == "c00000016" and event.get("action") == "register"))
        target["id"] = "late-boundary-target"
        target["event_order"] = trace[-1]["event_order"] + 2
        trace.append(target)
        for cp in fixture["checkpoints"]:
            if cp.get("kind") in ("probe_expire_before", "probe_expire_at") and \
                    cp["identity_samples"][0]["id"] == "c00000016":
                cp["identity_samples"][0]["id"] = target["id"]
    elif variant == "b16_earlier_finish_missing_classification":
        target = next(event for event in trace
                      if event.get("id") == "c00000320" and event.get("action") == "finish")
        later = copy.deepcopy(target)
        later["event_order"] += 1
        del target["classification"]
        trace.insert(trace.index(target) + 1, later)
    elif variant == "b17_link_classification_changed":
        target = next(event for event in trace if event.get("action") == "link_round")
        target["classification"] = "ignored_link"
    else:
        raise AssertionError(variant)
    report = {"mode": "predicate_unit", "churn_fixtures": [fixture],
              "test_scale": {"hours": 41, "pre_prune_hours": 3}}
    return report, attachments


def _variant_result(churn_input, variant):
    report, attachments = _variant_input(churn_input, variant)
    stdout = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    result = gate.finalize_acceptance(
        stdout_bytes=stdout, stderr_bytes=b"start record_baseline\nend record_baseline PASS\n",
        exit_code=0, attachments=attachments,
    )
    return json.dumps(result, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode()


# Generated with git show 5d93dc0:scripts/d7_ledger_measure_gate.py loaded
# into a temporary in-memory module, then _variant_result for each case.
# CI compares the complete sorted JSON bytes without reading Git history.
VARIANT_BASELINES = {
    'b08_target_after_cut': zlib.decompress(base64.b64decode(
        'eNq9WFlP4zAQ/iuWnxFqelDoW8shIa1Wq0W7L6tV5NoTauHYwUehqvjvO2nVBYeC2hw81bEz3xyZ+cbTNWWcQ+GZ5pDyBfAHJZ2nkz9rCkspoNy2kDnc+XtCLTBnNJ3QXDon9T1RMpfeUTwxT6kUeDLtJfjoPPMBheiv77+vf97e3F5f0ZeTwzALC84FC2kmnz3+VuD73cIPuoUfdgs/6hb+rFv4cUN4D86njjMFp84Ey6v45+3hwzOWCe6l3jLtpJdGV5RddBqrpNeeLzmeoQKBj6CZlaaiKmlPlYe8MJbZ1Ue6+u3p0sbmTKW4VpGSWWOG4otgdapZDhXgFq2fB4trAZ7JqvmD9rQItqqA1yQoHZSKcCIm+jG9uzsa4awxwvhYBFxwYwUIsmQqABHSsXsL4MiT9AuyJRWyA4gDF5HLzfT229HmXjR1OCaFWgjJF4YsLvZDQrYvhS08Blmy1zyIe/BpYaSOrySzpG7FwBK0J+WNiOw0G0uk5iYvFPiKO3V7uylAEwePGLssA+tIZk1OXhtLuayGbtQ025Kjy4tCYfgCo5EBX3G1+dTeynnY2Fi1vWrwuLHB50cbvPtme3vNLLloj0czFpRPH6QWsY5+7ysyr9+4obGCcelXaWGNyWLsTu/bs/6glX5z2UtqZ0fs/OlmCHpPIpeNm/uhetqKyLDziIy+KCJNxw4L8yCVSDdFVcEed4h93llZXjYeL5C25Nyykrsj4KQ+Y23PCGK5kqyueqMPCOsqrtaDOn+G6U1sKBuNINp4sh+4bpEeCD/oFr72vwPbCS4vewZOpiW8gLJx5MzzRaxjz80BocwSLA5Ku60Tiim3lA6zoxyhcLjFToTaV3SypmWrd+Xiv9CaZhJf3Cu9ZaYXVKokhxHrpwWzXrJI7I2vnwoP6ggNjxBCqU3MsE1NMqYc4AZ6KST36TbcG8/f+/LpJ361/pDXhp++9mrQR5ZELr5RvW9/WEm0l3/m9rvy')),
    'b09_target_after_cut': zlib.decompress(base64.b64decode(
        'eNq9V1lr20AQ/ivLPodg+Uhcv8U5IFBKaWhfSlnWq1E8RNpV93BiTP57RzYhWcUJto4+Wd7VfN/MaM4Nl0pB6aVWINQS1EOOzvPZ7w2HFaZQHVvIHJ38OeEWpDOaz3iBzqG+ZzkW6B2nG/MoMKWbi0FCf52XPpAQ//nt1/WP25vb6yv+fHIYZmnBuWBBZPjk6bcGP+wXftQv/Lhf+Em/8Gf9wp+3hPfgvHBK5nDqTLCqjj/tDh+eKE3oTHgrtUOPRtfIvvTqq2TQnS0F3RFBSn9BS4umRpV0R+WhKI2Vdv0R17A7Lm1sIXNBz3lEMm9dodQyWC20LKAG3KH2i2DpOQUvsa7+qDuWVK5r4A0LlA55HuFElej7xd3d0QhnrRHOWyNMj0Xg8FSiXTMlgwOGugyeUYlQwFJ08t4COPaIfsmUCdqDjZ0f1Yybi9uvxyocl4UmJsfZ3ghheLTT9kSmhb8Bq6K0COk9eFEa1PGkMU9G3cRqMm5tcutoT1pHe9I62pNp4w+3t5rPky/dVapMhtyLB9RpzDFs2ghhBdqzatplL3TGUsYqU5Q5eIhZWrcMWUqFfi1Ka0wWY/c60c6H3WTJ5SBpHB2x8afbNeN9Pl+2bp+H8nTlkXHvHpn8J4+0HewtLALmqdgmVQ37vEfsaW9pedl6gKeyhQsrq9UgAk6aV6zdHSMsVxWrq8Hkg4J1FWfrIaMEzyi8mQ2aJpWUaePZfuCmSXog/Khf+Mb7925HKqqeQbtfBZ9C1TgK6dUy5pi8dz1BmRVYWkVejk44hdwKHUVHtaTQ+kidiNjXfLbhKWaVBptXoQ3PkF7cK72rTM9EmqOCiRyKUlqPMhJ7Y+unwqMmQuMjhEhq6zNqU7NM5g7ogKxMUXmxc/fW8ve2fPqJX7U/5LXxp6+9KvSRJpGJb6j3nY9rgfb8D/TOgtc=')),
    'b16_earlier_finish_missing_classification': zlib.decompress(base64.b64decode(
        'eNq9l11P2zAUhv9KlGuEmn7Q0jtaQEKapmlou5kmy3VO4AjHzmyno6r47ztphYrTUkqc7KqJHT+vz+n5sNcxFwIKx5UAJh5BPEm0Lp7+WsewxBSqYQOZpZHfZ7EBbrWKp3GO1qJ6iCTm6GxMM/ovw5RmrnoJvVrHXUmL4h9ff958v7u9u7mOX85OYxYGrC0NsAyfHf3W8P1u8YNu8cNu8aNu8Rfd4seBeAfWMSu4hHOrSyPq/El7fHimNKEx5gxXFh1qVRO77NRXSa89W3KaI4GUXkFxg7omlbQn5SAvtOFm9Z5Wvz0tpU3OJaNn6YnMgiuUeCyNYornUAO3uPtFaeg5Bcexvv1BeyopX9XgDQuUKqX0OF4l+nZ1f/9pwkUwYRxMmAQTLkMJfqY3IiSfJewiXXJ6yFDwqsJ5kdJmqhr4U2JVgRZl+gCOFRqVf6yYJYN2AjMZBrtz1Ik7Q3vrMfa4Jd9NGlt+sNzPksv2YijjpXTsCVXqa/SbdkpYgnJRdRyOXuW0iVAJnRcSHPgqwT2FF1ygW7HCaJ357E6PvLN+O5k17wWUGc/48809ZL8GzIP766k6bXlk2LlHRv/JI6HVycCiRJmyTVLV2OMO2ZPO0nIefMKnsoULs1eu50nzirWdi4hlq2J13Ru9U7Cu/Wy9vbr78iE+o/COTKmiFNNIaRcdBjdN0hPxg27xjS/o20tUXvUMuhxW+BSqxpFzJx59jdG+6wmll2DorvI6dBZTyC3RUnRUtxi6X1InIvVVPF3HKWbVDta7Res4Q/rw4OptZXohUYkCRrzPCm4ccm/ZG1uPLh40WTT8xCJatfEZtalpxqUFGiArUxSObd29sXzflqN/8W73p3w2PPrZbkPv7cQz8Y30ofFhLdBe/gGj0Y3A')),
    'b17_link_classification_changed': zlib.decompress(base64.b64decode(
        'eNq9l19v2jAQwL9K5OeqIgFayhuUVkKapmnV9jJNlnEuxWpiZ7bDilC/+86wrnWACuJkTyR27ne+4/74NoRxDqVlkgPlS+BPuTCWjH9sCKxECm5ZQ2Zw5ecF0cCMkmRMCmGMkI9RLgphDcEd9ZuKFHcmvRhfjWW2QiHy7fP3u6/z+/ndjLxcnMYsNRhTaaCZeLb4W8Mn3eL73eIH3eKH3eKvusVfB+ItGEsNZzlcGlVpXueP2uPDM6YJrlGrmTTCCiVrym469VXca8+WAvdQQYqvIJkWqqYqbk+VhaJUmun1MV1Je7qk0gXLKT7nnpJpcIXiy0pLKlkBNXCLp19UGp9TsEzUj99vT0vK1jV4wwIlqzz3OF4l+jJ5eDibcBVMuA4mjIIJN6EEP9MbEeJgQnIu4VCwafhVCVdnFlX6CJaWSkj/8jCN++2EXzwINjk4gOOzA5hAqfgy4kpK4K6lRCl2GS0W1d+XLANtokyrIsK+w8H3nRfu95P5p7NPPGr8Nx8s59P4pr1SlbEqt/RJyNTXkTTthLACaSN33Y1e1SkdCclVUeZgfecmwT2DlYwLu6alVirz2Z1eaadJOzl124sbR4dv/OV2ztjP/tvg/nmqnrY8MujcI8P/5JHQm72GRSXylG6Tqsa+7pA96iwtb4Nv8Fi2xEIzV7s9cNy8Yu32ImQZV6xmveGRgjXzs/WUbkAyDO9IV67RpJFUNjoMbpqkJ+L73eIbD+C7IalwPQOHP4dPwTWOglm+9HUM912PKLUCjbPI69IFwZBbCYPR4aYUnB+xE6H2NRlviGv1xj38E9qQTOCHB6V3lekFleaCw5AltGTaCuaJvbP1Q+F+E6HBGUIotfUZtqlxxnIDuIBWpoJbunP31vJ9Wz78i99Of8pngw8/ezvQsZN4Jr5TfWh9UAu0lz89lYNY')),
}

@pytest.mark.parametrize("variant", tuple(VARIANT_BASELINES))
def test_mutated_final_result_matches_head_5d93dc0(churn_input, variant):
    assert _variant_result(churn_input, variant) == VARIANT_BASELINES[variant]


@pytest.mark.parametrize("variant, row, reason", (
    ("b08_target_after_cut", "B08", "close target finish absent before checkpoint"),
    ("b09_target_after_cut", "B09", "expiry target registration absent before checkpoint"),
))
def test_boundary_target_after_last_cut_preserves_head_reason(churn_input, variant, row, reason):
    report, _ = _variant_input(churn_input, variant)
    with pytest.raises(gate._EvidenceViolation, match=reason):
        gate._b_followup(row, report, report["churn_fixtures"], True)


def test_earlier_finish_missing_classification_preserves_head_reason(churn_input):
    report, _ = _variant_input(churn_input, "b16_earlier_finish_missing_classification")
    with pytest.raises(gate._EvidenceMissing, match="missing classification"):
        gate._evaluate_b_rest("B16", report)
