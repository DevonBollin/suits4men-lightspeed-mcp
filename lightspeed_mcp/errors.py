"""Exceptions with messages that are safe to show to the user.

No message built from these classes may contain a token, client secret, or
authorization code. The MCP server passes the message straight to Claude.
"""

REAUTH_HINT = (
    'To fix this, run bootstrap.py: open PowerShell in the project folder and run '
    '".venv\\Scripts\\python.exe bootstrap.py".'
)


class LightspeedError(Exception):
    """Base class for every error this package raises on purpose."""


class NotBootstrappedError(LightspeedError):
    def __init__(self, detail: str = "No saved Lightspeed credentials were found."):
        super().__init__(f"{detail} {REAUTH_HINT}")


class ReauthorizationRequired(LightspeedError):
    """The saved refresh token (or client secret) no longer works. Only bootstrap.py can fix this."""

    def __init__(self, detail: str):
        super().__init__(f"{detail} {REAUTH_HINT}")


class TokenRefreshError(LightspeedError):
    """A refresh failed for a reason that may be temporary (network, lock timeout, Lightspeed outage)."""


class AuthError(LightspeedError):
    """Lightspeed still rejected the request after a fresh access token."""


class WriteBlockedError(LightspeedError):
    """Raised for any attempt to send a non-GET request. Phase 1 is read-only."""


class RateLimitedError(LightspeedError):
    """Lightspeed kept answering 429 Too Many Requests."""


class SecretStoreError(LightspeedError):
    """Windows Credential Manager could not be read or written."""


class ApiError(LightspeedError):
    """Lightspeed answered with an HTTP error (403, 404, 5xx, ...)."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)
