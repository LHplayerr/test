"""Plugin contract for simulations.

A simulation is a pure-Python class implementing BaseSimulation. It has no
I/O, no async, no Django imports. The runtime adapts it to OPC UA and HTTP;
the plugin never knows those exist.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

CommandType = Literal["bool", "int", "float", "string"]
MeasurementType = Literal["bool", "int", "float", "string"]


@dataclass(frozen=True)
class CommandSpec:
    """One writable command exposed on the OPC UA server."""

    name: str
    type: CommandType
    description: str
    min: float | None = None        # numeric types only
    max: float | None = None        # numeric types only
    pulse: bool = False             # self-clearing, e.g. ResetFaultCmd
    default: Any = None


@dataclass(frozen=True)
class MeasurementSpec:
    """One read-only measurement exposed on the OPC UA server."""

    name: str
    type: MeasurementType
    unit: str
    description: str


@dataclass(frozen=True)
class StatusSpec:
    """One read-only status value (codes, strings, bitfield words)."""

    name: str
    type: MeasurementType
    description: str


@dataclass
class SimulationConfig:
    """Per-session configuration. Plugin-specific fields go in `params`."""

    session_id: str
    params: dict[str, Any] = field(default_factory=dict)


class BaseSimulation(ABC):
    """Contract every simulation implements.

    Design notes:
    - Schema is class-level so the OPC UA tree can be built before any
      instance exists.
    - step() is synchronous and must be fast. The runtime calls it at
      PHYSICS_HZ; blocking here stalls the whole session.
    - snapshot() returns a dict keyed by names declared in measurements()
      and status(). The adapter handles OPC UA and JSON serialization.
    - write_command() may raise ValueError; the adapter translates that
      into BadOutOfRange for the OPC UA client.
    """

    SIMULATION_ID: ClassVar[str]        # unique, lowercase, no spaces
    SIMULATION_NAME: ClassVar[str]      # human label
    SIMULATION_VERSION: ClassVar[str]   # semantic, e.g. "1.0.0"

    @classmethod
    @abstractmethod
    def commands(cls) -> dict[str, CommandSpec]: ...

    @classmethod
    @abstractmethod
    def measurements(cls) -> dict[str, MeasurementSpec]: ...

    @classmethod
    def status(cls) -> dict[str, StatusSpec]:
        """Optional. Override to expose status values."""
        return {}

    @abstractmethod
    def __init__(self, config: SimulationConfig) -> None: ...

    @abstractmethod
    def step(self, dt: float) -> None: ...

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return {"measurements": {...}, "status": {...}}.

        Keys must match the names declared in measurements() and status().
        Called on every OPC UA update and every HTTP poll.
        """

    @abstractmethod
    def write_command(self, name: str, value: Any) -> None: ...

    def on_client_activity(self) -> None:
        """Optional hook. Called when an external OPC UA read or write occurs.

        Default implementation does nothing. Simulations that want to react
        to being observed (or that need to suppress idle timeout) override.
        """