"""One line of verify output."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Result:
    status: str  # PASS, FAIL, SKIP or DEGRADED
    name: str
    detail: str = ""

    def line(self) -> str:
        return f"{self.status:<8} {self.name}" + (f": {self.detail}" if self.detail else "")
