from contextvars import ContextVar
from collections.abc import Callable

from core.errors import Cancelled

trace_holder: ContextVar[list] = ContextVar("trace_holder")
run_id_var: ContextVar[str] = ContextVar("run_id")
is_cancelled: ContextVar[Callable[[], bool]] = ContextVar("is_cancelled")
model_client_var: ContextVar = ContextVar("model_client")


def check_cancel() -> None:
    try:
        fn = is_cancelled.get()
    except LookupError:
        return
    if fn():
        raise Cancelled("run cancelled")
