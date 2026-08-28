from cart import discounted_total


def test_discounted_total():
    assert discounted_total([50.0, 50.0], 0.1) == 90.0

