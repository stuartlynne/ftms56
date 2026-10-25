import pytest

from ftms56.physics import equivalent_grade


@pytest.mark.parametrize("grade", [-10, 0, 4, 15])
@pytest.mark.parametrize("speed", [0, 4, 15])
@pytest.mark.parametrize("wind", [-8, 0, 5])
def test_neutral_gear_preserves_original_grade(grade, speed, wind):
    value = equivalent_grade(grade, 3.125, 3.125, speed, wind_mps=wind)
    assert value.grade == pytest.approx(grade, abs=1e-10)
    assert not value.clamped


def test_shifting_changes_load_even_on_flat_road():
    low = equivalent_grade(0, 1.5, 3, 8)
    neutral = equivalent_grade(0, 3, 3, 8)
    high = equivalent_grade(0, 4.5, 3, 8)
    assert low.grade < neutral.grade < high.grade


def test_limits_and_invalid_inputs():
    assert equivalent_grade(15, 5.49, 2, 20).clamped
    assert equivalent_grade(15, 5.49, 2, 20).grade == 20
    for kwargs in [dict(ratio=0), dict(speed_mps=-1), dict(mass_kg=0), dict(grade=float("nan"))]:
        values = dict(grade=5, ratio=3, physical_ratio=3, speed_mps=8)
        values.update(kwargs)
        with pytest.raises(ValueError):
            equivalent_grade(**values)
