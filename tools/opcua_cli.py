#!/usr/bin/env python3
"""Send OPC UA commands to a running CIP worker or to the gateway.

Usage against a worker:
    python tools/opcua_cli.py --endpoint opc.tcp://10.120.32.67:8080/cip/ \\
        PumpCmd=1 WaterValveCmd=1

Usage against the gateway (one or more sessions visible):
    # List sessions exposed by the gateway
    python tools/opcua_cli.py --endpoint opc.tcp://127.0.0.1:8080/gateway/ \\
        --list-sessions

    # Dump one session's tree through the gateway
    python tools/opcua_cli.py --endpoint opc.tcp://127.0.0.1:8080/gateway/ \\
        --session test1 --dump

    # Write a command through the gateway to session test1
    python tools/opcua_cli.py --endpoint opc.tcp://127.0.0.1:8080/gateway/ \\
        --session test1 WaterValveCmd=1 PumpCmd=1

    # Read a measurement through the gateway
    python tools/opcua_cli.py --endpoint opc.tcp://127.0.0.1:8080/gateway/ \\
        --session test1 --read LevelPercent FlowLpm
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from asyncua import Client, ua


NAMESPACE_BY_SIM = {
    "cip": "urn:cip-sim:cip:2.0.0",
    "blinker": "urn:cip-sim:blinker:1.0.0",
    "gateway": "urn:cip-sim:gateway:1.0.0",
}

FOLDER_NAME = {
    "cip": "Clean-In-Place",
    "blinker": "Blinking Light",
    "gateway": "Gateway",
}


def _parse_value(raw: str):
    low = raw.lower()
    if low in ("1", "true", "on", "yes"):
        return True, ua.VariantType.Boolean
    if low in ("0", "false", "off", "no"):
        return False, ua.VariantType.Boolean
    try:
        return float(raw), ua.VariantType.Double
    except ValueError:
        pass
    return raw, ua.VariantType.String


def _sim_id_from_endpoint(endpoint: str) -> str:
    tail = endpoint.rstrip("/").rsplit("/", 1)[-1]
    return tail or "cip"


# --- address resolution -----------------------------------------------------

async def _resolve_plugin(prefix: Client, sim_id: str, folder: str, name: str):
    idx = await prefix.get_namespace_index(NAMESPACE_BY_SIM[sim_id])
    return await prefix.nodes.root.get_child([
        "0:Objects",
        f"{idx}:{FOLDER_NAME[sim_id]}",
        f"{idx}:{folder}",
        f"{idx}:{name}",
    ])


async def _resolve_gateway(
    c: Client, session_id: str, folder: str, name: str
):
    idx = await c.get_namespace_index(NAMESPACE_BY_SIM["gateway"])
    return await c.nodes.root.get_child([
        "0:Objects",
        f"{idx}:Gateway",
        f"{idx}:Sessions",
        f"{idx}:{session_id}",
        f"{idx}:{folder}",
        f"{idx}:{name}",
    ])


# --- operations -------------------------------------------------------------

async def _list_sessions(c: Client) -> None:
    idx = await c.get_namespace_index(NAMESPACE_BY_SIM["gateway"])
    sessions = await c.nodes.root.get_child([
        "0:Objects", f"{idx}:Gateway", f"{idx}:Sessions",
    ])
    children = await sessions.get_children()
    if not children:
        print("(no sessions)")
        return
    for child in children:
        bn = await child.read_browse_name()
        print(bn.Name)


async def _dump_plugin(c: Client, sim_id: str) -> None:
    idx = await c.get_namespace_index(NAMESPACE_BY_SIM[sim_id])
    root = await c.nodes.root.get_child([
        "0:Objects", f"{idx}:{FOLDER_NAME[sim_id]}"
    ])
    for folder_name in ("Commands", "Measurements", "Status"):
        try:
            folder = await root.get_child([f"{idx}:{folder_name}"])
        except Exception:
            continue
        print(f"--- {folder_name} ---")
        for child in await folder.get_children():
            try:
                value = await child.read_value()
            except Exception as exc:
                value = f"<error: {exc}>"
            print(f"  {(await child.read_browse_name()).Name}: {value!r}")


async def _dump_gateway(c: Client, session_id: str) -> None:
    idx = await c.get_namespace_index(NAMESPACE_BY_SIM["gateway"])
    root = await c.nodes.root.get_child([
        "0:Objects", f"{idx}:Gateway", f"{idx}:Sessions", f"{idx}:{session_id}",
    ])
    for folder_name in ("Commands", "Measurements", "Status"):
        try:
            folder = await root.get_child([f"{idx}:{folder_name}"])
        except Exception:
            continue
        print(f"--- {folder_name} ---")
        for child in await folder.get_children():
            try:
                value = await child.read_value()
            except Exception as exc:
                value = f"<error: {exc}>"
            print(f"  {(await child.read_browse_name()).Name}: {value!r}")


async def _write_plugin(c: Client, sim_id: str, assignments: list[str]) -> None:
    for a in assignments:
        if "=" not in a:
            print(f"skip (no '='): {a}", file=sys.stderr)
            continue
        name, raw = a.split("=", 1)
        value, vtype = _parse_value(raw)
        node = await _resolve_plugin(c, sim_id, "Commands", name)
        await node.write_value(ua.Variant(value, vtype))
        print(f"wrote {name} = {value!r}")


async def _write_gateway(
    c: Client, session_id: str, assignments: list[str]
) -> None:
    for a in assignments:
        if "=" not in a:
            print(f"skip (no '='): {a}", file=sys.stderr)
            continue
        name, raw = a.split("=", 1)
        value, vtype = _parse_value(raw)
        node = await _resolve_gateway(c, session_id, "Commands", name)
        await node.write_value(ua.Variant(value, vtype))
        print(f"wrote {session_id}/{name} = {value!r}")


async def _find_plugin_node(c: Client, sim_id: str, name: str):
    """Search Commands, Measurements, Status for a node name.

    Reads may target any of the three folders. Commands first because
    operators tend to type command names when checking state.
    """
    last_exc: Exception | None = None
    for folder in ("Commands", "Measurements", "Status"):
        try:
            return await _resolve_plugin(c, sim_id, folder, name)
        except ua.UaStatusCodeError as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


async def _find_gateway_node(c: Client, session_id: str, name: str):
    """Same as _find_plugin_node, but through the gateway."""
    last_exc: Exception | None = None
    for folder in ("Commands", "Measurements", "Status"):
        try:
            return await _resolve_gateway(c, session_id, folder, name)
        except ua.UaStatusCodeError as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


async def _read_plugin(c: Client, sim_id: str, names: list[str]) -> None:
    for name in names:
        node = await _find_plugin_node(c, sim_id, name)
        print(f"{name} = {await node.read_value()!r}")


async def _read_gateway(c: Client, session_id: str, names: list[str]) -> None:
    for name in names:
        node = await _find_gateway_node(c, session_id, name)
        print(f"{session_id}/{name} = {await node.read_value()!r}")

# --- entry point ------------------------------------------------------------

async def _main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", required=True,
                   help="e.g. opc.tcp://127.0.0.1:8080/cip/")
    p.add_argument("--session", help="Session id (required for gateway writes/reads/dump).")
    p.add_argument("--list-sessions", action="store_true",
                   help="List sessions exposed by the gateway.")
    p.add_argument("assignments", nargs="*",
                   help="CommandName=Value pairs to write")
    p.add_argument("--read", nargs="+", metavar="NAME")
    p.add_argument("--dump", action="store_true")
    args = p.parse_args()

    sim_id = _sim_id_from_endpoint(args.endpoint)
    is_gateway = (sim_id == "gateway")

    if is_gateway and (args.assignments or args.read or args.dump) and not args.session:
        if args.list_sessions:
            pass  # ok
        else:
            print("--session is required for gateway operations "
                  "(or use --list-sessions)", file=sys.stderr)
            return 2

    async with Client(url=args.endpoint) as c:
        if is_gateway:
            if args.list_sessions:
                await _list_sessions(c)
            if args.dump:
                await _dump_gateway(c, args.session)
            if args.read:
                await _read_gateway(c, args.session, args.read)
            if args.assignments:
                await _write_gateway(c, args.session, args.assignments)
        else:
            if args.dump:
                await _dump_plugin(c, sim_id)
            if args.read:
                await _read_plugin(c, sim_id, args.read)
            if args.assignments:
                await _write_plugin(c, sim_id, args.assignments)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))