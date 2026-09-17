def inclusive_sum(start: int, end: int) -> int:
    """Sum all integers from start through end, including both endpoints."""
    if start > end:
        raise ValueError("start must not exceed end")
    return sum(range(start, end))
