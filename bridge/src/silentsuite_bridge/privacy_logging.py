"""Privacy-safe helpers for bounded Bridge diagnostics."""

import re

_BOUNDED_DIAGNOSTIC_CODES = frozenset(
    {
        "CacheBackfillIntegrityError",
        "CachePullItemIntegrityError",
        "CacheRevisionIntegrityError",
        "CacheSchemaRemoteUidIntegrityError",
        "CacheSchemaTokenRevisionIntegrityError",
        "CacheSyncTokenIntegrityError",
        "CacheUnresolvedRetryIntegrityError",
    }
)


class BoundedDiagnosticError(RuntimeError):
    """Carry one allowlisted product diagnostic code without private details."""

    def __init__(self, diagnostic_code):
        if diagnostic_code not in _BOUNDED_DIAGNOSTIC_CODES:
            raise ValueError("unsupported diagnostic code")
        super().__init__("Bridge operation failed")
        self.diagnostic_code = diagnostic_code


def bounded_identifier(value, *, fallback="Exception", max_length=64):
    """Return one short source-style identifier or a fixed fallback."""
    if (
        isinstance(value, str)
        and len(value) <= max_length
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
    ):
        return value
    return fallback


def bounded_exception_class(error):
    """Return a bounded exception-class identifier for product diagnostics."""
    if (
        isinstance(error, BoundedDiagnosticError)
        and error.diagnostic_code in _BOUNDED_DIAGNOSTIC_CODES
    ):
        return error.diagnostic_code
    return bounded_identifier(error.__class__.__name__)


def log_bounded_failure(logger, level, message, error):
    """Log a source-owned event and exception class without values or traceback."""
    exception_class = bounded_exception_class(error)
    logger.log(level, "%s (%s)", message, exception_class)
