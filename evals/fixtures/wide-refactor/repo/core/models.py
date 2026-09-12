from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    id: str
    body: str
    updated_at: float
    attempts: int = 0
