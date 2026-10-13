"""Basic PII scrubbing for captured training data: emails, phone numbers, card numbers.

Deliberately simple and predictable (a regex for emails; digit runs plus a Luhn check for
numbers), not an NER model. It catches the common formats and misses unusual ones;
tests/unit/test_pii.py lists both. Runs in the capture producer's background task, so it never
blocks a request, but it does share the event loop's CPU, so it's written to be cheap: emails
are searched outwards from each "@", and numbers by a linear `\\d+` scan. Single whole-text
regexes for both cost ~3-5x more, because they're tried at every character and backtrack.
"""

import re
from dataclasses import dataclass, field

EMAIL_TOKEN = "[EMAIL]"
PHONE_TOKEN = "[PHONE]"
CARD_TOKEN = "[CARD]"

# Emails are found from each "@" outwards (local part to the left, domain to the right), so
# the regexes only ever run on short windows instead of scanning the whole text.
_LOCAL = re.compile(r"[A-Za-z0-9._%+-]{1,64}$")
_DOMAIN = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_DIGITS = re.compile(r"\d+")
_JOINERS = frozenset(" .-()")  # separators allowed inside a phone/card number (max 2 in a row)
_NOT_BEFORE = frozenset("._-+")  # a number glued to these is part of a longer token


@dataclass
class Redactions:
    email: int = 0
    phone: int = 0
    card: int = 0

    def add(self, other: "Redactions") -> None:
        self.email += other.email
        self.phone += other.phone
        self.card += other.card


@dataclass
class Scrubbed:
    text: str
    redactions: Redactions = field(default_factory=Redactions)


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _number_runs(text: str) -> list[tuple[int, int, str]]:
    """(start, end, digits) for runs of digits joined by at most 2 separator characters."""
    runs: list[tuple[int, int, str]] = []
    start = end = -1
    digits = ""
    for m in _DIGITS.finditer(text):
        gap = text[end : m.start()]
        if start >= 0 and len(gap) <= 2 and all(c in _JOINERS for c in gap):
            end, digits = m.end(), digits + m.group()
            continue
        if start >= 0:
            runs.append((start, end, digits))
        start, end, digits = m.start(), m.end(), m.group()
    if start >= 0:
        runs.append((start, end, digits))
    return runs


def _scrub_numbers(text: str, counts: Redactions) -> str:
    out: list[str] = []
    last = 0
    for start, end, digits in _number_runs(text):
        if len(digits) < 10:
            continue  # years, amounts, short ids: never PII here
        if start and text[start - 1] in "+(":
            start -= 1
        before = text[start - 1] if start else " "
        after = text[end] if end < len(text) else " "
        if before.isalnum() or before in _NOT_BEFORE or after.isalnum():
            continue  # part of a longer token (an id, a version string)
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            token = CARD_TOKEN
            counts.card += 1
        elif len(digits) <= 15:
            token = PHONE_TOKEN
            counts.phone += 1
        else:
            continue  # long, not a valid card: an id, leave it
        out.append(text[last:start])
        out.append(token)
        last = end
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def _scrub_emails(text: str, counts: Redactions) -> str:
    out: list[str] = []
    last = 0
    at = text.find("@")
    while at != -1:
        local = _LOCAL.search(text, max(last, at - 64), at)
        domain = _DOMAIN.match(text, at + 1)
        if local is not None and domain is not None:
            out.append(text[last : local.start()])
            out.append(EMAIL_TOKEN)
            counts.email += 1
            last = domain.end()
        at = text.find("@", max(at + 1, last))
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def scrub(text: str) -> Scrubbed:
    counts = Redactions()
    if "@" in text:
        text = _scrub_emails(text, counts)
    return Scrubbed(_scrub_numbers(text, counts), counts)
