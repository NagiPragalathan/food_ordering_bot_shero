"""Configuration safety checks.

`is_unset` exists because python-dotenv keeps everything after `=` as the
value. A template line like `KEY=   # [REQUIRED] paste here` therefore sets
KEY to that comment, and a completely unconfigured deployment reported itself
as ready. These tests pin that behaviour.
"""

import pytest

from app.core.config import is_unset


@pytest.mark.parametrize("value", [
    "",
    "   ",
    None,
    "# [REQUIRED] WhatsApp channel (number) id",
    "#anything",
    "changeme",
    "CHANGEME",
    "TODO",
    "tbd",
    "placeholder",
    "<your-key>",
])
def test_blank_and_placeholder_values_count_as_unset(value):
    assert is_unset(value) is True


@pytest.mark.parametrize("value", [
    "6ab375f2fc1582e04820250f",
    "sk_test_abc123",
    "whsec_abc123",
    "0",                       # a legitimate value that is falsy as a string
    "  spaced-but-real  ",
])
def test_real_values_count_as_set(value):
    assert is_unset(value) is False


def test_env_example_has_no_inline_comments_after_values():
    """A comment after `=` becomes the value - it must never come back."""
    import pathlib

    offenders = []
    for number, line in enumerate(
        pathlib.Path(".env.example").read_text(encoding="utf-8").splitlines(), 1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if "#" in stripped.split("=", 1)[1]:
            offenders.append(f"line {number}: {stripped}")

    assert not offenders, (
        "inline comments after a value in .env.example:\n  " + "\n  ".join(offenders)
    )
