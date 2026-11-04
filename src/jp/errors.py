"""Exception hierarchy and exit codes for jp.

Exit codes (see docs/architecture.md):
    0   success
    1   generic / unexpected error
    2   usage error (bad CLI args)
    3   not inside a jp repo / config problem
    4   authentication problem (missing/invalid token)
    5   network / API transport problem
    6   safety refusal (path-jail, bad prefix, conflict abort, ...)
    7   partial failure (run completed but some files failed)

All jp-raised errors derive from ``JpError`` and carry an ``exit_code`` so the
CLI dispatcher can translate them into the correct process exit status.

Security note: error *messages* are always passed through ``ui.redact`` at the
boundary (see cli.py); no module should embed a raw token in a message.
"""

from __future__ import annotations

# Exit codes -----------------------------------------------------------------
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_AUTH = 4
EXIT_NETWORK = 5
EXIT_SAFETY = 6
EXIT_PARTIAL = 7


class JpError(Exception):
    """Base class for all expected, user-facing jp errors."""

    exit_code: int = EXIT_GENERIC

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UsageError(JpError):
    """Bad CLI usage / arguments."""

    exit_code = EXIT_USAGE


class ConfigError(JpError):
    """Not inside a jp repo, or the config/index is missing or malformed."""

    exit_code = EXIT_CONFIG


class AuthError(JpError):
    """Missing, unreadable, or rejected credentials."""

    exit_code = EXIT_AUTH


class NetworkError(JpError):
    """Transport-level failure talking to the Contents API."""

    exit_code = EXIT_NETWORK


class ApiError(NetworkError):
    """The API responded with a non-success HTTP status."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ServerDownError(NetworkError):
    """The JupyterHub single-user server is stopped / not routed.

    Empirically (research/01-jupyter-api.md §8) a stopped or unrouted server
    answers ``GET /api/status`` with a 3xx redirect whose ``Location`` points
    into the Hub's ``/hub/...`` space, rather than the 200 (up) or 403 (bad
    token) we get from a running server. This is a categorically different,
    *actionable* situation: the user must start their server from the Hub UI.
    """

    def __init__(
        self,
        message: str = (
            "your JupyterHub server appears to be stopped. "
            "Open the JupyterHub web UI and click 'Start My Server', then retry."
        ),
    ) -> None:
        super().__init__(message)


class TransferError(JpError):
    """A transfer finished but its bytes cannot be trusted."""


class SafetyError(JpError):
    """A safety invariant would have been violated; the operation was refused.

    Raised by the path-jail (paths.py), by prefix validation, and by the
    conflict detector. These are *intentional* refusals, never bugs.
    """

    exit_code = EXIT_SAFETY


class PartialFailure(JpError):
    """The run finished but one or more files failed in a recoverable way."""

    exit_code = EXIT_PARTIAL
