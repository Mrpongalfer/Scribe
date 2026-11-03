"""Fixture: type error with clean lint and formatting."""


def add(a: int, b: int) -> int:
    """Return the sum of two integers."""
    return a + b


VALUE: int = "not an int"
