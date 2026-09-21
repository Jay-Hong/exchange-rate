"""Temporary bs4 query interception, including sticky swallowed violations."""

from functools import wraps

from bs4 import Tag

from .errors import CaptureError
from .queries import query_key


def element_path(element):
    """Zero-based element-child indices from the soup root (text is not counted)."""
    path = []
    while element.parent is not None:
        parent = element.parent
        siblings = [child for child in parent.children if isinstance(child, Tag)]
        path.append(next(i for i, child in enumerate(siblings) if child is element))
        element = parent
    if element.name != "[document]":
        raise CaptureError("detached_element", "recording")
    return list(reversed(path))


class QueryRecorder:
    METHODS = ("select", "select_one", "find", "find_all", "find_parent")
    _active = False

    def __init__(self, registry):
        self.registry = registry
        self.calls = []
        self.violations = []
        self.elements = []
        self.originals = {}

    def __enter__(self):
        if QueryRecorder._active:
            raise CaptureError("nested_recorder", "recording")
        QueryRecorder._active = True
        for method in self.METHODS:
            original = getattr(Tag, method)
            self.originals[method] = original
            setattr(Tag, method, self._wrap(method, original))
        return self

    def _wrap(self, method, original):
        @wraps(original)
        def wrapped(element, *args, **kwargs):
            index = len(self.calls)
            try:
                allowed = query_key(method, args, kwargs) in self.registry.allowed_queries
            except CaptureError:
                allowed = False
            if not allowed:
                # Do not retain unregistered arguments: they can contain page text.
                self.violations.append(index)
                raise CaptureError("unregistered_query", f"queries[{index}]")
            call = {"method": method, "args": list(args), "kwargs": dict(kwargs),
                    "root": element_path(element), "results": None}
            self.calls.append(call)
            result = original(element, *args, **kwargs)
            elements = [result] if isinstance(result, Tag) else ([] if result is None else list(result))
            if any(not isinstance(item, Tag) for item in elements):
                self.violations.append(index)
                raise CaptureError("non_element_result", f"queries[{index}]")
            call["results"] = [element_path(item) for item in elements]
            self.elements.extend(elements)
            return result
        return wrapped

    def assert_valid(self):
        if self.violations:
            raise CaptureError("unregistered_query", f"queries[{self.violations[0]}]")
        if any(call["results"] is None for call in self.calls):
            raise CaptureError("incomplete_query", "recording")

    def __exit__(self, exc_type, exc, tb):
        for method, original in self.originals.items():
            setattr(Tag, method, original)
        QueryRecorder._active = False
        if exc_type is None:
            self.assert_valid()
