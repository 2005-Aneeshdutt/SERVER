import pytest
from pydantic import ValidationError

from gateway.core.config import Settings


def test_reservations_must_outlive_requests_plus_settle_retries() -> None:
    with pytest.raises(ValidationError, match="spend_reservation_ttl_s"):
        Settings(_env_file=None, total_timeout_s=300, spend_reservation_ttl_s=300)
    assert Settings(_env_file=None, total_timeout_s=120, spend_reservation_ttl_s=180)
