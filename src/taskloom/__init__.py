"""TaskLoom product package."""

from importlib.metadata import version


def package_version() -> str:
    """Read the single distribution version."""
    return version("taskloom")
