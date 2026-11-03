"""Fixture: undefined name — ruff reports F821, which it cannot autofix."""


def broken() -> int:
    """Reference an undefined name."""
    return undefined_name + 1
