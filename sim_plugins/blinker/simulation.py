"""Blinker: the minimal plugin. Proves the contract.

A light that blinks on a configurable period. No physics, no state beyond
a phase counter and a cycle count. Its entire job is to be the simplest
possible plugin, so any awkwardness in BaseSimulation surfaces here rather
than inside CIP.
"""

from __future__ import annotations

from typing import Any

from sim_runtime.base import (
    BaseSimulation,
    CommandSpec,
    MeasurementSpec,
    SimulationConfig,
    StatusSpec,
)


class Blinker(BaseSimulation):
    SIMULATION_ID = "blinker"
    SIMULATION_NAME = "Blinking Light"
    SIMULATION_VERSION = "1.0.0"

    def __init__(self, config: SimulationConfig) -> None:
        self._enabled = bool(config.params.get("enabled", False))
        self._period = float(config.params.get("period_seconds", 1.0))
        self._phase = 0.0
        self._blink_count = 0

    # -- schema -----------------------------------------------------------

    @classmethod
    def commands(cls) -> dict[str, CommandSpec]:
        return {
            "Enabled": CommandSpec(
                name="Enabled",
                type="bool",
                description="Start or stop the blink.",
                default=False,
            ),
            "PeriodSeconds": CommandSpec(
                name="PeriodSeconds",
                type="float",
                description="Blink period in seconds.",
                min=0.1,
                max=10.0,
                default=1.0,
            ),
            "ResetCountCmd": CommandSpec(
                name="ResetCountCmd",
                type="bool",
                description="Pulse to reset the blink counter to zero.",
                pulse=True,
                default=False,
            ),
        }

    @classmethod
    def measurements(cls) -> dict[str, MeasurementSpec]:
        return {
            "On": MeasurementSpec(
                name="On",
                type="bool",
                unit="",
                description="True while the light is in the on phase.",
            ),
            "Phase": MeasurementSpec(
                name="Phase",
                type="float",
                unit="s",
                description="Position within the current blink period.",
            ),
        }

    @classmethod
    def status(cls) -> dict[str, StatusSpec]:
        return {
            "State": StatusSpec(
                name="State",
                type="string",
                description="Human-readable state: Off, On, or Idle.",
            ),
            "BlinkCount": StatusSpec(
                name="BlinkCount",
                type="uint16",
                description="Completed blink cycles since the last reset.",
            ),
        }

    # -- physics ----------------------------------------------------------

    def step(self, dt: float) -> None:
        if not self._enabled:
            return

        self._phase += dt

        # A while loop rather than a single subtraction so a large dt
        # cannot silently swallow periods. At PHYSICS_HZ this never loops
        # more than once, but it costs nothing to be correct.
        while self._phase >= self._period:
            self._phase -= self._period
            self._blink_count += 1

    # -- observation ------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        on = self._enabled and self._phase < self._period / 2
        state = "On" if on else ("Off" if self._enabled else "Idle")
        return {
            "measurements": {
                "On": on,
                "Phase": round(self._phase, 3) if self._enabled else 0.0,
            },
            "status": {
                "State": state,
                # uint16 range guard so the OPC UA write never overflows.
                "BlinkCount": min(self._blink_count, 65535),
            },
        }

    # -- commands ---------------------------------------------------------

    def write_command(self, name: str, value: Any) -> None:
        if name == "Enabled":
            self._enabled = bool(value)
            if not self._enabled:
                self._phase = 0.0
            return

        if name == "PeriodSeconds":
            self._period = float(value)
            return

        if name == "ResetCountCmd":
            # The adapter treats pulse commands as True-then-cleared, so
            # we only act when the value is truthy.
            if value:
                self._blink_count = 0
                self._phase = 0.0
            return

        raise ValueError(f"Unknown command: {name}")