from tasks.fixtures.baseline_math_multiply.math_utils import multiply


def test_multiply_positive_numbers() -> None:
    assert multiply(3, 4) == 12


def test_multiply_negative_numbers() -> None:
    assert multiply(-2, 5) == -10
