"""End-to-end test for the blinker plugin through the OPC UA adapter.

Runs a real asyncua server on an ephemeral port, connects a real asyncua
client, writes a command, reads a measurement. This is the smallest
possible proof that the plugin contract works.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from asyncua import Client, ua

from sim_runtime.base import SimulationConfig
from sim_runtime.opcua_adapter import OPCUAAdapter
from sim_plugins.blinker.simulation import Blinker


def _free_port() -> int:
    """Ask the OS for a port that's currently free, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    """Poll until the OPC UA port accepts a TCP connection."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            _, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError(f"server on {host}:{port} never came up")


@pytest.mark.asyncio
async def test_blinker_roundtrip():
    port = _free_port()
    sim = Blinker(SimulationConfig(session_id="test", params={}))
    adapter = OPCUAAdapter(sim, advertise_host="127.0.0.1", port=port)
    await adapter.initialize()

    stop = asyncio.Event()
    server_task = asyncio.create_task(adapter.run(stop))

    try:
        await _wait_for_server("127.0.0.1", port)

        async with Client(url=f"opc.tcp://127.0.0.1:{port}/blinker/") as client:
            idx = await client.get_namespace_index(
                "urn:cip-sim:blinker:1.0.0"
            )

            enabled = await client.nodes.root.get_child(
                ["0:Objects", f"{idx}:Blinking Light",
                 f"{idx}:Commands", f"{idx}:Enabled"]
            )
            on_node = await client.nodes.root.get_child(
                ["0:Objects", f"{idx}:Blinking Light",
                 f"{idx}:Measurements", f"{idx}:On"]
            )
            period_node = await client.nodes.root.get_child(
                ["0:Objects", f"{idx}:Blinking Light",
                 f"{idx}:Commands", f"{idx}:PeriodSeconds"]
            )

            # Initially off.
            assert await on_node.read_value() is False

            # Enable with a short period so we see a transition quickly.
            await period_node.write_value(
                ua.Variant(0.5, ua.VariantType.Double)
            )
            await enabled.write_value(
                ua.Variant(True, ua.VariantType.Boolean)
            )

            # Give it a couple of periods to blink.
            await asyncio.sleep(0.6)

            # At some point during the sleep the light was on; we only
            # know it ran because the phase advanced. Read BlinkCount.
            count_node = await client.nodes.root.get_child(
                ["0:Objects", f"{idx}:Blinking Light",
                 f"{idx}:Status", f"{idx}:BlinkCount"]
            )
            count = await count_node.read_value()
            assert count >= 1, f"expected at least 1 blink, got {count}"

            # Disable and confirm On goes False.
            await enabled.write_value(
                ua.Variant(False, ua.VariantType.Boolean)
            )
            await asyncio.sleep(0.15)
            assert await on_node.read_value() is False

    finally:
        stop.set()
        await asyncio.wait_for(server_task, timeout=3.0)


@pytest.mark.asyncio
async def test_blinker_pulse_resets_count():
    port = _free_port()
    sim = Blinker(SimulationConfig(session_id="test", params={}))
    adapter = OPCUAAdapter(sim, advertise_host="127.0.0.1", port=port)
    await adapter.initialize()

    stop = asyncio.Event()
    server_task = asyncio.create_task(adapter.run(stop))

    try:
        await _wait_for_server("127.0.0.1", port)

        async with Client(url=f"opc.tcp://127.0.0.1:{port}/blinker/") as client:
            idx = await client.get_namespace_index("urn:cip-sim:blinker:1.0.0")

            async def child(folder: str, name: str):
                return await client.nodes.root.get_child(
                    ["0:Objects", f"{idx}:Blinking Light",
                     f"{idx}:{folder}", f"{idx}:{name}"]
                )

            enabled = await child("Commands", "Enabled")
            period = await child("Commands", "PeriodSeconds")
            reset = await child("Commands", "ResetCountCmd")
            count_node = await child("Status", "BlinkCount")

            await period.write_value(ua.Variant(0.2, ua.VariantType.Double))
            await enabled.write_value(ua.Variant(True, ua.VariantType.Boolean))
            await asyncio.sleep(0.7)

            count = await count_node.read_value()
            assert count >= 2, f"expected >=2 blinks before reset, got {count}"

            # Pulse the reset. The adapter clears it back to False.
            await reset.write_value(ua.Variant(True, ua.VariantType.Boolean))
            await asyncio.sleep(0.15)

            # Value on the node should be False again (auto-cleared).
            assert await reset.read_value() is False
            # Count should have been reset.
            assert await count_node.read_value() <= 1

    finally:
        stop.set()
        await asyncio.wait_for(server_task, timeout=3.0)