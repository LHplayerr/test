"""CIP process simulation.

Refactored from the original standalone simulator to conform to
BaseSimulation. The physics in _tick_locked is unchanged. What changed:

- Commands and measurements are declared as named schema entries instead
  of Modbus-shaped holding/input registers.
- Snapshot returns {"measurements": {...}, "status": {...}} as the plugin
  contract requires.
- Read-only UI hints (branch flows, pipe states) are exposed as status
  booleans. The dashboard never guesses physics.
- ResetFaultCmd is a pulse command. The adapter handles clear-on-next-tick;
  the plugin applies the reset immediately when write_command is called.

The internal lock protects all mutable state. step() and snapshot() may be
called from different threads (asyncio loop and HTTP thread respectively),
so every public method takes the lock.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from sim_runtime.base import (
    BaseSimulation,
    CommandSpec,
    MeasurementSpec,
    SimulationConfig,
    StatusSpec,
)


class CIPSimulation(BaseSimulation):
    SIMULATION_ID = "cip"
    SIMULATION_NAME = "Clean-In-Place"
    SIMULATION_VERSION = "2.0.0"

    # --- schema ---

    @classmethod
    def commands(cls) -> dict[str, CommandSpec]:
        return {
            "PumpCmd": CommandSpec(
                name="PumpCmd", type="bool",
                description="Run the circulation pump.",
            ),
            "WaterValveCmd": CommandSpec(
                name="WaterValveCmd", type="bool",
                description="Open the water supply valve.",
            ),
            "CausticValveCmd": CommandSpec(
                name="CausticValveCmd", type="bool",
                description="Open the caustic supply valve.",
            ),
            "AcidValveCmd": CommandSpec(
                name="AcidValveCmd", type="bool",
                description="Open the acid supply valve.",
            ),
            "ReturnValveCmd": CommandSpec(
                name="ReturnValveCmd", type="bool",
                description="Open the return (recovery) valve.",
            ),
            "DrainValveCmd": CommandSpec(
                name="DrainValveCmd", type="bool",
                description="Open the drain valve.",
            ),
            "HeaterCmd": CommandSpec(
                name="HeaterCmd", type="bool",
                description="Run the caustic tank heater.",
            ),
            "ResetFaultCmd": CommandSpec(
                name="ResetFaultCmd", type="bool",
                description="Pulse to clear the active fault word.",
                pulse=True,
            ),
            "CausticTempSetpointC": CommandSpec(
                name="CausticTempSetpointC", type="float",
                description="Caustic tank temperature setpoint.",
                min=20.0, max=80.0, default=45.0,
            ),
        }

    @classmethod
    def measurements(cls) -> dict[str, MeasurementSpec]:
        return {
            "LevelPercent": MeasurementSpec(
                name="LevelPercent", type="float", unit="%",
                description="Wash tank fill level.",
            ),
            "VolumeLiters": MeasurementSpec(
                name="VolumeLiters", type="float", unit="L",
                description="Total liquid volume in the tank.",
            ),
            "FlowLpm": MeasurementSpec(
                name="FlowLpm", type="float", unit="L/min",
                description="Current circulation or drain flow.",
            ),
            "TemperatureC": MeasurementSpec(
                name="TemperatureC", type="float", unit="deg C",
                description="Process loop temperature.",
            ),
            "Conductivity": MeasurementSpec(
                name="Conductivity", type="float", unit="sim units",
                description="Simulated conductivity (proxy for chemistry).",
            ),
            "CausticTankTempC": MeasurementSpec(
                name="CausticTankTempC", type="float", unit="deg C",
                description="Caustic supply tank temperature.",
            ),
        }

    @classmethod
    def status(cls) -> dict[str, StatusSpec]:
        specs = {
            "LiquidCode": StatusSpec("LiquidCode", "uint16",
                "0=Empty, 1=Water, 2=Caustic, 3=Acid, 4=Mixture."),
            "LiquidName": StatusSpec("LiquidName", "string",
                "Human-readable liquid name."),
            "RouteCode": StatusSpec("RouteCode", "uint16",
                "0=Closed, 1=Return, 2=Drain, 3=Return+Drain."),
            "RouteName": StatusSpec("RouteName", "string",
                "Human-readable route name."),
            "FaultWord": StatusSpec("FaultWord", "uint16",
                "Active faults as a bitfield."),
            "StatusWord": StatusSpec("StatusWord", "uint16",
                "Process status bits."),
            "SequenceHint": StatusSpec("SequenceHint", "string",
                "Best-guess name of the current sequence."),
        }
        # Branch flow flags consumed by the dashboard to animate pipes.
        # Exposing these keeps the physics on the server; the UI never
        # recomputes what it thinks should be flowing.
        for branch in (
            "waterInlet", "causticInlet", "acidInlet",
            "pumpRunning", "returnFlow", "drainFlow",
            "gravityDrain", "drainBlocked", "chemicalRecovery",
        ):
            specs[branch] = StatusSpec(branch, "bool",
                f"UI hint: {branch} is active.")
        return specs

    # --- lifecycle ---

    def __init__(self, config: SimulationConfig) -> None:
        self._lock = threading.Lock()

        # Commands (kept as plain attributes; the lock is the sync point)
        self._pump = False
        self._water_valve = False
        self._caustic_valve = False
        self._acid_valve = False
        self._return_valve = False
        self._drain_valve = False
        self._heater = False
        self._setpoint_c = 45.0

        # Physics state
        self._ambient_temp = 20.0
        self._water_supply_temp_c = 20.0
        self._temperature_c = 20.0
        self._caustic_tank_temp_c = 20.0
        self._water_l = 0.0
        self._caustic_l = 0.0
        self._acid_l = 0.0
        self._flow_lpm = 0.0
        self._conductivity = 0.0
        self._liquid_code = 0
        self._fault_word = 0
        self._status_word = 0
        self._route_code = 0
        self._sequence_hint = "Idle"

        # Optional initial state from config
        params = config.params or {}
        self._water_l = float(params.get("initial_water_l", 0.0))
        self._caustic_l = float(params.get("initial_caustic_l", 0.0))
        self._acid_l = float(params.get("initial_acid_l", 0.0))
        self._temperature_c = float(params.get("initial_temperature_c", 20.0))

        self._last_tick = time.monotonic()
        self._start_time = self._last_tick
        self._branches: dict[str, bool] = {}

    # --- step and snapshot ---

    def step(self, dt: float) -> None:
        with self._lock:
            self._tick_locked()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self._total_volume()
            fractions = self._fractions_locked()
            return {
                "measurements": {
                    "LevelPercent": round(total, 1),
                    "VolumeLiters": round(total, 1),
                    "FlowLpm": round(self._flow_lpm, 1),
                    "TemperatureC": round(self._temperature_c, 1),
                    "Conductivity": round(self._conductivity, 1),
                    "CausticTankTempC": round(self._caustic_tank_temp_c, 1),
                },
                "status": {
                    "LiquidCode": self._liquid_code,
                    "LiquidName": self._liquid_name(self._liquid_code),
                    "RouteCode": self._route_code,
                    "RouteName": self._route_name(self._route_code),
                    "FaultWord": self._fault_word,
                    "StatusWord": self._status_word,
                    "SequenceHint": self._sequence_hint,
                    **{k: bool(v) for k, v in self._branches.items()},
                },
            }

    # --- command dispatch ---

    def write_command(self, name: str, value: Any) -> None:
        with self._lock:
            if name == "PumpCmd":
                self._pump = bool(value)
            elif name == "WaterValveCmd":
                self._water_valve = bool(value)
            elif name == "CausticValveCmd":
                self._caustic_valve = bool(value)
            elif name == "AcidValveCmd":
                self._acid_valve = bool(value)
            elif name == "ReturnValveCmd":
                self._return_valve = bool(value)
            elif name == "DrainValveCmd":
                self._drain_valve = bool(value)
            elif name == "HeaterCmd":
                self._heater = bool(value)
            elif name == "CausticTempSetpointC":
                self._setpoint_c = float(value)
            elif name == "ResetFaultCmd":
                if value:
                    self._fault_word = 0
            else:
                raise ValueError(f"Unknown command: {name}")
            # Advance so the effect is visible in the next snapshot even
            # if step() hasn't fired yet.
            self._tick_locked()

    # --- physics (unchanged from original, just renamed internals) ---

    def _tick_locked(self) -> None:
        now = time.monotonic()
        dt = min(max(now - self._last_tick, 0.0), 1.0)
        if dt <= 0.0:
            return
        self._last_tick = now

        caustic_temp_setpoint_c = max(20.0, min(self._setpoint_c, 80.0))
        pump_cmd = self._pump
        water_cmd = self._water_valve
        caustic_cmd = self._caustic_valve
        acid_cmd = self._acid_valve
        return_cmd = self._return_valve
        drain_cmd = self._drain_valve
        heater_cmd = self._heater
        source_count = sum((water_cmd, caustic_cmd, acid_cmd))
        total_before = self._total_volume()

        fractions = self._fractions_locked()
        conductivity_preview = (
            0.0
            if total_before < 0.05
            else 0.5 * fractions["water"]
                 + 220.0 * fractions["caustic"]
                 + 160.0 * fractions["acid"]
        )
        chemical_in_tank = conductivity_preview >= 25.0

        fault_word = 0
        if pump_cmd and total_before < 1.0 and source_count == 0:
            fault_word |= 1 << 0
        if pump_cmd and not return_cmd and not drain_cmd:
            fault_word |= 1 << 1
        if source_count > 1:
            fault_word |= 1 << 2
        if self._temperature_c > 85.0:
            fault_word |= 1 << 3
        if drain_cmd and chemical_in_tank:
            fault_word |= 1 << 4
        self._fault_word = fault_word

        blocked = self._fault_word != 0
        pump_running = pump_cmd and not blocked
        inlet_boost = 1.5 if pump_running else 1.0

        if not blocked and source_count == 1:
            if water_cmd:
                self._water_l += 5.0 * inlet_boost * dt
            elif caustic_cmd:
                self._caustic_l += 2.5 * inlet_boost * dt
            elif acid_cmd:
                self._acid_l += 2.5 * inlet_boost * dt

        self._cap_volume_locked(100.0)
        self._route_code = (
            3 if return_cmd and drain_cmd
            else 1 if return_cmd
            else 2 if drain_cmd
            else 0
        )

        if heater_cmd and not blocked:
            heat_target = min(80.0, max(caustic_temp_setpoint_c + 5.0, 35.0))
            self._caustic_tank_temp_c += (
                (heat_target - self._caustic_tank_temp_c) * 0.22 * dt
            )
        else:
            self._caustic_tank_temp_c += (
                (self._ambient_temp - self._caustic_tank_temp_c) * 0.05 * dt
            )
        self._caustic_tank_temp_c = min(
            max(self._caustic_tank_temp_c, 15.0), 90.0
        )

        total = self._total_volume()
        fractions = self._fractions_locked()
        dominant_name = (
            max(fractions, key=fractions.get) if total > 0.0 else "water"
        )
        is_chemical_recovery = (
            return_cmd and pump_running and total > 0.5
            and dominant_name in ("caustic", "acid")
        )
        drain_blocked_by_chemicals = drain_cmd and chemical_in_tank

        if drain_cmd and not drain_blocked_by_chemicals:
            drain_rate = (
                7.0 if pump_running and not return_cmd
                else 2.5 if pump_running
                else 1.5
            )
            self._remove_volume_locked(drain_rate * dt)
        elif is_chemical_recovery:
            self._remove_volume_locked(6.0 * dt)

        total = self._total_volume()
        route_open = return_cmd or drain_cmd
        medium_available = total > 0.5 or source_count > 0
        if pump_running and route_open and medium_available:
            if return_cmd and drain_cmd:
                self._flow_lpm = 14.0
            elif is_chemical_recovery:
                self._flow_lpm = 11.0
            elif drain_blocked_by_chemicals:
                self._flow_lpm = 0.0
            elif drain_cmd:
                self._flow_lpm = 10.0
            else:
                self._flow_lpm = 12.0
        else:
            self._flow_lpm = 0.0

        source_temp = self._ambient_temp
        mix_gain = 0.10
        if water_cmd:
            source_temp = self._water_supply_temp_c
            mix_gain = 0.30
        elif caustic_cmd:
            source_temp = self._caustic_tank_temp_c
            mix_gain = 0.24
        elif acid_cmd:
            source_temp = self._ambient_temp
            mix_gain = 0.24
        elif pump_running and return_cmd:
            source_temp = self._temperature_c
            mix_gain = 0.02
        self._temperature_c += (
            (source_temp - self._temperature_c) * mix_gain * dt
        )
        self._temperature_c += (
            (self._ambient_temp - self._temperature_c) * 0.03 * dt
        )
        self._temperature_c = min(max(self._temperature_c, 15.0), 90.0)

        fractions = self._fractions_locked()
        self._conductivity = (
            0.0
            if total < 0.05
            else 0.5 * fractions["water"]
                 + 220.0 * fractions["caustic"]
                 + 160.0 * fractions["acid"]
        )
        self._liquid_code = self._compute_liquid_code_locked(fractions, total)
        self._sequence_hint = self._infer_sequence_hint_locked()

        status_word = 0
        if pump_running:
            status_word |= 1 << 0
        if source_count > 0:
            status_word |= 1 << 1
        if return_cmd:
            status_word |= 1 << 2
        if drain_cmd:
            status_word |= 1 << 3
        if self._flow_lpm > 0.1:
            status_word |= 1 << 4
        if heater_cmd and not blocked:
            status_word |= 1 << 5
        if total > 0.5:
            status_word |= 1 << 6
        if self._fault_word != 0:
            status_word |= 1 << 7
        self._status_word = status_word

        self._branches = {
            "waterInlet": bool(water_cmd and source_count == 1 and not blocked),
            "causticInlet": bool(caustic_cmd and source_count == 1 and not blocked),
            "acidInlet": bool(acid_cmd and source_count == 1 and not blocked),
            "pumpRunning": bool(pump_running),
            "returnFlow": bool(return_cmd and pump_running and self._flow_lpm > 0.1),
            "drainFlow": bool(
                drain_cmd and pump_running and self._flow_lpm > 0.1
                and not drain_blocked_by_chemicals
            ),
            "gravityDrain": bool(
                drain_cmd and (not pump_running)
                and (not drain_blocked_by_chemicals) and total > 0.5
            ),
            "drainBlocked": bool(drain_blocked_by_chemicals),
            "chemicalRecovery": bool(is_chemical_recovery),
        }

    # --- helpers (unchanged) ---

    def _fractions_locked(self) -> dict[str, float]:
        total = self._total_volume()
        if total <= 0.0:
            return {"water": 0.0, "caustic": 0.0, "acid": 0.0}
        return {
            "water": self._water_l / total,
            "caustic": self._caustic_l / total,
            "acid": self._acid_l / total,
        }

    def _compute_liquid_code_locked(
        self, fractions: dict[str, float], total: float
    ) -> int:
        if total < 0.05:
            return 0
        dominant_name = max(fractions, key=fractions.get)
        dominant = fractions[dominant_name]
        if dominant < 0.75:
            return 4
        return {"water": 1, "caustic": 2, "acid": 3}[dominant_name]

    def _infer_sequence_hint_locked(self) -> str:
        if self._fault_word:
            return "Fault"
        if self._caustic_valve and self._return_valve and self._pump:
            return "Caustic Return"
        if self._acid_valve and self._return_valve and self._pump:
            return "Acid Return"
        if self._caustic_valve:
            return "Caustic Wash"
        if self._acid_valve:
            return "Acid Wash"
        if self._water_valve and self._drain_valve:
            return "Rinse To Drain"
        if self._water_valve and self._return_valve:
            return "Recirculating Rinse"
        if self._pump and self._return_valve:
            return "Circulation"
        if self._drain_valve:
            return "Drain"
        return "Idle"

    def _remove_volume_locked(self, remove_liters: float) -> None:
        total = self._total_volume()
        if total <= 0.0:
            return
        amount = min(remove_liters, total)
        remaining_ratio = (total - amount) / total
        self._water_l *= remaining_ratio
        self._caustic_l *= remaining_ratio
        self._acid_l *= remaining_ratio

    def _cap_volume_locked(self, max_volume: float) -> None:
        total = self._total_volume()
        if total <= max_volume:
            return
        scale = max_volume / total
        self._water_l *= scale
        self._caustic_l *= scale
        self._acid_l *= scale

    def _total_volume(self) -> float:
        return self._water_l + self._caustic_l + self._acid_l

    @staticmethod
    def _liquid_name(code: int) -> str:
        return {0: "Empty", 1: "Water", 2: "Caustic",
                3: "Acid", 4: "Mixture"}.get(code, "Unknown")

    @staticmethod
    def _route_name(code: int) -> str:
        return {0: "Closed", 1: "Return", 2: "Drain",
                3: "Return + Drain"}.get(code, "Unknown")