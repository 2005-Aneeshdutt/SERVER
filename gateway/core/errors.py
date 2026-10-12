"""Error taxonomy.

Gateway errors are raised by our own checks (auth, unknown model). Provider errors are what
adapters normalize upstream failures into; `retryable` drives retry/failover in M2.
"""

from typing import Any, ClassVar


class GatewayError(Exception):
    status_code: ClassVar[int] = 500
    error_type: ClassVar[str] = "api_error"
    code: ClassVar[str] = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def headers(self) -> dict[str, str]:
        return {}

    def to_body(self) -> dict[str, Any]:
        """OpenAI-style error body, so OpenAI SDKs raise the right exception class."""
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.code,
                "param": None,
            }
        }


class InvalidAPIKey(GatewayError):
    status_code = 401
    error_type = "authentication_error"
    code = "invalid_api_key"


class AdminUnauthorized(GatewayError):
    status_code = 401
    error_type = "authentication_error"
    code = "invalid_admin_token"


class AdminDisabled(GatewayError):
    status_code = 503
    error_type = "api_error"
    code = "admin_disabled"


class NotFound(GatewayError):
    status_code = 404
    error_type = "invalid_request_error"
    code = "not_found"


class KeyStoreUnavailable(GatewayError):
    status_code = 503
    error_type = "api_error"
    code = "auth_unavailable"


class ModelNotFound(GatewayError):
    status_code = 404
    error_type = "invalid_request_error"
    code = "model_not_found"


class ModelNotAllowed(GatewayError):
    status_code = 403
    error_type = "permission_error"
    code = "model_not_allowed"


class InvalidRequest(GatewayError):
    status_code = 400
    error_type = "invalid_request_error"
    code = "invalid_request"


class NoProviderAvailable(GatewayError):
    """Every candidate for the request was skipped (circuit open / not configured)."""

    status_code = 503
    error_type = "upstream_error"
    code = "no_provider_available"


class RateLimitExceeded(GatewayError):
    """This API key exceeded its own RPM/TPM limit (not an upstream 429)."""

    status_code = 429
    error_type = "rate_limit_error"
    code = "rate_limit_exceeded"

    def __init__(self, message: str, headers: dict[str, str]) -> None:
        super().__init__(message)
        self._headers = headers

    def headers(self) -> dict[str, str]:
        return self._headers


class SpendCapExceeded(GatewayError):
    """Serving this request could push the key past its monthly spend cap."""

    status_code = 402
    error_type = "insufficient_quota"
    code = "spend_cap_exceeded"


class LimiterUnavailable(GatewayError):
    """Redis is down, so limits can't be enforced: fail closed rather than risk overspend."""

    status_code = 503
    error_type = "api_error"
    code = "limiter_unavailable"


class Overloaded(GatewayError):
    """This replica is at its in-flight cap: shed now rather than queue for seconds."""

    status_code = 503
    error_type = "api_error"
    code = "overloaded"

    def headers(self) -> dict[str, str]:
        return {"Retry-After": "1"}


class ProviderError(GatewayError):
    """Base for normalized upstream failures."""

    retryable: ClassVar[bool] = False

    def __init__(self, provider: str, message: str, upstream_status: int | None = None) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.upstream_status = upstream_status


class RateLimited(ProviderError):
    status_code = 429
    error_type = "rate_limit_error"
    code = "upstream_rate_limited"
    retryable = True

    def __init__(
        self,
        provider: str,
        message: str,
        upstream_status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(provider, message, upstream_status)
        self.retry_after_s = retry_after_s

    def headers(self) -> dict[str, str]:
        if self.retry_after_s is None:
            return {}
        return {"Retry-After": str(max(1, round(self.retry_after_s)))}


class ProviderUnavailable(ProviderError):
    """5xx, connection failures, malformed upstream responses."""

    status_code = 502
    error_type = "upstream_error"
    code = "provider_unavailable"
    retryable = True


class ProviderTimeout(ProviderError):
    status_code = 504
    error_type = "upstream_error"
    code = "provider_timeout"
    retryable = True


class BadRequest(ProviderError):
    """Upstream rejected the request because of something the client sent. Never retried."""

    status_code = 400
    error_type = "invalid_request_error"
    code = "upstream_bad_request"


class AuthError(ProviderError):
    """Upstream rejected *our* credentials. That's the gateway's fault, not the client's."""

    status_code = 502
    error_type = "upstream_error"
    code = "upstream_auth_failed"
