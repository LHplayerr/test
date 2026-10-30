"""Unit tests for the async port allocator.

Uses fakeredis.aioredis so no external service is required. The two
tests that matter most are the concurrent-acquire race and the stale-
release protection — those are the bugs the design exists to prevent.
"""

from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from worker_pool.port_allocator import PortAllocator, PortPoolExhausted


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def pool(redis_client):
    return PortAllocator(redis_client, start=5000, end=5002)


async def test_acquire_returns_port_in_range(pool):
    port = await pool.acquire("session-a")
    assert 5000 <= port <= 5002


async def test_acquire_second_call_gives_different_port(pool):
    p1 = await pool.acquire("session-a")
    p2 = await pool.acquire("session-b")
    assert p1 != p2


async def test_pool_exhaustion_raises(pool):
    await pool.acquire("a")
    await pool.acquire("b")
    await pool.acquire("c")
    with pytest.raises(PortPoolExhausted):
        await pool.acquire("d")


async def test_release_frees_the_port(pool):
    p1 = await pool.acquire("session-a")
    assert await pool.release(p1, "session-a")
    p2 = await pool.acquire("session-b")
    assert p2 == p1


async def test_release_by_wrong_session_is_rejected(pool):
    p1 = await pool.acquire("session-a")
    assert await pool.release(p1, "session-b") is False
    assert (await pool.snapshot())[p1] == "session-a"


async def test_release_twice_is_idempotent(pool):
    p1 = await pool.acquire("session-a")
    assert await pool.release(p1, "session-a") is True
    assert await pool.release(p1, "session-a") is False


async def test_concurrent_acquire_never_double_leases(pool):
    """Race N tasks for M ports; each port is leased at most once."""
    async def worker(sid: str):
        return await pool.acquire(sid)

    results = await asyncio.gather(
        worker("s0"), worker("s1"), worker("s2"),
        return_exceptions=True,
    )
    # Every task should succeed with 3 ports available
    ports = [r for r in results if isinstance(r, int)]
    assert len(ports) == 3
    assert len(set(ports)) == 3


async def test_reconcile_drops_orphans(pool):
    p1 = await pool.acquire("session-a")
    p2 = await pool.acquire("session-b")
    dropped = await pool.reconcile(live_session_ids={"session-a"})
    assert dropped == [p2]
    assert (await pool.snapshot()) == {p1: "session-a"}


async def test_reconcile_keeps_live_leases(pool):
    p1 = await pool.acquire("session-a")
    dropped = await pool.reconcile(live_session_ids={"session-a"})
    assert dropped == []
    assert (await pool.snapshot()) == {p1: "session-a"}