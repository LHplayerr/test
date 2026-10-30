"""Schema-driven OPC UA server adapter.

Builds an OPC UA address space from a plugin's declared commands,
measurements, and status values. Knows nothing about any specific
simulation — reads the schema, wires up nodes, and translates between
OPC UA and the plugin's pure-Python interface.

Design notes:
- One adapter per running session. The adapter owns the Server instance;
  the caller owns the asyncio event loop and the stop signal.
- Command writes are validated by the adapter (type + range) and then
  applied via BaseSimulation.write_command(). A ValueError from the plugin
  is translated to BadOutOfRange for the client.
- Pulse commands (spec.pulse=True) are auto-cleared on the next tick.
- External reads and writes are observed via asyncua's PostRead/PostWrite
  callbacks, which feed idle detection in the worker runtime.
- Browse names are qualified in the adapter's own namespace. Passing a
  plain string to add_object/add_variable/add_folder would place the
  BrowseName in ns=0 (the OPC UA base namespace), which makes client-side
  path lookups like `2:Commands` fail with BadNoMatch.
- Bind address and advertised address are separate. asyncua's
  set_endpoint() conflates the URL a server binds to with the URL it
  advertises. We bind to `bind_host` (usually 0.0.0.0 or 127.0.0.1) and
  then rewrite the advertised URLs in the endpoint list after init() so
  discovery responses carry `advertise_host`.
- run() accepts an optional on_ready callback that fires after the
  asyncua server context has been entered (i.e. after the port is
  bound). This lets the caller publish a "ready" signal only once the
  server is actually accepting connections, avoiding a race where a
  client connects before bind completes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from asyncua import Server, ua
from asyncua.common.callback import CallbackType
from asyncua.common.utils import ServiceError

from shared.constants import PHYSICS_HZ
from shared.logging import get_logger
from sim_runtime.base import BaseSimulation, CommandSpec


# ---------------------------------------------------------------------------
# Type mapping: plugin type name -> OPC UA variant type
# ---------------------------------------------------------------------------

_VARIANT_TYPES: dict[str, ua.VariantType] = {
    "bool": ua.VariantType.Boolean,
    "int": ua.VariantType.Int32,
    "uint": ua.VariantType.UInt32,
    "int16": ua.VariantType.Int16,
    "uint16": ua.VariantType.UInt16,
    "float": ua.VariantType.Double,
    "string": ua.VariantType.String,
}

_ZERO_VALUES: dict[str, Any] = {
    "bool": False,
    "int": 0,
    "uint": 0,
    "int16": 0,
    "uint16": 0,
    "float": 0.0,
    "string": "",
}


def _ua_type(type_name: str) -> ua.VariantType:
    try:
        return _VARIANT_TYPES[type_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported OPC UA type: {type_name!r}") from exc


def _zero_for(type_name: str) -> Any:
    try:
        return _ZERO_VALUES[type_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported type for default: {type_name!r}") from exc


def _coerce(value: Any, type_name: str) -> Any:
    """Coerce a snapshot value to the Python type matching the OPC UA type."""
    if type_name == "bool":
        return bool(value)
    if type_name in ("int", "int16"):
        return int(value)
    if type_name in ("uint", "uint16"):
        return max(0, int(value))
    if type_name == "float":
        return float(value)
    if type_name == "string":
        return str(value)
    raise ValueError(f"Unsupported type: {type_name!r}")


def _validate_command_value(raw: Any, spec: CommandSpec) -> Any:
    """Validate and normalise an incoming command value.

    Raises ServiceError with an appropriate OPC UA status code on any
    type or range mismatch. Returns the normalised value on success.
    """
    if spec.type == "bool":
        if not isinstance(raw, bool):
            raise ServiceError(ua.StatusCodes.BadTypeMismatch)
        return raw

    if spec.type in ("int", "int16", "uint", "uint16"):
        # Reject bool (subclass of int in Python) and floats.
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ServiceError(ua.StatusCodes.BadTypeMismatch)
        value = int(raw)

    elif spec.type == "float":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ServiceError(ua.StatusCodes.BadTypeMismatch)
        value = float(raw)

    elif spec.type == "string":
        if not isinstance(raw, str):
            raise ServiceError(ua.StatusCodes.BadTypeMismatch)
        return raw

    else:
        raise ServiceError(ua.StatusCodes.BadTypeMismatch)

    # Range checks apply to numeric types only.
    if spec.min is not None and value < spec.min:
        raise ServiceError(ua.StatusCodes.BadOutOfRange)
    if spec.max is not None and value > spec.max:
        raise ServiceError(ua.StatusCodes.BadOutOfRange)
    if spec.type in ("uint", "uint16") and value < 0:
        raise ServiceError(ua.StatusCodes.BadOutOfRange)

    return value


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class OPCUAAdapter:
    """Wraps a BaseSimulation in an OPC UA server driven by its schema."""

    def __init__(
        self,
        sim: BaseSimulation,
        *,
        advertise_host: str,
        port: int,
        bind_host: str = "0.0.0.0",
        server_name: str | None = None,
    ) -> None:
        self._sim = sim
        self._sim_id = sim.SIMULATION_ID
        self._advertise_host = advertise_host
        self._bind_host = bind_host
        self._port = port

        # The URL that goes into discovery responses and gets shown to
        # clients. This is what CODESYS or the gateway should use.
        self._endpoint = f"opc.tcp://{advertise_host}:{port}/{self._sim_id}/"

        # The URL asyncua actually binds the TCP socket to. Usually
        # 0.0.0.0 (all interfaces) or 127.0.0.1 (loopback only). We
        # rewrite the advertised URLs in the endpoint list after init()
        # so clients see _endpoint, not this.
        self._bind_endpoint = f"opc.tcp://{bind_host}:{port}/{self._sim_id}/"

        self._namespace_uri = f"urn:cip-sim:{self._sim_id}:{sim.SIMULATION_VERSION}"

        self._server = Server()
        self._server_name = server_name or f"{sim.SIMULATION_NAME} simulator"
        self._namespace_idx: int = 0

        # Node references kept for the update loop.
        self._command_nodes: dict[str, ua.Node] = {}
        self._measurement_nodes: dict[str, ua.Node] = {}
        self._status_nodes: dict[str, ua.Node] = {}

        # Pulse commands written since the last tick, to be cleared next tick.
        self._pulses_pending: set[str] = set()

        # External activity tracking (PostRead / PostWrite).
        self._last_external_activity: float | None = None
        self._external_request_count: int = 0

        self._log = get_logger(
            __name__, simulation_id=self._sim_id, port=port
        )

    # ---- public API ----

    @property
    def endpoint(self) -> str:
        """The advertised endpoint URL (what clients should use)."""
        return self._endpoint

    def last_external_activity(self) -> float | None:
        """Monotonic timestamp of the last external client read or write.

        None if no external client has touched this session yet.
        """
        return self._last_external_activity

    def external_request_count(self) -> int:
        return self._external_request_count

    async def initialize(self) -> None:
        """Build the address space. Called once before run()."""
        await self._server.init()

        # Bind to the bind_host interface. asyncua will use this URL for
        # both binding and advertising; we override the advertised URLs
        # immediately after init() below.
        self._server.set_endpoint(self._bind_endpoint)
        self._server.set_server_name(self._server_name)
        await self._server.set_application_uri(f"urn:cip-sim:server:{self._sim_id}")

        # v1: trusted intranet. No transport security, anonymous clients.
        # v2 direction: Basic256Sha256_SignAndEncrypt with per-session X.509
        # certificates and a trust list. See plan section on OPC UA security.
        self._server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self._server.set_identity_tokens([ua.AnonymousIdentityToken])

        self._namespace_idx = await self._server.register_namespace(
            self._namespace_uri
        )

        # If bind_host differs from advertise_host, rewrite the URL that
        # asyncua will report in GetEndpoints and in node metadata.
        if self._bind_host != self._advertise_host:
            self._rewrite_advertised_urls()

        ns = self._namespace_idx

        root = await self._server.nodes.objects.add_object(
            ua.NodeId(self._sim_id, ns),
            ua.QualifiedName(self._sim.SIMULATION_NAME, ns),
        )

        # Self-describing metadata. Consumers (the gateway, discovery tools,
        # CODESYS browsing) read these instead of relying on hardcoded maps.
        # Add a new plugin and these properties describe it automatically.
        await self._add_metadata_property(root, "SimulationId", self._sim_id)
        await self._add_metadata_property(
            root, "SimulationName", self._sim.SIMULATION_NAME
        )
        await self._add_metadata_property(
            root, "SimulationVersion", self._sim.SIMULATION_VERSION
        )
        await self._add_metadata_property(
            root, "NamespaceUri", self._namespace_uri
        )

        commands_folder = await root.add_folder(
            ua.NodeId(f"{self._sim_id}.Commands", ns),
            ua.QualifiedName("Commands", ns),
        )
        measurements_folder = await root.add_folder(
            ua.NodeId(f"{self._sim_id}.Measurements", ns),
            ua.QualifiedName("Measurements", ns),
        )
        status_folder = await root.add_folder(
            ua.NodeId(f"{self._sim_id}.Status", ns),
            ua.QualifiedName("Status", ns),
        )

        await self._build_commands(commands_folder)
        await self._build_measurements(measurements_folder)
        await self._build_status(status_folder)

        self._install_activity_callbacks()

        self._log.info(
            "OPC UA address space built",
            extra={
                "endpoint": self._endpoint,
                "bind": self._bind_endpoint,
                "namespace": self._namespace_uri,
            },
        )

    async def run(
        self,
        stop: asyncio.Event,
        *,
        on_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Run the server until `stop` is set.

        Enters the asyncua server context (starts accepting connections),
        then loops at PHYSICS_HZ: step the simulation, push values to OPC UA,
        await the stop event with a short timeout.

        If `on_ready` is provided, it is awaited exactly once, immediately
        after the server context is entered and the port is bound. Use it
        to publish a "session ready" signal without racing against the
        bind.
        """
        tick = 1.0 / PHYSICS_HZ
        last_tick = time.monotonic()

        async with self._server:
            # The socket is now bound; connections will be accepted.
            # Fire the ready callback before doing anything else so the
            # caller knows the endpoint is live.
            if on_ready is not None:
                try:
                    await on_ready()
                except Exception:
                    self._log.exception("on_ready callback raised")

            self._log.info("OPC UA server running", extra={"endpoint": self._endpoint})

            while not stop.is_set():
                now = time.monotonic()
                dt = now - last_tick
                last_tick = now

                try:
                    self._sim.step(dt)
                except Exception:
                    self._log.exception("simulation step raised")

                try:
                    await self._push_values()
                except Exception:
                    self._log.exception("failed to push values to OPC UA")

                try:
                    await asyncio.wait_for(stop.wait(), timeout=tick)
                except asyncio.TimeoutError:
                    pass

            self._log.info("OPC UA server stopping")

    # ---- advertised URL rewriting ----

    def _rewrite_advertised_urls(self) -> None:
        """Rewrite the endpoint URLs asyncua reports to clients.

        asyncua builds the endpoint description list during init() from
        the value passed to set_endpoint(). We bound the socket to
        bind_host, but clients need to see advertise_host in the
        discovery response, otherwise they'll try to connect to
        0.0.0.0:port (invalid) or 127.0.0.1:port (wrong machine).

        This mutates the endpoint list in place. It is called once,
        immediately after init(), before the server starts accepting
        connections.
        """
        try:
            endpoints = self._server.endpoints
        except AttributeError:
            self._log.warning(
                "asyncua server has no .endpoints list; advertised URLs "
                "will use the bind host. Advertise may be wrong."
            )
            return

        if not endpoints:
            self._log.warning(
                "asyncua endpoints list is empty; skipping URL rewrite"
            )
            return

        rewrite_from = f"opc.tcp://{self._bind_host}:{self._port}"
        for ep in endpoints:
            try:
                url = ep.EndpointUrl
                if url.startswith(rewrite_from):
                    ep.EndpointUrl = (
                        f"opc.tcp://{self._advertise_host}:{self._port}"
                        + url[len(rewrite_from):]
                    )
                else:
                    # Unexpected URL shape; fall back to the full
                    # advertised endpoint, keeping the path empty.
                    ep.EndpointUrl = self._endpoint
            except Exception:
                self._log.exception("failed to rewrite endpoint URL")

        self._log.info(
            "advertised endpoint URLs rewritten",
            extra={
                "bind": self._bind_endpoint,
                "advertise": self._endpoint,
                "count": len(endpoints),
            },
        )

    # ---- address space construction ----

    async def _build_commands(self, folder: ua.Node) -> None:
        ns = self._namespace_idx
        for name, spec in self._sim.commands().items():
            default = spec.default if spec.default is not None else _zero_for(spec.type)
            node = await folder.add_variable(
                ua.NodeId(f"{self._sim_id}.Commands.{name}", ns),
                ua.QualifiedName(name, ns),
                default,
                _ua_type(spec.type),
            )
            await node.write_attribute(
                ua.AttributeIds.Description,
                ua.DataValue(ua.Variant(ua.LocalizedText(spec.description))),
            )
            await node.set_writable()
            self._server.set_attribute_value_setter(
                node.nodeid, self._make_command_setter(spec)
            )
            self._command_nodes[name] = node

        self._log.info(
            "command nodes built",
            extra={"count": len(self._command_nodes), "names": list(self._command_nodes)},
        )

    async def _add_metadata_property(
        self, parent: ua.Node, name: str, value: str
    ) -> None:
        """Add a read-only string property under `parent`.

        Uses asyncua's add_property so the node is typed as a Property
        rather than a Variable. Clients that browse the tree see it as
        metadata, not as a data point.
        """
        ns = self._namespace_idx
        node = await parent.add_property(
            ua.NodeId(f"{self._sim_id}.{name}", ns),
            ua.QualifiedName(name, ns),
            value,
            ua.VariantType.String,
        )
        await node.write_attribute(
            ua.AttributeIds.Description,
            ua.DataValue(ua.Variant(ua.LocalizedText(
                f"Self-describing metadata: {name}"
            ))),
        )

    async def _build_measurements(self, folder: ua.Node) -> None:
        ns = self._namespace_idx
        for name, spec in self._sim.measurements().items():
            description = (
                f"{spec.description} [{spec.unit}]" if spec.unit else spec.description
            )
            node = await folder.add_variable(
                ua.NodeId(f"{self._sim_id}.Measurements.{name}", ns),
                ua.QualifiedName(name, ns),
                _zero_for(spec.type),
                _ua_type(spec.type),
            )
            await node.write_attribute(
                ua.AttributeIds.Description,
                ua.DataValue(ua.Variant(ua.LocalizedText(description))),
            )
            self._measurement_nodes[name] = node

        self._log.info(
            "measurement nodes built",
            extra={"count": len(self._measurement_nodes)},
        )

    async def _build_status(self, folder: ua.Node) -> None:
        ns = self._namespace_idx
        for name, spec in self._sim.status().items():
            node = await folder.add_variable(
                ua.NodeId(f"{self._sim_id}.Status.{name}", ns),
                ua.QualifiedName(name, ns),
                _zero_for(spec.type),
                _ua_type(spec.type),
            )
            await node.write_attribute(
                ua.AttributeIds.Description,
                ua.DataValue(ua.Variant(ua.LocalizedText(spec.description))),
            )
            self._status_nodes[name] = node

        if self._status_nodes:
            self._log.info(
                "status nodes built",
                extra={"count": len(self._status_nodes)},
            )

    # ---- command writes ----

    def _make_command_setter(
        self, spec: CommandSpec
    ) -> Callable[[Any, int, ua.DataValue], None]:
        """Return the value setter asyncua will call when a client writes.

        The setter is synchronous by contract. It must either store the
        accepted value on `node_data.attributes[attribute].value` or raise
        ServiceError. The adapter treats that assignment as the commit point.
        """

        def set_value(node_data, attribute, value: ua.DataValue) -> None:
            if value is None or value.Value is None or value.Value.is_array:
                raise ServiceError(ua.StatusCodes.BadTypeMismatch)
            if value.StatusCode is not None and value.StatusCode.is_bad():
                raise ServiceError(ua.StatusCodes.BadTypeMismatch)

            raw = value.Value.Value
            normalised = _validate_command_value(raw, spec)

            try:
                self._sim.write_command(spec.name, normalised)
            except ValueError as exc:
                self._log.warning(
                    "command rejected by plugin",
                    extra={"command": spec.name, "error": str(exc)},
                )
                raise ServiceError(ua.StatusCodes.BadOutOfRange) from exc

            # Accept the write. asyncua propagates this value to clients.
            node_data.attributes[attribute].value = value

            if spec.pulse:
                self._pulses_pending.add(spec.name)

        return set_value

    # ---- activity observation ----

    def _install_activity_callbacks(self) -> None:
        def on_activity(event, service) -> None:
            if not event.is_external:
                return
            self._last_external_activity = time.monotonic()
            self._external_request_count += 1
            try:
                self._sim.on_client_activity()
            except Exception:
                self._log.exception("on_client_activity hook raised")

        self._server.subscribe_server_callback(CallbackType.PostRead, on_activity)
        self._server.subscribe_server_callback(CallbackType.PostWrite, on_activity)

    # ---- update loop ----

    async def _push_values(self) -> None:
        """Clear pulses, then push every measurement and status to OPC UA."""
        # Pulse commands: reset to default one tick after acceptance so a
        # client can write True again on the next request.
        for name in list(self._pulses_pending):
            node = self._command_nodes.get(name)
            if node is not None:
                try:
                    await node.write_value(False, ua.VariantType.Boolean)
                except Exception:
                    self._log.exception(
                        "pulse clear failed", extra={"command": name}
                    )
            self._pulses_pending.discard(name)

        snap = self._sim.snapshot()
        measurements = snap.get("measurements", {})
        status = snap.get("status", {})

        for name, spec in self._sim.measurements().items():
            value = measurements.get(name)
            if value is None:
                continue
            node = self._measurement_nodes.get(name)
            if node is None:
                continue
            await node.write_value(_coerce(value, spec.type), _ua_type(spec.type))

        for name, spec in self._sim.status().items():
            value = status.get(name)
            if value is None:
                continue
            node = self._status_nodes.get(name)
            if node is None:
                continue
            await node.write_value(_coerce(value, spec.type), _ua_type(spec.type))