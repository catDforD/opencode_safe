from tasks.fixtures.fix_math_utils.math_utils import add


def test_add() -> None:
    assert add(2, 3) == 5
