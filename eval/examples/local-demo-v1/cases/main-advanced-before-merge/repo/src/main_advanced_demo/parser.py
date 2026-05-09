from __future__ import annotations


def parse_numbers(value: str) -> list[int]:
    return [int(part) for part in value.split(",")]
