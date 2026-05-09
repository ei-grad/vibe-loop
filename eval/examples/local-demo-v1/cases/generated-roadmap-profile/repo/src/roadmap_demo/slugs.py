from __future__ import annotations


def route_key(value: str) -> str:
    return value.strip().lower().replace(" ", "-")
