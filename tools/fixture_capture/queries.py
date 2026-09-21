"""Query call shapes, free of the selector registry and of the app import boundary.

Split out of registry.py (D1 slice 5b-1) so that modules which only need to compare recorded query shapes, such as the
detector and the admission layer, do not import `registry` -> `runtime` (which imports the app dynamically).
"""

from .errors import CaptureError


def query_key(method, args, kwargs):
    """Exact public call shape: list order, method and recursive are significant."""
    def freeze(value):
        if type(value) is list:
            return ("list", tuple(freeze(v) for v in value))
        if type(value) in (str, bool, int, type(None)):
            return (type(value).__name__, value)
        raise CaptureError("query_arguments", "registry")
    return method, tuple(freeze(v) for v in args), tuple(
        (k, freeze(v)) for k, v in sorted(kwargs.items()))
