from src.pricing import apply_discount


def test_ten_percent_off():
    assert apply_discount(200.0, 10) == 180.0


def test_no_discount():
    assert apply_discount(99.99, 0) == 99.99
