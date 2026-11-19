"""Typed TopstepX domain and transport exceptions."""

from __future__ import annotations


class TopstepError(Exception):
    """Base error for TopstepX transport and contract resolution."""


class TopstepAuthError(TopstepError):
    """Raised when TopstepX authentication is missing or rejected."""


class KeyRotationRequiredError(TopstepAuthError):
    """Raised when KEY_ROTATION_ENFORCE=true and the API key age exceeds KEY_ROTATION_DAYS."""


class ContractNotFoundError(TopstepError):
    """Raised when no exact contract matches the requested root symbol."""


class AmbiguousContractError(TopstepError):
    """Raised when multiple matching contracts cannot be uniquely resolved."""


class UpstreamTimeoutError(TopstepError):
    """Raised when an upstream HTTP operation exceeds its deadline."""


class UpstreamHTTPError(TopstepError):
    """Raised on non-success HTTP or TopstepX application responses."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
