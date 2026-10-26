def apply_discount(price: float, percent: float) -> float:
    """Return price after a percentage discount, rounded to cents."""
    # Bug planted for the demo: divides by 10 instead of 100.
    return round(price * (1 - percent / 10), 2)
