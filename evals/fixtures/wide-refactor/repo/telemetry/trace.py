import contextvars

_span = contextvars.ContextVar("span", default=None)


def current() -> str | None:
    return _span.get()
