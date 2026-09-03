"""Stable errors shared by the non-interactive CLI commands."""

from __future__ import annotations

from typing import Any


class HeadlessCommandError(Exception):
    """A user-actionable CLI failure with a stable code and exit status."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int = 5,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload
