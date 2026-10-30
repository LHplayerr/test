"""Mirror one worker's OPC UA address space inside the gateway.

Reads the worker's schema once at connect time to build a mirrored
subtree under the session folder. Subscribes to the worker's
measurement and status nodes so gateway values stay current. Command
nodes get a setter that forwards writes back to the worker through the
gateway's write queue.

The worker's root object exposes three self-describing properties that
this mirror reads on connect:

    SimulationId       e.g. "cip"
    SimulationName     e.g. "Clean-In-Place"
    NamespaceUri       e.g. "urn:cip-sim:cip:2.0.0"

Reading these instead of keeping a hardcoded map means adding a new
plugin to the project requires no changes to the gateway.

session_id is the UUID (used for logging and for the gateway's write
queue, which is keyed by UUID). session_name is the slug or UUID and
appears in the OPC UA folder path so CODESYS sees a stable name.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from asyncua import Client, Node, Server, ua

from shared.logging import get_logger


log = get_logger(__name__)


def _coerce_value(value: Any, variant_type: ua.VariantType) -> Any:
    """Coerce a Python value to match a target OPC UA variant type."""
    if variant_type == ua.VariantType.Boolean:
        return bool(value)
    if variant_type in (ua.VariantType.Int16,
                        ua.VariantType.Int32,
                        ua.VariantType.Int64):
        return int(value)
    if variant_type in (ua.VariantType.UInt16,
                        ua.VariantType.UInt32,
                        ua.VariantType.UInt64):
        v = int(value)
        return v if v >= 0 else 0
    if variant_type in (ua.VariantType.Float, ua.VariantType.Double):
        return float(value)
    if variant_type == ua.VariantType.String:
        return str(value)
    return value


def _default_for(variant_type: ua.VariantType) -> Any:
    if variant_type == ua.VariantType.Boolean:
        return False
    if variant_type in (ua.VariantType.Int16, ua.VariantType.Int32,
                        ua.VariantType.Int64):
        return 0
    if variant_type in (ua.VariantType.UInt16, ua.VariantType.UInt32,
                        ua.VariantType.UInt64):
        return 0
    if variant_type in (ua.VariantType.Float, ua.VariantType.Double):
        return 0.0
    if variant_type == ua.VariantType.String:
        return ""
    return None


class SimulationRootNotFound(RuntimeError):
    """Raised when a worker has no node carrying the SimulationId property."""


class WorkerMirror:
    def __init__(
        self,
        *,
        session_id: str,
        session_name: str,
        simulation_id: str,
        worker_endpoint: str,
        parent_folder: Node,
        namespace_idx: int,
        server: Server,
        enqueue_write: Callable[[str, str, Any], None],
    ) -> None:
        self.session_id = session_id          # UUID
        self.session_name = session_name      # slug or UUID
        self.simulation_id = simulation_id
        self.worker_endpoint = worker_endpoint
        self._parent_folder = parent_folder
        self._namespace_idx = namespace_idx
        self._server = server
        self._enqueue_write = enqueue_write

        self._client: Client | None = None
        self._session_folder: Node | None = None
        self._subscription = None

        # Discovered on connect.
        self._worker_ns: int = 0
        self._worker_ns_uri: str = ""
        self._worker_root: Node | None = None

        # worker NodeId (as string) -> (gateway Node, target VariantType)
        self._map: dict[str, tuple[Node, ua.VariantType]] = {}

    # --- lifecycle ---

    async def connect_and_mirror(self) -> None:
        self._client = Client(url=self.worker_endpoint)
        await self._client.connect()
        log.info("connected to worker", extra={
            "session_id": self.session_id,
            "session_name": self.session_name,
            "endpoint": self.worker_endpoint,
        })

        # Discover the simulation root by looking for a node with a
        # SimulationId property. No hardcoded namespace or folder name.
        self._worker_root, self._worker_ns_uri = (
            await self._discover_simulation_root()
        )
        try:
            self._worker_ns = await self._client.get_namespace_index(
                self._worker_ns_uri
            )
        except Exception as exc:
            raise SimulationRootNotFound(
                f"worker namespace {self._worker_ns_uri!r} not registered "
                f"on client side"
            ) from exc

        log.info("discovered simulation root", extra={
            "session_id": self.session_id,
            "session_name": self.session_name,
            "namespace": self._worker_ns_uri,
        })

        ns = self._namespace_idx
        self._session_folder = await self._parent_folder.add_folder(
            ua.NodeId(f"Gateway.Sessions.{self.session_name}", ns),
            ua.QualifiedName(self.session_name, ns),
        )

        for folder_name in ("Commands", "Measurements", "Status"):
            try:
                await self._mirror_folder(folder_name)
            except Exception:
                log.exception("failed to mirror folder",
                              extra={"folder": folder_name,
                                     "session_id": self.session_id,
                                     "session_name": self.session_name})

        await self._start_subscription()

    async def _discover_simulation_root(self) -> tuple[Node, str]:
        """Return (root_node, namespace_uri) by reading a SimulationId property.

        Walks the worker's Objects folder. Any child that has a property
        named SimulationId is treated as a simulation root. Reads the
        NamespaceUri property from the same node.
        """
        if self._client is None:
            raise SimulationRootNotFound("client not connected")

        objects = self._client.nodes.objects
        children = await objects.get_children()
        for child in children:
            try:
                props = await child.get_properties()
            except Exception:
                continue
            prop_map: dict[str, Any] = {}
            for prop in props:
                try:
                    name = (await prop.read_browse_name()).Name
                    value = await prop.read_value()
                    prop_map[name] = value
                except Exception:
                    continue
            if "SimulationId" not in prop_map:
                continue
            ns_uri = prop_map.get("NamespaceUri")
            if not isinstance(ns_uri, str) or not ns_uri:
                raise SimulationRootNotFound(
                    f"root {prop_map['SimulationId']!r} has no NamespaceUri"
                )
            return child, ns_uri

        raise SimulationRootNotFound(
            "no node with SimulationId property found under Objects"
        )

    async def disconnect(self) -> None:
        if self._subscription is not None:
            try:
                await self._subscription.delete()
            except Exception:
                pass
            self._subscription = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    # --- mirroring ---

    async def _mirror_folder(self, folder_name: str) -> None:
        worker_ns = self._worker_ns
        worker_folder = await self._worker_root.get_child(
            [f"{worker_ns}:{folder_name}"]
        )

        ns = self._namespace_idx
        gateway_folder = await self._session_folder.add_folder(
            ua.NodeId(
                f"Gateway.Sessions.{self.session_name}.{folder_name}", ns
            ),
            ua.QualifiedName(folder_name, ns),
        )

        for worker_child in await worker_folder.get_children():
            await self._mirror_variable(
                worker_child, gateway_folder, folder_name
            )

    async def _mirror_variable(
        self, worker_node: Node, gateway_folder: Node, folder_name: str
    ) -> None:
        name = (await worker_node.read_browse_name()).Name

        try:
            variant_type = await worker_node.read_data_type_as_variant_type()
        except Exception:
            log.warning("could not read data type; assuming Double",
                        extra={"session_id": self.session_id,
                               "session_name": self.session_name,
                               "name": name})
            variant_type = ua.VariantType.Double

        try:
            raw_value = await worker_node.read_value()
            value = _coerce_value(raw_value, variant_type)
        except Exception:
            value = _default_for(variant_type)

        ns = self._namespace_idx
        gateway_node = await gateway_folder.add_variable(
            ua.NodeId(
                f"Gateway.Sessions.{self.session_name}"
                f".{folder_name}.{name}", ns
            ),
            ua.QualifiedName(name, ns),
            value,
            variant_type,
        )
        await gateway_node.set_writable()

        self._map[worker_node.nodeid.to_string()] = (gateway_node, variant_type)

        if folder_name == "Commands":
            self._server.set_attribute_value_setter(
                gateway_node.nodeid,
                self._make_command_setter(name),
            )

    def _make_command_setter(self, name: str):
        # Writes are enqueued with session_id (UUID), which is what the
        # gateway's mirror dict is keyed on.
        session_id = self.session_id
        enqueue = self._enqueue_write

        def set_value(node_data, attribute, value: ua.DataValue) -> None:
            if value is None or value.Value is None:
                return
            raw = value.Value.Value
            node_data.attributes[attribute].value = value
            enqueue(session_id, name, raw)

        return set_value

    # --- subscription ---

    async def _start_subscription(self) -> None:
        if self._client is None or not self._map:
            return

        handler = _MirrorSubscriptionHandler(self._map)
        self._subscription = await self._client.create_subscription(500, handler)

        worker_nodes = [
            self._client.get_node(nid_str) for nid_str in self._map.keys()
        ]
        if worker_nodes:
            await self._subscription.subscribe_data_change(worker_nodes)

    # --- command forwarding ---

    async def forward_command(self, name: str, value: Any) -> None:
        if self._client is None or self._worker_root is None:
            return

        worker_ns = self._worker_ns
        node = await self._worker_root.get_child([
            f"{worker_ns}:Commands",
            f"{worker_ns}:{name}",
        ])

        variant_type = await node.read_data_type_as_variant_type()
        coerced = _coerce_value(value, variant_type)
        await node.write_value(ua.Variant(coerced, variant_type))


class _MirrorSubscriptionHandler:
    """Push worker value changes into the corresponding gateway node."""

    def __init__(
        self, mapping: dict[str, tuple[Node, ua.VariantType]]
    ) -> None:
        self._map = mapping

    def datachange_notification(self, node: Node, val: Any, data) -> None:
        entry = self._map.get(node.nodeid.to_string())
        if entry is None:
            return
        gateway_node, variant_type = entry

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        coerced = _coerce_value(val, variant_type)
        loop.create_task(_safe_write(gateway_node, coerced, variant_type))


async def _safe_write(
    node: Node, value: Any, variant_type: ua.VariantType
) -> None:
    try:
        await node.write_value(ua.Variant(value, variant_type))
    except Exception:
        log.warning("mirror value update skipped",
                    extra={"variant_type": str(variant_type)})