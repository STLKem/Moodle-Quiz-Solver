"""API key helpers — cleaning, validation, and config lookups."""

from __future__ import annotations


def clean_key(key: str | None) -> str:
    """Strip non-ASCII junk (e.g. №) and whitespace from an API key."""
    return "".join(ch for ch in (key or "") if ord(ch) < 128).strip()


def is_valid_key(key: str | None) -> bool:
    """Return True if the value looks like a real key, not a placeholder."""
    cleaned = clean_key(key)
    if not cleaned or len(cleaned) < 8:
        return False
    upper = cleaned.upper()
    return not (
        upper.startswith("YOUR")
        or upper.startswith("REPLACE")
        or upper.startswith("TODO")
        or upper == "NONE"
        or upper == "NULL"
    )


def get_key(config: dict, name: str) -> str:
    """Read and clean an API key from an agents config section."""
    return clean_key(config.get(name, ""))
