import pytest
from solution import pipe_wall_thickness


def test_known_value():
    expected = 10.0 * 200.0 / (2 * 138.0)
    assert pipe_wall_thickness(10.0, 200.0, 138.0) == pytest.approx(expected)


def test_doubles_with_pressure():
    assert pipe_wall_thickness(20.0, 200.0, 138.0) == pytest.approx(2 * pipe_wall_thickness(10.0, 200.0, 138.0))


def test_rejects_negative():
    with pytest.raises(ValueError):
        pipe_wall_thickness(-10.0, 200.0, 138.0)
    with pytest.raises(ValueError):
        pipe_wall_thickness(10.0, -200.0, 138.0)
    with pytest.raises(ValueError):
        pipe_wall_thickness(10.0, 200.0, -138.0)
