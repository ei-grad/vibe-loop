from __future__ import annotations


def normalize_slug(value: str) -> str:
    return "-".join(value.lower().split())
