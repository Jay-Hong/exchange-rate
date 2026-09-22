"""Admitted fixture entry point: one snapshot passes A, then D1."""

from pathlib import Path

from tools.fixture_capture import admission

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "bank_capture"
REVIEWS_ROOT = Path(__file__).parent / "fixture_reviews" / "bank_capture"
ADMITTED_ROUTES = ("bs_official", "citi_primary", "citi_secondary", "bs_mibank", "citi_mibank")


def admitted_evidence(route, *, root=None, reviews_root=None, admitted=None):
    admitted = ADMITTED_ROUTES if admitted is None else admitted
    admission.require(route in admitted, "gate.route")
    root = FIXTURE_ROOT if root is None else root
    reviews_root = REVIEWS_ROOT if reviews_root is None else reviews_root
    evidence = admission.load_evidence(route, root)
    approval = admission.read_approval(admission.approval_path(reviews_root, evidence))
    admission.admit(evidence.fixture, evidence.metadata_bytes, route, approval)
    return evidence
