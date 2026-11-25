import asyncio
import errno
import os
from threading import Event

import pytest

from bridge import desktop_gate
from bridge.desktop_gate import AsyncDesktopGate, DesktopLockCancelled, desktop_flock


async def test_local_gate_without_shared_path(monkeypatch):
    monkeypatch.delenv("AAW_DESKTOP_LOCK", raising=False)
    gate = AsyncDesktopGate()
    entered = asyncio.Event()

    async def enter():
        async with gate:
            entered.set()

    async with gate:
        task = asyncio.create_task(enter())
        await asyncio.sleep(0)
        assert gate.locked() and not entered.is_set()
    await asyncio.wait_for(task, timeout=2)
    assert entered.is_set() and not gate.locked()


async def test_threaded_worker_and_async_gate_share_one_lock(monkeypatch, tmp_path):
    pytest.importorskip("fcntl")
    monkeypatch.setenv("AAW_DESKTOP_LOCK", str(tmp_path / "desktop.lock"))
    worker_entered, release_worker = Event(), Event()
    async_entered = asyncio.Event()
    gate = AsyncDesktopGate(retry_interval=0.01)

    def worker():
        with desktop_flock():
            worker_entered.set()
            assert release_worker.wait(5)

    async def enter():
        async with gate:
            assert release_worker.is_set()
            async_entered.set()

    worker_task = asyncio.create_task(asyncio.to_thread(worker))
    try:
        assert await asyncio.to_thread(worker_entered.wait, 2)
        wait_task = asyncio.create_task(enter())
        await asyncio.sleep(0)
        assert gate.locked() and not async_entered.is_set()
    finally:
        release_worker.set()
        await asyncio.wait_for(worker_task, timeout=2)
    await asyncio.wait_for(wait_task, timeout=2)
    assert async_entered.is_set()


async def test_cancelling_async_gate_wait_closes_fd_and_releases_local_lock(monkeypatch, tmp_path):
    pytest.importorskip("fcntl")
    path = tmp_path / "desktop.lock"
    gate = AsyncDesktopGate(path, retry_interval=0.01)
    open_descriptor = desktop_gate._open_lock
    captured = []

    def capture_descriptor(value):
        descriptor = open_descriptor(value)
        captured.append(descriptor)
        return descriptor

    async def should_not_enter():
        async with gate:
            pytest.fail("The other desktop operation still holds the file lock")

    with desktop_flock(path):
        monkeypatch.setattr(desktop_gate, "_open_lock", capture_descriptor)
        task = asyncio.create_task(should_not_enter())
        await asyncio.sleep(0)
        assert len(captured) == 1 and gate.locked()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(OSError) as closed:
            os.fstat(captured[0])
        assert closed.value.errno == errno.EBADF
        assert not gate.locked()

    async with gate:
        assert gate.locked()


async def test_worker_wait_can_be_cancelled_without_owning_the_lock(monkeypatch, tmp_path):
    pytest.importorskip("fcntl")
    path = tmp_path / "desktop.lock"
    gate = AsyncDesktopGate(path)
    cancel, worker_waiting = Event(), Event()
    try_lock = desktop_gate._try_lock

    def observe_lock(descriptor):
        claimed = try_lock(descriptor)
        worker_waiting.set()
        return claimed

    def worker():
        with desktop_flock(path, cancel=cancel, retry_interval=0.01):
            pytest.fail("A cancelled desktop operation must not run")

    async with gate:
        monkeypatch.setattr(desktop_gate, "_try_lock", observe_lock)
        task = asyncio.create_task(asyncio.to_thread(worker))
        try:
            assert await asyncio.to_thread(worker_waiting.wait, 2)
            assert not task.done()
        finally:
            cancel.set()
            with pytest.raises(DesktopLockCancelled):
                await asyncio.wait_for(task, timeout=2)
        assert gate.locked()

    with desktop_flock(path):
        assert not gate.locked()


async def test_cancelling_inside_async_gate_releases_shared_lock(tmp_path):
    pytest.importorskip("fcntl")
    path = tmp_path / "desktop.lock"
    gate = AsyncDesktopGate(path)
    entered = asyncio.Event()

    async def hold():
        async with gate:
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(hold())
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not gate.locked()
    with desktop_flock(path):
        assert path.is_file()
