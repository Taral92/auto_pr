"""Lease bookkeeping. LEASE_S must exceed the wall clock budget."""


def validate(lease_s: int, max_wall_clock_s: int) -> None:
    if lease_s <= max_wall_clock_s:
        raise ValueError(
            f"lease_s={lease_s} must exceed max_wall_clock_s={max_wall_clock_s}"
        )
