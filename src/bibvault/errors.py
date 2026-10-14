"""Structured failures. ``category`` matches an acquisition status or ``failed``."""


class BibVaultError(Exception):
    category = "failed"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotPdfError(BibVaultError):
    category = "failed"


class NotHtmlError(BibVaultError):
    category = "failed"


class AuthenticationRequiredError(BibVaultError):
    category = "authentication_required"


class AccessDeniedError(BibVaultError):
    category = "access_denied"


class BrowserRequiredError(BibVaultError):
    category = "manual_action_required"


class NotFoundError(BibVaultError):
    category = "not_found"


class RateLimitedError(BibVaultError):
    category = "rate_limited"


class UnsafeURLError(BibVaultError):
    category = "failed"


class DownloadTooLargeError(BibVaultError):
    category = "failed"


class FileCollisionError(BibVaultError):
    category = "failed"


class ParseError(BibVaultError):
    category = "failed"
