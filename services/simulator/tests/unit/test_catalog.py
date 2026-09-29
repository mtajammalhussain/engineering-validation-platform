from dataclasses import FrozenInstanceError

import pytest

from simulator.catalog import CATALOG, WAKEUP_TIME, TestSpec

# Spec §8.1, row by row.
EXPECTED = [
    ("SLEEP_CURRENT", "Sleep Current", "current", "mA", 0.01, 0.40),
    ("ACTIVE_SUPPLY_CURRENT", "Active Supply Current", "current", "mA", 80, 250),
    ("WAKEUP_TIME", "Wake-up Time", "time", "ms", None, 150),
    ("CAN_CYCLE_TIME", "CAN Cycle Time", "time", "ms", 9.5, 10.5),
    ("UNDERVOLTAGE_RESET", "Undervoltage Reset", "voltage", "V", 5.5, 6.5),
]


def test_catalog_has_exactly_the_five_spec_entries():
    assert [
        (s.key, s.test_name, s.quantity, s.unit, s.limit_min, s.limit_max) for s in CATALOG
    ] == EXPECTED


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.key)
def test_every_entry_has_a_valid_limit_shape(spec):
    assert spec.limit_min is not None or spec.limit_max is not None
    if spec.limit_min is not None and spec.limit_max is not None:
        assert spec.limit_min < spec.limit_max


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.key)
def test_every_entry_is_two_sided_or_maximum_only(spec):
    # The generator supports only these two shapes (spec §8.3.1).
    assert spec.limit_max is not None


def test_wakeup_time_is_maximum_only():
    assert WAKEUP_TIME.limit_min is None
    assert WAKEUP_TIME.limit_max == 150


def test_entries_are_immutable():
    with pytest.raises(FrozenInstanceError):
        CATALOG[0].limit_max = 1.0


def test_catalog_is_an_immutable_tuple():
    assert isinstance(CATALOG, tuple)
    assert all(isinstance(spec, TestSpec) for spec in CATALOG)
