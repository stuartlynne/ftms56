"""Display inference against captured ratios, overlap and preset changes."""

import pytest

from ftms56.gears import GearEstimate
from ftms56.zwift import State, bytes_field, uint_field


@pytest.mark.parametrize("ratio,preset,gear", [
    (1.68, "Mixed Terrain", 8), (1.95, "Flat", 5),
    (1.6666, "All-Rounder", 5), (1.3437, "Climbing", 6),
])
def test_captured_starting_ratios(ratio, preset, gear):
    e = GearEstimate()
    e.update(ratio)
    assert e.status(ratio)["preset"] == preset
    assert e.status(ratio)["confidence"] == "inferred"
    assert e.gear(ratio) == gear


def test_ambiguous_guess_changes_with_evidence_and_recovers_from_unknown():
    e = GearEstimate()
    e.update(2.0)  # 48/24 or 30/15: guess All-Rounder.
    assert e.status(2.0)["preset"] == "All-Rounder"
    assert e.status(2.0)["confidence"] == "guess"
    e.update(3.0)  # 39/13 or 30/10: intersection identifies Climbing.
    assert e.status(3.0)["preset"] == "Climbing"
    assert e.status(3.0)["confidence"] == "inferred"
    e.update(2.1234)
    assert e.status(2.1234)["confidence"] == "approximate"
    assert e.status(2.1234)["ratio_matches"] is False
    e.update(1.95)  # User changed preset to Flat.
    assert e.status(1.95)["preset"] == "Flat"
    assert e.gear(1.95) == 5
    e.update(None)
    assert e.status(None)["confidence"] == "waiting"


def test_sram_sweep_and_return_to_sequential():
    e = GearEstimate()
    # Captured small-ring sweep, including one-unit truncated values.
    for r in [1.0606, 1.25, 1.4583, 1.6666, 1.8421, 2.0588, 2.3333, 2.5,
              2.6923, 2.9166, 3.1818, 3.5]:
        e.update(r)
    assert e.status(3.5)["style"] == "SRAM-style"
    assert e.status(3.5)["gear_label"] == "1-12"
    # Same-cog front swap, then adjacent sorted ratios that also fit SRAM:
    # these ambiguous transitions must not overturn the style estimate.
    for r in [4.8, 4.3636, 4.0, 3.6923]:
        e.update(r)
    assert e.status(3.6923)["style"] == "SRAM-style"
    # Switch to a sequential sweep: enough contrary evidence corrects it.
    for r in [1.0606, 1.25, 1.4545, 1.4583, 1.6666, 1.7142, 1.8421, 2.0,
              2.0588, 2.2857, 2.3333, 2.5]:
        e.update(r)
    assert e.status(2.5)["style"] == "Sequential"
    assert e.status(2.5)["gear_label"] == "12"


def test_inference_never_changes_received_ratio_or_retained_ratio_on_grade_update():
    state = State()
    for raw in [16800, 19500, 16666, 13437, 21234]:
        state.receive(b"\x04" + bytes_field(5, uint_field(2, raw)))
        assert state.ratio == raw / 10000
    state.receive(b"\x04" + bytes_field(4, uint_field(2, 12)))
    assert state.ratio == 2.1234
    assert state.gearing.status(state.ratio)["confidence"] == "approximate"
