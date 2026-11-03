"""Fixture: logic error — lint and types are clean, tests should fail."""


def add(a: int, b: int) -> int:
    """Return the sum of two integers (buggy implementation)."""
    return a - b
