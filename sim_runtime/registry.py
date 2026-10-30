"""Convention-based plugin discovery.

A plugin is a package under `sim_plugins/` whose `simulation.py` defines a
BaseSimulation subclass with a SIMULATION_ID. No decorator, no registry
file to edit — adding a directory is the entire installation procedure.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Iterable

from sim_runtime.base import BaseSimulation


class SimulationNotFound(KeyError):
    """Raised when a requested simulation_id is not in the registry."""


class SimulationRegistry:
    """Maps SIMULATION_ID to plugin class. Populated once per process."""

    def __init__(self) -> None:
        self._by_id: dict[str, type[BaseSimulation]] = {}

    def load_from(self, package: str = "sim_plugins") -> None:
        """Scan a package for plugin modules. Idempotent — safe to call again."""
        try:
            pkg = importlib.import_module(package)
        except ModuleNotFoundError as exc:
            raise RuntimeError(f"Plugin package {package!r} not found") from exc

        for _, mod_name, _ in pkgutil.iter_modules(pkg.__path__):
            module_path = f"{package}.{mod_name}.simulation"
            try:
                mod = importlib.import_module(module_path)
            except ModuleNotFoundError:
                # Not a plugin directory (no simulation.py). Skip quietly.
                continue

            for attr in vars(mod).values():
                if not (isinstance(attr, type) and issubclass(attr, BaseSimulation)):
                    continue
                if attr is BaseSimulation:
                    continue
                sim_id = getattr(attr, "SIMULATION_ID", None)
                if not sim_id:
                    raise RuntimeError(
                        f"{module_path}: {attr.__name__} is missing SIMULATION_ID"
                    )
                if sim_id in self._by_id:
                    other = self._by_id[sim_id].__module__
                    raise RuntimeError(
                        f"Duplicate SIMULATION_ID {sim_id!r}: "
                        f"{module_path} and {other}"
                    )
                self._by_id[sim_id] = attr

    def get(self, sim_id: str) -> type[BaseSimulation]:
        try:
            return self._by_id[sim_id]
        except KeyError as exc:
            raise SimulationNotFound(
                f"Unknown simulation {sim_id!r}. "
                f"Available: {sorted(self._by_id)}"
            ) from exc

    def ids(self) -> Iterable[str]:
        return sorted(self._by_id)

    def is_loaded(self) -> bool:
        return bool(self._by_id)