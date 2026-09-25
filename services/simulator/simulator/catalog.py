"""Built-in test catalog of the simulated test bench (docs/APP_SPEC.md §8.1).

The bench owns its limits, as simple constants in code. All values are invented.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TestSpec:
    """One test the bench can run. ``None`` means no limit on that side."""

    __test__ = False  # the name starts with "Test": tell pytest this is not a test class

    key: str
    test_name: str
    quantity: str  # used in the output line ("Measured current: ..."), never sent to the API
    unit: str
    limit_min: float | None
    limit_max: float | None


SLEEP_CURRENT = TestSpec("SLEEP_CURRENT", "Sleep Current", "current", "mA", 0.01, 0.40)
ACTIVE_SUPPLY_CURRENT = TestSpec(
    "ACTIVE_SUPPLY_CURRENT", "Active Supply Current", "current", "mA", 80, 250
)
WAKEUP_TIME = TestSpec("WAKEUP_TIME", "Wake-up Time", "time", "ms", None, 150)
CAN_CYCLE_TIME = TestSpec("CAN_CYCLE_TIME", "CAN Cycle Time", "time", "ms", 9.5, 10.5)
UNDERVOLTAGE_RESET = TestSpec("UNDERVOLTAGE_RESET", "Undervoltage Reset", "voltage", "V", 5.5, 6.5)

# A tuple, so the catalog itself cannot be changed at runtime either.
CATALOG: tuple[TestSpec, ...] = (
    SLEEP_CURRENT,
    ACTIVE_SUPPLY_CURRENT,
    WAKEUP_TIME,
    CAN_CYCLE_TIME,
    UNDERVOLTAGE_RESET,
)
