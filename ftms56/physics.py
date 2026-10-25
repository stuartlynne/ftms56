"""Translate a virtual drivetrain into an equivalent FTMS simulation grade.

At physical wheel speed v and ratio multiplier k, virtual road speed is k*v.
Power conservation gives trainer force k*F_road(k*v). Solve the trainer's
gravity/rolling model for a grade after subtracting its aerodynamic load.
This assumes the configured trainer mass and FTMS coefficients describe its
simulation model. Actual ride feel requires hardware validation.
"""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Translation:
    grade: float
    requested_grade: float
    clamped: bool
    ratio_multiplier: float


def equivalent_grade(grade: float, ratio: float, physical_ratio: float, speed_mps: float,
                     mass_kg: float = 85, trainer_mass_kg: float = 85,
                     wind_mps: float = 0, crr: float = .004, cw: float = .51,
                     min_grade: float = -20, max_grade: float = 20) -> Translation:
    values = (grade, ratio, physical_ratio, speed_mps, mass_kg, trainer_mass_kg,
              wind_mps, crr, cw, min_grade, max_grade)
    if not all(math.isfinite(x) for x in values):
        raise ValueError("non-finite simulation input")
    if min(ratio, physical_ratio, mass_kg, trainer_mass_kg) <= 0 or speed_mps < 0 or min(crr, cw) < 0:
        raise ValueError("invalid simulation input")
    if min_grade > max_grade:
        raise ValueError("invalid grade limits")
    k = ratio / physical_ratio
    theta = math.atan(grade / 100)

    def drag(speed):
        air = speed + wind_mps
        return .5 * cw * air * abs(air)

    road_force = mass_kg * 9.80665 * (math.sin(theta) + crr * math.cos(theta)) + drag(speed_mps * k)
    required = (k * road_force - drag(speed_mps)) / (trainer_mass_kg * 9.80665)
    sine = required / math.sqrt(1 + crr * crr)
    # Limit the inverse solution before tan() approaches a singularity.
    angle = math.asin(max(-.999999, min(.999999, sine))) - math.atan(crr)
    requested = 100 * math.tan(angle)
    output = max(min_grade, min(max_grade, requested))
    return Translation(output, requested, output != requested, k)
