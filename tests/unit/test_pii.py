import pytest

from gateway.capture.pii import CARD_TOKEN, EMAIL_TOKEN, PHONE_TOKEN, luhn_valid, scrub


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail me at jane.doe+bank@example.co.uk please", f"mail me at {EMAIL_TOKEN} please"),
        ("call +1 (415) 555-0132 today", f"call {PHONE_TOKEN} today"),
        ("my number is 07700 900123", f"my number is {PHONE_TOKEN}"),
        ("phone: 415.555.0132", f"phone: {PHONE_TOKEN}"),
        ("card 4111 1111 1111 1111 was charged", f"card {CARD_TOKEN} was charged"),
        ("card 4111-1111-1111-1111", f"card {CARD_TOKEN}"),
        ("5500005555555559 declined", f"{CARD_TOKEN} declined"),
    ],
)
def test_scrubs_common_formats(text: str, expected: str) -> None:
    assert scrub(text).text == expected


@pytest.mark.parametrize(
    "text",
    [
        "In 1990 the band moved to New York",  # a year
        "transfer of 2500.00 EUR on 2024-01-15",  # amount and date
        "order #12345678 shipped",  # 8 digits: too short for a phone
        "What was the score in game 23 of the 2008 season?",
        "4111 1111 1111 1112",  # 16 digits, fails Luhn -> not a card (and > 15 digits: not a phone)
    ],
)
def test_leaves_ordinary_numbers_alone(text: str) -> None:
    assert scrub(text).text == text


def test_counts_each_kind() -> None:
    result = scrub("a@b.io, c@d.org, call 4155550132, card 4111111111111111")
    assert (result.redactions.email, result.redactions.phone, result.redactions.card) == (2, 1, 1)


def test_luhn() -> None:
    assert luhn_valid("4111111111111111")
    assert not luhn_valid("4111111111111112")


@pytest.mark.parametrize(
    "text",
    [
        "jane at example dot com",  # obfuscated email
        "call five five five, oh one three two",  # spelled-out number
    ],
)
def test_known_misses_are_documented(text: str) -> None:
    """Basic scrubbing: these are NOT caught. Kept as a test so the limitation stays visible."""
    assert scrub(text).text == text
