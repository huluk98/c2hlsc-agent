from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class Diagnostic:
    severity: str
    code: str
    message: str
    location: str | None = None
    suggestion: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "location": self.location,
            "suggestion": self.suggestion,
        }


@dataclass
class DiagnosticBag:
    items: list[Diagnostic] = field(default_factory=list)

    def add(
        self,
        severity: str,
        code: str,
        message: str,
        location: str | None = None,
        suggestion: str | None = None,
    ) -> None:
        self.items.append(Diagnostic(severity, code, message, location, suggestion))

    def extend(self, diagnostics: Iterable[Diagnostic]) -> None:
        self.items.extend(diagnostics)

    @property
    def has_errors(self) -> bool:
        return any(item.severity.lower() == "error" for item in self.items)

    def by_severity(self, severity: str) -> list[Diagnostic]:
        return [item for item in self.items if item.severity.lower() == severity.lower()]

    def to_list(self) -> list[dict[str, str | None]]:
        return [item.to_dict() for item in self.items]
