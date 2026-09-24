from __future__ import annotations


def get_numeric_name(index: int) -> str:
    """Return 1-indexed numeric string for given 0-based index."""
    if index < 0:
        raise ValueError("index must be non-negative")
    return str(index + 1)


def get_letter_name(index: int) -> str:
    """Map 0-based index to alphabetical sequence with Title-case.

    Maps 0 -> 'A', 25 -> 'Z', 26 -> 'Aa', 27 -> 'Ab', 51 -> 'Az', 52 -> 'Ba',
    701 -> 'Zz', 702 -> 'Aaa', etc.
    """
    if index < 0:
        raise ValueError("index must be non-negative")
    chars: list[str] = []
    num = index + 1
    while num > 0:
        remainder = (num - 1) % 26
        chars.append(chr(65 + remainder))  # 65 = 'A'
        num = (num - 1) // 26

    raw_str = "".join(reversed(chars))  # e.g., "AA", "AB"

    # Capitalize first letter, lowercase the rest (e.g., "Aa", "Ab")
    return raw_str[0].upper() + raw_str[1:].lower()


def generate_account_names(count: int, mode: str) -> list[str]:
    """Generate sequential account names for the given count and mode ('num' or 'letter')."""
    if count < 0:
        raise ValueError("count must be non-negative")
    if count == 0:
        return []

    normalized_mode = mode.strip().lower()
    if normalized_mode == "num":
        return [get_numeric_name(i) for i in range(count)]
    elif normalized_mode == "letter":
        return [get_letter_name(i) for i in range(count)]
    else:
        raise ValueError(f"Invalid mode '{mode}'. Expected 'num' or 'letter'.")


__all__ = [
    "get_numeric_name",
    "get_letter_name",
    "generate_account_names",
]
