from __future__ import annotations


def is_valid_code(value: str) -> bool:
    if not value:
        return False
    return value.replace("-", "").isalnum() and "-" in value
