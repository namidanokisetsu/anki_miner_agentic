"""Typed errors shared by the application, CLI, and MCP adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AgentMiningError(Exception):
    """A stable, serializable application error."""

    code: str
    message: str
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


def require(condition: bool, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise AgentMiningError(code, message, details or None)
