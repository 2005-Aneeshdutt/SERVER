import httpx
import pytest

from gateway.core.errors import (
    AuthError,
    BadRequest,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)
from gateway.providers.http import iter_sse, map_status_error, map_transport_error


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (429, "{}", RateLimited),
        (401, "{}", AuthError),
        (403, "{}", AuthError),
        (
            400,
            '{"error":{"message":"API key not valid","details":[{"reason":"API_KEY_INVALID"}]}}',
            AuthError,
        ),
        (400, '{"error":{"message":"bad"}}', BadRequest),
        (422, "{}", BadRequest),
        (402, "{}", ProviderUnavailable),
        (404, "{}", ProviderUnavailable),
        (408, "{}", ProviderTimeout),
        (500, "{}", ProviderUnavailable),
        (529, "{}", ProviderUnavailable),  # Anthropic "overloaded"
    ],
)
def test_status_mapping(status: int, body: str, expected: type[ProviderError]) -> None:
    assert type(map_status_error("p", status, body, {})) is expected


def test_retryable_flags_match_spec() -> None:
    assert RateLimited.retryable and ProviderUnavailable.retryable and ProviderTimeout.retryable
    assert not BadRequest.retryable and not AuthError.retryable


def test_rate_limit_carries_retry_after() -> None:
    err = map_status_error("p", 429, "{}", {"retry-after": "7"})
    assert isinstance(err, RateLimited)
    assert err.retry_after_s == 7.0
    assert err.headers() == {"Retry-After": "7"}


def test_upstream_message_is_extracted() -> None:
    err = map_status_error("groq", 400, '{"error":{"message":"max_tokens too large"}}', {})
    assert err.message == "[groq] max_tokens too large"


def test_transport_errors() -> None:
    req = httpx.Request("POST", "http://x")
    assert isinstance(
        map_transport_error("p", httpx.ConnectTimeout("t", request=req)), ProviderTimeout
    )
    assert isinstance(
        map_transport_error("p", httpx.ReadTimeout("t", request=req)), ProviderTimeout
    )
    assert isinstance(
        map_transport_error("p", httpx.ConnectError("c", request=req)), ProviderUnavailable
    )


async def test_iter_sse_parses_events_comments_and_multiline_data() -> None:
    body = b": keepalive\n\nevent: ping\ndata: {}\n\ndata: a\ndata: b\n\ndata: tail"
    response = httpx.Response(200, content=body)
    events = [e async for e in iter_sse(response)]
    assert events == [("ping", "{}"), (None, "a\nb"), (None, "tail")]
