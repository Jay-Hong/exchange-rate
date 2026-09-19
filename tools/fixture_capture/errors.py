"""Only code-owned diagnostics may cross the capture boundary."""


class CaptureError(Exception):
    """Never pass response text, exception messages or filesystem paths here."""

    def __init__(self, rule, location="capture"):
        self.rule = rule
        self.location = location
        super().__init__(f"{location}: {rule}")


class DeadlineExpired(BaseException):
    """Bypass broad Exception handlers in requests and reporting helpers."""

    def __init__(self, rule):
        self.rule = rule
        super().__init__(rule)
